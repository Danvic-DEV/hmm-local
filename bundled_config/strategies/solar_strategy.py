"""
Solar Strategy - bin-packs enrolled miners against live solar surplus.

Bundled StrategyPlugin implementation (see app/core/strategy_plugin_base.py
for the contract, app/core/strategy_loader.py for how this file gets
discovered). Fully standalone from Price Band Strategy: own config table
(SolarStrategyConfig), own enrollment table (SolarMinerEnrollment), own
scheduler job (registered generically by the strategy loader, not called
from price_band_strategy.py).

Manages on/off and mode, per miner - never pool. Pool switching is
deliberately out of scope: on at least one supported miner (Avalon Nano),
a pool change triggers a full device reboot, and solar surplus can change
far more often than it's safe to reboot hardware.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import (
    Miner,
    MinerHASwitchLink,
    HomeAssistantConfig,
    HomeAssistantDevice,
    MinerModePowerStats,
    SolarMinerEnrollment,
    SolarStrategyConfig,
    Telemetry,
)
from core.audit import log_audit
from core.miner_capabilities import get_miner_capabilities
from core.strategy_plugin_base import StrategyExecutionResult, StrategyMetadata, StrategyPlugin

logger = logging.getLogger(__name__)

# Same EMA shape/alpha convention as MinerModePowerStats (app/core/scheduler.py _apply_running_power_sample)
SURPLUS_EMA_ALPHA = 0.20
ONOFF_CONFIRM_CYCLES = 5
RANKING_PRIMARY_WINDOW_HOURS = 6
RANKING_FALLBACK_WINDOW_HOURS = 48
TELEMETRY_FAILURE_WARN_THRESHOLD = 5


class SolarStrategy(StrategyPlugin):
    """Runs enrolled miners off live excess solar surplus."""

    strategy_id = "solar"

    def __init__(self):
        # In-memory state (resets on restart - accepted tradeoff, see research.md).
        # Instance attributes, not mutable class defaults - the loader keeps a
        # single long-lived instance per plugin, but this avoids relying on
        # that for correctness.
        self._surplus_ema: Optional[float] = None
        self._onoff_state: Dict[int, Tuple[bool, int]] = {}
        self._telemetry_failure_counts: Dict[int, int] = {}

    def get_metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id=self.strategy_id,
            display_name="Solar Strategy",
            version="1.0",
            description="Runs enrolled miners off live excess solar surplus, independent of Price Band Strategy.",
        )

    async def execute(self, db: AsyncSession) -> StrategyExecutionResult:
        config = await self._get_or_create_config(db)

        if not config.enabled:
            return StrategyExecutionResult(enabled=False)

        if not config.solar_surplus_entity_id:
            return StrategyExecutionResult(
                enabled=True,
                error="Solar Strategy is enabled but no surplus sensor is configured",
            )

        eligible_miners = await self._get_eligible_miners(db)
        if not eligible_miners:
            return StrategyExecutionResult(enabled=True, actions=[], details={"surplus_watts": None})

        raw_surplus_watts = await self._get_smoothed_surplus_watts(db, config)
        allocation, true_budget_watts = await self._compute_allocation(db, eligible_miners, raw_surplus_watts)

        actions: List[str] = []
        miners_by_id = {m.id: m for m in eligible_miners}

        for miner_id, target_mode in allocation.items():
            miner = miners_by_id[miner_id]
            currently_on = not await self._is_ha_device_off(db, miner_id)

            if not currently_on:
                if not self._should_act_on_transition(miner_id, desired_on=True, currently_on=False):
                    actions.append(f"{miner.name}: solar wants ON, awaiting confirm")
                    continue
                await self._set_ha_power(db, miner, turn_on=True)
                actions.append(f"{miner.name}: solar turned ON (budget={true_budget_watts:.0f}W)")
                await log_audit(
                    db, action="solar_strategy_on", resource_type="solar_strategy", resource_name=miner.name,
                    changes={"miner_id": miner.id, "surplus_watts": raw_surplus_watts, "budget_watts": true_budget_watts, "mode": target_mode},
                )
            else:
                self._should_act_on_transition(miner_id, desired_on=True, currently_on=True)  # clears stale streak

            mode_changed = await self._apply_mode(db, miner, target_mode)
            if mode_changed:
                actions.append(f"{miner.name}: solar mode={target_mode}")
                await log_audit(
                    db, action="solar_strategy_mode", resource_type="solar_strategy", resource_name=miner.name,
                    changes={"miner_id": miner.id, "surplus_watts": raw_surplus_watts, "mode": target_mode},
                )

        for miner in eligible_miners:
            if miner.id in allocation:
                continue
            currently_on = not await self._is_ha_device_off(db, miner.id)
            if not currently_on:
                self._should_act_on_transition(miner.id, desired_on=False, currently_on=False)
                continue
            if self._should_act_on_transition(miner.id, desired_on=False, currently_on=True):
                await self._set_ha_power(db, miner, turn_on=False)
                actions.append(f"{miner.name}: solar surplus insufficient, turned OFF")
                await log_audit(
                    db, action="solar_strategy_off", resource_type="solar_strategy", resource_name=miner.name,
                    changes={"miner_id": miner.id, "surplus_watts": raw_surplus_watts},
                )
            else:
                actions.append(f"{miner.name}: solar wants OFF, awaiting confirm")

        await db.commit()
        return StrategyExecutionResult(
            enabled=True,
            actions=actions,
            details={
                "surplus_watts": raw_surplus_watts,
                "true_budget_watts": true_budget_watts,
                "claimed_miner_ids": list(allocation.keys()),
            },
        )

    # -- config / enrollment -------------------------------------------------

    @staticmethod
    async def _get_or_create_config(db: AsyncSession) -> SolarStrategyConfig:
        result = await db.execute(select(SolarStrategyConfig))
        config = result.scalar_one_or_none()
        if not config:
            config = SolarStrategyConfig(enabled=False)
            db.add(config)
            await db.commit()
            await db.refresh(config)
        return config

    @staticmethod
    async def _get_eligible_miners(db: AsyncSession) -> List[Miner]:
        result = await db.execute(
            select(Miner)
            .join(SolarMinerEnrollment, Miner.id == SolarMinerEnrollment.miner_id)
            .where(SolarMinerEnrollment.enabled == True)
            .where(Miner.enabled == True)
        )
        return list(result.scalars().all())

    # -- surplus reading / smoothing -----------------------------------------

    async def _get_smoothed_surplus_watts(self, db: AsyncSession, config: SolarStrategyConfig) -> Optional[float]:
        config_result = await db.execute(select(HomeAssistantConfig))
        ha_config = config_result.scalar_one_or_none()
        if not ha_config or not ha_config.enabled:
            logger.warning("Solar Strategy: HA integration not configured or disabled")
            return self._surplus_ema

        from integrations.homeassistant import HomeAssistantIntegration

        ha = HomeAssistantIntegration(base_url=ha_config.base_url, access_token=ha_config.access_token)
        raw_value = await ha.get_device_state_value(config.solar_surplus_entity_id)
        if raw_value is None:
            logger.warning(f"Solar Strategy: no state for {config.solar_surplus_entity_id}")
            return self._surplus_ema

        try:
            sample = float(raw_value)
        except (TypeError, ValueError):
            logger.warning(f"Solar Strategy: non-numeric state for {config.solar_surplus_entity_id}: {raw_value!r}")
            return self._surplus_ema

        if self._surplus_ema is None:
            self._surplus_ema = sample
        else:
            self._surplus_ema = (SURPLUS_EMA_ALPHA * sample) + ((1.0 - SURPLUS_EMA_ALPHA) * self._surplus_ema)

        return self._surplus_ema

    # -- allocation (bin-pack) ------------------------------------------------

    @staticmethod
    def _candidate_modes(miner_type: str) -> List[str]:
        capability = get_miner_capabilities().get(miner_type)
        if not capability or not capability.available_modes:
            return []
        return list(capability.available_modes)

    @staticmethod
    async def _rank_by_efficiency_window(db: AsyncSession, miners: List[Miner], window_hours: int) -> List[Tuple[Miner, float]]:
        cutoff = datetime.utcnow() - timedelta(hours=window_hours)
        ranked: List[Tuple[Miner, float]] = []

        for miner in miners:
            result = await db.execute(
                select(Telemetry.hashrate, Telemetry.power_watts)
                .where(Telemetry.miner_id == miner.id)
                .where(Telemetry.timestamp > cutoff)
                .where(Telemetry.hashrate.isnot(None))
                .where(Telemetry.power_watts.isnot(None))
                .order_by(Telemetry.timestamp.desc())
                .limit(10)
            )
            rows = result.all()
            if not rows:
                continue

            total_hashrate = sum(r[0] for r in rows if r[0] and r[1])
            total_power = sum(r[1] for r in rows if r[0] and r[1])
            count = sum(1 for r in rows if r[0] and r[1])
            if count == 0:
                continue

            hashrate_ths = (total_hashrate / count) / 1000.0
            if hashrate_ths <= 0:
                continue
            w_per_th = (total_power / count) / hashrate_ths
            ranked.append((miner, w_per_th))

        ranked.sort(key=lambda pair: pair[1])
        return ranked

    async def _rank_by_efficiency(self, db: AsyncSession, miners: List[Miner]) -> List[Miner]:
        """Most-efficient-first ordering (lowest W/TH). Widens the telemetry
        window 6h -> 48h when the primary window has no data at all, same
        fallback Price Band Strategy's champion leaderboard uses, so a
        miner that's been idle a while (the normal state for one waiting on
        solar) still gets ranked by real recent data rather than dumped
        unordered at the end. Note: a miner's eligibility for allocation at
        all does NOT depend on this - that's driven by MinerModePowerStats,
        which is cumulative, not time-windowed (see _load_mode_power_stats)."""
        ranked = await self._rank_by_efficiency_window(db, miners, RANKING_PRIMARY_WINDOW_HOURS)
        if not ranked:
            ranked = await self._rank_by_efficiency_window(db, miners, RANKING_FALLBACK_WINDOW_HOURS)

        ranked_ids = {m.id for m, _ in ranked}
        unranked = [m for m in miners if m.id not in ranked_ids]
        return [m for m, _ in ranked] + unranked

    @staticmethod
    async def _load_mode_power_stats(db: AsyncSession, miner_ids: List[int]) -> Dict[int, Dict[str, float]]:
        """Per-miner, per-mode observed wattage. Cumulative/running stats
        (see MinerModePowerStats), NOT windowed to recent telemetry - a
        miner that's been idle for days still has this data from when it
        last ran, so idle time alone never excludes a miner from allocation.
        Only a miner that has genuinely never run in any mode is skipped
        (see _compute_allocation) - a deliberate "don't guess" choice."""
        if not miner_ids:
            return {}
        result = await db.execute(
            select(MinerModePowerStats).where(MinerModePowerStats.miner_id.in_(miner_ids))
        )
        stats: Dict[int, Dict[str, float]] = {}
        for row in result.scalars().all():
            watts = row.ema_power_watts if row.ema_power_watts is not None else row.avg_power_watts
            if watts is None:
                continue
            stats.setdefault(row.miner_id, {})[row.mode] = float(watts)
        return stats

    async def _compute_allocation(
        self, db: AsyncSession, eligible_miners: List[Miner], raw_surplus_watts: Optional[float]
    ) -> Tuple[Dict[int, str], float]:
        """Returns (allocation, true_budget_watts).

        The HA surplus sensor nets out currently-running monitored miners'
        own draw (it's generation minus total monitored socket power, and
        enrolled miners sit on monitored sockets) - so the raw reading is
        "surplus beyond what's already running", not the total reallocatable
        budget. Add back each already-on eligible miner's current draw
        before bin-packing, so an already-running miner isn't starved just
        because its own consumption was silently subtracted out upstream.
        The whole allocation is then recomputed fresh from that true total
        every cycle - not "keep what's allocated, try to add more".
        """
        if raw_surplus_watts is None or not eligible_miners:
            return {}, 0.0

        power_stats = await self._load_mode_power_stats(db, [m.id for m in eligible_miners])

        already_on_draw = 0.0
        for miner in eligible_miners:
            if await self._is_ha_device_off(db, miner.id):
                continue
            current_mode = miner.current_mode
            if not current_mode:
                continue
            watts = power_stats.get(miner.id, {}).get(current_mode)
            if watts:
                already_on_draw += watts

        true_budget_watts = float(raw_surplus_watts) + already_on_draw
        if true_budget_watts <= 0:
            return {}, true_budget_watts

        ordered_miners = await self._rank_by_efficiency(db, eligible_miners)

        allocation: Dict[int, str] = {}
        remaining_watts = true_budget_watts

        for miner in ordered_miners:
            modes = self._candidate_modes(miner.miner_type)
            if not modes:
                continue
            miner_stats = power_stats.get(miner.id, {})
            if not miner_stats:
                # Never run in any mode - no real wattage data to size it against, don't guess.
                continue

            best_mode: Optional[str] = None
            best_watts: Optional[float] = None
            for mode in reversed(modes):  # highest-draw first, use as much headroom as fits
                watts = miner_stats.get(mode)
                if watts is not None and watts <= remaining_watts:
                    best_mode = mode
                    best_watts = watts
                    break

            if best_mode is None:
                continue

            allocation[miner.id] = best_mode
            remaining_watts -= best_watts

        return allocation, true_budget_watts

    # -- debounce --------------------------------------------------------------

    def _should_act_on_transition(self, miner_id: int, desired_on: bool, currently_on: bool) -> bool:
        """5-consecutive-cycle debounce for on/off transitions only - mode-only
        changes on an already-on miner bypass this and apply immediately."""
        if desired_on == currently_on:
            self._onoff_state.pop(miner_id, None)
            return False

        prev_desired, count = self._onoff_state.get(miner_id, (None, 0))
        count = count + 1 if prev_desired == desired_on else 1
        self._onoff_state[miner_id] = (desired_on, count)

        return count >= ONOFF_CONFIRM_CYCLES

    # -- HA device control -------------------------------------------------------

    @staticmethod
    async def _get_ha_device(db: AsyncSession, miner_id: int) -> Optional[HomeAssistantDevice]:
        result = await db.execute(
            select(HomeAssistantDevice)
            .join(MinerHASwitchLink, MinerHASwitchLink.ha_device_id == HomeAssistantDevice.id)
            .where(MinerHASwitchLink.miner_id == miner_id)
            .where(HomeAssistantDevice.enrolled == True)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _is_ha_device_off(self, db: AsyncSession, miner_id: int) -> bool:
        device = await self._get_ha_device(db, miner_id)
        return bool(device and device.current_state == "off")

    async def _set_ha_power(self, db: AsyncSession, miner: Miner, turn_on: bool) -> bool:
        device = await self._get_ha_device(db, miner.id)
        if not device:
            logger.debug(f"Solar Strategy: no HA device linked to miner {miner.name}")
            return False

        config_result = await db.execute(select(HomeAssistantConfig))
        ha_config = config_result.scalar_one_or_none()
        if not ha_config or not ha_config.enabled:
            return False

        from integrations.homeassistant import HomeAssistantIntegration

        ha = HomeAssistantIntegration(base_url=ha_config.base_url, access_token=ha_config.access_token)
        success = await (ha.turn_on(device.entity_id) if turn_on else ha.turn_off(device.entity_id))
        if success:
            device.current_state = "on" if turn_on else "off"
            device.last_state_change = datetime.utcnow()
            await db.commit()
            if turn_on:
                await asyncio.sleep(3)  # let the miner boot before a mode call
        return success

    async def _apply_mode(self, db: AsyncSession, miner: Miner, target_mode: str) -> bool:
        """Set mode on a solar-claimed miner. Returns True if a mode change was applied.
        Never touches pool - see module docstring for why."""
        from adapters import get_adapter

        adapter = get_adapter(miner)
        if not adapter:
            logger.error(f"Solar Strategy: no adapter for miner {miner.name}")
            return False

        try:
            await db.refresh(miner)
            telemetry = await asyncio.wait_for(adapter.get_telemetry(), timeout=5.0)
            self._telemetry_failure_counts[miner.id] = 0
        except Exception as e:
            failure_count = self._telemetry_failure_counts.get(miner.id, 0) + 1
            self._telemetry_failure_counts[miner.id] = failure_count
            if failure_count >= TELEMETRY_FAILURE_WARN_THRESHOLD:
                logger.warning(
                    f"Solar Strategy: {miner.name} telemetry has failed {failure_count} consecutive times "
                    f"({e}) - mode cannot be verified or changed until it recovers"
                )
            else:
                logger.debug(f"Solar Strategy: could not get telemetry for {miner.name}: {e}")
            return False

        device_reported_mode = telemetry.extra_data.get("current_mode") if telemetry and telemetry.extra_data else None
        db_current_mode = miner.current_mode
        mode_already_correct = (
            device_reported_mode == target_mode if device_reported_mode else db_current_mode == target_mode
        )

        if device_reported_mode and device_reported_mode != db_current_mode:
            logger.warning(
                f"Solar Strategy: {miner.name} MODE DRIFT: DB says {db_current_mode}, "
                f"device reports {device_reported_mode}, target is {target_mode}"
            )

        if not target_mode or mode_already_correct:
            if miner.current_mode != target_mode and target_mode:
                miner.current_mode = target_mode
                await db.commit()
            return False

        try:
            mode_set = await adapter.set_mode(target_mode)
            if mode_set:
                miner.current_mode = target_mode
                miner.last_mode_change = datetime.utcnow()
                await db.commit()
                return True
            logger.warning(f"Solar Strategy: failed to set mode {target_mode} on {miner.name}")
        except Exception as e:
            logger.error(f"Solar Strategy: error setting mode for {miner.name}: {e}")

        return False
