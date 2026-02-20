"""
Energy Price Band Strategy - Core Logic Engine
Dynamic mining strategy optimised for provider-based energy pricing
Supports both solo and pooled mining options
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Tuple
import logging
import asyncio

from core.database import PriceBandStrategyConfig, MinerStrategy, Miner, Pool, EnergyPrice, Telemetry, PriceBandStrategyBand, HomeAssistantConfig, HomeAssistantDevice, StrategyBandModeTarget
from core.energy import get_current_energy_price
from core.audit import log_audit
from core.price_band_bands import ensure_strategy_bands, get_strategy_bands, get_band_for_price
from core.miner_capabilities import get_champion_lowest_mode
from core.config import app_config

logger = logging.getLogger(__name__)


async def ping_check(ip_address: str, timeout: int = 1) -> bool:
    """
    Check if an IP address responds to ping
    
    Args:
        ip_address: IP address to ping
        timeout: Ping timeout in seconds
        
    Returns:
        True if ping successful, False otherwise
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            'ping', '-c', '1', '-W', str(timeout), ip_address,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await asyncio.wait_for(proc.communicate(), timeout=timeout + 1)
        return proc.returncode == 0
    except (asyncio.TimeoutError, Exception) as e:
        logger.debug(f"Ping check failed for {ip_address}: {e}")
        return False


class PriceBandStrategy:
    """Price band strategy execution engine"""
    
    # Hysteresis counter requirement for upgrading bands
    HYSTERESIS_SLOTS = 2
    
    # Failure tracking for HA power cycling
    _miner_failure_counts: Dict[int, int] = {}

    @staticmethod
    def _parse_iso_datetime(value: object) -> Optional[datetime]:
        if not isinstance(value, str):
            return None
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except Exception:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _to_naive_utc(value: Optional[datetime]) -> Optional[datetime]:
        if not value:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    @staticmethod
    def _normalize_pool_url(url: str) -> str:
        """Strip protocol/trailing slashes for stable pool URL comparisons."""
        normalized = url.replace("stratum+tcp://", "").replace("stratum+ssl://", "")
        normalized = normalized.replace("http://", "").replace("https://", "")
        return normalized.rstrip("/").lower()

    @staticmethod
    def _get_target_mode_from_band(
        miner_type: str,
        band: PriceBandStrategyBand,
        band_mode_targets: Optional[Dict[int, Dict[str, str]]] = None,
        nmminer_mode: Optional[str] = None,
    ) -> Optional[str]:
        """Resolve target mode for a miner type from dynamic band mode targets."""
        if band_mode_targets:
            mode_targets = band_mode_targets.get(band.id, {})
            if miner_type in mode_targets:
                return mode_targets[miner_type]
        if miner_type == "nmminer":
            return nmminer_mode
        return None

    @staticmethod
    async def _load_band_mode_targets(
        db: AsyncSession,
        bands: List[PriceBandStrategyBand],
    ) -> Dict[int, Dict[str, str]]:
        """Load persisted per-band mode targets for dynamic miner types."""
        band_ids = [b.id for b in bands if b and b.id is not None]
        if not band_ids:
            return {}

        result = await db.execute(
            select(StrategyBandModeTarget).where(StrategyBandModeTarget.band_id.in_(band_ids))
        )
        rows = result.scalars().all()

        targets: Dict[int, Dict[str, str]] = {}
        for row in rows:
            targets.setdefault(row.band_id, {})[row.miner_type] = row.mode
        return targets

    @staticmethod
    def _get_champion_lowest_mode(miner_type: str) -> str:
        """Lowest efficiency mode used for champion mode."""
        return get_champion_lowest_mode(miner_type)
    
    @staticmethod
    async def control_ha_device_for_miner(db: AsyncSession, miner: Miner, turn_on: bool) -> bool:
        """
        Control Home Assistant device linked to a miner
        
        Args:
            db: Database session
            miner: Miner object
            turn_on: True to turn on, False to turn off
            
        Returns:
            True if device was controlled, False if no device linked or control failed
        """
        try:
            # Check if miner has a linked HA device
            result = await db.execute(
                select(HomeAssistantDevice)
                .where(HomeAssistantDevice.miner_id == miner.id)
                .where(HomeAssistantDevice.enrolled == True)
            )
            ha_device = result.scalar_one_or_none()
            
            if not ha_device:
                logger.debug(f"No HA device linked to miner {miner.name}")
                return False
            
            # Get HA config
            config_result = await db.execute(select(HomeAssistantConfig))
            ha_config = config_result.scalar_one_or_none()
            
            if not ha_config or not ha_config.enabled:
                logger.warning(f"HA integration not configured or disabled, cannot control device for {miner.name}")
                return False
            
            # Import here to avoid circular dependencies
            from integrations.homeassistant import HomeAssistantIntegration
            
            # Create HA integration instance
            ha_integration = HomeAssistantIntegration(
                base_url=ha_config.base_url,
                access_token=ha_config.access_token
            )
            
            # Check current state before sending command (optimization)
            current_state = await ha_integration.get_device_state(ha_device.entity_id)
            desired_state = "on" if turn_on else "off"
            
            # ALWAYS update database with actual HA state to prevent stale data
            if current_state:
                ha_device.current_state = current_state.state
                ha_device.last_state_change = PriceBandStrategy._to_naive_utc(
                    current_state.last_updated
                )
                # Update off timestamp if device is currently off
                if current_state.state == "off":
                    ha_device.last_off_command_timestamp = PriceBandStrategy._to_naive_utc(
                        current_state.last_updated
                    ) or datetime.utcnow()
                await db.commit()  # Commit actual state immediately, even if command will fail

            if current_state and current_state.state == desired_state:
                # Device already in desired state, nothing to do
                logger.debug(f"⏭️ HA device {ha_device.name} already {desired_state.upper()} for miner {miner.name} - skipping")
                return True  # Already in desired state, no action needed
            
            # Control device
            action = "turn_on" if turn_on else "turn_off"
            logger.info(f"HA: {action} device {ha_device.name} for miner {miner.name}")
            
            if turn_on:
                success = await ha_integration.turn_on(ha_device.entity_id)
            else:
                success = await ha_integration.turn_off(ha_device.entity_id)
            
            if success:
                ha_device.current_state = desired_state
                ha_device.last_state_change = datetime.utcnow()
                if desired_state == "off":
                    ha_device.last_off_command_timestamp = datetime.utcnow()
                else:
                    ha_device.last_off_command_timestamp = None
                await db.commit()  # Persist state changes to prevent reconciliation conflicts
                logger.info(f"✓ HA device {ha_device.name} {'ON' if turn_on else 'OFF'} for miner {miner.name}")
                return True
            else:
                logger.error(f"✗ Failed to control HA device {ha_device.name} for miner {miner.name}")
                return False
                
        except asyncio.CancelledError:
            logger.warning(f"HA device control cancelled for miner {miner.name}")
            return False
        except Exception as e:
            logger.error(f"Error controlling HA device for miner {miner.name}: {e}")
            return False

    @staticmethod
    async def _get_enrolled_ha_device(db: AsyncSession, miner_id: int) -> Optional[HomeAssistantDevice]:
        """Return enrolled HA device for miner, if any."""
        result = await db.execute(
            select(HomeAssistantDevice)
            .where(HomeAssistantDevice.miner_id == miner_id)
            .where(HomeAssistantDevice.enrolled == True)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _is_ha_device_off(db: AsyncSession, miner_id: int) -> bool:
        """Check whether enrolled HA device is currently OFF."""
        ha_device = await PriceBandStrategy._get_enrolled_ha_device(db, miner_id)
        return bool(ha_device and ha_device.current_state == "off")

    @staticmethod
    async def _enforce_ha_state(
        db: AsyncSession,
        miner: Miner,
        turn_on: bool,
        *,
        sleep_after_on_seconds: int = 0,
    ) -> bool:
        """Enforce HA state for miner and optionally wait for boot after turning on."""
        controlled = await PriceBandStrategy.control_ha_device_for_miner(db, miner, turn_on=turn_on)
        if controlled and turn_on and sleep_after_on_seconds > 0:
            await asyncio.sleep(sleep_after_on_seconds)
        return controlled
    
    @staticmethod
    async def _validate_and_power_cycle_ha_device(
        db: AsyncSession,
        miner: Miner,
        actions_taken: List[str]
    ) -> None:
        """
        Validate HA device state and power cycle if stuck
        
        Checks if miner has HA control, pings the miner, and if HA says ON
        but ping fails, power cycles the device (OFF → wait 10s → ON → wait 60s)
        
        Args:
            db: Database session
            miner: Miner object
            actions_taken: List to append action messages to
        """
        try:
            # Check if miner has linked HA device
            ha_device = await PriceBandStrategy._get_enrolled_ha_device(db, miner.id)
            
            if not ha_device:
                actions_taken.append(f"{miner.name}: Pool unknown (skipped, no HA control)")
                return
            
            # Get HA config
            config_result = await db.execute(select(HomeAssistantConfig))
            ha_config = config_result.scalar_one_or_none()
            
            if not ha_config or not ha_config.enabled:
                actions_taken.append(f"{miner.name}: Pool unknown (skipped, HA disabled)")
                return
            
            from integrations.homeassistant import HomeAssistantIntegration
            from core.notifications import NotificationService
            
            ha_integration = HomeAssistantIntegration(
                base_url=ha_config.base_url,
                access_token=ha_config.access_token
            )
            
            # Check HA device state
            device_state = await ha_integration.get_device_state(ha_device.entity_id)
            
            if not device_state or device_state.state != "on":
                # HA says device is OFF, that explains why miner isn't responding
                logger.info(f"{miner.name}: HA device is OFF, turning ON")
                await ha_integration.turn_on(ha_device.entity_id)
                ha_device.current_state = "on"
                ha_device.last_state_change = datetime.utcnow()
                actions_taken.append(f"{miner.name}: HA device turned ON (was OFF)")
                await db.commit()
                return
            
            # HA says ON, but let's verify miner is actually reachable
            logger.info(f"Pinging {miner.name} at {miner.ip_address} to validate reachability...")
            is_reachable = await ping_check(miner.ip_address, timeout=2)
            
            if is_reachable:
                # Miner is reachable, not a power issue
                actions_taken.append(f"{miner.name}: Pool unknown (skipped, device reachable)")
                return
            
            # HA says ON but ping fails - socket is stuck!
            logger.warning(
                f"⚠️  {miner.name}: HA device {ha_device.name} is ON but miner not reachable. "
                "Power cycling to unstick socket..."
            )
            
            # Power cycle: OFF → wait 10s → ON → wait 60s
            off_success = await ha_integration.turn_off(ha_device.entity_id)
            if not off_success:
                logger.error(f"Failed to turn OFF {ha_device.name} during power cycle")
                actions_taken.append(f"{miner.name}: HA power cycle FAILED (turn OFF failed)")
                return
            
            logger.info(f"🔄 Turned OFF {ha_device.name}, waiting 10s...")
            await asyncio.sleep(10)
            
            on_success = await ha_integration.turn_on(ha_device.entity_id)
            if not on_success:
                logger.error(f"Failed to turn ON {ha_device.name} during power cycle")
                actions_taken.append(f"{miner.name}: HA power cycle FAILED (turn ON failed)")
                return
            
            ha_device.current_state = "on"
            ha_device.last_state_change = datetime.utcnow()
            await db.commit()
            
            logger.info(f"✅ Power cycled {ha_device.name}, waiting 60s for miner boot...")
            
            # Send notification
            notification_service = NotificationService()
            await notification_service.send_to_all_channels(
                message=(
                    "🔄 HA Device Power Cycled\n"
                    f"Device {ha_device.name} was stuck OFF despite ON status. "
                    "Cycled device (OFF → wait 10s → ON) to force restart."
                ),
                alert_type="ha_device_power_cycle"
            )
            
            # Reset failure count after successful power cycle
            PriceBandStrategy._reset_miner_failure(miner.id)
            
            actions_taken.append(f"{miner.name}: HA device power cycled (stuck socket)")
            
            # Wait for boot
            await asyncio.sleep(60)
            
        except Exception as e:
            logger.error(f"Error validating/power cycling HA device for {miner.name}: {e}", exc_info=True)
            actions_taken.append(f"{miner.name}: HA validation error")

    @staticmethod
    def _increment_miner_failure(miner_id: int) -> int:
        """Increment and return consecutive failure count for a miner."""
        count = PriceBandStrategy._miner_failure_counts.get(miner_id, 0) + 1
        PriceBandStrategy._miner_failure_counts[miner_id] = count
        return count

    @staticmethod
    def _reset_miner_failure(miner_id: int) -> None:
        """Reset consecutive failure count for a miner."""
        PriceBandStrategy._miner_failure_counts[miner_id] = 0

    @staticmethod
    async def _promote_champion_on_failure_if_needed(
        db: AsyncSession,
        strategy: PriceBandStrategyConfig,
        champion_mode_active: bool,
        miner: Miner,
        reason: str,
        actions_taken: List[str],
    ) -> bool:
        """Promote next champion when the current champion fails.

        Returns True when a promotion occurred.
        """
        if not (champion_mode_active and strategy.current_champion_miner_id == miner.id):
            return False

        all_enrolled = await PriceBandStrategy.get_enrolled_miners(db)
        new_champion = await PriceBandStrategy.promote_next_champion(
            db, strategy, all_enrolled, miner.id, reason
        )
        if new_champion:
            actions_taken.append(f"Champion failed, promoted: {new_champion.name}")
            return True

        return False
    
    @staticmethod
    async def get_enrolled_miners(db: AsyncSession) -> List[Miner]:
        """
        Get list of miners enrolled in strategy
        
        Args:
            db: Database session
            
        Returns:
            List of enrolled Miner objects
        """
        result = await db.execute(
            select(Miner)
            .join(MinerStrategy, Miner.id == MinerStrategy.miner_id)
            .where(MinerStrategy.strategy_enabled == True)
            .where(Miner.enabled == True)
        )
        return result.scalars().all()

    @staticmethod
    async def _is_miner_currently_enrolled(db: AsyncSession, miner_id: int) -> bool:
        """Check live enrollment state to avoid acting on stale in-memory miner snapshots."""
        result = await db.execute(
            select(MinerStrategy)
            .join(Miner, Miner.id == MinerStrategy.miner_id)
            .where(MinerStrategy.miner_id == miner_id)
            .where(MinerStrategy.strategy_enabled == True)
            .where(Miner.enabled == True)
            .limit(1)
        )
        return result.scalar_one_or_none() is not None
    
    @staticmethod
    async def validate_required_pools(db: AsyncSession, bands: List[PriceBandStrategyBand]) -> Tuple[bool, List[str]]:
        """
        Validate that required pools are configured for enabled band coins
        
        Checks that pools exist for any coins configured in bands (not OFF)
        
        Args:
            db: Database session
            bands: List of configured strategy bands
            
        Returns:
            (is_valid, list_of_violations)
        """
        from core.config import app_config
        
        violations = []
        
        # Get unique pool IDs from bands (excluding OFF/None)
        required_pool_ids = set(band.target_pool_id for band in bands if band.target_pool_id is not None)
        
        if not required_pool_ids:
            return (True, [])  # No pools configured, nothing to validate
        
        # Validate that all required pool IDs exist and are enabled
        pools_result = await db.execute(
            select(Pool).where(Pool.id.in_(required_pool_ids), Pool.enabled == True)
        )
        available_pools = pools_result.scalars().all()
        available_pool_ids = {p.id for p in available_pools}
        
        # Check for missing pools
        missing_pool_ids = required_pool_ids - available_pool_ids
        if missing_pool_ids:
            # Fetch pool names for better error messages
            all_pools_result = await db.execute(select(Pool).where(Pool.id.in_(missing_pool_ids)))
            missing_pools = all_pools_result.scalars().all()
            
            for pool in missing_pools:
                violations.append(f"Missing or disabled pool: {pool.name} (Pool #{pool.id})")
            
            # Check if any were not found at all
            found_missing_ids = {p.id for p in missing_pools}
            unfound_ids = missing_pool_ids - found_missing_ids
            for pool_id in unfound_ids:
                violations.append(f"Pool #{pool_id} does not exist")
        
        return (len(violations) == 0, violations)

    
    @staticmethod
    async def get_next_slot_price(db: AsyncSession) -> Optional[float]:
        """
        Get the price for the next Agile slot (30 minutes from now)
        
        Returns:
            Price in pence/kWh or None if not available
        """
        from core.config import app_config
        
        region = app_config.get("octopus_agile.region", "H")
        now = datetime.utcnow()
        next_slot_start = now + timedelta(minutes=30)
        
        result = await db.execute(
            select(EnergyPrice)
            .where(EnergyPrice.region == region)
            .where(EnergyPrice.valid_from <= next_slot_start)
            .where(EnergyPrice.valid_to > next_slot_start)
            .limit(1)
        )
        next_price = result.scalar_one_or_none()
        return next_price.price_pence if next_price else None
    
    @staticmethod
    async def determine_band_with_hysteresis(
        db: AsyncSession,
        current_price: float,
        strategy: PriceBandStrategyConfig,
        bands: List[PriceBandStrategyBand]
    ) -> Tuple[PriceBandStrategyBand, int]:
        """
        Determine target price band with look-ahead confirmation
        
        When upgrading to a better band, checks if the NEXT slot also
        qualifies for that band. Only upgrades if confirmed, preventing
        oscillation from single cheap slots.
        
        CRITICAL: OFF band is ALWAYS immediate - no confirmation needed.
        
        Args:
            db: Database session
            current_price: Current energy price (p/kWh)
            strategy: Current strategy state
            bands: List of configured price bands (ordered by sort_order)
            
        Returns:
            (target_band_object, new_hysteresis_counter)
        """
        # Get current and new band objects
        current_band_obj = None
        if strategy.current_band_sort_order:
            # Find current band by sort_order (more reliable than coin name)
            for band in bands:
                if band.sort_order == strategy.current_band_sort_order:
                    current_band_obj = band
                    break
        
        # If no current band, start with first band (worst/OFF)
        if not current_band_obj:
            current_band_obj = bands[0]
        
        # Get new band for current price
        new_band_obj = get_band_for_price(bands, current_price)
        
        if not new_band_obj:
            logger.error("Could not determine band for current price")
            return (bands[0], 0)  # Default to first band (OFF)
        
        # SAFETY: If current price hits OFF band (target_pool_id = None), turn off immediately
        if new_band_obj.target_pool_id is None:
            logger.warning(f"Price hit OFF threshold: {current_price:.2f}p - IMMEDIATE shutdown")
            return (new_band_obj, 0)
        
        # Compare band positions (lower sort_order = cheaper = better pricing)
        current_idx = current_band_obj.sort_order
        new_idx = new_band_obj.sort_order
        
        # Special case: Transitioning from OFF to any active state
        # When coming from OFF, ensure next slot won't immediately go back to OFF
        if current_band_obj.target_pool_id is None and new_band_obj.target_pool_id is not None:
            # Get next slot price to verify we won't immediately turn off again
            next_slot_price = await PriceBandStrategy.get_next_slot_price(db)
            
            if next_slot_price is None:
                # No future price data, stay OFF to be safe
                logger.warning(f"No next slot price available, staying OFF")
                return (current_band_obj, 0)
            
            next_band_obj = get_band_for_price(bands, next_slot_price)
            
            if not next_band_obj:
                # Invalid next band, stay OFF
                return (current_band_obj, 0)
            
            # Check if next slot is also active (not OFF)
            if next_band_obj.target_pool_id is None:
                # Next slot goes back to OFF, don't turn on for just 1 slot
                pool_name = await PriceBandStrategy._get_pool_name(db, new_band_obj.target_pool_id)
                logger.info(f"Skipping OFF→{pool_name} transition: next slot returns to OFF (current: {current_price:.2f}p → next: {next_slot_price:.2f}p)")
                return (current_band_obj, 0)
            else:
                # Next slot stays active, safe to turn on
                pool_name = await PriceBandStrategy._get_pool_name(db, new_band_obj.target_pool_id)
                logger.info(f"OFF→{pool_name} transition confirmed: next slot stays active (current: {current_price:.2f}p → next: {next_slot_price:.2f}p)")
                return (new_band_obj, 0)
        
        # If price improved (lower sort_order = cheaper = better band) 
        if new_idx < current_idx:
            # Upgrading band - check next slot for confirmation
            next_slot_price = await PriceBandStrategy.get_next_slot_price(db)
            
            if next_slot_price is None:
                # No future price data, stay in current band
                current_pool_name = await PriceBandStrategy._get_pool_name(db, current_band_obj.target_pool_id)
                logger.warning(f"No next slot price available, staying in {current_pool_name}")
                return (current_band_obj, 0)
            
            next_band_obj = get_band_for_price(bands, next_slot_price)
            
            if not next_band_obj:
                # Invalid next band, stay safe
                return (current_band_obj, 0)
            
            next_idx = next_band_obj.sort_order
            
            # Check if next slot is also in the better band (or even better)
            # Lower sort_order = cheaper = better
            if next_idx <= new_idx:
                # Next slot confirms the improvement, upgrade immediately
                curr_pool = await PriceBandStrategy._get_pool_name(db, current_band_obj.target_pool_id)
                new_pool = await PriceBandStrategy._get_pool_name(db, new_band_obj.target_pool_id)
                logger.info(f"Next slot confirms improvement (current: {current_price:.2f}p → next: {next_slot_price:.2f}p), upgrading from {curr_pool} to {new_pool}")
                return (new_band_obj, 0)
            else:
                # Next slot goes back to worse band, stay put
                curr_pool = await PriceBandStrategy._get_pool_name(db, current_band_obj.target_pool_id)
                logger.info(f"Next slot returns to worse pricing (current: {current_price:.2f}p → next: {next_slot_price:.2f}p), staying in {curr_pool}")
                return (current_band_obj, 0)
        
        # If price worsened (higher sort_order = more expensive = worse band)
        elif new_idx > current_idx:
            # Immediate downgrade
            curr_pool = await PriceBandStrategy._get_pool_name(db, current_band_obj.target_pool_id)
            new_pool = await PriceBandStrategy._get_pool_name(db, new_band_obj.target_pool_id)
            logger.info(f"Price worsened, immediate downgrade from {curr_pool} to {new_pool}")
            return (new_band_obj, 0)
        
        # Price unchanged
        else:
            # Stay in current band
            return (current_band_obj, 0)
    
    @staticmethod
    async def _get_pool_name(db: AsyncSession, pool_id: Optional[int]) -> str:
        """
        Get pool name from pool_id for logging purposes.
        
        Args:
            db: Database session
            pool_id: Pool ID or None for OFF
            
        Returns:
            Pool name or 'OFF'
        """
        if pool_id is None:
            return "OFF"
        
        result = await db.execute(select(Pool).where(Pool.id == pool_id))
        pool = result.scalar_one_or_none()
        return pool.name if pool else f"Pool#{pool_id}"

    @staticmethod
    def _get_failover_settings() -> Dict[str, object]:
        return {
            "enabled": bool(app_config.get("price_band_strategy.failover.enabled", False)),
            "backup_pool_id": app_config.get("price_band_strategy.failover.backup_pool_id", None),
            "hard_lock_active": bool(app_config.get("price_band_strategy.failover.hard_lock_active", False)),
        }

    @staticmethod
    async def _notify_failover_event(message: str) -> None:
        try:
            from core.notifications import send_alert

            await send_alert(message=message, alert_type="aggregation_status")
        except Exception as exc:
            logger.warning("Failover notification failed: %s", exc)

    @staticmethod
    async def _apply_failover_pool_override(
        db: AsyncSession,
        target_pool: Optional[Pool],
        actions_taken: List[str],
        strategy: Optional[PriceBandStrategyConfig] = None,
    ) -> Optional[Pool]:
        if not target_pool:
            return target_pool

        settings = PriceBandStrategy._get_failover_settings()
        failover_enabled = bool(settings.get("enabled", False))
        backup_pool_id = settings.get("backup_pool_id")

        if not failover_enabled or backup_pool_id is None:
            return target_pool

        try:
            backup_pool_id_int = int(backup_pool_id)
        except Exception:
            logger.warning("Failover backup_pool_id is invalid: %s", backup_pool_id)
            return target_pool

        backup_result = await db.execute(
            select(Pool).where(Pool.id == backup_pool_id_int, Pool.enabled == True)
        )
        backup_pool = backup_result.scalar_one_or_none()
        if not backup_pool:
            logger.warning("Failover backup pool #%s is missing or disabled", backup_pool_id_int)
            return target_pool

        hard_lock_active = bool(settings.get("hard_lock_active", False))
        state_data = dict(strategy.state_data or {}) if strategy else {}
        was_on_backup = bool(state_data.get("failover_on_backup", False))
        was_hard_locked = bool(state_data.get("failover_hard_lock_active", False))

        def _persist_state() -> None:
            if strategy is not None:
                strategy.state_data = state_data

        if hard_lock_active:
            if not was_hard_locked:
                await PriceBandStrategy._notify_failover_event(
                    f"🔒 Failover hard-lock active. Holding miners on backup pool {backup_pool.name}."
                )
            state_data["failover_hard_lock_active"] = True

            if target_pool.id != backup_pool.id:
                actions_taken.append(f"Failover hard-lock active: using backup pool {backup_pool.name}")
                logger.info(
                    "Failover hard-lock active: overriding target pool %s -> %s",
                    target_pool.name,
                    backup_pool.name,
                )
            if not was_on_backup:
                await PriceBandStrategy._notify_failover_event(
                    f"⚠️ Failover active. Switching strategy miners to backup pool {backup_pool.name}."
                )
            state_data["failover_on_backup"] = True
            _persist_state()
            return backup_pool
        if was_on_backup:
            await PriceBandStrategy._notify_failover_event(
                f"✅ Restored to primary strategy pool {target_pool.name}."
            )

        state_data["failover_on_backup"] = False
        state_data["failover_hard_lock_active"] = False
        _persist_state()

        return target_pool
    
    @staticmethod
    async def get_efficiency_leaderboard(db: AsyncSession, enrolled_miners: List[Miner]) -> List[Tuple[Miner, float]]:
        """
        Get efficiency leaderboard for enrolled miners (sorted by W/TH, best first)
        
        Args:
            db: Database session
            enrolled_miners: List of enrolled miners
            
        Returns:
            List of (miner, w_per_th) tuples sorted by efficiency (lowest W/TH = best)
        """
        efficiency_list = []
        
        # Get recent telemetry for each miner (last 6 hours)
        cutoff = datetime.utcnow() - timedelta(hours=6)
        
        for miner in enrolled_miners:
            # Get recent telemetry
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
                logger.debug(f"{miner.name}: No recent telemetry for efficiency calculation")
                continue
            
            # Calculate average efficiency
            total_hashrate = 0.0
            total_power = 0.0
            count = 0
            
            for row in rows:
                if row[0] and row[1]:  # hashrate and power_watts
                    total_hashrate += row[0]
                    total_power += row[1]
                    count += 1
            
            if count > 0:
                avg_hashrate_ghs = total_hashrate / count
                avg_power_watts = total_power / count
                
                # Convert to TH/s and calculate W/TH
                hashrate_ths = avg_hashrate_ghs / 1000.0
                
                if hashrate_ths > 0:
                    w_per_th = avg_power_watts / hashrate_ths
                    efficiency_list.append((miner, w_per_th))
                    logger.debug(f"{miner.name}: {w_per_th:.2f} W/TH ({avg_power_watts:.1f}W @ {hashrate_ths:.3f} TH/s)")
        
        # Sort by efficiency (lower is better)
        efficiency_list.sort(key=lambda x: x[1])
        
        logger.info(f"Efficiency leaderboard: {len(efficiency_list)} miners ranked")
        for i, (miner, wth) in enumerate(efficiency_list):
            logger.info(f"  #{i+1}: {miner.name} = {wth:.2f} W/TH")
        
        return efficiency_list
    
    @staticmethod
    async def promote_next_champion(
        db: AsyncSession,
        strategy: PriceBandStrategyConfig,
        enrolled_miners: List[Miner],
        failed_champion_id: int,
        reason: str
    ) -> Optional[Miner]:
        """
        Promote the next best miner to champion when current champion fails
        
        Args:
            db: Database session
            strategy: PriceBandStrategyConfig object
            enrolled_miners: List of all enrolled miners
            failed_champion_id: ID of the failed champion
            reason: Reason for promotion
            
        Returns:
            New champion miner or None if no candidates
        """
        logger.warning(f"Champion #{failed_champion_id} failed: {reason}")
        logger.info("Promoting next best miner to champion...")
        
        # Get efficiency leaderboard
        efficiency_ranking = await PriceBandStrategy.get_efficiency_leaderboard(db, enrolled_miners)
        
        # Find next best candidate (skip the failed champion)
        for miner, wth in efficiency_ranking:
            if miner.id != failed_champion_id:
                # Found next champion
                strategy.current_champion_miner_id = miner.id
                
                logger.info(f"New champion: {miner.name} ({wth:.2f} W/TH)")
                
                await log_audit(
                    db,
                    action="champion_promoted",
                    resource_type="price_band_strategy",
                    resource_name="Champion Mode",
                    changes={
                        "failed_champion_id": failed_champion_id,
                        "new_champion_id": miner.id,
                        "new_champion_name": miner.name,
                        "efficiency_wth": round(wth, 2),
                        "reason": reason
                    }
                )
                
                # Send notification
                from core.notifications import send_alert
                await send_alert(
                    message=f"Champion Promoted: Champion failed ({reason}). {miner.name} promoted to champion ({wth:.2f} W/TH).",
                    alert_type="high_temperature",  # Reuse existing type for alerts
                )
                
                return miner
        
        # No candidates left
        logger.error("No champion candidates available")
        strategy.current_champion_miner_id = None
        
        await log_audit(
            db,
            action="champion_exhausted",
            resource_type="price_band_strategy",
            resource_name="Champion Mode",
            changes={
                "failed_champion_id": failed_champion_id,
                "reason": "No more candidates"
            }
        )
        
        return None
    
    @staticmethod
    async def execute_strategy(db: AsyncSession) -> Dict:
        """
        Execute the price band strategy
        
        Returns:
            Execution report dict with actions taken
        """
        logger.info("=" * 60)
        logger.info("EXECUTING PRICE BAND STRATEGY")
        logger.info("=" * 60)
        
        # Get strategy config
        result = await db.execute(select(PriceBandStrategyConfig))
        strategy = result.scalar_one_or_none()
        
        if not strategy or not strategy.enabled:
            logger.info("Strategy disabled, skipping execution")
            return {"enabled": False, "message": "Strategy is disabled"}
        
        # Ensure bands are initialized (handles migration from old versions)
        await ensure_strategy_bands(db, strategy.id)
        
        # Get configured bands
        bands = await get_strategy_bands(db, strategy.id)
        band_mode_targets = await PriceBandStrategy._load_band_mode_targets(db, bands)
        
        if not bands:
            logger.error("No bands configured for strategy")
            return {"error": "NO_BANDS", "message": "No price bands configured"}
        
        # Get enrolled miners
        enrolled_miners = await PriceBandStrategy.get_enrolled_miners(db)
        
        if not enrolled_miners:
            logger.warning("No miners enrolled in strategy")
            return {"enabled": True, "miners": 0, "message": "No enrolled miners"}
        
        logger.info(f"Enrolled miners: {len(enrolled_miners)}")
        
        # Validate required pools exist for configured bands
        is_valid, violations = await PriceBandStrategy.validate_required_pools(db, bands)
        
        if not is_valid:
            logger.error(f"Pool validation FAILED: {violations}")
            await log_audit(
                db,
                action="price_band_strategy_disabled",
                resource_type="price_band_strategy",
                resource_name="Price Band Strategy",
                status="error",
                error_message=f"Pool validation failed: {', '.join(violations)}",
                changes={"violations": violations}
            )
            # CRITICAL: Disable strategy on validation failure
            strategy.enabled = False
            await db.commit()
            return {
                "enabled": False,
                "error": "VALIDATION_FAILED",
                "violations": violations,
                "message": "Strategy disabled due to missing required pools"
            }
        
        # Get current energy price
        current_price_obj = await get_current_energy_price(db)
        
        if current_price_obj is None:
            logger.error("Failed to get current energy price")
            return {"error": "NO_PRICE_DATA", "message": "No energy price data available"}
        
        current_price = current_price_obj.price_pence
        logger.info(f"Current energy price: {current_price}p/kWh")
        
        # Apply hysteresis logic to determine target band with look-ahead confirmation
        target_band_obj, new_counter = await PriceBandStrategy.determine_band_with_hysteresis(
            db, current_price, strategy, bands
        )
        
        if not target_band_obj:
            logger.error("Could not determine band for current price")
            return {"error": "BAND_ERROR", "message": "Could not determine price band"}
        
        target_pool_name = await PriceBandStrategy._get_pool_name(db, target_band_obj.target_pool_id)
        logger.info(f"Target band: {target_pool_name} (sort_order={target_band_obj.sort_order}) @ {current_price}p/kWh")
        
        # Detect if this is an actual band transition using sort_order (unique identifier)
        is_band_transition = strategy.current_band_sort_order != target_band_obj.sort_order
        
        if is_band_transition:
            # Look up current band for logging
            current_band_obj = None
            if strategy.current_band_sort_order is not None:
                for band in bands:
                    if band.sort_order == strategy.current_band_sort_order:
                        current_band_obj = band
                        break
            
            current_pool_name = await PriceBandStrategy._get_pool_name(db, current_band_obj.target_pool_id) if current_band_obj else "Unknown"
            logger.info(f"BAND TRANSITION: band #{strategy.current_band_sort_order} ({current_pool_name}) → band #{target_band_obj.sort_order} ({target_pool_name})")
        else:
            logger.debug(f"Staying in current band #{target_band_obj.sort_order}: {target_pool_name}")
        
        # Update strategy state
        strategy.current_price_band = target_pool_name  # Store pool name for display
        strategy.current_band_sort_order = target_band_obj.sort_order  # Track specific band
        strategy.last_price_checked = current_price
        strategy.last_action_time = datetime.utcnow()
        strategy.hysteresis_counter = new_counter
        
        # Champion Mode: Handle Band 5 (sort_order == 5)
        is_band_5 = target_band_obj.sort_order == 5
        champion_mode_active = strategy.champion_mode_enabled and is_band_5
        
        # Clear champion when exiting Band 5
        if strategy.current_champion_miner_id and not is_band_5:
            logger.info(f"Exiting Band 5 - clearing champion (was miner #{strategy.current_champion_miner_id})")
            strategy.current_champion_miner_id = None
            await log_audit(
                db,
                action="champion_cleared",
                resource_type="price_band_strategy",
                resource_name="Champion Mode",
                changes={"reason": "Exited Band 5"}
            )
        
        # Select champion ONLY when entering Band 5 for the first time (sticky throughout Band 5)
        if champion_mode_active and is_band_transition and not strategy.current_champion_miner_id:
            logger.info("=" * 60)
            logger.info("CHAMPION MODE ACTIVE - Band 5 Entry")
            logger.info("=" * 60)
            
            # Get efficiency leaderboard
            efficiency_ranking = await PriceBandStrategy.get_efficiency_leaderboard(db, enrolled_miners)
            
            if not efficiency_ranking:
                logger.error("No efficiency data available for champion selection")
                champion_mode_active = False  # Disable champion mode for this execution
            else:
                # Select champion (most efficient miner)
                champion, champion_wth = efficiency_ranking[0]
                strategy.current_champion_miner_id = champion.id
                
                logger.info(f"Champion selected: {champion.name} ({champion_wth:.2f} W/TH)")
                
                await log_audit(
                    db,
                    action="champion_selected",
                    resource_type="price_band_strategy",
                    resource_name="Champion Mode",
                    changes={
                        "champion_miner_id": champion.id,
                        "champion_name": champion.name,
                        "efficiency_wth": round(champion_wth, 2),
                        "band": target_band_obj.sort_order,
                        "price": current_price
                    }
                )
        
        # Get target pool from band (pool-id driven)
        target_pool = None
        target_pool_name = None
        
        # Check if band specifies a pool (None = OFF state)
        if target_band_obj.target_pool_id:
            # NEW: Direct pool ID reference
            result = await db.execute(
                select(Pool).where(Pool.id == target_band_obj.target_pool_id, Pool.enabled == True)
            )
            target_pool = result.scalar_one_or_none()
            
            if not target_pool:
                logger.error(f"Pool #{target_band_obj.target_pool_id} not found or disabled")
                return {
                    "error": "POOL_NOT_FOUND",
                    "message": f"Pool #{target_band_obj.target_pool_id} is not available"
                }
            
            target_pool_name = target_pool.name
            logger.info(f"Target pool: {target_pool_name} (ID: {target_pool.id})")
        
        actions_taken = []
        effective_band_mode_targets = {band_id: dict(targets) for band_id, targets in band_mode_targets.items()}

        requested_target_pool_name = target_pool_name
        target_pool = await PriceBandStrategy._apply_failover_pool_override(db, target_pool, actions_taken, strategy)
        if target_pool:
            target_pool_name = target_pool.name
        
        # Handle OFF state - turn off HA devices
        if not target_pool:
            logger.info(f"Target is OFF (price: {current_price}p/kWh)")
            
            # Only control HA devices on actual transition to OFF, not every execution
            if is_band_transition:
                logger.info("TRANSITIONING TO OFF - turning off linked HA devices")
                ha_actions = []
                for miner in enrolled_miners:
                    if not await PriceBandStrategy._is_miner_currently_enrolled(db, miner.id):
                        logger.info(f"{miner.name}: skipped OFF action (miner no longer enrolled)")
                        ha_actions.append(f"{miner.name}: skipped (no longer enrolled)")
                        continue
                    controlled = await PriceBandStrategy._enforce_ha_state(db, miner, turn_on=False)
                    if controlled:
                        ha_actions.append(f"{miner.name}: HA device turned OFF")
                    else:
                        ha_actions.append(f"{miner.name}: No HA device linked")
                
                await log_audit(
                    db,
                    action="price_band_strategy_off_detected",
                    resource_type="price_band_strategy",
                    resource_name="Price Band Strategy",
                    changes={
                        "price": current_price,
                        "miners_enrolled": len(enrolled_miners),
                        "ha_devices_controlled": len([a for a in ha_actions if "turned OFF" in a])
                    }
                )
                
                await db.commit()
                
                return {
                    "enabled": True,
                    "price": current_price,
                    "band": "OFF",
                    "pool": None,
                    "miners": len(enrolled_miners),
                    "message": f"Transitioned to OFF - {len([a for a in ha_actions if 'turned OFF' in a])} HA devices turned off",
                    "actions": ha_actions
                }
            else:
                # Already in OFF band, no action needed
                logger.debug("Already in OFF band (no action needed)")
                await db.commit()
                return {
                    "enabled": True,
                    "price": current_price,
                    "band": "OFF",
                    "pool": None,
                    "miners": len(enrolled_miners),
                    "message": "Already in OFF state (no action taken)",
                    "actions": []
                }
        
        else:
            # Pool is active (not OFF)
            logger.info(f"Target pool: {target_pool_name} (price: {current_price}p/kWh)")
            
            # Champion Mode: If active, only run champion miner, turn off all others
            if champion_mode_active and strategy.current_champion_miner_id:
                champion_miner = next((m for m in enrolled_miners if m.id == strategy.current_champion_miner_id), None)
                
                if not champion_miner:
                    logger.error(f"Champion miner #{strategy.current_champion_miner_id} not found in enrolled miners")
                    # Fall back to normal processing
                    champion_mode_active = False
                else:
                    logger.info(f"Champion Mode: Processing champion {champion_miner.name}")
                    
                    # Only control HA devices on band transition (same pattern as normal mode)
                    if is_band_transition:
                        logger.info("Champion Mode transition: Controlling HA devices")
                        
                        # Turn OFF all non-champion miners via HA
                        for miner in enrolled_miners:
                            if miner.id != strategy.current_champion_miner_id:
                                if not await PriceBandStrategy._is_miner_currently_enrolled(db, miner.id):
                                    logger.info(f"{miner.name}: skipped champion OFF action (miner no longer enrolled)")
                                    actions_taken.append(f"{miner.name}: Skipped (no longer enrolled)")
                                    continue
                                controlled = await PriceBandStrategy._enforce_ha_state(db, miner, turn_on=False)
                                if controlled:
                                    actions_taken.append(f"{miner.name}: HA device OFF (not champion)")
                                else:
                                    actions_taken.append(f"{miner.name}: Excluded (no HA link)")
                        
                        # Turn ON champion via HA (state-checked, only if not already ON)
                        await PriceBandStrategy._enforce_ha_state(
                            db,
                            champion_miner,
                            turn_on=True,
                            sleep_after_on_seconds=3,
                        )
                    
                    # Process champion miner only
                    enrolled_miners = [champion_miner]
                    
                    # Champion uses lowest mode
                    champion_target_mode = PriceBandStrategy._get_champion_lowest_mode(champion_miner.miner_type)
                    
                    logger.info(f"Champion {champion_miner.name} will use lowest mode: {champion_target_mode}")
                    
                    # Override band mode for champion
                    effective_band_mode_targets.setdefault(target_band_obj.id, {})[
                        champion_miner.miner_type
                    ] = champion_target_mode
            
            # Apply changes to each miner
            from adapters import get_adapter
            
            for miner in enrolled_miners:
                if not await PriceBandStrategy._is_miner_currently_enrolled(db, miner.id):
                    logger.info(f"{miner.name}: skipped (miner no longer enrolled)")
                    actions_taken.append(f"{miner.name}: Skipped (no longer enrolled)")
                    continue
                # Get target mode from band based on miner type
                target_mode = PriceBandStrategy._get_target_mode_from_band(
                    miner.miner_type,
                    target_band_obj,
                    band_mode_targets=effective_band_mode_targets,
                    nmminer_mode="fixed",
                )
                if target_mode is None:
                    logger.warning(f"Unknown miner type {miner.miner_type} for {miner.name}")
                
                # Managed externally mode doubles as HA-off per band
                if target_mode == "managed_externally":
                    # Only control HA device on actual band transitions
                    if is_band_transition:
                        controlled = await PriceBandStrategy._enforce_ha_state(db, miner, turn_on=False)
                        if controlled:
                            actions_taken.append(f"{miner.name}: HA device OFF (band transition)")
                        else:
                            actions_taken.append(f"{miner.name}: External control (no HA link)")
                    else:
                        # Already in this band, HA device should already be OFF
                        logger.debug(f"{miner.name}: Already in managed_externally mode (no action needed)")
                    continue
                else:
                    # Ensure HA device is ON (if miner is enrolled and should be active)
                    # Only execute turn_on command on band transitions OR if device is currently OFF
                    if is_band_transition:
                        await PriceBandStrategy._enforce_ha_state(
                            db,
                            miner,
                            turn_on=True,
                            sleep_after_on_seconds=3,
                        )
                    else:
                        # Check if device is currently OFF and needs to be turned ON
                        if await PriceBandStrategy._is_ha_device_off(db, miner.id):
                            logger.info(f"{miner.name}: HA device is OFF, turning ON")
                            await PriceBandStrategy._enforce_ha_state(
                                db,
                                miner,
                                turn_on=True,
                                sleep_after_on_seconds=3,
                            )
                
                logger.info(f"Miner {miner.name} ({miner.miner_type}): target mode = {target_mode}")
                
                # Get adapter
                adapter = get_adapter(miner)
                if not adapter:
                    logger.error(f"No adapter for miner {miner.name}")
                    actions_taken.append(f"{miner.name}: FAILED (no adapter)")
                    continue
                
                # Check current state to determine if changes are needed
                try:
                    # Refresh miner from DB to get latest mode
                    await db.refresh(miner)
                    
                    # Get telemetry with timeout to avoid hanging on offline devices
                    telemetry = await asyncio.wait_for(adapter.get_telemetry(), timeout=5.0)
                    
                    # Reset failure count on successful telemetry retrieval
                    PriceBandStrategy._reset_miner_failure(miner.id)
                    
                    current_pool = telemetry.pool_in_use if telemetry else None
                    
                    # Check device-reported mode (actual device state) vs database
                    device_reported_mode = telemetry.extra_data.get("current_mode") if telemetry and telemetry.extra_data else None
                    db_current_mode = miner.current_mode
                    
                    # Build expected pool URL
                    target_pool_url = f"{target_pool.url}:{target_pool.port}"
                    
                    # Guard: Check if pool was recently switched (within 3 minutes)
                    # This prevents reboot loops on Avalon miners that take time to reconnect
                    if miner.last_pool_switch:
                        seconds_since_switch = (datetime.utcnow() - miner.last_pool_switch).total_seconds()
                        if seconds_since_switch < 180:  # 3 minutes cooldown
                            logger.info(
                                f"{miner.name} pool switched {int(seconds_since_switch)}s ago; "
                                f"waiting for reboot to complete ({int(180 - seconds_since_switch)}s remaining)"
                            )
                            actions_taken.append(f"{miner.name}: Waiting for pool switch to complete")
                            continue
                    
                    # Guard: if pool is missing/empty, treat as unknown and skip pool switch
                    if not current_pool:
                        logger.warning(
                            f"{miner.name} reported no pool; skipping pool switch this cycle"
                        )
                        
                        # Track consecutive failures and trigger HA validation
                        failure_count = PriceBandStrategy._increment_miner_failure(miner.id)
                        
                        # After 6 consecutive failures (6 minutes), check if HA device is stuck
                        # This prevents false positives from temporary telemetry issues
                        if failure_count >= 6:
                            await PriceBandStrategy._validate_and_power_cycle_ha_device(
                                db, miner, actions_taken
                            )
                            
                            await PriceBandStrategy._promote_champion_on_failure_if_needed(
                                db,
                                strategy,
                                champion_mode_active,
                                miner,
                                f"Pool unknown after {failure_count} failures",
                                actions_taken,
                            )
                        else:
                            actions_taken.append(f"{miner.name}: Pool unknown (skipped)")
                        
                        # Skip all further processing for this miner (no pool switch, no mode change)
                        continue
                    else:
                        # Use normalized URL comparison to prevent port-only matches
                        current_pool_normalized = PriceBandStrategy._normalize_pool_url(current_pool)
                        target_pool_normalized = PriceBandStrategy._normalize_pool_url(target_pool_url)
                        pool_already_correct = (target_pool_normalized == current_pool_normalized)
                    # Mode is correct if device reports the target mode (not just database)
                    mode_already_correct = device_reported_mode == target_mode if device_reported_mode else db_current_mode == target_mode
                    
                    if pool_already_correct and mode_already_correct:
                        logger.debug(f"{miner.name} already on {target_pool.name} with mode {target_mode}")
                        actions_taken.append(f"{miner.name}: Already correct (no change)")
                        continue
                    
                    # Log what needs to change
                    if not pool_already_correct:
                        logger.info(f"{miner.name} needs pool change: {current_pool} → {target_pool_url}")
                    if not mode_already_correct:
                        if device_reported_mode and device_reported_mode != db_current_mode:
                            logger.warning(f"{miner.name} MODE DRIFT: DB says {db_current_mode}, device reports {device_reported_mode}, target is {target_mode}")
                        else:
                            logger.info(f"{miner.name} needs mode change: {db_current_mode} → {target_mode}")
                    
                except asyncio.TimeoutError:
                    # Track consecutive telemetry timeouts
                    timeout_count = PriceBandStrategy._increment_miner_failure(miner.id)
                    
                    if timeout_count >= 5:
                        # After 5 consecutive timeouts (5 minutes), something is likely wrong
                        logger.warning(f"{miner.name} telemetry timeout {timeout_count} times - attempting reconfiguration")
                        
                        promoted = await PriceBandStrategy._promote_champion_on_failure_if_needed(
                            db,
                            strategy,
                            champion_mode_active,
                            miner,
                            f"Telemetry timeout after {timeout_count} failures",
                            actions_taken,
                        )
                        if promoted:
                            # Don't continue - let new champion be selected and activated
                            continue
                        
                        pool_already_correct = False
                        mode_already_correct = False
                    else:
                        # Skip reconfiguration until we hit threshold
                        logger.warning(f"{miner.name} telemetry timeout ({timeout_count}/5) - skipping this cycle")
                        actions_taken.append(f"{miner.name}: Telemetry timeout (skipped)")
                        continue
                except Exception as e:
                    logger.warning(f"Could not check current state for {miner.name}: {e}")
                    # Continue with switch attempt if we can't verify current state
                    pool_already_correct = False
                    mode_already_correct = False
                
                # Switch pool (only if needed)
                if not pool_already_correct:
                    try:
                        pool_switched = await adapter.switch_pool(
                            pool_url=target_pool.url,
                            pool_port=target_pool.port,
                            pool_user=target_pool.user,
                            pool_password=target_pool.password
                        )
                        
                        if not pool_switched:
                            logger.warning(f"Failed to switch {miner.name} to {target_pool.name}")
                            
                            # Track consecutive pool switch failures
                            failure_count = PriceBandStrategy._increment_miner_failure(miner.id)
                            
                            # After 3 consecutive failures, check if HA device is stuck
                            if failure_count >= 3:
                                await PriceBandStrategy._validate_and_power_cycle_ha_device(
                                    db, miner, actions_taken
                                )
                                
                                await PriceBandStrategy._promote_champion_on_failure_if_needed(
                                    db,
                                    strategy,
                                    champion_mode_active,
                                    miner,
                                    "Pool switch failed after 3 attempts",
                                    actions_taken,
                                )
                            else:
                                actions_taken.append(f"{miner.name}: Pool switch FAILED")
                            continue
                        
                        # Reset failure count on successful switch
                        PriceBandStrategy._reset_miner_failure(miner.id)
                        logger.info(f"Switched {miner.name} to pool {target_pool.name}")
                        
                        # Wait for miner to finish rebooting after pool switch
                        logger.debug(f"Waiting 8 seconds for {miner.name} to reboot after pool switch...")
                        await asyncio.sleep(8)
                    except Exception as e:
                        logger.error(f"Error switching pool for {miner.name}: {e}")
                        actions_taken.append(f"{miner.name}: Pool switch ERROR - {e}")
                        continue
                else:
                    logger.debug(f"{miner.name} pool already correct, skipping pool switch")
                
                # Set mode (only if needed)
                if target_mode and not mode_already_correct:
                    try:
                        mode_set = await adapter.set_mode(target_mode)
                        
                        if not mode_set:
                            logger.warning(f"Failed to set mode {target_mode} on {miner.name}")
                            actions_taken.append(f"{miner.name}: Mode change FAILED")
                        else:
                            logger.info(f"Set {miner.name} to mode {target_mode}")
                            actions_taken.append(f"{miner.name}: mode={target_mode}")
                            
                            # Update miner's last mode change time
                            miner.current_mode = target_mode
                            miner.last_mode_change = datetime.utcnow()
                    except Exception as e:
                        logger.error(f"Error setting mode for {miner.name}: {e}")
                        actions_taken.append(f"{miner.name}: Mode change ERROR - {e}")
                elif mode_already_correct:
                    logger.debug(f"{miner.name} mode already correct, skipping mode change")
                    # Update database to reflect device state (in case it was None or drifted)
                    if miner.current_mode != target_mode:
                        miner.current_mode = target_mode
                        logger.debug(f"Updated {miner.name} database mode to match device: {target_mode}")
                    if not pool_already_correct:
                        # Pool was changed but mode was already correct
                        actions_taken.append(f"{miner.name}: {target_pool_name} pool (mode already {target_mode})")
                else:
                    # No target mode specified
                    actions_taken.append(f"{miner.name}: {target_pool_name} pool (mode unchanged)")
            
            await log_audit(
                db,
                action="price_band_strategy_executed",
                resource_type="price_band_strategy",
                resource_name="Price Band Strategy",
                changes={
                    "price": current_price,
                    "band": f"Band {target_band_obj.sort_order}: {target_pool_name}",
                    "pool": target_pool_name,
                    "miners_affected": len(enrolled_miners),
                    "hysteresis_counter": new_counter
                }
            )
            
            # Log system event
            from core.database import Event
            event = Event(
                event_type="info",
                source="price_band_strategy",
                message=f"Price band strategy executed: {target_pool_name} @ {current_price}p/kWh ({len(enrolled_miners)} miners)"
            )
            db.add(event)
        
        await db.commit()
        
        report = {
            "enabled": True,
            "price": current_price,
            "band": f"Band {target_band_obj.sort_order}: {target_pool_name}",
            "pool": target_pool_name,
            "requested_pool": requested_target_pool_name,
            "miners": len(enrolled_miners),
            "actions": actions_taken,
            "hysteresis_counter": new_counter
        }
        
        logger.info(f"Strategy execution complete: {report}")
        
        return report
    
    @staticmethod
    async def reconcile_strategy(db: AsyncSession) -> Dict:
        """
        Reconcile strategy - ensure enrolled miners match intended state
        Runs every 5 minutes to catch drift from manual changes or failures
        
        Returns:
            Reconciliation report dict
        """
        logger.debug("Reconciling Price Band Strategy")
        
        # Get strategy config
        result = await db.execute(select(PriceBandStrategyConfig))
        strategy = result.scalar_one_or_none()
        
        if not strategy or not strategy.enabled:
            return {"reconciled": False, "message": "Strategy disabled"}
        
        # Ensure bands exist
        from core.price_band_bands import ensure_strategy_bands, get_strategy_bands, get_band_for_price
        await ensure_strategy_bands(db, strategy.id)
        
        # Get enrolled miners
        enrolled_miners = await PriceBandStrategy.get_enrolled_miners(db)
        
        if not enrolled_miners:
            return {"reconciled": False, "message": "No enrolled miners"}
        
        # Get current state
        current_band = strategy.current_price_band
        if not current_band:
            logger.debug("No current band set, skipping reconciliation")
            return {"reconciled": False, "message": "No band state"}
        
        # Get bands and find matching band
        bands = await get_strategy_bands(db, strategy.id)
        band_mode_targets = await PriceBandStrategy._load_band_mode_targets(db, bands)
        
        # Use the stored band decision from hysteresis logic
        # Reconciliation enforces what strategy execution already decided
        target_band_sort_order = strategy.current_band_sort_order
        if target_band_sort_order is None:
            logger.warning("No band decision stored, skipping reconciliation")
            return {"reconciled": False, "message": "No stored band decision"}
        
        band = next((b for b in bands if b.sort_order == target_band_sort_order), None)
        
        if not band:
            logger.error(f"Stored band #{target_band_sort_order} not found in configuration")
            return {"reconciled": False, "message": "Invalid stored band"}
        
        target_pool_id = band.target_pool_id
        
        # If OFF state (None pool ID), ensure HA devices are actually off
        if target_pool_id is None:
            ha_corrections = []
            for miner in enrolled_miners:
                # Check if HA device is enrolled and linked
                ha_device = await PriceBandStrategy._get_enrolled_ha_device(db, miner.id)
                
                if ha_device:
                    # Get current device state from HA
                    try:
                        config_result = await db.execute(select(HomeAssistantConfig))
                        ha_config = config_result.scalar_one_or_none()
                        
                        if ha_config and ha_config.enabled:
                            from integrations.homeassistant import HomeAssistantIntegration
                            ha_integration = HomeAssistantIntegration(
                                base_url=ha_config.base_url,
                                access_token=ha_config.access_token
                            )
                            
                            state = await ha_integration.get_device_state(ha_device.entity_id)
                            if state:
                                ha_device.current_state = state.state
                                ha_device.last_state_change = PriceBandStrategy._to_naive_utc(
                                    state.last_updated
                                )
                                if (
                                    state.state == "off"
                                    and ha_device.last_off_command_timestamp is None
                                ):
                                    # Seed OFF timestamp so scheduler reconciliation can
                                    # evaluate OFF-state telemetry mismatches for this miner.
                                    ha_device.last_off_command_timestamp = datetime.utcnow()
                            if state and state.state == "on":
                                # Device is ON but should be OFF
                                logger.warning(f"Reconciliation: HA device {ha_device.name} for {miner.name} is ON during OFF period - turning off")
                                success = await ha_integration.turn_off(ha_device.entity_id)
                                if success:
                                    ha_device.current_state = "off"
                                    ha_device.last_state_change = datetime.utcnow()
                                    ha_device.last_off_command_timestamp = datetime.utcnow()
                                    ha_corrections.append(f"{miner.name}: HA device turned OFF")
                                else:
                                    ha_corrections.append(f"{miner.name}: HA device turn OFF FAILED")
                    except Exception as e:
                        logger.error(f"Reconciliation: Failed to check HA device for {miner.name}: {e}")
            
            if ha_corrections:
                await log_audit(
                    db,
                    action="price_band_strategy_reconciled_ha_devices",
                    resource_type="price_band_strategy",
                    resource_name="Price Band Strategy",
                    changes={"corrections": ha_corrections, "band": "OFF"}
                )
                await db.commit()
        
            # ALWAYS return after OFF handling - don't fall through to active mining logic
            return {
                "reconciled": True,
                "band": "OFF",
                "coin": None,
                "corrections": 0,
                "ha_corrections": len(ha_corrections),
                "details": ha_corrections if ha_corrections else ["All HA devices already OFF"]
            }
        
        # Find target pool using modern direct ID lookup
        result = await db.execute(
            select(Pool).where(Pool.id == target_pool_id, Pool.enabled == True)
        )
        target_pool = result.scalar_one_or_none()
        
        if not target_pool:
            logger.error(f"Reconciliation: Pool #{target_pool_id} not found or disabled")
            return {"reconciled": False, "error": "POOL_NOT_FOUND"}
        
        pre_override_pool_name = target_pool.name
        target_pool = await PriceBandStrategy._apply_failover_pool_override(db, target_pool, [], strategy)
        if not target_pool:
            logger.error("Reconciliation: failover override removed target pool")
            return {"reconciled": False, "error": "POOL_NOT_FOUND"}

        if pre_override_pool_name != target_pool.name:
            logger.info(
                "Reconciliation target overridden by failover: %s -> %s",
                pre_override_pool_name,
                target_pool.name,
            )
        logger.info(f"Reconciliation target pool: {target_pool.name} (ID: {target_pool.id})")
        
        # Check each miner and re-apply if needed
        from adapters import get_adapter
        corrections = []
        ha_corrections = []
        
        # Build target pool URL for comparison
        target_pool_url = f"{target_pool.url}:{target_pool.port}"
        
        # Check if Champion Mode is active
        is_band_5 = band.sort_order == 5
        champion_mode_active = strategy.champion_mode_enabled and is_band_5
        champion_miner_id = strategy.current_champion_miner_id if champion_mode_active else None
        
        if champion_mode_active and champion_miner_id:
            logger.info(f"Reconciliation: Champion mode active, champion is miner #{champion_miner_id}")
        
        for miner in enrolled_miners:
            if not await PriceBandStrategy._is_miner_currently_enrolled(db, miner.id):
                logger.info(f"Reconciliation: skipping {miner.name} (no longer enrolled)")
                continue
            # Determine target mode based on miner type
            target_mode = PriceBandStrategy._get_target_mode_from_band(
                miner.miner_type,
                band,
                band_mode_targets=band_mode_targets,
            )
            
            # Champion Mode: Only process champion, turn off others
            if champion_mode_active and champion_miner_id:
                if miner.id != champion_miner_id:
                    # This is NOT the champion - ensure it's OFF
                    controlled = await PriceBandStrategy._enforce_ha_state(db, miner, turn_on=False)
                    if controlled:
                        logger.debug(f"Reconciliation: {miner.name} kept OFF (not champion)")
                    continue  # Skip processing this miner
                else:
                    # This IS the champion - ensure it's ON and use lowest mode
                    await PriceBandStrategy._enforce_ha_state(db, miner, turn_on=True)
                    
                    # Override target mode to lowest for champion
                    target_mode = PriceBandStrategy._get_champion_lowest_mode(miner.miner_type)
            else:
                # Normal mode (not Champion mode)
                if target_mode == "managed_externally":
                    controlled = await PriceBandStrategy._enforce_ha_state(db, miner, turn_on=False)
                    if controlled:
                        ha_corrections.append(f"{miner.name}: HA device enforced OFF")
                    continue
                else:
                    await PriceBandStrategy._enforce_ha_state(db, miner, turn_on=True)
            
            # Check both pool AND mode in single pass
            pool_correct = False
            mode_correct = miner.current_mode == target_mode
            
            adapter = get_adapter(miner)
            if adapter:
                try:
                    # Get current pool from telemetry
                    telemetry = await adapter.get_telemetry()
                    if telemetry and telemetry.pool_in_use:
                        # Use normalized URL comparison to prevent port-only matches
                        current_pool_normalized = PriceBandStrategy._normalize_pool_url(telemetry.pool_in_use)
                        target_pool_normalized = PriceBandStrategy._normalize_pool_url(target_pool_url)
                        pool_correct = (target_pool_normalized == current_pool_normalized)
                except Exception as e:
                    logger.warning(f"Reconciliation: Could not check pool for {miner.name}: {e}")
            
            # If either pool or mode is wrong, correct both
            if not pool_correct or not mode_correct:
                issues = []
                if not pool_correct:
                    issues.append("pool")
                if not mode_correct:
                    issues.append(f"mode ({miner.current_mode} → {target_mode})")
                
                logger.info(f"Reconciliation: {miner.name} drift detected: {', '.join(issues)}")
                
                if adapter:
                    try:
                        # Switch pool if needed
                        if not pool_correct:
                            await adapter.switch_pool(
                                pool_url=target_pool.url,
                                pool_port=target_pool.port,
                                pool_user=target_pool.user,
                                pool_password=target_pool.password
                            )
                            logger.info(f"Reconciliation: Corrected {miner.name} to pool {target_pool.name}")
                            # Wait for miner to reboot after pool switch
                            await asyncio.sleep(8)
                        
                        # Apply correct mode
                        if target_mode:
                            await adapter.set_mode(target_mode)
                            miner.current_mode = target_mode
                            miner.last_mode_change = datetime.utcnow()
                        
                        corrections.append(f"{miner.name}: corrected {', '.join(issues)}")
                        logger.info(f"Reconciliation: {miner.name} fully corrected")
                    except Exception as e:
                        logger.error(f"Reconciliation failed for {miner.name}: {e}")
                        corrections.append(f"{miner.name}: correction FAILED - {e}")
        
        await db.commit()
        
        if corrections or ha_corrections:
            await log_audit(
                db,
                action="price_band_strategy_reconciled",
                resource_type="price_band_strategy",
                resource_name="Price Band Strategy",
                changes={
                    "corrections": corrections,
                    "ha_corrections": ha_corrections,
                    "band": current_band,
                    "pool": target_pool.name
                }
            )
            await db.commit()
        
        return {
            "reconciled": True,
            "band": current_band,
            "pool": target_pool.name,
            "corrections": len(corrections),
            "ha_corrections": len(ha_corrections),
            "details": corrections + ha_corrections
        }

