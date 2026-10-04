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

Negative energy price override: if the current grid price is negative
(being paid to consume), every eligible miner is run at its highest known
wattage mode regardless of live surplus, and the ON transition bypasses
the usual 5-cycle debounce (reacting to a negative price immediately is
the whole point). Returning to a non-negative price does NOT force an
immediate OFF - it simply stops overriding, so the normal surplus-based
allocation resumes and sheds via its own debounced OFF logic like any
other miner that no longer fits the budget.
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
from core.energy import get_current_energy_price
from core.miner_capabilities import get_miner_capabilities
from core.strategy_plugin_base import StrategyExecutionResult, StrategyMetadata, StrategyPlugin

logger = logging.getLogger(__name__)

# Same EMA shape/alpha convention as MinerModePowerStats (app/core/scheduler.py _apply_running_power_sample)
SURPLUS_EMA_ALPHA = 0.20
ONOFF_CONFIRM_CYCLES = 5
DEFAULT_SURPLUS_BUFFER_WATTS = 100.0  # fallback only - normal path always reads config.surplus_buffer_watts
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
            version="1.4",
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

        # Always keep the surplus EMA warm, even during a negative-price
        # override, so it's caught up and ready the moment normal rules resume.
        raw_surplus_watts = await self._get_smoothed_surplus_watts(db, config)

        price = await get_current_energy_price(db)
        negative_price_override = price is not None and price.price_pence < 0

        if negative_price_override:
            # Deliberately ignores the buffer too - a negative price means
            # ignore surplus entirely, not just trim it.
            allocation, true_budget_watts = await self._compute_override_allocation(db, eligible_miners)
        else:
            buffer_watts = config.surplus_buffer_watts if config.surplus_buffer_watts is not None else DEFAULT_SURPLUS_BUFFER_WATTS
            allocation, true_budget_watts = await self._compute_allocation(
                db, eligible_miners, raw_surplus_watts, buffer_watts=buffer_watts
            )

        actions: List[str] = []
        miners_by_id = {m.id: m for m in eligible_miners}

        for miner_id, target_mode in allocation.items():
            miner = miners_by_id[miner_id]
            # Live poll, not cache - this cycle IS the reconciliation (see
            # module docstring: no separate reconcile job, every 1-minute
            # cycle independently re-verifies real state).
            live_state = await self._get_live_device_state(db, miner_id)
            currently_on = live_state == "on"

            if not currently_on:
                if not negative_price_override and not self._should_act_on_transition(miner_id, desired_on=True, currently_on=False):
                    actions.append(f"{miner.name}: solar wants ON, awaiting confirm")
                    continue
                if negative_price_override:
                    # Bypassing the debounce below - drop any in-progress streak
                    # so it doesn't carry a stale partial count into normal
                    # surplus-based decisions once the override ends.
                    self._onoff_state.pop(miner_id, None)
                await self._set_ha_power(db, miner, turn_on=True)
                reason = f"negative price {price.price_pence:.2f}p/kWh" if negative_price_override else f"budget={true_budget_watts:.0f}W"
                actions.append(f"{miner.name}: solar turned ON ({reason})")
                await log_audit(
                    db, action="solar_strategy_on", resource_type="solar_strategy", resource_name=miner.name,
                    changes={
                        "miner_id": miner.id, "surplus_watts": raw_surplus_watts, "budget_watts": true_budget_watts,
                        "mode": target_mode, "negative_price_override": negative_price_override,
                    },
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
            live_state = await self._get_live_device_state(db, miner.id)
            currently_on = live_state == "on"
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
                "negative_price_override": negative_price_override,
                "energy_price_pence": price.price_pence if price else None,
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
        self, db: AsyncSession, eligible_miners: List[Miner], raw_surplus_watts: Optional[float],
        buffer_watts: float = 0.0,
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

        buffer_watts is reserved headroom subtracted from the budget before
        bin-packing - deliberately don't chase surplus down to literal zero,
        since the EMA/historical wattage estimate and the real sensor
        reading will never line up exactly, and the goal is free solar, not
        a sliver of paid grid import.
        """
        if raw_surplus_watts is None or not eligible_miners:
            return {}, 0.0

        power_stats = await self._load_mode_power_stats(db, [m.id for m in eligible_miners])

        already_on_draw = 0.0
        for miner in eligible_miners:
            if await self._get_live_device_state(db, miner.id) != "on":
                continue
            current_mode = miner.current_mode
            if not current_mode:
                continue
            watts = power_stats.get(miner.id, {}).get(current_mode)
            if watts:
                already_on_draw += watts

        true_budget_watts = float(raw_surplus_watts) + already_on_draw - buffer_watts
        if true_budget_watts <= 0:
            return {}, true_budget_watts

        ordered_miners = await self._rank_by_efficiency(db, eligible_miners)

        # Only consider miners with real wattage data for at least one mode -
        # never guess a mode's draw (see _load_mode_power_stats).
        candidates: List[Tuple[Miner, List[str]]] = []
        for miner in ordered_miners:
            modes = self._candidate_modes(miner.miner_type)
            miner_stats = power_stats.get(miner.id, {})
            known_modes = [m for m in modes if m in miner_stats]
            if known_modes:
                candidates.append((miner, known_modes))

        allocation: Dict[int, str] = {}
        remaining_watts = true_budget_watts

        # Pass 1 - baseline: reserve each miner's LOWEST known-wattage mode
        # first, most efficient first. A shrinking budget then sheds the
        # least efficient miner entirely before anyone loses their floor -
        # proactive mode reduction across the whole enrolled fleet, instead
        # of the most efficient miner maxing out its highest mode and
        # starving everyone below it straight to off with no step-down.
        for miner, known_modes in candidates:
            lowest_mode = known_modes[0]
            watts = power_stats[miner.id][lowest_mode]
            if watts <= remaining_watts:
                allocation[miner.id] = lowest_mode
                remaining_watts -= watts

        # Pass 2 - upgrade: with whatever's left after baselines, bump
        # already-baselined miners up to the highest mode they can now
        # afford, most efficient first.
        for miner, known_modes in candidates:
            if miner.id not in allocation:
                continue
            current_mode = allocation[miner.id]
            current_watts = power_stats[miner.id][current_mode]
            budget_for_this_miner = remaining_watts + current_watts

            best_mode, best_watts = current_mode, current_watts
            for mode in reversed(known_modes):  # highest-draw first
                watts = power_stats[miner.id][mode]
                if watts <= budget_for_this_miner:
                    best_mode, best_watts = mode, watts
                    break

            allocation[miner.id] = best_mode
            remaining_watts = budget_for_this_miner - best_watts

        return allocation, true_budget_watts

    async def _compute_override_allocation(
        self, db: AsyncSession, eligible_miners: List[Miner]
    ) -> Tuple[Dict[int, str], float]:
        """Negative energy price override: claim every eligible miner at its
        highest known-wattage mode, ignoring live surplus entirely. Still
        skips a miner with no real wattage data at all - same "don't guess"
        rule as the normal bin-pack, there's no mode to pick for it."""
        power_stats = await self._load_mode_power_stats(db, [m.id for m in eligible_miners])

        allocation: Dict[int, str] = {}
        total_watts = 0.0
        for miner in eligible_miners:
            miner_stats = power_stats.get(miner.id)
            if not miner_stats:
                continue
            best_mode = max(miner_stats, key=miner_stats.get)
            allocation[miner.id] = best_mode
            total_watts += miner_stats[best_mode]

        return allocation, total_watts

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

    async def _get_live_device_state(self, db: AsyncSession, miner_id: int) -> Optional[str]:
        """Poll HA directly for a miner's linked device state - never trust
        the cached DB value alone. There's no separate reconciliation job;
        this cycle (run every minute) IS the reconciliation, so it must
        re-verify live truth each time, not a belief it wrote last cycle.
        Updates the cached HomeAssistantDevice.current_state as a side
        effect (same "always write back real state" rule Price Band
        Strategy follows) so other code reading that cache stays in sync.
        Returns None if there's no linked device at all.
        """
        device = await self._get_ha_device(db, miner_id)
        if not device:
            return None

        config_result = await db.execute(select(HomeAssistantConfig))
        ha_config = config_result.scalar_one_or_none()
        if not ha_config or not ha_config.enabled:
            return device.current_state

        from integrations.homeassistant import HomeAssistantIntegration

        ha = HomeAssistantIntegration(base_url=ha_config.base_url, access_token=ha_config.access_token)
        state = await ha.get_device_state(device.entity_id)
        if state:
            device.current_state = state.state
            device.last_state_change = (
                state.last_updated.replace(tzinfo=None) if state.last_updated else datetime.utcnow()
            )
            await db.commit()
            return state.state

        # Live poll failed (HA unreachable etc.) - fall back to cache rather
        # than guessing blind, but this is a real degraded condition.
        logger.debug(f"Solar Strategy: live HA poll failed for device {device.entity_id}, using cached state")
        return device.current_state

    async def _confirm_ha_state(self, ha, entity_id: str, desired_state: str, *, attempts: int = 2, delay_seconds: int = 1) -> bool:
        for attempt in range(attempts):
            state = await ha.get_device_state(entity_id)
            if state and state.state == desired_state:
                return True
            if attempt < attempts - 1:
                await asyncio.sleep(delay_seconds)
        return False

    async def _verify_miner_reachability(
        self, miner: Miner, *, should_be_online: bool, attempts: int = 2, delay_seconds: int = 2
    ) -> Optional[bool]:
        """Cross-check against the miner's real network presence when HA's
        own report is ambiguous - HA's state can lag or be wrong. Returns
        None (unknown) rather than guessing when reachability can't be
        determined at all."""
        try:
            from adapters import create_adapter

            adapter = create_adapter(miner.miner_type, miner.id, miner.name, miner.ip_address, miner.port, miner.config)
            if not adapter or not hasattr(adapter, "is_online"):
                return None

            for attempt in range(attempts):
                try:
                    is_online = await asyncio.wait_for(adapter.is_online(), timeout=4.0)
                except Exception:
                    is_online = None
                if is_online is not None:
                    return bool(is_online) == should_be_online
                if attempt < attempts - 1:
                    await asyncio.sleep(delay_seconds)
            return None
        except Exception as e:
            logger.debug(f"Solar Strategy: reachability check unavailable for {miner.name}: {e}")
            return None

    async def _set_ha_power(self, db: AsyncSession, miner: Miner, turn_on: bool) -> bool:
        """Issue an on/off command and confirm it actually took effect -
        never trust a bare success boolean from the HTTP call alone. Falls
        back to checking the miner's real reachability when HA's own state
        report doesn't confirm, same asymmetric judgment call Price Band
        Strategy makes: turning ON is accepted optimistically when
        reachability is simply unknown (not confirmed-wrong), turning OFF
        is not."""
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
        desired_state = "on" if turn_on else "off"

        success = await (ha.turn_on(device.entity_id) if turn_on else ha.turn_off(device.entity_id))
        if not success:
            logger.error(f"Solar Strategy: HA command failed for {miner.name}")
            return False

        def _accept(observed_state: Optional[str] = None) -> bool:
            device.current_state = observed_state or desired_state
            device.last_state_change = datetime.utcnow()
            return True

        confirmed = await self._confirm_ha_state(ha, device.entity_id, desired_state)
        if confirmed:
            _accept(desired_state)
        else:
            reachable_match = await self._verify_miner_reachability(miner, should_be_online=turn_on)
            if reachable_match is True:
                logger.warning(
                    f"Solar Strategy: HA state for {miner.name} unconfirmed but miner reachability matched; accepting"
                )
                _accept(desired_state)
            elif turn_on and reachable_match is None:
                logger.warning(
                    f"Solar Strategy: turn_on for {miner.name} could not be fully verified; continuing optimistically"
                )
                _accept(desired_state)
            else:
                logger.error(
                    f"Solar Strategy: command for {miner.name} was not verified "
                    f"(ha_confirmed=False, reachable_match={reachable_match})"
                )
                return False

        await db.commit()
        if turn_on:
            await asyncio.sleep(3)  # let the miner boot before a mode call
        return True

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
