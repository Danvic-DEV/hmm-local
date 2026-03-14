"""
APScheduler for periodic tasks
"""
import logging
import asyncio
import statistics
import inspect
import aiohttp
import os
import random
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func, and_, delete, text
from typing import Optional, Any, Coroutine, cast
from core.config import app_config
from core.cloud_push import init_cloud_service, get_cloud_service
from core.database import EnergyPrice, Telemetry, Miner, AuditLog

logger = logging.getLogger(__name__)


def _as_dict(value, default=None):
    """Safely coerce config values to a dict."""
    if isinstance(value, dict):
        return value
    return {} if default is None else default


def _as_int(value, default: int) -> int:
    """Safely coerce config values to int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value, default: str) -> str:
    """Safely coerce config values to str."""
    if isinstance(value, str) and value.strip():
        return value
    return default


def _as_float(value, default: float) -> float:
    """Safely coerce config values to float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class SchedulerService:
    """Scheduler service wrapper"""
    
    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.nmminer_listener = None
        self.nmminer_adapters = {}  # Shared adapter registry for NMMiner devices
        self.runtime_protection_status: dict[str, Any] = {
            "degraded_mode": False,
            "reason": None,
            "entered_at": None,
            "last_recovered_at": None,
            "last_checked_at": None,
            "stale_miners": 0,
            "total_miners": 0,
            "raw_total_miners": 0,
            "exempt_miners": 0,
            "stale_ratio": 0.0,
            "stale_cutoff_seconds": 180,
        }
        self.energy_provider_status: dict[str, Any] = {
            "provider_id": None,
            "configured_provider_id": None,
            "region": None,
            "last_run_at": None,
            "last_success_at": None,
            "last_error": None,
            "last_result": None,
        }
        
        # Initialize cloud service
        cloud_config = _as_dict(app_config.get("cloud", {}))
        init_cloud_service(cloud_config)

    def _register_core_jobs(self):
        """Register core recurring scheduler jobs."""
        self.scheduler.add_job(
            self._update_energy_prices,
            IntervalTrigger(minutes=30),
            id="update_energy_prices",
            name="Update Octopus Agile prices"
        )

        self.scheduler.add_job(
            self._collect_telemetry,
            IntervalTrigger(seconds=30),
            id="collect_telemetry",
            name="Collect miner telemetry"
        )

        self.scheduler.add_job(
            self._telemetry_freshness_watchdog,
            IntervalTrigger(minutes=1),
            id="telemetry_freshness_watchdog",
            name="Telemetry freshness watchdog"
        )

        self.scheduler.add_job(
            self._evaluate_automation_rules,
            IntervalTrigger(seconds=60),
            id="evaluate_automation_rules",
            name="Evaluate automation rules"
        )

        self.scheduler.add_job(
            self._reconcile_automation_rules,
            IntervalTrigger(minutes=5),
            id="reconcile_automation_rules",
            name="Reconcile miners with active automation rules"
        )

        self.scheduler.add_job(
            self._check_alerts,
            IntervalTrigger(minutes=5),
            id="check_alerts",
            name="Check for alert conditions"
        )

        self.scheduler.add_job(
            self._record_health_scores,
            IntervalTrigger(hours=1),
            id="record_health_scores",
            name="Record miner health scores"
        )

        self.scheduler.add_job(
            self._update_crypto_prices,
            IntervalTrigger(minutes=10),
            id="update_crypto_prices",
            name="Update crypto price cache"
        )

        self.scheduler.add_job(
            self._log_system_summary,
            IntervalTrigger(hours=6),
            id="log_system_summary",
            name="Log system status summary"
        )

    def _register_maintenance_jobs(self):
        """Register maintenance and database lifecycle jobs."""
        # Heavy maintenance tasks now run during strategy OFF periods instead of fixed schedules
        # Includes: aggregation, purge operations, VACUUM, ANALYZE
        # Triggered by _execute_price_band_strategy() when entering OFF state
        # Fallback: If strategy disabled or hasn't run in 7 days, runs daily at 3am
        self.scheduler.add_job(
            self._fallback_maintenance,
            CronTrigger(hour=3, minute=0),
            id="fallback_maintenance",
            name="Fallback maintenance (3am daily if needed)"
        )

        self.scheduler.add_job(
            self._purge_old_energy_prices,
            IntervalTrigger(days=7),
            id="purge_old_energy_prices",
            name="Purge energy prices older than 60 days"
        )

        self.scheduler.add_job(
            self._vacuum_database,
            IntervalTrigger(days=30),
            id="vacuum_database",
            name="Optimize database (VACUUM)"
        )

        self.scheduler.add_job(
            self._backup_database,
            CronTrigger(hour=2, minute=0),
            id="backup_database",
            name="Backup database daily at 2am"
        )

        self.scheduler.add_job(
            self._monitor_database_health,
            IntervalTrigger(minutes=5),
            id="monitor_database_health",
            name="Monitor database connection pool and performance"
        )

        self.scheduler.add_job(
            self._refresh_dashboard_materialized_view,
            IntervalTrigger(minutes=5),
            id="refresh_dashboard_mv",
            name="Refresh dashboard materialized view (PostgreSQL)"
        )

        self.scheduler.add_job(
            self._ensure_future_partitions,
            CronTrigger(day=1, hour=1, minute=0),  # 1st of each month at 1am
            id="ensure_partitions",
            name="Ensure future telemetry partitions exist (PostgreSQL)"
        )

        self.scheduler.add_job(
            self._check_index_health,
            IntervalTrigger(days=7),
            id="check_index_health",
            name="Check PostgreSQL index health and bloat"
        )

        self.scheduler.add_job(
            self._aggregate_daily_stats,
            IntervalTrigger(hours=24),
            id="aggregate_daily_stats",
            name="Aggregate daily statistics at midnight",
            next_run_time=self._get_next_midnight(),
            misfire_grace_time=600  # Allow up to 10 minutes late execution
        )

    def _register_pool_and_ha_jobs(self):
        """Register pool, HA, discovery, and update jobs."""
        self.scheduler.add_job(
            self._auto_discover_miners,
            IntervalTrigger(hours=24),
            id="auto_discover_miners",
            name="Auto-discover miners on configured networks"
        )

        self.scheduler.add_job(
            self._monitor_pool_health,
            IntervalTrigger(minutes=5),
            id="monitor_pool_health",
            name="Monitor pool health and connectivity"
        )

        self.scheduler.add_job(
            self._sync_avalon_pool_slots,
            IntervalTrigger(minutes=15),
            id="sync_avalon_pool_slots",
            name="Sync Avalon Nano pool slot configurations"
        )

        self.scheduler.add_job(
            self._purge_old_pool_health,
            IntervalTrigger(days=7),
            id="purge_old_pool_health",
            name="Purge pool health data older than 30 days"
        )

        self.scheduler.add_job(
            self._purge_old_high_diff_shares,
            IntervalTrigger(days=1),
            id="purge_old_high_diff_shares",
            name="Purge high diff shares older than 180 days"
        )

        self.scheduler.add_job(
            self._monitor_ha_keepalive,
            IntervalTrigger(minutes=1),
            id="monitor_ha_keepalive",
            name="Monitor Home Assistant connectivity"
        )

        self.scheduler.add_job(
            self._poll_ha_device_states,
            IntervalTrigger(minutes=5),
            id="poll_ha_device_states",
            name="Poll Home Assistant device states"
        )

        self.scheduler.add_job(
            self._reconcile_ha_device_states,
            IntervalTrigger(minutes=5),
            id="reconcile_ha_device_states",
            name="Reconcile stuck Home Assistant devices"
        )

        self.scheduler.add_job(
            self._check_update_notifications,
            IntervalTrigger(hours=6),
            id="check_update_notifications",
            name="Check for driver updates"
        )

        self.scheduler.add_job(
            self._start_nmminer_listener,
            id="start_nmminer_listener",
            name="Start NMMiner UDP listener"
        )

    def _register_metrics_jobs(self):
        """Register metrics computation and cleanup jobs."""
        self.scheduler.add_job(
            self._compute_hourly_metrics,
            'cron',
            minute=5,
            id="compute_hourly_metrics",
            name="Compute hourly metrics"
        )

        self.scheduler.add_job(
            self._compute_daily_metrics,
            'cron',
            hour=0,
            minute=30,
            id="compute_daily_metrics",
            name="Compute daily metrics"
        )

        self.scheduler.add_job(
            self._cleanup_old_metrics,
            'cron',
            day=1,
            hour=2,
            minute=0,
            id="cleanup_old_metrics",
            name="Cleanup old metrics (>1 year)"
        )

    def _register_anomaly_jobs(self):
        """Register anomaly detection and model training jobs."""
        self.scheduler.add_job(
            self._update_miner_baselines,
            IntervalTrigger(hours=1),
            id="update_miner_baselines",
            name="Update miner performance baselines"
        )

        self.scheduler.add_job(
            self._check_miner_health,
            IntervalTrigger(minutes=5),
            id="check_miner_health",
            name="Check miner health and detect anomalies"
        )

        self.scheduler.add_job(
            self._train_ml_models,
            IntervalTrigger(days=7),
            id="train_ml_models",
            name="Train ML anomaly detection models (weekly)"
        )

    def _register_cloud_jobs(self):
        """Register cloud push jobs when cloud integration is enabled."""
        cloud_config = _as_dict(app_config.get("cloud", {}))
        if cloud_config.get("enabled", False):
            push_interval = max(1, _as_int(cloud_config.get("push_interval_minutes", 5), 5))
            self.scheduler.add_job(
                self._push_to_cloud,
                IntervalTrigger(minutes=push_interval),
                id="push_to_cloud",
                name="Push telemetry to HMM Cloud"
            )
            self.scheduler.add_job(
                self._push_audit_logs_to_cloud,
                IntervalTrigger(minutes=max(push_interval, 10)),
                id="push_audit_logs_to_cloud",
                name="Push audit logs to HMM Cloud"
            )

    def _register_strategy_jobs(self):
        """Register Price Band Strategy recurring jobs."""
        self.scheduler.add_job(
            self._execute_price_band_strategy,
            IntervalTrigger(minutes=1),
            id="execute_price_band_strategy",
            name="Execute Price Band Strategy every minute"
        )

        self.scheduler.add_job(
            self._reconcile_price_band_strategy,
            IntervalTrigger(minutes=5),
            id="reconcile_price_band_strategy",
            name="Reconcile Price Band Strategy every 5 minutes"
        )

    def _register_startup_jobs(self):
        """Register one-shot startup jobs executed as scheduler starts."""
        self.scheduler.add_job(
            self._update_energy_prices,
            id="update_energy_prices_immediate",
            name="Immediate energy price fetch"
        )

        self.scheduler.add_job(
            self._update_crypto_prices,
            id="update_crypto_prices_immediate",
            name="Immediate crypto price fetch"
        )

        self.scheduler.add_job(
            self._sync_avalon_pool_slots,
            id="sync_avalon_pool_slots_immediate",
            name="Immediate Avalon pool slots sync"
        )

        self.scheduler.add_job(
            self._execute_price_band_strategy,
            id="execute_price_band_strategy_immediate",
            name="Immediate Price Band Strategy execution"
        )

        self.scheduler.add_job(
            self._reconcile_price_band_strategy,
            id="reconcile_price_band_strategy_immediate",
            name="Immediate Price Band Strategy reconciliation"
        )

        self.scheduler.add_job(
            self._backfill_missing_daily_stats,
            id="backfill_missing_daily_stats_immediate",
            name="Backfill missing daily aggregations on startup"
        )

    def _validate_registered_jobs(self):
        """Validate that critical recurring scheduler jobs are present."""
        registered_ids = {job.id for job in self.scheduler.get_jobs()}
        required_ids = {
            "update_energy_prices",
            "collect_telemetry",
            "telemetry_freshness_watchdog",
            "evaluate_automation_rules",
            "reconcile_automation_rules",
            "execute_price_band_strategy",
            "reconcile_price_band_strategy",
            "monitor_database_health",
            "monitor_pool_health",
            "monitor_ha_keepalive",
        }

        missing_ids = sorted(required_ids - registered_ids)
        if missing_ids:
            logger.error("Scheduler missing critical jobs: %s", ", ".join(missing_ids))
            raise RuntimeError(f"Scheduler missing critical jobs: {', '.join(missing_ids)}")
    
    def start(self):
        """Start scheduler"""
        if self.scheduler.running:
            logger.info("Scheduler already running")
            return

        existing_jobs = self.scheduler.get_jobs()
        if existing_jobs:
            logger.warning(
                "Clearing %s existing scheduler jobs before registration",
                len(existing_jobs),
            )
            self.scheduler.remove_all_jobs()

        self._register_core_jobs()
        self._register_maintenance_jobs()
        self._register_pool_and_ha_jobs()
        self._register_cloud_jobs()
        self._register_metrics_jobs()
        self._register_anomaly_jobs()
        self._register_strategy_jobs()
        self._register_startup_jobs()

        # Update auto-discovery job interval based on config before start
        self._update_discovery_schedule()
        self._validate_registered_jobs()

        self.scheduler.start()
        logger.info("Scheduler started with %s jobs", len(self.scheduler.get_jobs()))
    
    def _update_discovery_schedule(self):
        """Update auto-discovery job interval based on config"""
        try:
            discovery_config = _as_dict(app_config.get("network_discovery", {}))
            scan_interval_hours = max(1, _as_int(discovery_config.get("scan_interval_hours", 24), 24))
            
            # Remove existing job
            try:
                self.scheduler.remove_job("auto_discover_miners")
            except Exception as e:
                logger.debug("No existing auto_discover_miners job to remove: %s", e)
            
            # Re-add with new interval
            self.scheduler.add_job(
                self._auto_discover_miners,
                IntervalTrigger(hours=scan_interval_hours),
                id="auto_discover_miners",
                name=f"Auto-discover miners every {scan_interval_hours}h"
            )
            logger.info("Updated auto-discovery interval to %s hours", scan_interval_hours)
        except Exception as e:
            logger.error("Failed to update discovery schedule: %s", e)
    
    def shutdown(self):
        """Shutdown scheduler"""
        # Listener can still be running independently of APScheduler state.
        self._stop_nmminer_listener()

        if not self.scheduler.running:
            logger.info("Scheduler already stopped")
            return

        try:
            self.scheduler.shutdown()
            logger.info("Scheduler stopped")
        except Exception as e:
            logger.exception("Scheduler shutdown failed: %s", e)

    def _stop_nmminer_listener(self):
        """Stop NMMiner UDP listener if running."""
        if not self.nmminer_listener:
            return

        listener = self.nmminer_listener
        try:
            stop_result = listener.stop()
            if inspect.isawaitable(stop_result):
                stop_coro = cast(Coroutine[Any, Any, Any], stop_result)
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(stop_coro)
                except RuntimeError:
                    asyncio.run(stop_coro)
            logger.info("NMMiner UDP listener stop requested")
        except Exception as e:
            logger.exception("Failed to stop NMMiner UDP listener cleanly: %s", e)
        finally:
            self.nmminer_listener = None

    def get_energy_provider_status(self):
        """Get last energy provider sync status."""
        return dict(self.energy_provider_status)

    def get_runtime_protection_status(self) -> dict[str, Any]:
        """Get current runtime protection/degraded-mode status."""
        return dict(self.runtime_protection_status)

    def _is_degraded_mode_active(self) -> bool:
        """Return True when runtime protection has enabled degraded mode."""
        return bool(self.runtime_protection_status.get("degraded_mode", False))

    def _should_skip_non_critical_job(self, job_name: str) -> bool:
        """Load-shedding guard for non-critical scheduler jobs."""
        if not self._is_degraded_mode_active():
            return False

        reason = self.runtime_protection_status.get("reason") or "telemetry_stale"
        logger.warning(
            "Load shedding active - skipping non-critical job '%s' (reason=%s)",
            job_name,
            reason,
        )
        return True

    async def _telemetry_freshness_watchdog(self):
        """
        Detect stale telemetry and enable degraded mode when freshness drops.
        Degraded mode protects core telemetry by deferring non-critical jobs.
        """
        from core.database import (
            AsyncSessionLocal,
            HomeAssistantDevice,
            Miner,
            MinerStrategy,
            PriceBandStrategyBand,
            PriceBandStrategyConfig,
            Telemetry,
        )

        stale_cutoff_seconds = max(60, _as_int(app_config.get("telemetry.watchdog_stale_seconds", 180), 180))
        stale_ratio_threshold = min(
            1.0,
            max(0.0, _as_float(app_config.get("telemetry.watchdog_stale_ratio_threshold", 0.5), 0.5)),
        )
        stale_miners_threshold = max(
            1,
            _as_int(app_config.get("telemetry.watchdog_stale_miners_threshold", 2), 2),
        )

        now = datetime.utcnow()
        cutoff = now - timedelta(seconds=stale_cutoff_seconds)

        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Miner.id, func.max(Telemetry.timestamp))
                    .outerjoin(Telemetry, Telemetry.miner_id == Miner.id)
                    .where(Miner.enabled == True)
                    .where(Miner.miner_type != "nmminer")
                    .group_by(Miner.id)
                )
                rows = result.all()

                # Exempt miners intentionally OFF due to strategy or HA state.
                # These miners should not count as stale telemetry failures.
                intentionally_off_ids: set[int] = set()

                # 1) Home Assistant devices currently OFF (enrolled for automation).
                try:
                    ha_result = await db.execute(
                        select(HomeAssistantDevice.miner_id)
                        .where(HomeAssistantDevice.enrolled == True)
                        .where(HomeAssistantDevice.current_state == "off")
                        .where(HomeAssistantDevice.miner_id.isnot(None))
                    )
                    intentionally_off_ids.update(
                        int(miner_id)
                        for miner_id in ha_result.scalars().all()
                        if miner_id is not None
                    )
                except Exception as ha_exc:
                    logger.warning("Telemetry watchdog: HA OFF exemption lookup failed: %s", ha_exc)

                # 2) Strategy OFF/champion cases.
                try:
                    strategy_result = await db.execute(select(PriceBandStrategyConfig).limit(1))
                    strategy = strategy_result.scalar_one_or_none()

                    if strategy and strategy.enabled and strategy.current_band_sort_order is not None:
                        band_result = await db.execute(
                            select(PriceBandStrategyBand)
                            .where(PriceBandStrategyBand.strategy_id == strategy.id)
                            .where(PriceBandStrategyBand.sort_order == strategy.current_band_sort_order)
                            .limit(1)
                        )
                        active_band = band_result.scalar_one_or_none()

                        if active_band:
                            enrolled_result = await db.execute(
                                select(MinerStrategy.miner_id)
                                .join(Miner, Miner.id == MinerStrategy.miner_id)
                                .where(MinerStrategy.strategy_enabled == True)
                                .where(Miner.enabled == True)
                            )
                            enrolled_ids = {
                                int(miner_id)
                                for miner_id in enrolled_result.scalars().all()
                                if miner_id is not None
                            }

                            # OFF band: all strategy-enrolled miners intentionally OFF.
                            if active_band.target_pool_id is None:
                                intentionally_off_ids.update(enrolled_ids)

                            # Champion mode on Band 5: non-champions intentionally OFF.
                            if (
                                active_band.sort_order == 5
                                and strategy.champion_mode_enabled
                                and strategy.current_champion_miner_id
                            ):
                                intentionally_off_ids.update(
                                    miner_id
                                    for miner_id in enrolled_ids
                                    if miner_id != strategy.current_champion_miner_id
                                )
                except Exception as strategy_exc:
                    logger.warning(
                        "Telemetry watchdog: strategy OFF exemption lookup failed: %s",
                        strategy_exc,
                    )

            raw_total_miners = len(rows)
            monitored_rows = [
                (miner_id, last_ts)
                for miner_id, last_ts in rows
                if int(miner_id) not in intentionally_off_ids
            ]
            total_miners = len(monitored_rows)
            exempt_miners = raw_total_miners - total_miners
            stale_miners = sum(1 for _, last_ts in monitored_rows if last_ts is None or last_ts < cutoff)
            stale_ratio = (stale_miners / total_miners) if total_miners > 0 else 0.0

            should_degrade = (
                total_miners > 0
                and stale_miners >= stale_miners_threshold
                and stale_ratio >= stale_ratio_threshold
            )

            previously_degraded = self._is_degraded_mode_active()
            self.runtime_protection_status.update(
                {
                    "degraded_mode": should_degrade,
                    "reason": "telemetry_stale" if should_degrade else None,
                    "last_checked_at": now.isoformat(),
                    "stale_miners": stale_miners,
                    "total_miners": total_miners,
                    "raw_total_miners": raw_total_miners,
                    "exempt_miners": exempt_miners,
                    "stale_ratio": round(stale_ratio, 3),
                    "stale_cutoff_seconds": stale_cutoff_seconds,
                }
            )

            if should_degrade and not previously_degraded:
                self.runtime_protection_status["entered_at"] = now.isoformat()
                logger.warning(
                    "Telemetry watchdog entering degraded mode: stale=%s/%s (%.1f%%, cutoff=%ss)",
                    stale_miners,
                    total_miners,
                    stale_ratio * 100,
                    stale_cutoff_seconds,
                )
                try:
                    from core.notifications import send_alert

                    await send_alert(
                        "⚠️ Runtime protection enabled (degraded mode)\n\n"
                        f"Stale telemetry miners: {stale_miners}/{total_miners} "
                        f"({stale_ratio * 100:.1f}%)\n"
                        f"Freshness cutoff: {stale_cutoff_seconds}s\n"
                        "Non-critical background jobs are temporarily deferred.",
                        alert_type="system",
                    )
                except Exception as alert_error:
                    logger.error("Failed to send degraded-mode alert: %s", alert_error)

            elif not should_degrade and previously_degraded:
                self.runtime_protection_status["last_recovered_at"] = now.isoformat()
                logger.info(
                    "Telemetry watchdog recovered; leaving degraded mode (stale=%s/%s)",
                    stale_miners,
                    total_miners,
                )
                try:
                    from core.notifications import send_alert

                    await send_alert(
                        "✅ Runtime protection recovered\n\n"
                        "Telemetry freshness has returned to normal.\n"
                        "Deferred non-critical jobs are resumed.",
                        alert_type="system",
                    )
                except Exception as alert_error:
                    logger.error("Failed to send degraded-mode recovery alert: %s", alert_error)

        except Exception as e:
            logger.error("Telemetry freshness watchdog failed: %s", e)
    
    async def _update_energy_prices(self):
        """Update energy prices via configured energy provider plugin."""
        from core.config import app_config
        from core.database import AsyncSessionLocal, EnergyPrice, Event, engine
        from providers.energy.loader import get_energy_provider_loader

        enabled = app_config.get("octopus_agile.enabled", False)
        logger.info("Octopus Agile enabled: %s", enabled)

        if not enabled:
            logger.warning("Octopus Agile is disabled in config")
            return

        configured_provider_id = _as_str(
            app_config.get("energy.provider_id", "octopus_agile"),
            "octopus_agile",
        )
        provider_config = _as_dict(app_config.get(f"energy.providers.{configured_provider_id}", {}))
        region = _as_str(provider_config.get("region") or app_config.get("octopus_agile.region", "H"), "H")
        is_postgresql = 'postgresql' in str(engine.url)
        self.energy_provider_status.update({
            "configured_provider_id": configured_provider_id,
            "region": region,
            "last_run_at": datetime.utcnow().isoformat(),
        })

        try:
            loader = get_energy_provider_loader()
        except Exception as exc:
            logger.error(f"Energy provider loader not initialized: {exc}")
            self.energy_provider_status.update({
                "provider_id": None,
                "last_error": f"loader_not_initialized: {exc}",
            })
            async with AsyncSessionLocal() as db:
                event = Event(
                    event_type="error",
                    source="energy_provider",
                    message=f"Energy provider loader not initialized: {exc}"[:500]
                )
                db.add(event)
                await db.commit()
            return

        provider = loader.get_provider(configured_provider_id)
        if not provider:
            provider = loader.get_default_provider()

        if not provider:
            logger.error("No energy providers loaded")
            self.energy_provider_status.update({
                "provider_id": None,
                "last_error": "no_providers_loaded",
            })
            async with AsyncSessionLocal() as db:
                event = Event(
                    event_type="error",
                    source="energy_provider",
                    message="No energy providers loaded"[:500]
                )
                db.add(event)
                await db.commit()
            return

        provider_id = provider.provider_id
        self.energy_provider_status["provider_id"] = provider_id
        if provider_id != configured_provider_id:
            logger.warning(
                f"Configured energy provider '{configured_provider_id}' not found; "
                f"falling back to '{provider_id}'"
            )

        validation_errors = provider.validate_config({**provider_config, "region": region})
        if validation_errors:
            logger.error(f"Energy provider config invalid for {provider_id}: {validation_errors}")
            self.energy_provider_status.update({
                "last_error": f"validation_failed: {validation_errors}",
            })
            async with AsyncSessionLocal() as db:
                event = Event(
                    event_type="error",
                    source=f"energy_provider:{provider_id}",
                    message=f"Provider config invalid: {validation_errors}"[:500]
                )
                db.add(event)
                await db.commit()
            return

        logger.info(
            "Fetching prices via provider '%s' for region '%s'",
            provider_id,
            region,
        )

        try:
            now_utc = datetime.utcnow()
            slots = await provider.fetch_prices(
                region=region,
                start_utc=now_utc,
                end_utc=now_utc + timedelta(hours=48),
                config=provider_config,
            )

            if not slots:
                logger.warning(f"No price data returned from energy provider '{provider_id}'")
                self.energy_provider_status.update({
                    "last_error": "no_price_data_returned",
                    "last_result": {
                        "provider_id": provider_id,
                        "region": region,
                        "fetched": 0,
                        "inserted": 0,
                        "updated": 0,
                    }
                })
                async with AsyncSessionLocal() as db:
                    event = Event(
                        event_type="warning",
                        source=f"energy_provider:{provider_id}",
                        message=f"No price data returned for region {region}"[:500]
                    )
                    db.add(event)
                    await db.commit()
                return

            inserted_count = 0
            updated_count = 0

            async with AsyncSessionLocal() as db:
                for slot in slots:
                    valid_from = slot.valid_from
                    valid_to = slot.valid_to

                    if is_postgresql:
                        if valid_from.tzinfo is not None:
                            valid_from = valid_from.astimezone(timezone.utc).replace(tzinfo=None)
                        if valid_to.tzinfo is not None:
                            valid_to = valid_to.astimezone(timezone.utc).replace(tzinfo=None)

                    result = await db.execute(
                        select(EnergyPrice)
                        .where(EnergyPrice.region == region)
                        .where(EnergyPrice.valid_from == valid_from)
                    )
                    existing = result.scalar_one_or_none()

                    if existing:
                        changed = False
                        new_price = float(slot.price_pence)

                        if existing.valid_to != valid_to:
                            existing.valid_to = valid_to
                            changed = True

                        if float(existing.price_pence) != new_price:
                            existing.price_pence = new_price
                            changed = True

                        if changed:
                            updated_count += 1
                    else:
                        db.add(
                            EnergyPrice(
                                region=region,
                                valid_from=valid_from,
                                valid_to=valid_to,
                                price_pence=float(slot.price_pence),
                            )
                        )
                        inserted_count += 1

                await db.commit()

            total_slots = len(slots)
            self.energy_provider_status.update({
                "last_success_at": datetime.utcnow().isoformat(),
                "last_error": None,
                "last_result": {
                    "provider_id": provider_id,
                    "region": region,
                    "fetched": total_slots,
                    "inserted": inserted_count,
                    "updated": updated_count,
                }
            })
            logger.info(
                "Updated energy prices via '%s' for region '%s': %s inserted, %s updated, %s fetched",
                provider_id,
                region,
                inserted_count,
                updated_count,
                total_slots,
            )

            async with AsyncSessionLocal() as db:
                event = Event(
                    event_type="info",
                    source=f"energy_provider:{provider_id}",
                    message=(
                        f"Updated energy prices for region {region}: "
                        f"{inserted_count} inserted, {updated_count} updated, {total_slots} fetched"
                    )[:500]
                )
                db.add(event)
                await db.commit()

        except Exception as exc:
            logger.exception(f"Failed to update energy prices via provider '{provider_id}'")
            self.energy_provider_status.update({
                "last_error": str(exc),
            })
            async with AsyncSessionLocal() as db:
                msg = f"Exception fetching energy prices via {provider_id}: {exc}"
                event = Event(
                    event_type="error",
                    source=f"energy_provider:{provider_id}",
                    message=msg[:500]
                )
                db.add(event)
                await db.commit()

    async def _update_crypto_prices(self):
        """Update cached crypto prices every 10 minutes"""
        from api.settings import update_crypto_prices_cache
        await update_crypto_prices_cache()
    
    async def _collect_miner_telemetry(self, miner, agile_in_off_state, db):
        """Collect telemetry from a single miner (used for parallel collection)"""
        from core.database import Telemetry, Event, Pool, MinerStrategy, EnergyPrice, HomeAssistantDevice
        from adapters import create_adapter
        from sqlalchemy import select
        
        try:
            logger.debug("Collecting telemetry from %s (%s)", miner.name, miner.miner_type)
            
            # Create adapter
            adapter = create_adapter(
                miner.miner_type,
                miner.id,
                miner.name,
                miner.ip_address,
                miner.port,
                miner.config
            )
            
            if not adapter:
                return

            # Check if HA explicitly turned this miner OFF
            ha_off_state = False
            with db.no_autoflush:
                ha_result = await db.execute(
                    select(HomeAssistantDevice)
                    .where(HomeAssistantDevice.miner_id == miner.id)
                    .where(HomeAssistantDevice.enrolled == True)
                )
                ha_device = ha_result.scalar_one_or_none()

            if ha_device:
                if (ha_device.current_state or "").lower() == "off":
                    ha_off_state = True
                elif ha_device.last_off_command_timestamp:
                    hours_since_off = (datetime.utcnow() - ha_device.last_off_command_timestamp).total_seconds() / 3600
                    if hours_since_off <= 6:
                        ha_off_state = True
            
            # Optimization: If Agile is OFF, ping first before attempting full telemetry
            # This avoids long timeout waits for miners that are powered off
            if agile_in_off_state or ha_off_state:
                try:
                    is_online = await asyncio.wait_for(adapter.is_online(), timeout=2.0)
                    if not is_online:
                        reason = "Agile OFF" if agile_in_off_state else "HA OFF"
                        logger.debug("%s offline (%s ping failed) - skipping telemetry", miner.name, reason)
                        return
                except asyncio.TimeoutError:
                    reason = "Agile OFF" if agile_in_off_state else "HA OFF"
                    logger.debug("%s ping timeout (%s) - skipping telemetry", miner.name, reason)
                    return
            
            # Get telemetry
            telemetry = await adapter.get_telemetry()
            
            if telemetry:
                # Track high difficulty shares (ASIC miners only)
                if miner.miner_type in ["avalon_nano", "bitaxe", "nerdqaxe"] and telemetry.extra_data:
                    from core.high_diff_tracker import track_high_diff_share
                    
                    # Extract best diff based on miner type
                    current_best_diff = None
                    if miner.miner_type in ["bitaxe", "nerdqaxe"]:
                        current_best_diff = telemetry.extra_data.get("best_session_diff")
                    elif miner.miner_type == "avalon_nano":
                        current_best_diff = telemetry.extra_data.get("best_share_diff") or telemetry.extra_data.get("best_share")
                    
                    if current_best_diff:
                        # Get previous best from last telemetry reading
                        prev_result = await db.execute(
                            select(Telemetry)
                            .where(Telemetry.miner_id == miner.id)
                            .order_by(Telemetry.timestamp.desc())
                            .limit(1)
                        )
                        prev_telemetry = prev_result.scalar_one_or_none()
                        
                        previous_best = None
                        if prev_telemetry and prev_telemetry.data:
                            if miner.miner_type in ["bitaxe", "nerdqaxe"]:
                                previous_best = prev_telemetry.data.get("best_session_diff")
                            elif miner.miner_type == "avalon_nano":
                                previous_best = prev_telemetry.data.get("best_share_diff") or prev_telemetry.data.get("best_share")
                        
                        # Only track if this is a new personal best (ensure numeric comparison)
                        try:
                            # Parse values that may have unit suffixes (e.g., "130.46 k" = 130460)
                            def parse_difficulty(value):
                                if value is None:
                                    return None
                                if isinstance(value, (int, float)):
                                    return float(value)
                                
                                # Handle string values with unit suffixes
                                value_str = str(value).strip().lower()
                                multipliers = {
                                    'k': 1_000,
                                    'm': 1_000_000,
                                    'g': 1_000_000_000,
                                    't': 1_000_000_000_000
                                }
                                
                                for suffix, multiplier in multipliers.items():
                                    if suffix in value_str:
                                        # Extract numeric part and multiply
                                        num_str = value_str.replace(suffix, '').strip()
                                        return float(num_str) * multiplier
                                
                                # No suffix, just convert to float
                                return float(value_str)
                            
                            current_val = parse_difficulty(current_best_diff)
                            previous_val = parse_difficulty(previous_best)
                            
                            if current_val is not None and (previous_val is None or current_val > previous_val):
                                # Get network difficulty if available
                                network_diff = telemetry.extra_data.get("network_difficulty")
                                
                                # Get pool name from active pool (parse like dashboard.py does)
                                pool_name = "Unknown Pool"
                                if telemetry.pool_in_use:
                                    pool_str = telemetry.pool_in_use
                                    # Remove protocol
                                    if '://' in pool_str:
                                        pool_str = pool_str.split('://')[1]
                                    # Extract host and port
                                    if ':' in pool_str:
                                        parts = pool_str.split(':')
                                        host = parts[0]
                                        try:
                                            port = int(parts[1])
                                            # Look up pool by host and port
                                            pool_result = await db.execute(
                                                select(Pool).where(
                                                    Pool.url == host,
                                                    Pool.port == port
                                                )
                                            )
                                            pool = pool_result.scalar_one_or_none()
                                            if pool:
                                                pool_name = pool.name
                                        except (ValueError, IndexError):
                                            pass
                                
                                await track_high_diff_share(
                                    db=db,
                                    miner_id=miner.id,
                                    miner_name=miner.name,
                                    miner_type=miner.miner_type,
                                    pool_name=pool_name,
                                    difficulty=current_best_diff,
                                    network_difficulty=network_diff,
                                    hashrate=telemetry.hashrate,
                                    hashrate_unit=telemetry.extra_data.get("hashrate_unit", "GH/s"),
                                    miner_mode=miner.current_mode,
                                    previous_best=previous_best
                                )
                        except (ValueError, TypeError) as e:
                            logger.warning(f"Invalid difficulty value for {miner.name}: current={current_best_diff}, previous={previous_best}")
                
                # Update miner's current_mode if detected in telemetry
                # BUT: Skip if miner is enrolled in Price Band Strategy (strategy owns mode)
                if telemetry.extra_data and "current_mode" in telemetry.extra_data:
                    detected_mode = telemetry.extra_data["current_mode"]
                    if detected_mode and miner.current_mode != detected_mode:
                        # Check if miner is enrolled in strategy
                        strategy_result = await db.execute(
                            select(MinerStrategy)
                            .where(MinerStrategy.miner_id == miner.id)
                            .where(MinerStrategy.strategy_enabled == True)
                        )
                        enrolled_in_strategy = strategy_result.scalar_one_or_none()
                        
                        if enrolled_in_strategy:
                            logger.info(
                                "%s enrolled in strategy - ignoring telemetry mode %s (keeping %s)",
                                miner.name,
                                detected_mode,
                                miner.current_mode,
                            )
                        else:
                            miner.current_mode = detected_mode
                            logger.info("Updated %s mode to %s", miner.name, detected_mode)
                
                # Update firmware version if detected
                if telemetry.extra_data:
                    version = (
                        telemetry.extra_data.get("firmware_version")
                        or telemetry.extra_data.get("version")
                        or telemetry.extra_data.get("firmware")
                    )
                    if version and miner.firmware_version != version:
                        miner.firmware_version = version
                        logger.info("Updated %s firmware to %s", miner.name, version)
                
                # Save to database
                # Extract hashrate_unit from extra_data if present (ASICs = GH/s)
                hashrate_unit = "GH/s"  # Default for ASIC miners
                if telemetry.extra_data and "hashrate_unit" in telemetry.extra_data:
                    hashrate_unit = telemetry.extra_data["hashrate_unit"]

                # Sanity check: reject extreme hashrate spikes (corrupt readings)
                # Use mode-aware robust median/MAD with adaptive learning so
                # sustained, valid step-changes are not permanently rejected.
                try:
                    from core.utils import format_hashrate

                    if telemetry.hashrate is not None and telemetry.hashrate > 0:
                        active_mode = (
                            (telemetry.extra_data or {}).get("current_mode")
                            or miner.current_mode
                            or "unknown"
                        )
                        current_ghs = float(format_hashrate(telemetry.hashrate, hashrate_unit)["value"])

                        recent_result = await db.execute(
                            select(Telemetry.hashrate, Telemetry.hashrate_unit, Telemetry.mode, Telemetry.data)
                            .where(Telemetry.miner_id == miner.id)
                            .order_by(Telemetry.timestamp.desc())
                            .limit(40)
                        )
                        recent_rows = recent_result.all()
                        recent_hashrates: list[float] = []
                        recent_mode_hashrates: list[float] = []
                        recent_mode_rejected: list[float] = []

                        for hr_val, hr_unit, row_mode, row_data in recent_rows:
                            row_mode_normalized = row_mode or "unknown"

                            if hr_val is not None:
                                unit = hr_unit or "GH/s"
                                normalized = float(format_hashrate(hr_val, unit)["value"])
                                if normalized > 0:
                                    recent_hashrates.append(normalized)
                                    if row_mode_normalized == active_mode:
                                        recent_mode_hashrates.append(normalized)

                            if row_mode_normalized == active_mode and isinstance(row_data, dict):
                                rejected_obj = row_data.get("sanity_rejected_hashrate")
                                if isinstance(rejected_obj, dict):
                                    rejected_normalized = rejected_obj.get("normalized_ghs")
                                    try:
                                        rejected_value = float(rejected_normalized)
                                        if rejected_value > 0:
                                            recent_mode_rejected.append(rejected_value)
                                    except (TypeError, ValueError):
                                        pass

                        # Prefer mode-specific baseline; fallback to all data for same miner.
                        baseline = recent_mode_hashrates if len(recent_mode_hashrates) >= 10 else recent_hashrates

                        # Need enough history to make a robust decision
                        if len(baseline) >= 10:
                            median = statistics.median(baseline)
                            deviations = [abs(v - median) for v in baseline]
                            mad = statistics.median(deviations)

                            is_outlier = False
                            if mad == 0:
                                if median > 0 and current_ghs > (median * 10):
                                    is_outlier = True
                            else:
                                z = 0.6745 * (current_ghs - median) / mad
                                if z > 10.0:
                                    is_outlier = True

                            should_accept_outlier = False
                            acceptance_reason = None

                            if is_outlier:
                                # Grace period after explicit mode changes so a new
                                # operating profile can establish a fresh baseline.
                                mode_learning_grace_minutes = 20
                                if miner.last_mode_change:
                                    minutes_since_mode_change = (
                                        datetime.utcnow() - miner.last_mode_change
                                    ).total_seconds() / 60.0
                                    if minutes_since_mode_change <= mode_learning_grace_minutes:
                                        should_accept_outlier = True
                                        acceptance_reason = "mode_change_grace"

                                # Adaptive learning: if recent rejected readings in
                                # this mode are tightly clustered with current value,
                                # treat them as a stable step-change and accept.
                                if not should_accept_outlier and recent_mode_rejected:
                                    learning_samples = [current_ghs] + recent_mode_rejected[:5]
                                    if len(learning_samples) >= 4:
                                        cluster_median = statistics.median(learning_samples)
                                        if cluster_median > 0:
                                            max_pct_spread = max(
                                                abs(v - cluster_median) / cluster_median
                                                for v in learning_samples
                                            ) * 100.0
                                            if max_pct_spread <= 8.0:
                                                should_accept_outlier = True
                                                acceptance_reason = "consistent_mode_cluster"

                            if is_outlier and not should_accept_outlier:
                                telemetry.extra_data = telemetry.extra_data or {}
                                telemetry.extra_data["sanity_rejected_hashrate"] = {
                                    "original_hashrate": telemetry.hashrate,
                                    "original_hashrate_unit": hashrate_unit,
                                    "normalized_ghs": current_ghs,
                                    "median_ghs": median,
                                    "mad_ghs": mad,
                                    "active_mode": active_mode,
                                }
                                telemetry.hashrate = None
                                logger.warning(
                                    "Rejected outlier hashrate for %s: mode=%s current=%.3f GH/s median=%.3f GH/s mad=%.3f",
                                    miner.name,
                                    active_mode,
                                    current_ghs,
                                    median,
                                    mad,
                                )
                            elif is_outlier and should_accept_outlier:
                                telemetry.extra_data = telemetry.extra_data or {}
                                telemetry.extra_data["sanity_mode_learning"] = {
                                    "accepted_outlier": True,
                                    "reason": acceptance_reason,
                                    "active_mode": active_mode,
                                    "normalized_ghs": current_ghs,
                                    "median_ghs": median,
                                    "mad_ghs": mad,
                                }
                                logger.info(
                                    "Accepted mode-learning hashrate for %s: mode=%s reason=%s current=%.3f GH/s median=%.3f GH/s",
                                    miner.name,
                                    active_mode,
                                    acceptance_reason,
                                    current_ghs,
                                    median,
                                )
                except Exception as e:
                    logger.warning("Hashrate sanity check failed for %s: %s", miner.name, e)
                
                # Calculate energy cost if we have power data
                energy_cost = None
                if telemetry.power_watts is not None and telemetry.power_watts > 0:
                    try:
                        # Query Agile price for this timestamp
                        with db.no_autoflush:
                            price_query = select(EnergyPrice).where(
                                EnergyPrice.valid_from <= telemetry.timestamp,
                                EnergyPrice.valid_to > telemetry.timestamp
                            ).limit(1)
                            result = await db.execute(price_query)
                            price_row = result.scalar_one_or_none()
                        
                        if price_row:
                            # Calculate cost for 1 minute: (watts / 60 / 1000) * price_pence
                            # Result is in pence
                            energy_cost = (telemetry.power_watts / 60.0 / 1000.0) * price_row.price_pence
                    except Exception as e:
                        logger.warning("Could not calculate energy cost for %s: %s", miner.name, e)
                
                # Calculate delta shares BEFORE adding new telemetry to session
                new_shares = 0
                if telemetry.pool_in_use and telemetry.pool_difficulty and telemetry.shares_accepted:
                    try:
                        from core.high_diff_tracker import update_pool_block_effort, extract_coin_from_pool_name, get_network_difficulty
                        from sqlalchemy import select
                        from core.database import Pool
                        
                        # Query for previous telemetry BEFORE adding current to session
                        previous_telemetry = await db.execute(
                            select(Telemetry)
                            .where(Telemetry.miner_id == miner.id)
                            .order_by(Telemetry.timestamp.desc())
                            .limit(1)
                        )
                        prev = previous_telemetry.scalar_one_or_none()
                        
                        # Calculate new shares: current - previous (handle restarts where current < previous)
                        if prev and prev.shares_accepted:
                            if telemetry.shares_accepted >= prev.shares_accepted:
                                new_shares = telemetry.shares_accepted - prev.shares_accepted
                            else:
                                # Miner restarted, use current value
                                new_shares = telemetry.shares_accepted
                        else:
                            # First telemetry record, use current value
                            new_shares = telemetry.shares_accepted
                    except Exception as e:
                        logger.warning(f"Failed to calculate delta shares for {miner.name}: {e}")
                        new_shares = 0
                
                # NOW add telemetry to session AFTER calculating delta
                db_telemetry = Telemetry(
                    miner_id=miner.id,
                    timestamp=telemetry.timestamp,
                    hashrate=telemetry.hashrate,
                    hashrate_unit=hashrate_unit,
                    temperature=telemetry.temperature,
                    power_watts=telemetry.power_watts,
                    energy_cost=energy_cost,
                    shares_accepted=telemetry.shares_accepted,
                    shares_rejected=telemetry.shares_rejected,
                    pool_difficulty=telemetry.pool_difficulty,
                    pool_in_use=telemetry.pool_in_use,
                    mode=miner.current_mode,
                    data=telemetry.extra_data
                )
                db.add(db_telemetry)
                
                # Update pool block effort tracking with calculated delta
                if new_shares > 0 and telemetry.pool_in_use:
                    try:
                        # Find pool by matching URL and port (same logic as high diff tracking)
                        pool_str = telemetry.pool_in_use
                        if '://' in pool_str:
                            pool_str = pool_str.split('://')[1]
                        
                        if ':' in pool_str:
                            parts = pool_str.split(':')
                            pool_url = parts[0]
                            pool_port = int(parts[1])
                            
                            # Look up pool by exact host and port match
                            result = await db.execute(
                                select(Pool).where(
                                    Pool.url == pool_url,
                                    Pool.port == pool_port
                                )
                            )
                            pool = result.scalar_one_or_none()
                            
                            if pool:
                                # Extract coin from pool name
                                coin = extract_coin_from_pool_name(pool.name)
                                
                                if coin:
                                    # Get network difficulty from pool's driver if possible, fallback to Solopool.org
                                    network_diff = await get_network_difficulty(coin, pool_name=pool.name)
                                    pool_difficulty = telemetry.pool_difficulty
                                    if pool_difficulty is None:
                                        logger.debug(
                                            "Skipping pool effort update for %s (missing pool difficulty)",
                                            miner.name,
                                        )
                                    else:
                                        # Update cumulative effort using proper pool name and DELTA shares
                                        await update_pool_block_effort(
                                            db=db,
                                            pool_name=pool.name,
                                            coin=coin,
                                            new_shares=new_shares,
                                            pool_difficulty=float(pool_difficulty),
                                            network_difficulty=network_diff
                                        )
                            else:
                                logger.debug(f"Could not find pool for URL: {telemetry.pool_in_use}")
                    except Exception as e:
                        logger.warning(f"Failed to update pool effort for {miner.name}: {e}")
                
                return True
            else:
                # Log offline event
                event = Event(
                    event_type="warning",
                    source=f"miner_{miner.id}",
                    message=f"Failed to get telemetry from {miner.name}"
                )
                db.add(event)
                return False
        
        except Exception as e:
            logger.warning("Error collecting telemetry from miner %s: %s", miner.id, e)
            # Log miner connection error
            event = Event(
                event_type="error",
                source=f"miner_{miner.id}",
                message=f"Error collecting telemetry from {miner.name}: {str(e)}"
            )
            db.add(event)
            return False
        return False
    
    async def _collect_telemetry(self):
        """Collect telemetry from all miners"""
        from core.database import AsyncSessionLocal, Miner, Telemetry, Event, Pool, MinerStrategy, EnergyPrice, PriceBandStrategyConfig, engine
        from core.telemetry_metrics import update_concurrency_peak, update_backlog
        from adapters import create_adapter
        from sqlalchemy import select, String
        
        logger.debug("Starting telemetry collection")
        
        # Detect database type
        is_postgresql = 'postgresql' in str(engine.url)
        collection_mode = "parallel" if is_postgresql else "sequential"
        logger.info("Using %s telemetry collection mode", collection_mode)

        telemetry_concurrency = max(1, _as_int(app_config.get("telemetry.concurrency", 5), 5))
        jitter_max_ms = max(0, _as_int(app_config.get("telemetry.jitter_max_ms", 500), 500))
        
        try:
            async with AsyncSessionLocal() as db:
                # Check if strategy is in OFF state
                agile_in_off_state = False
                strategy_result = await db.execute(select(PriceBandStrategyConfig).limit(1))
                strategy = strategy_result.scalar_one_or_none()
                if strategy and strategy.enabled and strategy.current_price_band == "OFF":
                    agile_in_off_state = True
                    logger.info("Price band strategy is OFF - using ping-first optimization")
                
                # Get all enabled miners
                result = await db.execute(select(Miner).where(Miner.enabled == True))
                miners = result.scalars().all()
                
                logger.info("Found %s enabled miners", len(miners))
                
                # PostgreSQL: Use parallel collection with concurrency + jitter
                if is_postgresql:
                    semaphore = asyncio.Semaphore(telemetry_concurrency)
                    counter_lock = asyncio.Lock()
                    current_inflight = 0
                    concurrency_peak = 0

                    async def collect_with_metrics(target_miner):
                        nonlocal current_inflight, concurrency_peak
                        if target_miner.miner_type == "nmminer":
                            return None
                        jitter_seconds = random.uniform(0, jitter_max_ms) / 1000.0
                        if jitter_seconds > 0:
                            await asyncio.sleep(jitter_seconds)
                        async with semaphore:
                            async with counter_lock:
                                current_inflight += 1
                                concurrency_peak = max(concurrency_peak, current_inflight)
                            try:
                                from core.database import AsyncSessionLocal
                                async with AsyncSessionLocal() as task_db:
                                    wrote = await self._collect_miner_telemetry(
                                        target_miner,
                                        agile_in_off_state,
                                        task_db
                                    )
                                    if wrote:
                                        await task_db.commit()
                                    else:
                                        await task_db.rollback()
                                    return wrote
                            finally:
                                async with counter_lock:
                                    current_inflight = max(0, current_inflight - 1)

                    tasks = [collect_with_metrics(miner) for miner in miners]
                    
                    # Collect telemetry in parallel (bounded)
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    
                    # Log any exceptions
                    for i, result in enumerate(results):
                        if isinstance(result, Exception):
                            logger.warning("Error collecting telemetry task %s: %s", i, result)

                    update_concurrency_peak(concurrency_peak)
                
                # Sequential mode: Use one-at-a-time collection
                else:
                    sequential_miners = [m for m in miners if m.miner_type != "nmminer"]
                    for miner in miners:
                        # Skip NMMiner - it uses passive UDP listening
                        if miner.miner_type == "nmminer":
                            continue
                        
                        jitter_seconds = random.uniform(0, jitter_max_ms) / 1000.0
                        if jitter_seconds > 0:
                            await asyncio.sleep(jitter_seconds)

                        # Collect telemetry sequentially
                        try:
                            await self._collect_miner_telemetry(miner, agile_in_off_state, db)
                        except Exception as e:
                            logger.warning("Error in sequential collection for %s: %s", miner.name, e)
                        
                        # Stagger requests to avoid overwhelming miners
                        await asyncio.sleep(0.05)

                    update_concurrency_peak(1 if sequential_miners else 0)

                # Track telemetry backlog (miners without recent telemetry)
                await db.flush()
                cutoff = datetime.utcnow() - timedelta(minutes=2)
                backlog_result = await db.execute(
                    select(Miner.id, func.max(Telemetry.timestamp))
                    .outerjoin(Telemetry, Telemetry.miner_id == Miner.id)
                    .where(Miner.enabled == True)
                    .where(Miner.miner_type != "nmminer")
                    .group_by(Miner.id)
                )
                backlog_rows = backlog_result.all()
                backlog_count = sum(
                    1
                    for _, last_ts in backlog_rows
                    if last_ts is None or last_ts < cutoff
                )
                update_backlog(backlog_count)
                
                # Commit with retry logic for database locks
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        await db.commit()
                        break
                    except Exception as commit_error:
                        if "database is locked" in str(commit_error) and attempt < max_retries - 1:
                            logger.warning(
                                "Database locked, retrying commit (attempt %s/%s)",
                                attempt + 1,
                                max_retries,
                            )
                            await asyncio.sleep(0.5 * (attempt + 1))  # Exponential backoff
                            await db.rollback()
                        else:
                            raise
        
                # Log successful collection
                logger.info("Telemetry collection completed: %s miners", len(miners))
                async with AsyncSessionLocal() as db:
                    event = Event(
                        event_type="info",
                        source="telemetry",
                        message=f"Collected telemetry from {len(miners)} enabled miners"
                    )
                    db.add(event)
                    await db.commit()
        
        except Exception as e:
            logger.error("Error in telemetry collection: %s", e)
            # Log system error
            async with AsyncSessionLocal() as db:
                event = Event(
                    event_type="error",
                    source="scheduler",
                    message=f"Error in telemetry collection: {str(e)}"
                )
                db.add(event)
                await db.commit()
    
    async def _evaluate_automation_rules(self):
        """Evaluate and execute automation rules"""
        from core.database import AsyncSessionLocal, AutomationRule, Miner, EnergyPrice, Telemetry, Event
        from adapters import create_adapter
        
        try:
            async with AsyncSessionLocal() as db:
                # Get all enabled rules
                result = await db.execute(
                    select(AutomationRule)
                    .where(AutomationRule.enabled == True)
                    .order_by(AutomationRule.priority)
                )
                rules = result.scalars().all()
                
                for rule in rules:
                    try:
                        triggered = False
                        execution_context = {}
                        
                        logger.debug(
                            "Evaluating rule '%s' (id=%s, trigger=%s)",
                            rule.name,
                            rule.id,
                            rule.trigger_type,
                        )
                        
                        # Evaluate trigger
                        if rule.trigger_type == "price_threshold":
                            triggered, execution_context = await self._check_price_threshold(db, rule.trigger_config, rule)
                            logger.debug("Price threshold check for '%s': triggered=%s", rule.name, triggered)
                        
                        elif rule.trigger_type == "time_window":
                            triggered = self._check_time_window(rule.trigger_config)
                            logger.debug("Time window check for '%s': triggered=%s", rule.name, triggered)
                        
                        elif rule.trigger_type == "miner_offline":
                            triggered = await self._check_miner_offline(db, rule.trigger_config)
                            logger.debug("Miner offline check for '%s': triggered=%s", rule.name, triggered)
                        
                        elif rule.trigger_type == "miner_overheat":
                            triggered = await self._check_miner_overheat(db, rule.trigger_config)
                            logger.debug("Miner overheat check for '%s': triggered=%s", rule.name, triggered)
                        
                        elif rule.trigger_type == "pool_failure":
                            triggered = await self._check_pool_failure(db, rule.trigger_config)
                            logger.debug("Pool failure check for '%s': triggered=%s", rule.name, triggered)
                        
                        # Execute action if triggered
                        if triggered:
                            logger.info("Rule '%s' triggered; executing action '%s'", rule.name, rule.action_type)
                            await self._execute_action(db, rule)
                            # Update execution tracking
                            rule.last_executed_at = datetime.utcnow()
                            if execution_context:
                                rule.last_execution_context = execution_context
                        else:
                            logger.debug("Rule '%s' not triggered", rule.name)
                    
                    except Exception as e:
                        logger.exception("Error evaluating rule %s: %s", rule.id, e)
                
                await db.commit()
        
        except Exception as e:
            logger.exception("Error in automation rule evaluation: %s", e)
    
    async def _check_price_threshold(self, db, config: dict, rule: Optional[Any] = None) -> tuple[bool, dict]:
        """Check if current energy price meets threshold
        Returns: (triggered, context_dict)
        """
        condition = config.get("condition", "below")  # below, above, between, outside
        
        from core.database import EnergyPrice
        
        now = datetime.utcnow()
        result = await db.execute(
            select(EnergyPrice)
            .where(EnergyPrice.valid_from <= now)
            .where(EnergyPrice.valid_to > now)
            .limit(1)
        )
        price = result.scalar_one_or_none()
        
        if not price:
            return False, {}
        
        # Create execution context with price slot info
        context = {
            "price_id": price.id,
            "valid_from": price.valid_from.isoformat(),
            "valid_to": price.valid_to.isoformat(),
            "price_pence": price.price_pence
        }
        
        # Check if we already executed for this price slot
        if rule and rule.last_execution_context:
            last_price_id = rule.last_execution_context.get("price_id")
            if last_price_id == price.id:
                # Already executed for this price slot, don't trigger again
                logger.debug("Already executed for price slot %s; skipping", price.id)
                return False, context
        
        triggered = False
        if condition == "below":
            threshold = config.get("threshold", 0)
            triggered = price.price_pence < threshold
            logger.debug("Price check below: %sp < %sp => %s", price.price_pence, threshold, triggered)
        elif condition == "above":
            threshold = config.get("threshold", 0)
            triggered = price.price_pence > threshold
            logger.debug("Price check above: %sp > %sp => %s", price.price_pence, threshold, triggered)
        elif condition == "between":
            threshold_min = config.get("threshold_min", 0)
            threshold_max = config.get("threshold_max", 999)
            triggered = threshold_min <= price.price_pence <= threshold_max
            logger.debug(
                "Price check between: %sp in [%sp, %sp] => %s",
                price.price_pence,
                threshold_min,
                threshold_max,
                triggered,
            )
        elif condition == "outside":
            threshold_min = config.get("threshold_min", 0)
            threshold_max = config.get("threshold_max", 999)
            triggered = price.price_pence < threshold_min or price.price_pence > threshold_max
            logger.debug(
                "Price check outside: %sp outside [%sp, %sp] => %s",
                price.price_pence,
                threshold_min,
                threshold_max,
                triggered,
            )
        
        return triggered, context
    
    def _check_time_window(self, config: dict) -> bool:
        """Check if current time is within window"""
        from datetime import time
        
        start_str = config.get("start", "00:00")
        end_str = config.get("end", "23:59")
        
        start_time = datetime.strptime(start_str, "%H:%M").time()
        end_time = datetime.strptime(end_str, "%H:%M").time()
        current_time = datetime.utcnow().time()
        
        if start_time < end_time:
            return start_time <= current_time <= end_time
        else:
            # Handle overnight windows
            return current_time >= start_time or current_time <= end_time
    
    async def _check_miner_offline(self, db, config: dict) -> bool:
        """Check if miner is offline"""
        from core.database import Miner, Telemetry
        
        miner_id = config.get("miner_id")
        timeout_minutes = config.get("timeout_minutes", 5)
        
        if not miner_id:
            return False
        
        # Check last telemetry
        cutoff = datetime.utcnow() - timedelta(minutes=timeout_minutes)
        result = await db.execute(
            select(Telemetry)
            .where(Telemetry.miner_id == miner_id)
            .where(Telemetry.timestamp > cutoff)
            .limit(1)
        )
        
        return result.scalar_one_or_none() is None
    
    async def _check_miner_overheat(self, db, config: dict) -> bool:
        """Check if miner is overheating"""
        from core.database import Telemetry
        
        miner_id = config.get("miner_id")
        threshold = config.get("threshold", 80)
        
        if not miner_id:
            return False
        
        # Get latest telemetry
        result = await db.execute(
            select(Telemetry)
            .where(Telemetry.miner_id == miner_id)
            .order_by(Telemetry.timestamp.desc())
            .limit(1)
        )
        telemetry = result.scalar_one_or_none()
        
        if not telemetry or not telemetry.temperature:
            return False
        
        try:
            temp_value = float(telemetry.temperature)
            return temp_value > threshold
        except (ValueError, TypeError):
            logger.warning(f"Invalid temperature value in overheat check: {telemetry.temperature}")
            return False
    
    async def _check_pool_failure(self, db, config: dict) -> bool:
        """Check if pool connection is failing"""
        from core.database import Telemetry
        
        miner_id = config.get("miner_id")
        
        if not miner_id:
            return False
        
        # Get latest telemetry
        result = await db.execute(
            select(Telemetry)
            .where(Telemetry.miner_id == miner_id)
            .order_by(Telemetry.timestamp.desc())
            .limit(1)
        )
        telemetry = result.scalar_one_or_none()
        
        # Consider pool failure if no pool_in_use or shares not increasing
        return telemetry and not telemetry.pool_in_use
    
    async def _execute_action(self, db, rule: Any):
        """Execute automation action"""
        from core.database import Miner, Pool, Event
        from adapters import create_adapter
        
        action_type = rule.action_type
        action_config = rule.action_config
        
        if action_type == "apply_mode":
            mode = action_config.get("mode")
            miner_id = action_config.get("miner_id")
            miner_ids = action_config.get("miner_ids")
            
            logger.info("Automation apply_mode: miner_id=%s miner_ids=%s mode=%s", miner_id, miner_ids, mode)
            
            if not mode:
                logger.error("Automation apply_mode missing mode")
                return
            
            # Resolve miner(s) to apply mode to
            miners_to_update = []

            if isinstance(miner_ids, list) and miner_ids:
                valid_miner_ids = []
                for raw_miner_id in miner_ids:
                    try:
                        valid_miner_ids.append(int(raw_miner_id))
                    except (TypeError, ValueError):
                        logger.warning("Automation apply_mode skipping invalid miner id in miner_ids: %s", raw_miner_id)

                if valid_miner_ids:
                    logger.info("Automation apply_mode targeting explicit miner list: %s", valid_miner_ids)
                    result = await db.execute(
                        select(Miner).where(Miner.id.in_(valid_miner_ids)).where(Miner.enabled == True)
                    )
                    miners_to_update = result.scalars().all()
                    logger.info("Found %s enabled miners in explicit list", len(miners_to_update))
                else:
                    logger.error("Automation apply_mode miner_ids provided but no valid ids found")
                    return
            
            elif isinstance(miner_id, str) and miner_id.startswith("type:"):
                # Apply to all miners of this type
                miner_type = miner_id[5:]  # Remove "type:" prefix
                logger.info("Automation apply_mode targeting miner type '%s'", miner_type)
                result = await db.execute(
                    select(Miner).where(Miner.miner_type == miner_type).where(Miner.enabled == True)
                )
                miners_to_update = result.scalars().all()
                logger.info("Found %s enabled miners of type '%s'", len(miners_to_update), miner_type)
            elif miner_id is not None:
                # Single miner by ID
                result = await db.execute(select(Miner).where(Miner.id == miner_id))
                miner = result.scalar_one_or_none()
                if miner:
                    miners_to_update = [miner]
                else:
                    logger.error("Automation apply_mode miner id %s not found", miner_id)
            else:
                logger.error("Automation apply_mode missing miner target (miner_id or miner_ids)")
                return
            
            # Apply mode to all resolved miners
            for miner in miners_to_update:
                adapter = create_adapter(miner.miner_type, miner.id, miner.name, miner.ip_address, miner.port, miner.config)
                if adapter:
                    logger.info("Applying mode '%s' to %s (%s)", mode, miner.name, miner.miner_type)
                    success = await adapter.set_mode(mode)
                    if success:
                        miner.current_mode = mode
                        miner.last_mode_change = datetime.utcnow()
                        
                        # Control linked Home Assistant device (turn OFF for low/eco modes, ON for high/turbo/oc)
                        low_power_modes = ['low', 'eco']
                        turn_on = mode not in low_power_modes
                        await self._control_ha_device_for_automation(db, miner, turn_on)
                        
                        event = Event(
                            event_type="info",
                            source=f"automation_rule_{rule.id}",
                            message=f"Applied mode '{mode}' to {miner.name} (triggered by '{rule.name}')",
                            data={"rule": rule.name, "miner": miner.name, "mode": mode}
                        )
                        db.add(event)
                        logger.info("Successfully applied mode '%s' to %s", mode, miner.name)
                    else:
                        logger.error("Failed to apply mode '%s' to %s", mode, miner.name)
                else:
                    logger.error("Failed to create adapter for %s", miner.name)
        
        elif action_type == "switch_pool":
            miner_id = action_config.get("miner_id")
            pool_id = action_config.get("pool_id")
            
            if miner_id and pool_id:
                result = await db.execute(select(Miner).where(Miner.id == miner_id))
                miner = result.scalar_one_or_none()
                
                result = await db.execute(select(Pool).where(Pool.id == pool_id))
                pool = result.scalar_one_or_none()
                
                if miner and pool:
                    adapter = create_adapter(miner.miner_type, miner.id, miner.name, miner.ip_address, miner.port, miner.config)
                    if adapter:
                        success = await adapter.switch_pool(pool.url, pool.port, pool.user, pool.password)
                        if success:
                            event = Event(
                                event_type="info",
                                source=f"automation_rule_{rule.id}",
                                message=f"Switched {miner.name} to pool {pool.name} (triggered by '{rule.name}')",
                                data={"rule": rule.name, "miner": miner.name, "pool": pool.name}
                            )
                            db.add(event)
        
        elif action_type == "control_ha_device":
            device_id = action_config.get("device_id")
            command = action_config.get("command")  # "turn_on" or "turn_off"
            
            if device_id and command:
                from core.database import HomeAssistantConfig, HomeAssistantDevice
                
                # Check if HA is configured
                result = await db.execute(select(HomeAssistantConfig).where(HomeAssistantConfig.enabled == True).limit(1))
                ha_config = result.scalar_one_or_none()
                
                if not ha_config:
                    logger.error("Automation control_ha_device requested but Home Assistant is not configured")
                    return
                
                # Get the device
                result = await db.execute(select(HomeAssistantDevice).where(HomeAssistantDevice.id == device_id))
                ha_device = result.scalar_one_or_none()
                
                if not ha_device:
                    logger.error("Automation HA device id %s not found", device_id)
                    return
                
                # Import HA integration at runtime
                from integrations.homeassistant import HomeAssistantIntegration
                
                ha = HomeAssistantIntegration(ha_config.base_url, ha_config.access_token)
                
                logger.info("Automation %s HA device %s (rule=%s)", command, ha_device.name, rule.name)
                
                if command == "turn_on":
                    success = await ha.turn_on(ha_device.entity_id)
                elif command == "turn_off":
                    success = await ha.turn_off(ha_device.entity_id)
                else:
                    logger.error("Automation invalid HA command '%s'", command)
                    return
                
                if success:
                    ha_device.current_state = "on" if command == "turn_on" else "off"
                    await db.commit()
                    
                    event = Event(
                        event_type="info",
                        source=f"automation_rule_{rule.id}",
                        message=f"Turned {command.replace('_', ' ')} HA device {ha_device.name} (triggered by '{rule.name}')",
                        data={"rule": rule.name, "device": ha_device.name, "command": command}
                    )
                    db.add(event)
                    logger.info("HA device %s %s", ha_device.name, command.replace('_', ' '))
                else:
                    logger.error("Failed to %s HA device %s", command, ha_device.name)
        
        elif action_type == "send_alert":
            message = action_config.get("message", "Automation alert triggered")
            event = Event(
                event_type="alert",
                source=f"automation_rule_{rule.id}",
                message=f"{message} (triggered by '{rule.name}')",
                data={"rule": rule.name}
            )
            db.add(event)
            logger.warning("Automation alert: %s", message)
        
        elif action_type == "log_event":
            message = action_config.get("message", "Automation event logged")
            event = Event(
                event_type="info",
                source=f"automation_rule_{rule.id}",
                message=f"{message} (triggered by '{rule.name}')",
                data={"rule": rule.name}
            )
            db.add(event)
    
    async def _reconcile_automation_rules(self):
        """Reconcile miners that should be in a specific state based on currently active automation rules"""
        from core.database import AsyncSessionLocal, AutomationRule, Miner, EnergyPrice, Pool
        from adapters import get_adapter
        
        try:
            async with AsyncSessionLocal() as db:
                # Get all enabled rules
                result = await db.execute(
                    select(AutomationRule)
                    .where(AutomationRule.enabled == True)
                    .order_by(AutomationRule.priority)
                )
                rules = result.scalars().all()
                
                reconciled_count = 0
                checked_count = 0
                
                for rule in rules:
                    try:
                        # Check if rule is currently triggered
                        triggered = False
                        
                        if rule.trigger_type == "price_threshold":
                            triggered, _ = await self._check_price_threshold(db, rule.trigger_config, None)
                        elif rule.trigger_type == "time_window":
                            triggered = self._check_time_window(rule.trigger_config)
                        # Note: miner_offline, overheat, pool_failure are reactive, not persistent states to reconcile
                        
                        if not triggered:
                            continue
                        
                        # Rule is currently active - verify miners are in correct state
                        action_type = rule.action_type
                        action_config = rule.action_config
                        
                        if action_type == "apply_mode":
                            expected_mode = action_config.get("mode")
                            miner_id = action_config.get("miner_id")
                            miner_ids = action_config.get("miner_ids")
                            
                            if not expected_mode:
                                continue
                            
                            # Resolve miners
                            miners_to_check = []

                            if isinstance(miner_ids, list) and miner_ids:
                                valid_miner_ids = []
                                for raw_miner_id in miner_ids:
                                    try:
                                        valid_miner_ids.append(int(raw_miner_id))
                                    except (TypeError, ValueError):
                                        logger.debug("Skipping invalid miner id in reconcile miner_ids: %s", raw_miner_id)

                                if valid_miner_ids:
                                    result = await db.execute(
                                        select(Miner).where(Miner.id.in_(valid_miner_ids)).where(Miner.enabled == True)
                                    )
                                    miners_to_check = result.scalars().all()
                                else:
                                    continue
                            
                            elif isinstance(miner_id, str) and miner_id.startswith("type:"):
                                miner_type = miner_id[5:]
                                result = await db.execute(
                                    select(Miner).where(Miner.miner_type == miner_type).where(Miner.enabled == True)
                                )
                                miners_to_check = result.scalars().all()
                            elif miner_id is not None:
                                result = await db.execute(select(Miner).where(Miner.id == miner_id))
                                miner = result.scalar_one_or_none()
                                if miner:
                                    miners_to_check = [miner]
                            else:
                                continue
                            
                            # Check each miner's current mode
                            for miner in miners_to_check:
                                checked_count += 1
                                adapter = get_adapter(miner)
                                
                                if not adapter:
                                    continue
                                
                                # Get current mode from miner
                                try:
                                    current_mode = await adapter.get_mode()
                                    
                                    if current_mode and current_mode != expected_mode:
                                        logger.info(
                                            f"🔄 Reconciling automation: {miner.name} is in mode '{current_mode}' "
                                            f"but should be '{expected_mode}' (rule: {rule.name})"
                                        )
                                        
                                        # Apply correct mode
                                        success = await adapter.set_mode(expected_mode)
                                        
                                        if success:
                                            miner.current_mode = expected_mode
                                            reconciled_count += 1
                                            logger.info(f"✓ Reconciled {miner.name} to mode '{expected_mode}'")
                                            
                                            from core.database import Event
                                            event = Event(
                                                event_type="info",
                                                source=f"automation_reconciliation",
                                                message=f"Reconciled {miner.name} to mode '{expected_mode}' (rule: {rule.name})",
                                                data={"rule": rule.name, "miner": miner.name, "mode": expected_mode}
                                            )
                                            db.add(event)
                                            await db.commit()
                                            
                                            # Log to audit trail
                                            from core.audit import log_audit
                                            await log_audit(
                                                db,
                                                action="automation_rule_reconciled",
                                                resource_type="miner",
                                                resource_name=miner.name,
                                                changes={
                                                    "rule_name": rule.name,
                                                    "from_mode": current_mode,
                                                    "to_mode": expected_mode,
                                                    "reason": "Miner was out of sync with active automation rule"
                                                }
                                            )
                                            await db.commit()
                                            
                                            # Log system event
                                            event = Event(
                                                event_type="info",
                                                source="automation",
                                                message=f"Reconciled {miner.name} to {expected_mode} mode (rule: {rule.name})"
                                            )
                                            db.add(event)
                                            await db.commit()
                                        else:
                                            logger.warning(f"✗ Failed to reconcile {miner.name} to mode '{expected_mode}'")
                                
                                except Exception as e:
                                    logger.debug(f"Could not get current mode for {miner.name}: {e}")
                                    continue
                        
                        elif action_type == "switch_pool":
                            miner_id = action_config.get("miner_id")
                            pool_id = action_config.get("pool_id")
                            
                            if not miner_id or not pool_id:
                                continue
                            
                            result = await db.execute(select(Miner).where(Miner.id == miner_id))
                            miner = result.scalar_one_or_none()
                            
                            result = await db.execute(select(Pool).where(Pool.id == pool_id))
                            expected_pool = result.scalar_one_or_none()
                            
                            if not miner or not expected_pool:
                                continue
                            
                            checked_count += 1
                            adapter = get_adapter(miner)
                            
                            if not adapter:
                                continue
                            
                            # Get current pool
                            try:
                                telemetry = await adapter.get_telemetry()
                                
                                if telemetry and telemetry.pool_in_use:
                                    current_pool_url = telemetry.pool_in_use
                                    expected_pool_url = f"{expected_pool.url}"
                                    
                                    # Normalize URLs for comparison
                                    def normalize_url(url: str) -> str:
                                        url = url.replace("stratum+tcp://", "").replace("http://", "").replace("https://", "")
                                        url = url.rstrip("/")
                                        return url.lower()
                                    
                                    if normalize_url(current_pool_url) != normalize_url(expected_pool_url):
                                        logger.info(
                                            f"🔄 Reconciling automation: {miner.name} is on pool '{current_pool_url}' "
                                            f"but should be on '{expected_pool.name}' (rule: {rule.name})"
                                        )
                                        
                                        # Switch to correct pool
                                        success = await adapter.switch_pool(
                                            expected_pool.url, expected_pool.port, 
                                            expected_pool.user, expected_pool.password
                                        )
                                        
                                        if success:
                                            reconciled_count += 1
                                            logger.info(f"✓ Reconciled {miner.name} to pool '{expected_pool.name}'")
                                            
                                            from core.database import Event
                                            event = Event(
                                                event_type="info",
                                                source=f"automation_reconciliation",
                                                message=f"Reconciled {miner.name} to pool '{expected_pool.name}' (rule: {rule.name})",
                                                data={"rule": rule.name, "miner": miner.name, "pool": expected_pool.name}
                                            )
                                            db.add(event)
                                        else:
                                            logger.warning(f"✗ Failed to reconcile {miner.name} to pool '{expected_pool.name}'")
                            
                            except Exception as e:
                                logger.debug(f"Could not get current pool for {miner.name}: {e}")
                                continue
                    
                    except Exception as e:
                        logger.error(f"Error reconciling automation rule {rule.name}: {e}")
                        continue
                
                await db.commit()
                
                if reconciled_count > 0:
                    logger.info(f"✅ Automation reconciliation: {reconciled_count}/{checked_count} miners reconciled")
        
        except Exception as e:
            logger.error(f"Failed to reconcile automation rules: {e}")
            logger.exception("Failed to reconcile automation rules with exception")
    
    async def _start_nmminer_listener(self):
        """Start NMMiner UDP listener (one-time startup)"""
        from core.database import AsyncSessionLocal, Miner
        from core.miner_loader import get_miner_loader
        
        try:
            async with AsyncSessionLocal() as db:
                # Get all NMMiner devices
                result = await db.execute(
                    select(Miner)
                    .where(Miner.miner_type == "nmminer")
                    .where(Miner.enabled == True)
                )
                nmminers = result.scalars().all()

                loader = get_miner_loader()
                nmminer_adapter_class = loader.get_driver("nmminer")
                if not nmminer_adapter_class:
                    logger.warning("NMMiner driver not loaded - UDP listener not started")
                    return

                nmminer_module = loader.get_driver_module("nmminer")
                if nmminer_module is None:
                    logger.warning("NMMiner driver module not available - UDP listener not started")
                    return

                nmminer_listener_class = getattr(nmminer_module, "NMMinerUDPListener", None)
                if nmminer_listener_class is None:
                    logger.warning("NMMinerUDPListener not found in loaded NMMiner driver")
                    return
                
                # Create adapter registry (shared across system)
                self.nmminer_adapters = {}
                for miner in nmminers:
                    adapter = nmminer_adapter_class(miner.id, miner.name, miner.ip_address, miner.port, miner.config)
                    self.nmminer_adapters[miner.ip_address] = adapter
                
                if not self.nmminer_adapters:
                    logger.info("No NMMiner devices found - UDP listener not started")
                    return
                
                # Start UDP listener with shared adapters
                listener = nmminer_listener_class(self.nmminer_adapters)
                self.nmminer_listener = listener
                
                # Run in background (non-blocking) with error handling
                import asyncio
                
                async def run_listener():
                    try:
                        await listener.start()
                    except Exception as e:
                        logger.exception("NMMiner UDP listener crashed: %s", e)
                
                asyncio.create_task(run_listener())
                
                logger.info("NMMiner UDP listener started for %s devices", len(nmminers))
        
        except Exception as e:
            logger.exception("Failed to start NMMiner UDP listener: %s", e)
    
    async def reload_nmminer_adapters(self):
        """Reload NMMiner adapter registry (called when miners added/removed/updated)"""
        from core.database import AsyncSessionLocal, Miner
        from core.miner_loader import get_miner_loader
        
        try:
            async with AsyncSessionLocal() as db:
                # Get all NMMiner devices
                result = await db.execute(
                    select(Miner)
                    .where(Miner.miner_type == "nmminer")
                    .where(Miner.enabled == True)
                )
                nmminers = result.scalars().all()

                loader = get_miner_loader()
                nmminer_adapter_class = loader.get_driver("nmminer")
                if not nmminer_adapter_class:
                    logger.warning("NMMiner driver not loaded - cannot reload adapters")
                    self.nmminer_adapters.clear()
                    return
                
                # Update adapter registry
                old_count = len(self.nmminer_adapters)
                self.nmminer_adapters.clear()
                
                for miner in nmminers:
                    adapter = nmminer_adapter_class(miner.id, miner.name, miner.ip_address, miner.port, miner.config)
                    self.nmminer_adapters[miner.ip_address] = adapter
                
                new_count = len(self.nmminer_adapters)
                logger.info("NMMiner adapter registry reloaded: %s -> %s devices", old_count, new_count)
                
                # If we went from 0 to >0 and listener isn't running, start it
                if old_count == 0 and new_count > 0 and self.nmminer_listener is None:
                    await self._start_nmminer_listener()
        
        except Exception as e:
            logger.exception("Failed to reload NMMiner adapters: %s", e)
    
    async def _aggregate_telemetry(self):
        """
        Aggregate telemetry data at 00:05 daily.
        
        Creates hourly and daily aggregates from raw telemetry, then prunes old raw data:
        - Hourly aggregates: Keep for 30 days
        - Daily aggregates: Keep forever
        - Raw telemetry: Keep for 7 days only
        
        This reduces AI context size by 56x (hourly) to 789x (daily).
        """
        from core.database import AsyncSessionLocal, Telemetry, TelemetryHourly, TelemetryDaily, Miner
        from sqlalchemy import delete
        
        try:
            async with AsyncSessionLocal() as db:
                # Get yesterday's date range
                yesterday = (datetime.utcnow() - timedelta(days=1)).date()
                start_time = datetime.combine(yesterday, datetime.min.time())
                end_time = datetime.combine(yesterday, datetime.max.time())
                
                logger.info("Aggregating telemetry for %s", yesterday)
                
                # Get all active miners
                result = await db.execute(select(Miner))
                miners = result.scalars().all()
                
                hourly_count = 0
                daily_count = 0
                
                for miner in miners:
                    # ========== HOURLY AGGREGATION ==========
                    # Get raw telemetry for this miner for yesterday
                    query = select(Telemetry).where(
                        and_(
                            Telemetry.miner_id == miner.id,
                            Telemetry.timestamp >= start_time,
                            Telemetry.timestamp <= end_time
                        )
                    ).order_by(Telemetry.timestamp)
                    
                    result = await db.execute(query)
                    telemetry_records = result.scalars().all()
                    
                    if not telemetry_records:
                        continue
                    
                    # Group by hour
                    hourly_data = {}
                    for record in telemetry_records:
                        hour_start = record.timestamp.replace(minute=0, second=0, microsecond=0)
                        
                        if hour_start not in hourly_data:
                            hourly_data[hour_start] = []
                        hourly_data[hour_start].append(record)
                    
                    # Create hourly aggregates
                    for hour_start, records in hourly_data.items():
                        # Calculate aggregates
                        uptime_minutes = len(records)
                        hashrates = [r.hashrate for r in records if r.hashrate is not None]
                        temperatures = [r.temperature for r in records if r.temperature is not None]
                        power_values = [r.power_watts for r in records if r.power_watts is not None]
                        energy_costs = [r.energy_cost for r in records if r.energy_cost is not None]
                        
                        # Get hashrate unit (assume consistent)
                        hashrate_unit = records[0].hashrate_unit if records[0].hashrate_unit else "GH/s"
                        
                        # Calculate totals and averages
                        avg_hashrate = sum(hashrates) / len(hashrates) if hashrates else None
                        min_hashrate = min(hashrates) if hashrates else None
                        max_hashrate = max(hashrates) if hashrates else None
                        avg_temperature = sum(temperatures) / len(temperatures) if temperatures else None
                        peak_temperature = max(temperatures) if temperatures else None
                        
                        # Total kWh = sum of (watts / 60 / 1000) for each minute
                        total_kwh = sum(p / 60.0 / 1000.0 for p in power_values) if power_values else None
                        
                        # Total energy cost = sum of all energy_cost values (already in pence)
                        total_energy_cost = sum(energy_costs) if energy_costs else None
                        
                        # Shares (use last cumulative value - first cumulative value for delta)
                        shares_records = [r for r in records if r.shares_accepted is not None]
                        shares_accepted = shares_records[-1].shares_accepted if shares_records else None
                        
                        shares_rejected_records = [r for r in records if r.shares_rejected is not None]
                        shares_rejected = shares_rejected_records[-1].shares_rejected if shares_rejected_records else None
                        
                        # Reject rate
                        reject_rate_pct = None
                        if shares_accepted and shares_rejected is not None:
                            total_shares = shares_accepted + shares_rejected
                            reject_rate_pct = (shares_rejected / total_shares * 100) if total_shares > 0 else 0
                        
                        # Check if hourly aggregate already exists
                        existing = await db.execute(
                            select(TelemetryHourly).where(
                                and_(
                                    TelemetryHourly.miner_id == miner.id,
                                    TelemetryHourly.hour_start == hour_start
                                )
                            )
                        )
                        if existing.scalar_one_or_none():
                            continue  # Skip if already aggregated
                        
                        # Create hourly aggregate
                        hourly_agg = TelemetryHourly(
                            miner_id=miner.id,
                            hour_start=hour_start,
                            uptime_minutes=uptime_minutes,
                            avg_hashrate=avg_hashrate,
                            min_hashrate=min_hashrate,
                            max_hashrate=max_hashrate,
                            hashrate_unit=hashrate_unit,
                            avg_temperature=avg_temperature,
                            peak_temperature=peak_temperature,
                            total_kwh=total_kwh,
                            total_energy_cost=total_energy_cost,
                            shares_accepted=shares_accepted,
                            shares_rejected=shares_rejected,
                            reject_rate_pct=reject_rate_pct
                        )
                        db.add(hourly_agg)
                        hourly_count += 1
                    
                    # ========== DAILY AGGREGATION ==========
                    # Use the hourly data we already have
                    if telemetry_records:
                        # Calculate daily aggregates from raw data
                        daily_uptime = len(telemetry_records)
                        uptime_percentage = (daily_uptime / 1440.0) * 100  # 1440 minutes in a day
                        
                        hashrates = [r.hashrate for r in telemetry_records if r.hashrate is not None]
                        temperatures = [r.temperature for r in telemetry_records if r.temperature is not None]
                        power_values = [r.power_watts for r in telemetry_records if r.power_watts is not None]
                        energy_costs = [r.energy_cost for r in telemetry_records if r.energy_cost is not None]
                        
                        daily_avg_hashrate = sum(hashrates) / len(hashrates) if hashrates else None
                        daily_min_hashrate = min(hashrates) if hashrates else None
                        daily_max_hashrate = max(hashrates) if hashrates else None
                        daily_avg_temperature = sum(temperatures) / len(temperatures) if temperatures else None
                        daily_peak_temperature = max(temperatures) if temperatures else None
                        daily_total_kwh = sum(p / 60.0 / 1000.0 for p in power_values) if power_values else None
                        daily_total_cost = sum(energy_costs) if energy_costs else None
                        
                        # Shares (use last - first for daily delta)
                        shares_records = [r for r in telemetry_records if r.shares_accepted is not None]
                        daily_shares_accepted = shares_records[-1].shares_accepted if shares_records else None
                        
                        shares_rejected_records = [r for r in telemetry_records if r.shares_rejected is not None]
                        daily_shares_rejected = shares_rejected_records[-1].shares_rejected if shares_rejected_records else None
                        
                        daily_reject_rate = None
                        if daily_shares_accepted and daily_shares_rejected is not None:
                            total_shares = daily_shares_accepted + daily_shares_rejected
                            daily_reject_rate = (daily_shares_rejected / total_shares * 100) if total_shares > 0 else 0
                        
                        # Simple health score (0-100) based on uptime and reject rate
                        health_score = None
                        if uptime_percentage is not None:
                            health_score = uptime_percentage  # Start with uptime %
                            if daily_reject_rate is not None:
                                # Reduce health by reject rate penalty
                                health_score = max(0, health_score - (daily_reject_rate * 5))
                        
                        # Check if daily aggregate already exists
                        existing = await db.execute(
                            select(TelemetryDaily).where(
                                and_(
                                    TelemetryDaily.miner_id == miner.id,
                                    TelemetryDaily.date == start_time
                                )
                            )
                        )
                        if existing.scalar_one_or_none():
                            continue  # Skip if already aggregated
                        
                        # Create daily aggregate
                        daily_agg = TelemetryDaily(
                            miner_id=miner.id,
                            date=start_time,
                            uptime_minutes=daily_uptime,
                            uptime_percentage=uptime_percentage,
                            avg_hashrate=daily_avg_hashrate,
                            min_hashrate=daily_min_hashrate,
                            max_hashrate=daily_max_hashrate,
                            hashrate_unit=hashrate_unit,
                            avg_temperature=daily_avg_temperature,
                            peak_temperature=daily_peak_temperature,
                            total_kwh=daily_total_kwh,
                            total_energy_cost=daily_total_cost,
                            shares_accepted=daily_shares_accepted,
                            shares_rejected=daily_shares_rejected,
                            reject_rate_pct=daily_reject_rate,
                            health_score=health_score
                        )
                        db.add(daily_agg)
                        daily_count += 1
                
                # Commit all aggregates
                await db.commit()
                logger.info(
                    "Created %s hourly and %s daily aggregates for %s",
                    hourly_count,
                    daily_count,
                    yesterday,
                )
                
                # ========== PRUNE OLD DATA ==========
                # Prune raw telemetry older than 7 days
                cutoff_raw = datetime.utcnow() - timedelta(days=7)
                result = await db.execute(
                    delete(Telemetry).where(Telemetry.timestamp < cutoff_raw)
                )
                await db.commit()
                pruned_raw = getattr(result, "rowcount", 0) or 0
                if pruned_raw > 0:
                    logger.info("Pruned %s raw telemetry records older than 7 days", pruned_raw)
                
                # Prune hourly aggregates older than 30 days
                cutoff_hourly = datetime.utcnow() - timedelta(days=30)
                result = await db.execute(
                    delete(TelemetryHourly).where(TelemetryHourly.hour_start < cutoff_hourly)
                )
                await db.commit()
                pruned_hourly = getattr(result, "rowcount", 0) or 0
                if pruned_hourly > 0:
                    logger.info("Pruned %s hourly aggregates older than 30 days", pruned_hourly)
                
                # Daily aggregates are kept forever
                
        except Exception as e:
            logger.exception("Telemetry aggregation failed: %s", e)
    
    async def _purge_old_telemetry(self):
        """Purge telemetry data older than 30 days (increased for long-term analytics)"""
        from core.database import AsyncSessionLocal, Telemetry
        from sqlalchemy import delete
        
        try:
            cutoff_time = datetime.utcnow() - timedelta(days=30)
            
            async with AsyncSessionLocal() as db:
                # Delete old telemetry records
                result = await db.execute(
                    delete(Telemetry)
                    .where(Telemetry.timestamp < cutoff_time)
                )
                await db.commit()
                
                deleted_count = getattr(result, "rowcount", 0) or 0
                if deleted_count > 0:
                    logger.info("Purged %s telemetry records older than 30 days", deleted_count)
        
        except Exception as e:
            logger.error("Failed to purge old telemetry: %s", e)
    
    def _get_next_midnight(self):
        """Calculate next midnight UTC for daily aggregation"""
        now = datetime.utcnow()
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return next_midnight
    
    async def _aggregate_daily_stats(self):
        """Aggregate yesterday's stats into daily tables at midnight"""
        from core.aggregation import aggregate_daily_stats

        if self._should_skip_non_critical_job("aggregate_daily_stats"):
            return
        
        try:
            await aggregate_daily_stats()
            logger.info("Daily stats aggregation complete")
        except Exception as e:
            logger.exception("Daily stats aggregation failed: %s", e)
    
    async def _backfill_missing_daily_stats(self):
        """Check for and backfill any missing daily aggregations (last 30 days)"""
        from core.aggregation import aggregate_daily_stats
        from core.database import AsyncSessionLocal, DailyMinerStats, Telemetry
        from sqlalchemy import select, func
        
        try:
            logger.info("Checking for missing daily aggregations")
            
            async with AsyncSessionLocal() as db:
                # Check last 30 days for missing data
                today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                start_date = today - timedelta(days=30)
                
                missing_dates = []
                
                for i in range(30):
                    check_date = today - timedelta(days=i+1)  # Yesterday and older
                    
                    # Check if we have any daily stats for this date
                    result = await db.execute(
                        select(func.count(DailyMinerStats.id))
                        .where(DailyMinerStats.date == check_date)
                    )
                    count = result.scalar()
                    
                    # If no stats but we have telemetry data for that day, it's missing
                    if count == 0:
                        # Check if there's telemetry for this date
                        tel_result = await db.execute(
                            select(func.count(Telemetry.id))
                            .where(
                                and_(
                                    Telemetry.timestamp >= check_date,
                                    Telemetry.timestamp < check_date + timedelta(days=1)
                                )
                            )
                        )
                        tel_count = tel_result.scalar() or 0
                        
                        if tel_count > 0:
                            missing_dates.append(check_date)
                
                if missing_dates:
                    logger.info("Found %s missing daily aggregation(s)", len(missing_dates))
                    for missing_date in sorted(missing_dates):
                        logger.info("Backfilling missing daily aggregation for %s", missing_date.date())
                        try:
                            await aggregate_daily_stats(missing_date)
                            logger.info("Backfilled daily aggregation for %s", missing_date.date())
                        except Exception as e:
                            logger.error("Failed to backfill %s: %s", missing_date.date(), e)
                            logger.error(f"Failed to backfill {missing_date.date()}: {e}", exc_info=True)
                else:
                    logger.info("No missing daily aggregations found")
        
        except Exception as e:
            logger.exception("Failed to check for missing daily stats: %s", e)
    
    async def _log_system_summary(self):
        """Log system status summary every 6 hours"""
        from core.database import AsyncSessionLocal, Event, Miner, Telemetry
        from sqlalchemy import select, func
        
        try:
            async with AsyncSessionLocal() as db:
                # Get miner counts
                result = await db.execute(select(Miner))
                all_miners = result.scalars().all()
                total_miners = len(all_miners)
                enabled_miners = len([m for m in all_miners if m.enabled])
                
                # Get recent telemetry success count (last 6 hours)
                six_hours_ago = datetime.utcnow() - timedelta(hours=6)
                result = await db.execute(
                    select(func.count(Telemetry.id))
                    .where(Telemetry.timestamp >= six_hours_ago)
                )
                telemetry_count = result.scalar() or 0
                
                # Get average hashrate and power for enabled miners
                from core.health import HealthScoringService
                total_hashrate = 0.0
                total_power = 0.0
                health_scores = []
                
                for miner in all_miners:
                    if miner.enabled:
                        result = await db.execute(
                            select(Telemetry)
                            .where(Telemetry.miner_id == miner.id)
                            .order_by(Telemetry.timestamp.desc())
                            .limit(1)
                        )
                        latest_telemetry = result.scalars().first()
                        
                        if latest_telemetry:
                            total_hashrate += latest_telemetry.hashrate or 0
                            total_power += latest_telemetry.power_watts or 0
                            health_result = await HealthScoringService.calculate_health_score(miner.id, db, hours=24)
                            if health_result and health_result.get("overall_score") is not None:
                                health_scores.append(float(health_result["overall_score"]))
                
                avg_health = sum(health_scores) / len(health_scores) if health_scores else 0
                
                # Create summary event
                message = (
                    f"System Status: {enabled_miners}/{total_miners} miners online | "
                    f"Telemetry collected: {telemetry_count} | "
                    f"Total hashrate: {total_hashrate:.2f} GH/s | "
                    f"Total power: {total_power:.2f}W | "
                    f"Avg health: {avg_health:.1f}/100"
                )
                
                event = Event(
                    event_type="info",
                    source="scheduler",
                    message=message
                )
                db.add(event)
                await db.commit()
                
                logger.info(message)
        
        except Exception as e:
            logger.error("Failed to log system summary: %s", e)
    
    async def _auto_discover_miners(self):
        """Auto-discover miners on configured networks"""
        from core.database import AsyncSessionLocal, Miner, Event
        from core.discovery import MinerDiscoveryService
        from sqlalchemy import select
        
        try:
            # Check if discovery is enabled
            discovery_config = _as_dict(app_config.get("network_discovery", {}))
            if not discovery_config.get("enabled", False):
                logger.info("Auto-discovery is disabled, skipping scan")
                return
            
            # Get configured networks
            networks = discovery_config.get("networks", [])
            if not networks:
                logger.info("No networks configured for auto-discovery")
                return
            
            auto_add = discovery_config.get("auto_add", False)
            total_found = 0
            total_added = 0
            
            logger.info("Starting auto-discovery on %s network(s)", len(networks))
            
            async with AsyncSessionLocal() as db:
                # Get existing miners
                result = await db.execute(select(Miner))
                existing_miners = result.scalars().all()
                existing_ips = {m.ip_address for m in existing_miners}
                
                # Scan each network
                for network in networks:
                    network_cidr = network.get("cidr") if isinstance(network, dict) else network
                    network_name = network.get("name", network_cidr) if isinstance(network, dict) else network_cidr

                    if not isinstance(network_cidr, str) or not network_cidr.strip():
                        logger.warning("Skipping invalid network entry: %s", network)
                        continue
                    
                    logger.info("Scanning network: %s", network_name)
                    
                    discovered = await MinerDiscoveryService.discover_miners(
                        network_cidr=network_cidr,
                        timeout=2.0
                    )
                    
                    total_found += len(discovered)
                    
                    # Add new miners if auto-add is enabled
                    if auto_add:
                        for miner_info in discovered:
                            if miner_info['ip'] not in existing_ips:
                                # Create new miner
                                new_miner = Miner(
                                    name=miner_info['name'],
                                    miner_type=miner_info['type'],
                                    ip_address=miner_info['ip'],
                                    port=miner_info['port'],
                                    enabled=True
                                )
                                db.add(new_miner)
                                existing_ips.add(miner_info['ip'])
                                total_added += 1
                                logger.info("Auto-added miner: %s (%s)", miner_info['name'], miner_info['ip'])
                
                # Commit changes
                if total_added > 0:
                    await db.commit()
                    
                    # Log event
                    event = Event(
                        event_type="info",
                        source="scheduler",
                        message=f"Auto-discovery: Found {total_found} miner(s), added {total_added} new miner(s)"
                    )
                    db.add(event)
                    await db.commit()
                
                logger.info("Auto-discovery complete: %s found, %s added", total_found, total_added)
        
        except Exception as e:
            logger.exception("Auto-discovery failed: %s", e)
    
    async def _purge_old_events(self):
        """Purge events older than 30 days"""
        from core.database import AsyncSessionLocal, Event
        from sqlalchemy import delete
        
        try:
            cutoff_time = datetime.utcnow() - timedelta(days=30)
            
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    delete(Event)
                    .where(Event.timestamp < cutoff_time)
                )
                await db.commit()
                
                deleted_count = getattr(result, "rowcount", 0) or 0
                if deleted_count > 0:
                    logger.info("Purged %s events older than 30 days", deleted_count)
        
        except Exception as e:
            logger.error("Failed to purge old events: %s", e)
    
    async def _db_maintenance(self):
        """
        Comprehensive database maintenance during OFF periods
        Includes: purge old data, VACUUM, ANALYZE
        """
        from core.database import AsyncSessionLocal, engine
        from sqlalchemy import text

        if self._should_skip_non_critical_job("db_maintenance"):
            return
        
        logger.info("Starting database maintenance")
        
        try:
            # 1. Aggregate pool health data
            await self._aggregate_pool_health()
            
            # 2. Aggregate miner analytics data
            await self._aggregate_miner_analytics()
            
            # 3. Purge old telemetry
            await self._purge_old_telemetry()
            
            # 4. Purge old events
            await self._purge_old_events()
            
            # 5. Purge old pool health (raw + hourly)
            await self._purge_old_pool_health()
            
            # 6. Purge old miner analytics (hourly only)
            await self._purge_old_miner_analytics()
            
            # 7. Purge old audit logs
            await self._purge_old_audit_logs()
            
            # 8. Purge old notification logs
            await self._purge_old_notification_logs()
            
            # 9. Purge old health scores
            await self._purge_old_health_scores()
            
            # 10. Database VACUUM (defragment and reclaim space)
            logger.info("Running VACUUM ANALYZE")
            # PostgreSQL VACUUM must run outside a transaction
            # Create connection with AUTOCOMMIT isolation for VACUUM
            async with engine.execution_options(isolation_level="AUTOCOMMIT").connect() as conn:
                await conn.execute(text("VACUUM ANALYZE"))
                logger.info("VACUUM ANALYZE complete")
            
            logger.info("Database maintenance complete")
            
        except Exception as e:
            logger.error("Database maintenance failed: %s", e)
            raise

    async def _run_maintenance_cycle(
        self,
        *,
        reason: str,
        max_retries: int = 1,
        retry_interval: int = 1800,
        include_attempt_in_success: bool = False,
    ) -> bool:
        """Run aggregation + DB maintenance with retry and notifications."""
        from core.notifications import send_alert

        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"📊 Maintenance attempt {attempt}/{max_retries} ({reason})")

                await self._aggregate_telemetry()
                await self._db_maintenance()

                success_msg = (
                    "🔧 Database maintenance complete\n\n"
                    f"Reason: {reason}\n"
                    "✅ Telemetry aggregation complete\n"
                    "✅ Old data purged\n"
                    "✅ Database optimized (VACUUM + ANALYZE)\n"
                    f"⏰ Time: {datetime.utcnow().strftime('%H:%M UTC')}"
                )
                if include_attempt_in_success:
                    success_msg += f"\n🔄 Attempt: {attempt}/{max_retries}"

                await send_alert(success_msg, alert_type="aggregation_status")
                return True

            except Exception as e:
                logger.error(f"❌ Maintenance attempt {attempt}/{max_retries} failed: {e}")

                if attempt < max_retries:
                    logger.info(f"⏳ Retrying in {retry_interval // 60} minutes...")
                    await asyncio.sleep(retry_interval)
                else:
                    await send_alert(
                        "⚠️ Database maintenance FAILED\n\n"
                        f"Reason: {reason}\n"
                        f"❌ All {max_retries} attempts failed\n"
                        f"Last error: {str(e)[:200]}\n"
                        f"⏰ Time: {datetime.utcnow().strftime('%H:%M UTC')}\n\n"
                        "Check logs for details.",
                        alert_type="aggregation_status"
                    )

        return False
    
    async def _fallback_maintenance(self):
        """
        Fallback maintenance runs daily at 3am IF:
        1. Strategy is disabled (no OFF triggers), OR
        2. Last maintenance was >7 days ago (strategy never hitting OFF)
        """
        from core.database import AsyncSessionLocal, PriceBandStrategyConfig
        from sqlalchemy import select

        if self._should_skip_non_critical_job("fallback_maintenance"):
            return
        
        try:
            async with AsyncSessionLocal() as db:
                # Check strategy state
                strategy_result = await db.execute(select(PriceBandStrategyConfig).limit(1))
                strategy = strategy_result.scalar_one_or_none()
                
                should_run = False
                reason = ""
                
                if not strategy or not strategy.enabled:
                    should_run = True
                    reason = "Strategy disabled"
                elif strategy.last_aggregation_time is None:
                    should_run = True
                    reason = "Never run before"
                else:
                    days_since = (datetime.utcnow() - strategy.last_aggregation_time).total_seconds() / 86400
                    if days_since >= 7:
                        should_run = True
                        reason = f"Last run {days_since:.1f} days ago"
                
                if should_run:
                    logger.info(f"🔧 Fallback maintenance triggered: {reason}")

                    success = await self._run_maintenance_cycle(
                        reason=f"fallback: {reason}",
                        max_retries=1,
                        include_attempt_in_success=False,
                    )

                    if not success:
                        return

                    # Update timestamp if we have strategy
                    if strategy:
                        strategy.last_aggregation_time = datetime.utcnow()
                        await db.commit()
                    
                    logger.info("✅ Fallback maintenance complete")
                else:
                    logger.debug("Fallback maintenance skipped (strategy handling it)")
        
        except Exception as e:
            logger.error(f"Fallback maintenance failed: {e}")
            logger.exception("Fallback maintenance failed with exception")
    
    async def _purge_old_energy_prices(self):
        """Purge energy prices older than 60 days"""
        from core.database import AsyncSessionLocal, EnergyPrice
        from sqlalchemy import delete
        
        try:
            cutoff_time = datetime.utcnow() - timedelta(days=60)
            
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    delete(EnergyPrice)
                    .where(EnergyPrice.valid_from < cutoff_time)
                )
                await db.commit()
                
                deleted_count = getattr(result, "rowcount", 0) or 0
                if deleted_count > 0:
                    logger.info("Purged %s energy prices older than 60 days", deleted_count)
        
        except Exception as e:
            logger.error("Failed to purge old energy prices: %s", e)
    
    async def _vacuum_database(self):
        """Run VACUUM to optimize PostgreSQL database"""
        from core.database import engine
        from sqlalchemy import text
        
        try:
            async with engine.begin() as conn:
                # PostgreSQL: VACUUM ANALYZE (outside transaction)
                autocommit_conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
                await autocommit_conn.execute(text("VACUUM ANALYZE"))
                logger.info("PostgreSQL optimized (VACUUM ANALYZE completed)")
        
        except Exception as e:
            logger.error("Failed to vacuum database: %s", e)
    
    async def _check_alerts(self):
        """Check for alert conditions and send notifications"""
        from core.database import AsyncSessionLocal, Miner, Telemetry, AlertConfig, AlertThrottle
        from core.notifications import send_alert
        from sqlalchemy import and_
        
        try:
            async with AsyncSessionLocal() as db:
                # Get enabled alert configs
                result = await db.execute(
                    select(AlertConfig).where(AlertConfig.enabled == True)
                )
                alert_configs = result.scalars().all()
                
                if not alert_configs:
                    return
                
                # Get all miners
                result = await db.execute(select(Miner).where(Miner.enabled == True))
                miners = result.scalars().all()
                
                for miner in miners:
                    # Get latest telemetry
                    result = await db.execute(
                        select(Telemetry)
                        .where(Telemetry.miner_id == miner.id)
                        .order_by(Telemetry.timestamp.desc())
                        .limit(1)
                    )
                    latest_telemetry = result.scalar_one_or_none()
                    
                    for alert_config in alert_configs:
                        alert_cfg = _as_dict(alert_config.config)
                        alert_triggered = False
                        message = ""
                        
                        # Check miner offline
                        if alert_config.alert_type == "miner_offline":
                            timeout_minutes = _as_int(alert_cfg.get("timeout_minutes", 5), 5)
                            if not latest_telemetry or \
                               (datetime.utcnow() - latest_telemetry.timestamp).seconds > timeout_minutes * 60:
                                alert_triggered = True
                                message = f"⚠️ <b>Miner Offline</b>\n\n{miner.name} has been offline for more than {timeout_minutes} minutes"
                        
                        # Check high temperature
                        elif alert_config.alert_type == "high_temperature":
                            # Use different default thresholds for different miner types
                            # Avalon Nano: 95°C, NerdQaxe: 75°C, Bitaxe: 70°C
                            if 'avalon' in miner.miner_type.lower():
                                default_threshold = 95
                            elif 'nerdqaxe' in miner.miner_type.lower():
                                default_threshold = 75
                            elif 'bitaxe' in miner.miner_type.lower():
                                default_threshold = 70
                            else:
                                default_threshold = 75  # Generic fallback
                            
                            threshold = _as_float(alert_cfg.get("threshold_celsius", default_threshold), float(default_threshold))
                            
                            # Auto-upgrade old thresholds to new standards
                            if 'avalon' in miner.miner_type.lower() and threshold in [75, 90]:
                                threshold = 95
                            elif 'bitaxe' in miner.miner_type.lower() and threshold == 75:
                                threshold = 70
                            
                            # Ensure temperature is a float for comparison
                            if latest_telemetry and latest_telemetry.temperature:
                                try:
                                    temp_value = float(latest_telemetry.temperature)
                                    if temp_value > threshold:
                                        alert_triggered = True
                                        message = f"🌡️ <b>High Temperature Alert</b>\n\n{miner.name} temperature: {temp_value:.1f}°C (threshold: {threshold}°C)"
                                except (ValueError, TypeError):
                                    logger.warning(f"Invalid temperature value for {miner.name}: {latest_telemetry.temperature}")
                        
                        # Check high reject rate
                        elif alert_config.alert_type == "high_reject_rate":
                            threshold_percent = _as_float(alert_cfg.get("threshold_percent", 5), 5.0)
                            if latest_telemetry and latest_telemetry.shares_accepted and latest_telemetry.shares_rejected:
                                total_shares = latest_telemetry.shares_accepted + latest_telemetry.shares_rejected
                                if total_shares > 0:
                                    reject_rate = (latest_telemetry.shares_rejected / total_shares) * 100
                                    if reject_rate > threshold_percent:
                                        alert_triggered = True
                                        message = f"📉 <b>High Reject Rate</b>\n\n{miner.name} reject rate: {reject_rate:.1f}% (threshold: {threshold_percent}%)"
                        
                        # Check pool failure
                        elif alert_config.alert_type == "pool_failure":
                            if latest_telemetry and not latest_telemetry.pool_in_use:
                                alert_triggered = True
                                message = f"🌊 <b>Pool Connection Failed</b>\n\n{miner.name} is not connected to any pool"
                        
                        # Check low hashrate
                        elif alert_config.alert_type == "low_hashrate":
                            drop_percent = _as_float(alert_cfg.get("drop_percent", 30), 30.0)
                            if latest_telemetry and latest_telemetry.hashrate:
                                # Skip alert if mode changed in last 20 minutes (intentional hashrate change)
                                if miner.last_mode_change:
                                    time_since_mode_change = (datetime.utcnow() - miner.last_mode_change).total_seconds() / 60
                                    if time_since_mode_change < 20:
                                        logger.info(
                                            "Skipping hashrate alert for %s: mode changed %.1f min ago",
                                            miner.name,
                                            time_since_mode_change,
                                        )
                                        continue
                                
                                # Get average hashrate from last 10 readings
                                result = await db.execute(
                                    select(Telemetry)
                                    .where(Telemetry.miner_id == miner.id)
                                    .where(Telemetry.hashrate != None)
                                    .order_by(Telemetry.timestamp.desc())
                                    .limit(10)
                                )
                                recent_telemetry = result.scalars().all()
                                
                                if len(recent_telemetry) >= 5:
                                    hashrates = [float(t.hashrate) for t in recent_telemetry if t.hashrate is not None]
                                    if not hashrates:
                                        continue
                                    avg_hashrate = sum(hashrates) / len(hashrates)
                                    if latest_telemetry.hashrate < avg_hashrate * (1 - drop_percent / 100):
                                        alert_triggered = True
                                        message = f"⚡ <b>Low Hashrate Alert</b>\n\n{miner.name} hashrate dropped {drop_percent}% below average\nCurrent: {latest_telemetry.hashrate:.2f} GH/s\nAverage: {avg_hashrate:.2f} GH/s"
                        
                        # Send notification if alert triggered
                        if alert_triggered:
                            # Check throttling - get cooldown period from alert config (default 1 hour)
                            cooldown_minutes = _as_int(alert_cfg.get("cooldown_minutes", 60), 60)
                            
                            # Check if we recently sent this alert for this miner
                            result = await db.execute(
                                select(AlertThrottle).where(
                                    and_(
                                        AlertThrottle.miner_id == miner.id,
                                        AlertThrottle.alert_type == alert_config.alert_type
                                    )
                                )
                            )
                            throttle = result.scalar_one_or_none()
                            
                            should_send = False
                            if not throttle:
                                # First time sending this alert
                                should_send = True
                                throttle = AlertThrottle(
                                    miner_id=miner.id,
                                    alert_type=alert_config.alert_type,
                                    last_sent=datetime.utcnow(),
                                    send_count=1
                                )
                                db.add(throttle)
                            else:
                                # Check if cooldown period has passed
                                time_since_last = (datetime.utcnow() - throttle.last_sent).total_seconds() / 60
                                if time_since_last >= cooldown_minutes:
                                    should_send = True
                                    throttle.last_sent = datetime.utcnow()
                                    throttle.send_count += 1
                            
                            if should_send:
                                await send_alert(message, alert_config.alert_type)
                                await db.commit()
                                logger.info("Alert sent: %s for %s", alert_config.alert_type, miner.name)
                            else:
                                logger.info(
                                    "Alert throttled: %s for %s (cooldown: %s min)",
                                    alert_config.alert_type,
                                    miner.name,
                                    cooldown_minutes,
                                )
        
        except Exception as e:
            logger.exception("Failed to check alerts: %s", e)
    
    async def _record_health_scores(self):
        """Record health scores for all active miners"""
        from core.database import AsyncSessionLocal
        from core.health import record_health_scores
        
        try:
            async with AsyncSessionLocal() as db:
                await record_health_scores(db)
                logger.info("Health scores recorded")
        
        except Exception as e:
            logger.exception("Failed to record health scores: %s", e)
    
    async def _update_platform_version_cache(self):
        """Update platform version cache from GitHub API every 5 minutes"""
        from core.database import AsyncSessionLocal, PlatformVersionCache
        from sqlalchemy import select
        import httpx
        import os
        
        logger.info("Updating platform version cache from GitHub")
        
        try:
            async with AsyncSessionLocal() as db:
                # Fetch from GitHub
                github_owner = "renegadeuk"
                github_repo = "hmm-local"
                github_branch = "main"
                
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        # Get latest commits
                        commits_url = f"https://api.github.com/repos/{github_owner}/{github_repo}/commits"
                        params = {"sha": github_branch, "per_page": 20}
                        response = await client.get(commits_url, params=params)
                        
                        if response.status_code == 403:
                            # Rate limited - keep existing cache
                            logger.warning("GitHub API rate limited, keeping existing cache")
                            
                            # Update last_checked and mark as unavailable
                            result = await db.execute(select(PlatformVersionCache).where(PlatformVersionCache.id == 1))
                            cache = result.scalar_one_or_none()
                            if cache:
                                cache.last_checked = datetime.utcnow()
                                cache.github_available = False
                                cache.error_message = "GitHub API rate limited (60 requests/hour)"
                                await db.commit()
                                logger.info("Cache last_checked updated while GitHub is rate limited")
                            return
                        
                        response.raise_for_status()
                        commits = response.json()
                        
                        if not commits:
                            logger.warning("No commits found in GitHub response")
                            return
                        
                        latest_commit = commits[0]
                        sha = latest_commit["sha"]
                        sha_short = sha[:7]
                        message = latest_commit["commit"]["message"].split("\n")[0]
                        author = latest_commit["commit"]["author"]["name"]
                        date = latest_commit["commit"]["author"]["date"]
                        
                        # Generate tag and image
                        tag = f"{github_branch}-{sha_short}"
                        image = f"ghcr.io/{github_owner}/{github_repo}:{tag}"
                        
                        # Prepare changelog
                        changelog = []
                        for commit in commits:
                            changelog.append({
                                "sha": commit["sha"][:7],
                                "message": commit["commit"]["message"].split("\n")[0],
                                "author": commit["commit"]["author"]["name"],
                                "date": commit["commit"]["author"]["date"]
                            })
                        
                        # Upsert cache (single row with id=1)
                        result = await db.execute(select(PlatformVersionCache).where(PlatformVersionCache.id == 1))
                        cache = result.scalar_one_or_none()
                        
                        if cache:
                            # Update existing
                            cache.latest_commit = sha
                            cache.latest_commit_short = sha_short
                            cache.latest_message = message
                            cache.latest_author = author
                            cache.latest_date = date
                            cache.latest_tag = tag
                            cache.latest_image = image
                            cache.changelog = {"commits": changelog}
                            cache.last_checked = datetime.utcnow()
                            cache.github_available = True
                            cache.error_message = None
                        else:
                            # Insert new
                            cache = PlatformVersionCache(
                                id=1,
                                latest_commit=sha,
                                latest_commit_short=sha_short,
                                latest_message=message,
                                latest_author=author,
                                latest_date=date,
                                latest_tag=tag,
                                latest_image=image,
                                changelog=changelog,
                                last_checked=datetime.utcnow(),
                                github_available=True,
                                error_message=None
                            )
                            db.add(cache)
                        
                        await db.commit()
                        logger.info("Platform version cache updated: %s", tag)
                
                except httpx.HTTPError as e:
                    logger.error("GitHub API error while updating version cache: %s", e)
                    # Mark cache as unavailable but keep existing data
                    result = await db.execute(select(PlatformVersionCache).where(PlatformVersionCache.id == 1))
                    cache = result.scalar_one_or_none()
                    if cache:
                        cache.last_checked = datetime.utcnow()
                        cache.github_available = False
                        cache.error_message = str(e)
                        await db.commit()
        
        except Exception as e:
            logger.exception("Failed to update platform version cache: %s", e)
    
    async def _check_update_notifications(self):
        """Check for driver updates and send notifications"""
        from core.database import AsyncSessionLocal
        from core.notifications import NotificationService
        import httpx
        
        logger.info("Checking for available driver updates")
        
        try:
            notifications_to_send = []
            
            # Check driver updates
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get("http://localhost:8080/api/drivers/status")
                    if response.status_code == 200:
                        drivers_info = response.json()
                        updates_available = []
                        
                        for driver in drivers_info:
                            if driver.get("status") == "update_available":
                                updates_available.append(f"{driver['display_name']} ({driver['current_version']} → {driver['available_version']})")
                        
                        if updates_available:
                            message = (
                                f"📦 <b>Driver Updates Available</b>\n\n"
                                f"{len(updates_available)} driver(s) have updates:\n"
                                + "\n".join(f"• {u}" for u in updates_available[:5])  # Show first 5
                            )
                            if len(updates_available) > 5:
                                message += f"\n... and {len(updates_available) - 5} more"
                            
                            message += "\n\nVisit Settings → Driver Updates to install"
                            notifications_to_send.append(("driver_update", message))
                            logger.info("%s driver update(s) available", len(updates_available))
            except Exception as e:
                logger.warning("Failed to check driver updates: %s", e)
            
            # Send notifications if any updates found
            if notifications_to_send:
                notification_service = NotificationService()
                
                for alert_type, message in notifications_to_send:
                    # Send to all enabled channels
                    telegram_sent = await notification_service.send_notification("telegram", message, alert_type)
                    
                    # Format message for Discord (replace HTML tags)
                    discord_message = message.replace("<b>", "**").replace("</b>", "**").replace("<i>", "*").replace("</i>", "*")
                    discord_sent = await notification_service.send_notification("discord", discord_message, alert_type)
                    
                    if telegram_sent or discord_sent:
                        logger.info("Sent %s notification", alert_type)
            else:
                logger.info("No updates available")
        
        except Exception as e:
            logger.exception("Failed to check for updates: %s", e)
    
    async def _auto_optimize_miners(self):
        """Automatically optimize miner modes based on energy prices"""
        from core.config import app_config
        from core.database import AsyncSessionLocal, Miner
        from core.energy import EnergyOptimizationService
        from sqlalchemy import select
        
        logger.info("Auto-optimization job triggered")
        
        # Check if auto-optimization is enabled
        enabled = app_config.get("energy_optimization.enabled", False)
        logger.info("Auto-optimization enabled: %s", enabled)
        if not enabled:
            return
        
        # Get band thresholds (CHEAP / MODERATE / EXPENSIVE)
        cheap_threshold = _as_float(app_config.get("energy_optimization.cheap_threshold", 15.0), 15.0)
        expensive_threshold = _as_float(app_config.get("energy_optimization.expensive_threshold", 25.0), 25.0)
        logger.info(
            "Band thresholds: CHEAP < %sp | MODERATE %s-%sp | EXPENSIVE >= %sp",
            cheap_threshold,
            cheap_threshold,
            expensive_threshold,
            expensive_threshold,
        )
        
        try:
            async with AsyncSessionLocal() as db:
                # Get current price band recommendation
                recommendation = await EnergyOptimizationService.should_mine_now(db, cheap_threshold, expensive_threshold)
                logger.debug("Energy optimization recommendation: %s", recommendation)
                
                if "error" in recommendation:
                    logger.warning("Auto-optimization skipped: %s", recommendation['error'])
                    return
                
                band = recommendation["band"]
                target_mode_name = recommendation["mode"]
                current_price = recommendation["current_price_pence"]
                logger.info(
                    "Current band: %s, mode: %s, price: %sp/kWh",
                    band,
                    target_mode_name,
                    current_price,
                )
                
                # Get all enabled miners that support mode changes (not NMMiner)
                result = await db.execute(
                    select(Miner)
                    .where(Miner.enabled == True)
                    .where(Miner.miner_type != 'nmminer')
                )
                miners = result.scalars().all()
                logger.info("Found %s enabled miners (excluding NMMiner)", len(miners))
                
                # Skip if EXPENSIVE band (can't turn off miners)
                if band == "EXPENSIVE":
                    logger.info("Band is EXPENSIVE - skipping miner control (cannot turn off miners)")
                    # Still control HA devices below
                else:
                    # Mode mapping: CHEAP → high/oc, MODERATE → low/eco
                    mode_map = {
                        "avalon_nano_3": {"low": "low", "high": "high"},
                        "avalon_nano": {"low": "low", "high": "high"},
                        "bitaxe": {"low": "eco", "high": "oc"},
                        "nerdqaxe": {"low": "eco", "high": "turbo"}
                    }
                    
                    for miner in miners:
                        logger.debug("Processing miner %s (type=%s)", miner.name, miner.miner_type)
                        if miner.miner_type not in mode_map:
                            logger.debug("Skipping %s: type not in mode_map", miner.name)
                            continue
                        
                        # Determine target mode: "high" for CHEAP, "low" for MODERATE
                        target_mode = mode_map[miner.miner_type][target_mode_name]
                        logger.debug("Target mode for %s: %s", miner.name, target_mode)
                        
                        # Create adapter
                        from adapters import create_adapter
                        adapter = create_adapter(miner.miner_type, miner.id, miner.name, miner.ip_address, miner.port, miner.config)
                        
                        if adapter:
                            try:
                                # Get current mode from database
                                current_mode = miner.current_mode
                                logger.debug("Current mode for %s: %s", miner.name, current_mode)
                                
                                # Only change if different
                                if current_mode != target_mode:
                                    logger.info("Changing %s mode: %s -> %s", miner.name, current_mode, target_mode)
                                    success = await adapter.set_mode(target_mode)
                                    if success:
                                        # Update database
                                        miner.current_mode = target_mode
                                        miner.last_mode_change = datetime.utcnow()
                                        await db.commit()
                                        logger.info(
                                            "Auto-optimized %s: %s -> %s (band=%s, price=%sp/kWh)",
                                            miner.name,
                                            current_mode,
                                            target_mode,
                                            band,
                                            current_price,
                                        )
                                    else:
                                        logger.error("Failed to set mode for %s", miner.name)
                                else:
                                    logger.debug("%s already in %s mode, skipping", miner.name, target_mode)
                            
                            except Exception as e:
                                logger.exception("Failed to auto-optimize %s: %s", miner.name, e)
                        else:
                            logger.error("No adapter for %s", miner.name)
                
                # Control Home Assistant devices based on band (ON for CHEAP/MODERATE, OFF for EXPENSIVE)
                ha_should_be_on = band in ["CHEAP", "MODERATE"]
                for miner in miners:
                    await self._control_ha_device_for_energy_optimization(db, miner, ha_should_be_on)
                
                action_desc = {"CHEAP": "full power", "MODERATE": "reduced power", "EXPENSIVE": "HA devices off"}
                logger.info(
                    "Auto-optimization complete: %s (band=%s, price=%sp/kWh)",
                    action_desc.get(band),
                    band,
                    current_price,
                )
        
        except Exception as e:
            logger.exception("Failed to auto-optimize miners: %s", e)
    
    async def _reconcile_energy_optimization(self):
        """Reconcile miners that are out of sync with energy optimization state"""
        from core.config import app_config
        from core.database import AsyncSessionLocal, Miner, Event
        from core.energy import EnergyOptimizationService
        from adapters import get_adapter
        from sqlalchemy import select
        
        try:
            # Check if auto-optimization is enabled
            enabled = app_config.get("energy_optimization.enabled", False)
            if not enabled:
                logger.debug("Energy optimization reconciliation skipped: not enabled")
                return
            
            # Get band thresholds
            cheap_threshold = _as_float(app_config.get("energy_optimization.cheap_threshold", 15.0), 15.0)
            expensive_threshold = _as_float(app_config.get("energy_optimization.expensive_threshold", 25.0), 25.0)
            
            async with AsyncSessionLocal() as db:
                # Get current price band recommendation
                recommendation = await EnergyOptimizationService.should_mine_now(db, cheap_threshold, expensive_threshold)
                
                if "error" in recommendation:
                    logger.debug(f"Energy optimization reconciliation skipped: {recommendation.get('error')}")
                    return
                
                band = recommendation["band"]
                target_mode_name = recommendation["mode"]
                current_price = recommendation["current_price_pence"]
                
                logger.info(f"⚡ Energy reconciliation check: price={current_price}p, band={band}, target_mode={target_mode_name}")
                
                # Get all enabled miners that support mode changes
                result = await db.execute(
                    select(Miner)
                    .where(Miner.enabled == True)
                    .where(Miner.miner_type != 'nmminer')
                )
                miners = result.scalars().all()
                
                logger.info(f"⚡ Checking {len(miners)} miners for energy optimization state")
                
                # Skip if EXPENSIVE band (can't turn off miners)
                if band == "EXPENSIVE":
                    logger.info("⚡ Band is EXPENSIVE - skipping miner reconciliation")
                else:
                    mode_map = {
                        "avalon_nano_3": {"low": "low", "high": "high"},
                        "avalon_nano": {"low": "low", "high": "high"},
                        "bitaxe": {"low": "eco", "high": "oc"},
                        "nerdqaxe": {"low": "eco", "high": "turbo"}
                    }
                    
                    reconciled_count = 0
                    checked_count = 0
                    
                    for miner in miners:
                        if miner.miner_type not in mode_map:
                            logger.debug(f"Skipping {miner.name}: type {miner.miner_type} not in mode_map")
                            continue
                        
                        # Determine expected mode based on band: "high" for CHEAP, "low" for MODERATE
                        expected_mode = mode_map[miner.miner_type][target_mode_name]
                        
                        adapter = get_adapter(miner)
                        if not adapter:
                            logger.warning(f"No adapter for {miner.name}")
                            continue
                        
                        try:
                            # Get actual current mode from miner hardware
                            logger.info(f"⚡ Checking {miner.name} ({miner.miner_type}): expected mode='{expected_mode}'")
                            current_mode = await adapter.get_mode()
                            checked_count += 1
                            
                            logger.info(f"⚡ {miner.name}: current_mode='{current_mode}', expected='{expected_mode}'")
                            
                            if current_mode is None:
                                logger.warning(f"{miner.name}: could not determine current mode from hardware")
                            elif current_mode == expected_mode:
                                logger.info(f"✓ {miner.name}: already in correct mode '{expected_mode}'")
                            else:
                                logger.info(
                                    f"🔄 Reconciling energy optimization: {miner.name} is in mode '{current_mode}' "
                                    f"but should be '{expected_mode}' (band: {band}, price: {current_price}p)"
                                )
                                
                                # Apply correct mode
                                success = await adapter.set_mode(expected_mode)
                                
                                if success:
                                    miner.current_mode = expected_mode
                                    reconciled_count += 1
                                    logger.info(f"✅ Reconciled {miner.name} to mode '{expected_mode}'")
                                    
                                    # Log to audit trail
                                    from core.audit import log_audit
                                    await log_audit(
                                        db,
                                        action="energy_optimization_reconciled",
                                        resource_type="miner",
                                        resource_name=miner.name,
                                        changes={
                                            "from_mode": current_mode,
                                            "to_mode": expected_mode,
                                            "current_price": current_price,
                                            "band": band,
                                            "cheap_threshold": cheap_threshold,
                                            "expensive_threshold": expensive_threshold,
                                            "reason": "Miner was out of sync with energy optimization state"
                                        }
                                    )
                                    await db.commit()
                                    
                                    # Log system event
                                    event = Event(
                                        event_type="info",
                                        source="energy_optimization",
                                        message=f"Reconciled {miner.name} to {expected_mode} mode (band: {band}, price: {current_price}p)"
                                    )
                                    db.add(event)
                                    await db.commit()
                                else:
                                    logger.warning(f"❌ Failed to reconcile {miner.name} to mode '{expected_mode}'")
                        
                        except Exception as e:
                            logger.error(f"❌ Error checking {miner.name}: {e}")
                            logger.exception("Error checking %s during energy reconciliation", miner.name)
                            continue
                        
                        # Stagger requests to avoid overwhelming miners
                        await asyncio.sleep(2)
                    
                    if reconciled_count > 0:
                        await db.commit()
                        logger.info(f"✅ Energy reconciliation complete: {reconciled_count}/{checked_count} miners reconciled")
                    else:
                        logger.info(f"✅ Energy reconciliation complete: All {checked_count} miners already in correct state")
                
                # Control Home Assistant devices based on band (ON for CHEAP/MODERATE, OFF for EXPENSIVE)
                ha_should_be_on = band in ["CHEAP", "MODERATE"]
                for miner in miners:
                    await self._control_ha_device_for_energy_optimization(db, miner, ha_should_be_on)
        
        except Exception as e:
            logger.error(f"Failed to reconcile energy optimization: {e}")
            logger.exception("Failed to reconcile energy optimization with exception")
    
    async def _monitor_pool_health(self):
        """Monitor health of all enabled pools"""
        from core.database import AsyncSessionLocal, Pool
        from core.pool_health import PoolHealthService
        from sqlalchemy import select
        
        try:
            # Use asyncio.create_task to ensure proper greenlet context
            async with AsyncSessionLocal() as db:
                # Get all enabled pools
                result = await db.execute(select(Pool).where(Pool.enabled == True))
                pools = result.scalars().all()
                
                for pool in pools:
                    # Store pool name before try block to avoid post-failure DB access
                    pool_name = pool.name
                    pool_id = pool.id
                    
                    # Retry logic for database locks
                    max_retries = 3
                    for attempt in range(max_retries):
                        try:
                            await PoolHealthService.monitor_pool(pool_id, db)
                            logger.info("Pool health check completed: %s", pool_name)
                            break
                        except Exception as e:
                            error_str = str(e)
                            if "database is locked" in error_str and attempt < max_retries - 1:
                                logger.warning(
                                    "Pool health check for %s locked, retrying (attempt %s/%s)",
                                    pool_name,
                                    attempt + 1,
                                    max_retries,
                                )
                                await db.rollback()
                                await asyncio.sleep(0.5 * (attempt + 1))  # Exponential backoff
                            else:
                                # Final attempt failed or non-lock error
                                await db.rollback()
                                logger.error(f"Failed to monitor pool {pool_name}: {e}", exc_info=True)
                                break
                    
                    # Stagger requests to avoid overwhelming pools
                    await asyncio.sleep(2)
        
        except Exception as e:
            logger.exception("Failed to monitor pool health: %s", e)
    
    async def _purge_old_pool_health(self):
        """Purge raw pool health data older than 7 days (aggregated data retained longer)"""
        from core.database import AsyncSessionLocal, PoolHealth, PoolHealthHourly
        from sqlalchemy import delete
        
        try:
            async with AsyncSessionLocal() as db:
                # Purge raw data older than 7 days
                raw_cutoff = datetime.utcnow() - timedelta(days=7)
                raw_result = await db.execute(
                    delete(PoolHealth).where(PoolHealth.timestamp < raw_cutoff)
                )
                
                # Purge hourly aggregates older than 30 days
                hourly_cutoff = datetime.utcnow() - timedelta(days=30)
                hourly_result = await db.execute(
                    delete(PoolHealthHourly).where(PoolHealthHourly.hour_start < hourly_cutoff)
                )
                
                await db.commit()
                raw_pruned = getattr(raw_result, "rowcount", 0) or 0
                hourly_pruned = getattr(hourly_result, "rowcount", 0) or 0
                logger.info(
                    "Purged %s raw pool health records (>7d), %s hourly aggregates (>30d)",
                    raw_pruned,
                    hourly_pruned,
                )
        
        except Exception as e:
            logger.error("Failed to purge old pool health data: %s", e)
    
    async def _purge_old_miner_analytics(self):
        """Purge hourly miner analytics older than 30 days (daily aggregates retained forever)"""
        from core.database import AsyncSessionLocal, HourlyMinerAnalytics
        from sqlalchemy import delete
        
        try:
            async with AsyncSessionLocal() as db:
                # Purge hourly data older than 30 days (daily aggregates kept forever)
                hourly_cutoff = datetime.utcnow() - timedelta(days=30)
                hourly_result = await db.execute(
                    delete(HourlyMinerAnalytics).where(HourlyMinerAnalytics.hour_start < hourly_cutoff)
                )
                
                await db.commit()
                hourly_pruned = getattr(hourly_result, "rowcount", 0) or 0
                logger.info("Purged %s hourly miner analytics records (>30d)", hourly_pruned)
        
        except Exception as e:
            logger.error("Failed to purge old miner analytics data: %s", e)
    
    async def _purge_old_audit_logs(self):
        """Purge audit logs older than 90 days"""
        from core.database import AsyncSessionLocal, AuditLog
        from sqlalchemy import delete
        
        try:
            async with AsyncSessionLocal() as db:
                cutoff = datetime.utcnow() - timedelta(days=90)
                result = await db.execute(
                    delete(AuditLog).where(AuditLog.timestamp < cutoff)
                )
                
                await db.commit()
                deleted_count = getattr(result, "rowcount", 0) or 0
                if deleted_count > 0:
                    logger.info("Purged %s audit log records (>90d)", deleted_count)
        
        except Exception as e:
            logger.error("Failed to purge old audit logs: %s", e)
    
    async def _purge_old_notification_logs(self):
        """Purge notification logs older than 90 days"""
        from core.database import AsyncSessionLocal, NotificationLog
        from sqlalchemy import delete
        
        try:
            async with AsyncSessionLocal() as db:
                cutoff = datetime.utcnow() - timedelta(days=90)
                result = await db.execute(
                    delete(NotificationLog).where(NotificationLog.timestamp < cutoff)
                )
                
                await db.commit()
                deleted_count = getattr(result, "rowcount", 0) or 0
                if deleted_count > 0:
                    logger.info("Purged %s notification log records (>90d)", deleted_count)
        
        except Exception as e:
            logger.error("Failed to purge old notification logs: %s", e)
    
    async def _purge_old_health_scores(self):
        """Purge health scores older than 30 days"""
        from core.database import AsyncSessionLocal, HealthScore
        from sqlalchemy import delete
        
        try:
            async with AsyncSessionLocal() as db:
                cutoff = datetime.utcnow() - timedelta(days=30)
                result = await db.execute(
                    delete(HealthScore).where(HealthScore.timestamp < cutoff)
                )
                
                await db.commit()
                deleted_count = getattr(result, "rowcount", 0) or 0
                if deleted_count > 0:
                    logger.info("Purged %s health score records (>30d)", deleted_count)
        
        except Exception as e:
            logger.error("Failed to purge old health scores: %s", e)
    
    async def _aggregate_pool_health(self):
        """Aggregate raw pool health checks into hourly and daily summaries"""
        from core.database import AsyncSessionLocal, PoolHealth, PoolHealthHourly, PoolHealthDaily, Pool
        from sqlalchemy import select, func, and_
        
        logger.info("Aggregating pool health data")
        
        try:
            async with AsyncSessionLocal() as db:
                # Get all pools
                pools_result = await db.execute(select(Pool))
                pools = pools_result.scalars().all()
                
                if not pools:
                    logger.info("No pools to aggregate")
                    return
                
                now = datetime.utcnow()
                hourly_created = 0
                daily_created = 0
                
                for pool in pools:
                    # ===== HOURLY AGGREGATION =====
                    # Find the latest hourly aggregate
                    latest_hourly = await db.execute(
                        select(PoolHealthHourly)
                        .where(PoolHealthHourly.pool_id == pool.id)
                        .order_by(PoolHealthHourly.hour_start.desc())
                        .limit(1)
                    )
                    last_hourly = latest_hourly.scalar_one_or_none()
                    
                    # Start from last aggregate or 7 days ago
                    if last_hourly:
                        start_time = last_hourly.hour_start + timedelta(hours=1)
                    else:
                        start_time = now - timedelta(days=7)
                    
                    # Round down to start of hour
                    start_time = start_time.replace(minute=0, second=0, microsecond=0)
                    
                    # Aggregate each complete hour
                    current_hour = start_time
                    while current_hour < now.replace(minute=0, second=0, microsecond=0):
                        hour_end = current_hour + timedelta(hours=1)
                        
                        # Get all checks for this hour
                        checks = await db.execute(
                            select(PoolHealth)
                            .where(and_(
                                PoolHealth.pool_id == pool.id,
                                PoolHealth.timestamp >= current_hour,
                                PoolHealth.timestamp < hour_end
                            ))
                        )
                        hour_checks = checks.scalars().all()
                        
                        if hour_checks:
                            # Calculate aggregates
                            total_checks = len(hour_checks)
                            uptime_checks = sum(1 for c in hour_checks if c.is_reachable)
                            response_times = [c.response_time_ms for c in hour_checks if c.response_time_ms is not None]
                            health_scores = [c.health_score for c in hour_checks if c.health_score is not None]
                            reject_rates = [c.reject_rate for c in hour_checks if c.reject_rate is not None]
                            
                            hourly_agg = PoolHealthHourly(
                                pool_id=pool.id,
                                hour_start=current_hour,
                                checks_count=total_checks,
                                avg_response_time_ms=sum(response_times) / len(response_times) if response_times else None,
                                max_response_time_ms=max(response_times) if response_times else None,
                                uptime_checks=uptime_checks,
                                uptime_percentage=(uptime_checks / total_checks * 100) if total_checks > 0 else 0,
                                avg_health_score=sum(health_scores) / len(health_scores) if health_scores else None,
                                avg_reject_rate=sum(reject_rates) / len(reject_rates) if reject_rates else None,
                                total_shares_accepted=sum(c.shares_accepted or 0 for c in hour_checks),
                                total_shares_rejected=sum(c.shares_rejected or 0 for c in hour_checks)
                            )
                            db.add(hourly_agg)
                            hourly_created += 1
                        
                        current_hour = hour_end
                    
                    # ===== DAILY AGGREGATION =====
                    # Find the latest daily aggregate
                    latest_daily = await db.execute(
                        select(PoolHealthDaily)
                        .where(PoolHealthDaily.pool_id == pool.id)
                        .order_by(PoolHealthDaily.date.desc())
                        .limit(1)
                    )
                    last_daily = latest_daily.scalar_one_or_none()
                    
                    # Start from last aggregate or 7 days ago
                    if last_daily:
                        start_date = last_daily.date + timedelta(days=1)
                    else:
                        start_date = now - timedelta(days=7)
                    
                    # Round down to start of day
                    start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
                    
                    # Aggregate each complete day
                    current_date = start_date
                    yesterday = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    
                    while current_date < yesterday:
                        date_end = current_date + timedelta(days=1)
                        
                        # Get all checks for this day
                        checks = await db.execute(
                            select(PoolHealth)
                            .where(and_(
                                PoolHealth.pool_id == pool.id,
                                PoolHealth.timestamp >= current_date,
                                PoolHealth.timestamp < date_end
                            ))
                        )
                        day_checks = checks.scalars().all()
                        
                        if day_checks:
                            # Calculate aggregates
                            total_checks = len(day_checks)
                            uptime_checks = sum(1 for c in day_checks if c.is_reachable)
                            downtime_checks = total_checks - uptime_checks
                            # Estimate downtime: assume 5-min check interval
                            downtime_minutes = downtime_checks * 5
                            
                            response_times = [c.response_time_ms for c in day_checks if c.response_time_ms is not None]
                            health_scores = [c.health_score for c in day_checks if c.health_score is not None]
                            reject_rates = [c.reject_rate for c in day_checks if c.reject_rate is not None]
                            
                            daily_agg = PoolHealthDaily(
                                pool_id=pool.id,
                                date=current_date,
                                checks_count=total_checks,
                                avg_response_time_ms=sum(response_times) / len(response_times) if response_times else None,
                                max_response_time_ms=max(response_times) if response_times else None,
                                uptime_checks=uptime_checks,
                                uptime_percentage=(uptime_checks / total_checks * 100) if total_checks > 0 else 0,
                                avg_health_score=sum(health_scores) / len(health_scores) if health_scores else None,
                                avg_reject_rate=sum(reject_rates) / len(reject_rates) if reject_rates else None,
                                total_shares_accepted=sum(c.shares_accepted or 0 for c in day_checks),
                                total_shares_rejected=sum(c.shares_rejected or 0 for c in day_checks),
                                downtime_minutes=downtime_minutes
                            )
                            db.add(daily_agg)
                            daily_created += 1
                        
                        current_date = date_end
                
                await db.commit()
                logger.info(
                    "Pool health aggregation complete: %s hourly, %s daily records created",
                    hourly_created,
                    daily_created,
                )
        
        except Exception as e:
            logger.exception("Failed to aggregate pool health: %s", e)
    
    async def _aggregate_miner_analytics(self):
        """Aggregate raw telemetry into hourly and daily miner analytics"""
        try:
            from core.database import AsyncSessionLocal, Telemetry, HourlyMinerAnalytics, DailyMinerAnalytics, Miner, Pool
            from sqlalchemy import select, func, and_
            from datetime import datetime, timedelta
            
            async with AsyncSessionLocal() as db:
                logger.info("Starting miner analytics aggregation")
                
                # Get all miners
                miners_result = await db.execute(select(Miner))
                miners = miners_result.scalars().all()
                
                hourly_created = 0
                daily_created = 0
                
                for miner in miners:
                    # Determine coin type based on miner type or pool
                    coin = "BTC"  # Default
                    
                    # ========== HOURLY AGGREGATION ==========
                    # Find last hourly aggregation for this miner
                    last_hourly = await db.execute(
                        select(HourlyMinerAnalytics)
                        .where(HourlyMinerAnalytics.miner_id == miner.id)
                        .order_by(HourlyMinerAnalytics.hour_start.desc())
                        .limit(1)
                    )
                    last_hourly_record = last_hourly.scalar_one_or_none()
                    
                    # Start from last aggregation or 30 days ago
                    if last_hourly_record:
                        start_time = last_hourly_record.hour_start + timedelta(hours=1)
                    else:
                        start_time = datetime.utcnow() - timedelta(days=30)
                    
                    # Aggregate hour by hour
                    current_hour = start_time.replace(minute=0, second=0, microsecond=0)
                    now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
                    
                    while current_hour < now:
                        hour_end = current_hour + timedelta(hours=1)
                        
                        # Get all telemetry for this hour
                        telemetry_result = await db.execute(
                            select(Telemetry)
                            .where(and_(
                                Telemetry.miner_id == miner.id,
                                Telemetry.timestamp >= current_hour,
                                Telemetry.timestamp < hour_end
                            ))
                            .order_by(Telemetry.timestamp)
                        )
                        telemetry_records = telemetry_result.scalars().all()
                        
                        if telemetry_records:
                            # Calculate aggregates
                            hashrates = [t.hashrate for t in telemetry_records if t.hashrate is not None]
                            powers = [t.power_watts for t in telemetry_records if t.power_watts is not None]
                            temps = [t.temperature for t in telemetry_records if t.temperature is not None]
                            
                            if hashrates:
                                avg_hashrate_gh = sum(hashrates) / len(hashrates)
                                
                                # Calculate total hashes (avg_hashrate * uptime_seconds)
                                uptime_seconds = len(telemetry_records) * 60  # Assuming 1min intervals
                                total_hashes_gh = (avg_hashrate_gh * uptime_seconds) / 3600  # Convert to GH
                                
                                # Aggregate shares
                                total_accepted = sum(t.shares_accepted or 0 for t in telemetry_records if t.shares_accepted is not None)
                                total_rejected = sum(t.shares_rejected or 0 for t in telemetry_records if t.shares_rejected is not None)
                                
                                # Calculate mode distribution and changes
                                mode_minutes = {}
                                mode_changes = 0
                                prev_mode = None
                                
                                for t in telemetry_records:
                                    if t.mode:
                                        mode_minutes[t.mode] = mode_minutes.get(t.mode, 0) + 1
                                        if prev_mode and prev_mode != t.mode:
                                            mode_changes += 1
                                        prev_mode = t.mode
                                
                                # Dominant mode = most minutes
                                dominant_mode = max(mode_minutes.items(), key=lambda x: x[1])[0] if mode_minutes else None
                                mode_distribution = mode_minutes if mode_minutes else None
                                
                                # Get pool_id (from first record with pool info)
                                pool_id = None
                                for t in telemetry_records:
                                    if t.pool_in_use:
                                        # Try to match pool by URL/user
                                        pool_result = await db.execute(
                                            select(Pool).where(Pool.url.like(f"%{t.pool_in_use}%")).limit(1)
                                        )
                                        pool = pool_result.scalar_one_or_none()
                                        if pool:
                                            pool_id = pool.id
                                            break
                                
                                # Calculate derived metrics
                                avg_power = sum(powers) / len(powers) if powers else None
                                avg_temp = sum(temps) / len(temps) if temps else None
                                watts_per_gh = avg_power / avg_hashrate_gh if avg_power and avg_hashrate_gh else None
                                reject_rate = (total_rejected * 100.0 / (total_accepted + total_rejected)) if (total_accepted + total_rejected) > 0 else None
                                hashes_per_share = total_hashes_gh / total_accepted if total_accepted > 0 else None
                                
                                # Create hourly record
                                hourly_agg = HourlyMinerAnalytics(
                                    miner_id=miner.id,
                                    pool_id=pool_id,
                                    coin=coin,
                                    hour_start=current_hour,
                                    mode=dominant_mode,
                                    mode_changes=mode_changes,
                                    mode_distribution=mode_distribution,
                                    total_hashes_gh=total_hashes_gh,
                                    avg_hashrate_gh=avg_hashrate_gh,
                                    peak_hashrate_gh=max(hashrates),
                                    min_hashrate_gh=min(hashrates),
                                    uptime_seconds=uptime_seconds,
                                    shares_accepted=total_accepted,
                                    shares_rejected=total_rejected,
                                    avg_power_watts=avg_power,
                                    min_power_watts=min(powers) if powers else None,
                                    max_power_watts=max(powers) if powers else None,
                                    avg_chip_temp_c=avg_temp,
                                    max_chip_temp_c=max(temps) if temps else None,
                                    watts_per_gh=watts_per_gh,
                                    hashes_per_share=hashes_per_share,
                                    reject_rate_percent=reject_rate
                                )
                                db.add(hourly_agg)
                                hourly_created += 1
                        
                        current_hour = hour_end
                    
                    # ========== DAILY AGGREGATION ==========
                    # Find last daily aggregation
                    last_daily = await db.execute(
                        select(DailyMinerAnalytics)
                        .where(DailyMinerAnalytics.miner_id == miner.id)
                        .order_by(DailyMinerAnalytics.date.desc())
                        .limit(1)
                    )
                    last_daily_record = last_daily.scalar_one_or_none()
                    
                    # Start from last aggregation or 90 days ago
                    if last_daily_record:
                        start_date = last_daily_record.date + timedelta(days=1)
                    else:
                        start_date = datetime.utcnow() - timedelta(days=90)
                    
                    # Aggregate day by day from hourly data
                    current_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
                    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                    
                    while current_date < today:
                        date_end = current_date + timedelta(days=1)
                        
                        # Get hourly records for this day
                        hourly_result = await db.execute(
                            select(HourlyMinerAnalytics)
                            .where(and_(
                                HourlyMinerAnalytics.miner_id == miner.id,
                                HourlyMinerAnalytics.hour_start >= current_date,
                                HourlyMinerAnalytics.hour_start < date_end
                            ))
                        )
                        hourly_records = hourly_result.scalars().all()
                        
                        if hourly_records:
                            # Aggregate daily metrics
                            total_hashes_th = sum(h.total_hashes_gh for h in hourly_records) / 1000.0  # Convert to TH
                            uptime_hours = sum(h.uptime_seconds for h in hourly_records) / 3600.0
                            
                            hashrates = [h.avg_hashrate_gh for h in hourly_records]
                            powers = [h.avg_power_watts for h in hourly_records if h.avg_power_watts is not None]
                            temps = [h.avg_chip_temp_c for h in hourly_records if h.avg_chip_temp_c is not None]
                            
                            # Mode distribution
                            mode_counts = {}
                            for h in hourly_records:
                                if h.mode:
                                    mode_counts[h.mode] = mode_counts.get(h.mode, 0) + (h.uptime_seconds / 3600.0)
                            
                            # Calculate daily aggregates
                            avg_hashrate_gh = sum(hashrates) / len(hashrates)
                            avg_power = sum(powers) / len(powers) if powers else None
                            total_energy_kwh = (avg_power * uptime_hours / 1000.0) if avg_power else None
                            avg_watts_per_gh = avg_power / avg_hashrate_gh if avg_power and avg_hashrate_gh else None
                            
                            total_shares = sum(h.shares_accepted for h in hourly_records)
                            total_rejects = sum(h.shares_rejected for h in hourly_records)
                            avg_reject_rate = (total_rejects * 100.0 / (total_shares + total_rejects)) if (total_shares + total_rejects) > 0 else None
                            
                            # Create daily record
                            daily_agg = DailyMinerAnalytics(
                                miner_id=miner.id,
                                coin=coin,
                                date=current_date,
                                total_hashes_th=total_hashes_th,
                                avg_hashrate_gh=avg_hashrate_gh,
                                peak_hashrate_gh=max(h.peak_hashrate_gh for h in hourly_records if h.peak_hashrate_gh),
                                uptime_hours=uptime_hours,
                                total_shares_accepted=total_shares,
                                total_shares_rejected=total_rejects,
                                avg_reject_rate_percent=avg_reject_rate,
                                best_share_difficulty=max((h.best_share_difficulty for h in hourly_records if h.best_share_difficulty), default=None),
                                avg_power_watts=avg_power,
                                max_power_watts=max(powers) if powers else None,
                                total_energy_kwh=total_energy_kwh,
                                avg_temp_c=sum(temps) / len(temps) if temps else None,
                                max_temp_c=max(temps) if temps else None,
                                avg_watts_per_gh=avg_watts_per_gh,
                                mode_distribution=mode_counts
                            )
                            db.add(daily_agg)
                            daily_created += 1
                        
                        current_date = date_end
                
                await db.commit()
                logger.info(
                    "Miner analytics aggregation complete: %s hourly, %s daily records created",
                    hourly_created,
                    daily_created,
                )
        
        except Exception as e:
            logger.exception("Failed to aggregate miner analytics: %s", e)
    
    async def _sync_avalon_pool_slots(self):
        """Sync Avalon Nano pool slot configurations"""
        try:
            from core.database import AsyncSessionLocal
            from core.pool_slots import sync_avalon_nano_pool_slots
            
            async with AsyncSessionLocal() as db:
                await sync_avalon_nano_pool_slots(db)
        
        except Exception as e:
            logger.error(f"Failed to sync Avalon pool slots: {e}")
            logger.exception("Avalon pool slot sync error")
    
    async def _monitor_ha_keepalive(self):
        """Monitor Home Assistant connectivity and send alerts if down"""
        try:
            from core.database import AsyncSessionLocal, HomeAssistantConfig
            from integrations.homeassistant import HomeAssistantIntegration
            from core.notifications import NotificationService
            from datetime import datetime, timedelta
            from sqlalchemy import select
            
            async with AsyncSessionLocal() as db:
                # Get HA config
                result = await db.execute(select(HomeAssistantConfig))
                ha_config = result.scalar_one_or_none()
                
                if not ha_config:
                    return
                
                # Only monitor if keepalive is enabled
                if not ha_config.keepalive_enabled:
                    return
                
                ha_config.keepalive_last_check = datetime.utcnow()
                
                # Test connection
                ha_integration = HomeAssistantIntegration(ha_config.base_url, ha_config.access_token)
                success = await ha_integration.test_connection()
                
                now = datetime.utcnow()
                
                if success:
                    # Connection successful
                    was_down = ha_config.keepalive_downtime_start is not None
                    downtime_start = ha_config.keepalive_downtime_start
                    
                    ha_config.keepalive_last_success = now
                    
                    # Send recovery notification if was previously down
                    if was_down and downtime_start is not None:
                        downtime_duration = (now - downtime_start).total_seconds()
                        minutes_down = int(downtime_duration / 60)
                        
                        try:
                            notification_service = NotificationService()
                            await notification_service.send_to_all_channels(
                                message=(
                                    "🟢 Home Assistant Online\n"
                                    f"Home Assistant is back online after {minutes_down} minute(s) of downtime."
                                ),
                                alert_type="ha_offline"
                            )
                            logger.info(f"✅ Home Assistant recovered after {minutes_down} minutes")
                        except Exception as e:
                            logger.error(f"Failed to send HA recovery notification: {e}")
                    
                    # Reset downtime tracking (always do this, even if notification fails)
                    ha_config.keepalive_downtime_start = None
                    ha_config.keepalive_alerts_sent = 0
                
                else:
                    # Connection failed
                    if ha_config.keepalive_downtime_start is None:
                        # First failure - start tracking and send immediate notification
                        ha_config.keepalive_downtime_start = now
                        ha_config.keepalive_alerts_sent = 1  # Mark first alert as sent
                        
                        try:
                            notification_service = NotificationService()
                            results = await notification_service.send_to_all_channels(
                                message=(
                                    "🔴 Home Assistant Offline\n"
                                    f"Home Assistant has gone offline. Unable to reach {ha_config.base_url}"
                                ),
                                alert_type="ha_offline"
                            )
                            if results:
                                logger.warning(f"⚠️  Home Assistant offline - immediate alert sent to: {list(results.keys())}")
                            else:
                                logger.warning(f"⚠️  Home Assistant offline - NO notification channels enabled!")
                        except Exception as e:
                            logger.error(f"Failed to send HA offline notification: {e}")
                    
                    else:
                        # Calculate downtime
                        downtime_seconds = (now - ha_config.keepalive_downtime_start).total_seconds()
                        downtime_minutes = int(downtime_seconds / 60)
                        
                        # Escalating follow-up alerts: 5 min, 15 min, 30 min
                        alert_thresholds = [5, 15, 30]
                        should_alert = False
                        
                        for threshold in alert_thresholds:
                            # Check if we've crossed this threshold and haven't sent this alert yet
                            threshold_index = alert_thresholds.index(threshold)
                            # alerts_sent = 1 (immediate), so 5min is index 1, 15min is index 2, etc.
                            if downtime_minutes >= threshold and ha_config.keepalive_alerts_sent <= (threshold_index + 1):
                                should_alert = True
                                ha_config.keepalive_alerts_sent = threshold_index + 2
                                break
                        
                        if should_alert:
                            try:
                                notification_service = NotificationService()
                                
                                if downtime_minutes >= 30:
                                    severity = "🔴🔴"
                                elif downtime_minutes >= 15:
                                    severity = "🔴"
                                else:
                                    severity = "🟠"
                                
                                results = await notification_service.send_to_all_channels(
                                    message=(
                                        f"{severity} Home Assistant Still Offline\n"
                                        f"Home Assistant has been offline for {downtime_minutes} minute(s). "
                                        f"Still unable to reach {ha_config.base_url}"
                                    ),
                                    alert_type="ha_offline"
                                )
                                if results:
                                    logger.warning(f"⚠️  Home Assistant offline for {downtime_minutes} minutes (escalation alert sent to: {list(results.keys())})")
                                else:
                                    logger.warning(f"⚠️  Home Assistant offline for {downtime_minutes} minutes (NO notification channels enabled!)")
                            except Exception as e:
                                logger.error(f"Failed to send HA offline escalation notification: {e}")
                        else:
                            logger.debug(f"Home Assistant offline for {downtime_minutes} minutes (no new alert)")
                
                await db.commit()
        
        except Exception as e:
            logger.error(f"Failed to monitor Home Assistant keepalive: {e}")
            logger.exception("Home Assistant keepalive monitoring failed")
    
    async def _poll_ha_device_states(self):
        """Poll Home Assistant device states every 5 minutes"""
        try:
            from core.database import AsyncSessionLocal, HomeAssistantConfig, HomeAssistantDevice
            from integrations.homeassistant import HomeAssistantIntegration
            from sqlalchemy import select
            
            async with AsyncSessionLocal() as db:
                # Check if HA is configured and enabled
                result = await db.execute(select(HomeAssistantConfig))
                ha_config = result.scalar_one_or_none()
                
                if not ha_config or not ha_config.enabled:
                    return
                
                # Get all enrolled devices (only poll devices we care about)
                result = await db.execute(
                    select(HomeAssistantDevice).where(HomeAssistantDevice.enrolled == True)
                )
                devices = result.scalars().all()
                
                if not devices:
                    return
                
                # Initialize HA integration
                ha = HomeAssistantIntegration(ha_config.base_url, ha_config.access_token)
                
                # Poll each device state
                updated_count = 0
                for device in devices:
                    try:
                        state = await ha.get_device_state(device.entity_id)
                        
                        if state:
                            # Only update if state has changed
                            if device.current_state != state.state:
                                device.current_state = state.state
                                device.last_state_change = datetime.utcnow()
                                updated_count += 1
                            
                    except Exception as e:
                        logger.warning(f"Failed to poll state for {device.entity_id}: {e}")
                        # Mark as unavailable if we can't reach it
                        if device.current_state != "unavailable":
                            device.current_state = "unavailable"
                            device.last_state_change = datetime.utcnow()
                            updated_count += 1
                
                await db.commit()
                
                if updated_count > 0:
                    logger.info(f"📊 Updated {updated_count}/{len(devices)} HA device states")
        
        except Exception as e:
            logger.error(f"Failed to poll Home Assistant device states: {e}")
    
    async def _reconcile_ha_device_states(self):
        """Check devices that were turned OFF and reconcile if still receiving telemetry"""
        try:
            from core.database import AsyncSessionLocal, HomeAssistantDevice, HomeAssistantConfig, Telemetry, PriceBandStrategyConfig, MinerStrategy
            from integrations.homeassistant import HomeAssistantIntegration
            from core.notifications import NotificationService
            from sqlalchemy import select
            from datetime import timedelta
            import asyncio
            
            async with AsyncSessionLocal() as db:
                # Get HA config
                ha_config_result = await db.execute(
                    select(HomeAssistantConfig).where(HomeAssistantConfig.enabled == True)
                )
                ha_config = ha_config_result.scalars().first()
                if not ha_config:
                    return
                
                # Check if champion mode is active (skip reconciliation for current champion)
                strategy_result = await db.execute(select(PriceBandStrategyConfig).limit(1))
                strategy = strategy_result.scalar_one_or_none()
                
                champion_miner_id = None
                if strategy and strategy.champion_mode_enabled and strategy.current_champion_miner_id:
                    champion_miner_id = strategy.current_champion_miner_id
                    logger.info(f"🏆 Champion mode active: protecting champion miner #{champion_miner_id} from reconciliation")
                
                # Get devices that should be OFF and have linked miners
                now = datetime.utcnow()
                five_minutes_ago = now - timedelta(minutes=5)
                three_minutes_ago = now - timedelta(minutes=3)
                
                devices_result = await db.execute(
                    select(HomeAssistantDevice).where(
                        HomeAssistantDevice.current_state == "off",
                        HomeAssistantDevice.miner_id.isnot(None),
                        HomeAssistantDevice.last_off_command_timestamp.isnot(None),
                        HomeAssistantDevice.last_off_command_timestamp <= five_minutes_ago
                    )
                )
                devices = devices_result.scalars().all()
                
                if not devices:
                    return
                
                ha_integration = HomeAssistantIntegration(
                    base_url=ha_config.base_url,
                    access_token=ha_config.access_token
                )
                
                notification_service = NotificationService()
                
                for ha_device in devices:
                    # Skip reconciliation for miners no longer enrolled in strategy
                    if ha_device.miner_id is not None:
                        enrolled_result = await db.execute(
                            select(MinerStrategy)
                            .where(MinerStrategy.miner_id == ha_device.miner_id)
                            .where(MinerStrategy.strategy_enabled == True)
                            .limit(1)
                        )
                        if enrolled_result.scalar_one_or_none() is None:
                            logger.info(
                                f"⏭️  Skipping HA reconciliation for {ha_device.name} (miner #{ha_device.miner_id}) - "
                                "miner not enrolled in strategy"
                            )
                            ha_device.last_off_command_timestamp = None
                            await db.commit()
                            continue

                    # Skip reconciliation for active champion miner
                    if champion_miner_id and ha_device.miner_id == champion_miner_id:
                        logger.info(
                            f"⏭️  Skipping reconciliation for {ha_device.name} (miner #{ha_device.miner_id}) - "
                            "active champion in champion mode"
                        )
                        continue
                    
                    # Check if miner has sent telemetry in last 3 minutes
                    telemetry_result = await db.execute(
                        select(Telemetry)
                        .where(
                            Telemetry.miner_id == ha_device.miner_id,
                            Telemetry.timestamp >= three_minutes_ago
                        )
                        .limit(1)
                    )
                    recent_telemetry = telemetry_result.scalars().first()
                    
                    if recent_telemetry:
                        # Device is still sending telemetry despite being OFF - reconcile!
                        logger.warning(
                            f"⚠️  HA Device {ha_device.name} ({ha_device.entity_id}) is OFF but miner "
                            f"#{ha_device.miner_id} still sending telemetry. Reconciling..."
                        )
                        
                        # Cycle device: ON → wait 10s → OFF
                        on_success = await ha_integration.turn_on(ha_device.entity_id)
                        if on_success:
                            logger.info(f"🔄 Turned ON {ha_device.name} for reconciliation")
                            await asyncio.sleep(10)
                            
                            off_success = await ha_integration.turn_off(ha_device.entity_id)
                            if off_success:
                                ha_device.last_off_command_timestamp = datetime.utcnow()
                                ha_device.current_state = "off"
                                ha_device.last_state_change = datetime.utcnow()
                                await db.commit()
                                
                                logger.info(f"✅ Reconciled {ha_device.name} - turned OFF after 10s delay")
                                
                                # Send notification
                                await notification_service.send_to_all_channels(
                                    message=(
                                        "🔄 HA Device Reconciled\n"
                                        f"Device {ha_device.name} was stuck ON despite OFF command. "
                                        "Cycled device (ON → wait 10s → OFF) to force shutdown."
                                    ),
                                    alert_type="ha_device_reconciliation"
                                )
                            else:
                                logger.error(f"❌ Failed to turn OFF {ha_device.name} during reconciliation")
                        else:
                            logger.error(f"❌ Failed to turn ON {ha_device.name} during reconciliation")
        
        except Exception as e:
            logger.error(f"Error reconciling HA device states: {e}", exc_info=True)
    
    async def _execute_price_band_strategy(self):
        """Execute Price Band Strategy every minute"""
        try:
            logger.info("Executing Price Band Strategy")
            from core.database import AsyncSessionLocal, PriceBandStrategyConfig
            from core.price_band_strategy import PriceBandStrategy
            from sqlalchemy import select
            from datetime import datetime
            
            async with AsyncSessionLocal() as db:
                report = await PriceBandStrategy.execute_strategy(db)
                
                if report.get("enabled"):
                    logger.info(f"Price Band Strategy executed: {report}")
                    
                    # Check if we just entered OFF state and should trigger aggregation
                    if report.get("band") and "OFF" in report.get("band", ""):
                        strategy_result = await db.execute(select(PriceBandStrategyConfig).limit(1))
                        strategy = strategy_result.scalar_one_or_none()
                        
                        if strategy:
                            # Check if aggregation hasn't run in the last 22 hours
                            should_aggregate = False
                            if strategy.last_aggregation_time is None:
                                should_aggregate = True
                            else:
                                hours_since_agg = (datetime.utcnow() - strategy.last_aggregation_time).total_seconds() / 3600
                                if hours_since_agg >= 22:
                                    should_aggregate = True
                            
                            if should_aggregate:
                                logger.info("🗜️ OFF state detected - triggering maintenance (miners idle)")

                                success = await self._run_maintenance_cycle(
                                    reason="strategy OFF state",
                                    max_retries=3,
                                    retry_interval=1800,
                                    include_attempt_in_success=True,
                                )

                                if success:
                                    strategy.last_aggregation_time = datetime.utcnow()
                                    await db.commit()
                                    logger.info("✅ Maintenance complete during OFF period")
                            else:
                                logger.debug(f"⏭️ Skipping maintenance (last ran {hours_since_agg:.1f}h ago)")
                else:
                    logger.debug(f"Price Band Strategy: {report.get('message', 'disabled')}")
        
        except Exception as e:
            logger.error(f"Failed to execute Price Band Strategy: {e}")
            logger.exception("Price Band Strategy execution failed")
    
    async def _reconcile_price_band_strategy(self):
        """Reconcile Price Band Strategy - ensure miners match intended state"""
        try:
            from core.database import AsyncSessionLocal
            from core.price_band_strategy import PriceBandStrategy
            
            async with AsyncSessionLocal() as db:
                report = await PriceBandStrategy.reconcile_strategy(db)
                
                if report.get("reconciled"):
                    logger.info(f"Price Band Strategy reconciliation: {report}")
        
        except Exception as e:
            logger.error(f"Failed to reconcile Price Band Strategy: {e}")
            logger.exception("Price Band Strategy reconciliation failed")
    
    async def _purge_old_high_diff_shares(self):
        """Purge high diff shares older than 180 days"""
        from core.high_diff_tracker import cleanup_old_shares
        from core.database import AsyncSessionLocal
        
        try:
            async with AsyncSessionLocal() as db:
                await cleanup_old_shares(db, days=180)
        except Exception as e:
            logger.error(f"Failed to purge old high diff shares: {e}", exc_info=True)
    
    async def _push_to_cloud(self):
        """Push telemetry to HMM Cloud"""
        from core.database import AsyncSessionLocal, Miner, Telemetry
        from sqlalchemy import select
        
        cloud_service = get_cloud_service()
        if not cloud_service or not cloud_service.enabled:
            logger.debug("Cloud push skipped (not enabled)")
            return
        
        try:
            async with AsyncSessionLocal() as db:
                # Get all miners
                result = await db.execute(select(Miner))
                miners = result.scalars().all()
                
                # Build telemetry payload with latest telemetry for each miner
                miners_data = []
                for miner in miners:
                    # Get latest telemetry for this miner
                    telemetry_result = await db.execute(
                        select(Telemetry)
                        .where(Telemetry.miner_id == miner.id)
                        .order_by(Telemetry.timestamp.desc())
                        .limit(1)
                    )
                    latest_telemetry = telemetry_result.scalar_one_or_none()
                    
                    # Check if telemetry is recent (within last 10 minutes)
                    has_recent_data = False
                    if latest_telemetry:
                        telemetry_age_seconds = (datetime.utcnow() - latest_telemetry.timestamp).total_seconds()
                        has_recent_data = telemetry_age_seconds <= 600  # 10 minutes
                    
                    # Always include the miner, but use zeros if data is stale/missing
                    if has_recent_data and latest_telemetry is not None:
                        lt = latest_telemetry
                        # Normalize hashrate to GH/s for cloud consistency
                        hashrate_ghs = 0.0
                        if lt.hashrate:
                            # Get unit from column or default to GH/s
                            unit = lt.hashrate_unit or "GH/s"
                            hashrate_value = float(lt.hashrate)
                            
                            # Convert to GH/s
                            if unit == "KH/s":
                                hashrate_ghs = hashrate_value / 1_000_000  # KH/s to GH/s
                            elif unit == "MH/s":
                                hashrate_ghs = hashrate_value / 1_000  # MH/s to GH/s
                            elif unit == "GH/s":
                                hashrate_ghs = hashrate_value
                            elif unit == "TH/s":
                                hashrate_ghs = hashrate_value * 1_000  # TH/s to GH/s
                            else:
                                hashrate_ghs = hashrate_value  # Assume GH/s if unknown
                        
                        miners_data.append({
                            "name": miner.name,
                            "type": miner.miner_type,
                            "ip_address": miner.ip_address,
                            "telemetry": {
                                "timestamp": int(lt.timestamp.timestamp()),
                                "hashrate": hashrate_ghs,  # Always in GH/s
                                "temperature": float(lt.temperature) if lt.temperature else None,
                                "power": float(lt.power_watts) if lt.power_watts else 0.0,
                                "shares_accepted": lt.shares_accepted or 0,
                                "shares_rejected": lt.shares_rejected or 0
                            }
                        })
                    else:
                        # Send miner with zero values (offline/stale data)
                        miners_data.append({
                            "name": miner.name,
                            "type": miner.miner_type,
                            "ip_address": miner.ip_address,
                            "telemetry": {
                                "timestamp": int(datetime.utcnow().timestamp()),
                                "hashrate": 0.0,
                                "temperature": None,
                                "power": 0.0,
                                "shares_accepted": 0,
                                "shares_rejected": 0
                            }
                        })
                        logger.debug(f"Pushing {miner.name} with zeros (offline/stale)")
                
                # Calculate aggregated totals
                total_hashrate_ghs = sum(m["telemetry"]["hashrate"] for m in miners_data)
                total_power_watts = sum(m["telemetry"]["power"] for m in miners_data)
                miners_online = sum(1 for m in miners_data if m["telemetry"]["hashrate"] > 0.000001)
                
                # Calculate 24h cost using actual energy prices (same logic as dashboard)
                from core.config import app_config
                cost_24h_gbp = 0.0
                try:
                    if app_config.get("octopus_agile.enabled", False):
                        # Get energy prices for last 24 hours
                        cutoff_24h = datetime.utcnow() - timedelta(hours=24)
                        price_result = await db.execute(
                            select(EnergyPrice)
                            .where(EnergyPrice.valid_from >= cutoff_24h)
                            .order_by(EnergyPrice.valid_from)
                        )
                        energy_prices = price_result.scalars().all()
                        
                        # Helper to find price for timestamp
                        def get_price(ts):
                            for price in energy_prices:
                                if price.valid_from <= ts < price.valid_to:
                                    return price.price_pence
                            return None
                        
                        # Calculate cost for each miner
                        total_cost_pence = 0.0
                        for miner in miners:
                            telem_result = await db.execute(
                                select(Telemetry.power_watts, Telemetry.timestamp)
                                .where(Telemetry.miner_id == miner.id)
                                .where(Telemetry.timestamp > cutoff_24h)
                                .order_by(Telemetry.timestamp)
                            )
                            telem_records = telem_result.all()
                            
                            for i, (power, ts) in enumerate(telem_records):
                                # Fallback to manual power if no auto-detected power
                                if not power or power <= 0:
                                    if miner.manual_power_watts:
                                        power = miner.manual_power_watts
                                    else:
                                        continue
                                
                                price_pence = get_price(ts)
                                if not price_pence:
                                    continue
                                
                                # Calculate duration
                                if i < len(telem_records) - 1:
                                    next_ts = telem_records[i + 1][1]
                                    duration_hours = (next_ts - ts).total_seconds() / 3600.0
                                    
                                    # Cap duration at 10 minutes to prevent counting offline gaps
                                    # Telemetry is recorded every 30s, so >10min gap = miner was offline
                                    max_duration_hours = 10.0 / 60.0  # 10 minutes in hours
                                    if duration_hours > max_duration_hours:
                                        duration_hours = max_duration_hours
                                else:
                                    duration_hours = 30.0 / 3600.0  # 30 seconds
                                
                                kwh = (power / 1000.0) * duration_hours
                                total_cost_pence += kwh * price_pence
                        
                        cost_24h_gbp = total_cost_pence / 100.0
                except Exception as e:
                    logger.warning(f"Failed to calculate 24h cost: {e}")
                
                # Always push (even if empty) to maintain keepalive/heartbeat
                success = await cloud_service.push_telemetry(
                    miners_data,
                    aggregate={
                        "total_hashrate_ghs": total_hashrate_ghs,
                        "total_power_watts": total_power_watts,
                        "miners_online": miners_online,
                        "total_miners": len(miners_data),
                        "cost_24h_gbp": cost_24h_gbp
                    }
                )
                if success:
                    if miners_data:
                        logger.info(f"✓ Pushed {len(miners_data)} miners to cloud")
                    else:
                        logger.debug("✓ Sent keepalive to cloud (no miners)")
                else:
                    logger.warning(f"✗ Failed to push to cloud ({len(miners_data)} miners)")
                    
        except Exception as e:
            logger.error(f"Failed to push to cloud: {e}", exc_info=True)

    # Watermark: track highest local audit_log id already successfully pushed
    _audit_log_watermark: int = 0

    async def _push_audit_logs_to_cloud(self):
        """Push new audit log entries to HMM Cloud (incremental, deduped by local_id)."""
        from core.database import AsyncSessionLocal, AuditLog as LocalAuditLog

        cloud_service = get_cloud_service()
        if not cloud_service or not cloud_service.enabled:
            logger.debug("Audit log cloud push skipped (not enabled)")
            return

        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(LocalAuditLog)
                    .where(LocalAuditLog.id > self.__class__._audit_log_watermark)
                    .order_by(LocalAuditLog.id.asc())
                    .limit(50)
                )
                logs = result.scalars().all()

            if not logs:
                logger.debug("No new audit logs to push to cloud")
                return

            payload = []
            for log in logs:
                payload.append({
                    "local_id": log.id,
                    "timestamp": int(log.timestamp.timestamp()),
                    "action": log.action,
                    "resource_type": log.resource_type,
                    "resource_name": log.resource_name,
                    "status": log.status,
                    "changes": log.changes,
                    "error_message": log.error_message,
                })

            success = await cloud_service.push_audit_logs(payload)
            if success:
                self.__class__._audit_log_watermark = max(log.id for log in logs)
                logger.info(f"✓ Pushed {len(payload)} audit log entries to cloud")
            else:
                logger.warning("✗ Failed to push audit logs to cloud")

        except Exception as e:
            logger.error(f"Failed to push audit logs to cloud: {e}", exc_info=True)

    async def _compute_hourly_metrics(self):
        """Compute metrics for the previous hour"""
        if self._should_skip_non_critical_job("compute_hourly_metrics"):
            return

        try:
            from core.metrics import MetricsEngine
            from core.database import AsyncSessionLocal
            from datetime import datetime, timedelta
            
            # Calculate previous hour (floor to hour)
            now = datetime.utcnow()
            previous_hour = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            
            async with AsyncSessionLocal() as db:
                engine = MetricsEngine(db)
                await engine.compute_hourly_metrics(previous_hour)
                await db.commit()
            
            logger.info(f"✅ Computed hourly metrics for {previous_hour.strftime('%Y-%m-%d %H:00')}")
        except Exception as e:
            logger.error(f"❌ Failed to compute hourly metrics: {e}", exc_info=True)

    async def _compute_daily_metrics(self):
        """Compute daily metrics for the previous day"""
        if self._should_skip_non_critical_job("compute_daily_metrics"):
            return

        try:
            from core.metrics import MetricsEngine
            from core.database import AsyncSessionLocal
            from datetime import datetime, timedelta
            
            # Calculate previous day (midnight)
            now = datetime.utcnow()
            previous_day = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            
            async with AsyncSessionLocal() as db:
                engine = MetricsEngine(db)
                await engine.compute_daily_metrics(previous_day)
                await db.commit()
            
            logger.info(f"✅ Computed daily metrics for {previous_day.strftime('%Y-%m-%d')}")
        except Exception as e:
            logger.error(f"❌ Failed to compute daily metrics: {e}", exc_info=True)

    async def _cleanup_old_metrics(self):
        """Cleanup metrics older than 1 year"""
        if self._should_skip_non_critical_job("cleanup_old_metrics"):
            return

        try:
            from core.metrics import MetricsEngine
            from core.database import AsyncSessionLocal
            
            async with AsyncSessionLocal() as db:
                engine = MetricsEngine(db)
                deleted_count = await engine.cleanup_old_metrics(days=365)
                await db.commit()
            
            logger.info(f"✅ Cleaned up {deleted_count} old metrics (>365 days)")
        except Exception as e:
            logger.error(f"❌ Failed to cleanup old metrics: {e}", exc_info=True)

    async def _control_ha_device_for_energy_optimization(self, db, miner, turn_on: bool):
        """Control Home Assistant device linked to miner for energy optimization"""
        try:
            from core.database import HomeAssistantConfig, HomeAssistantDevice
            
            # Check if HA is configured
            result = await db.execute(select(HomeAssistantConfig).where(HomeAssistantConfig.enabled == True).limit(1))
            ha_config = result.scalar_one_or_none()
            if not ha_config:
                return
            
            # Find device linked to this miner
            result = await db.execute(
                select(HomeAssistantDevice)
                .where(HomeAssistantDevice.miner_id == miner.id)
                .where(HomeAssistantDevice.enrolled == True)
            )
            ha_device = result.scalar_one_or_none()
            if not ha_device:
                return
            
            # Import HA integration at runtime to avoid circular dependencies
            from integrations.homeassistant import HomeAssistantIntegration
            
            ha = HomeAssistantIntegration(ha_config.base_url, ha_config.access_token)
            
            action = "ON" if turn_on else "OFF"
            logger.info(f"⚡ Energy Optimization: turn_{action.lower()} HA device {ha_device.name} for miner {miner.name}")
            
            if turn_on:
                success = await ha.turn_on(ha_device.entity_id)
            else:
                success = await ha.turn_off(ha_device.entity_id)
            
            if success:
                ha_device.current_state = "on" if turn_on else "off"
                if not turn_on:  # Track when OFF command sent for reconciliation
                    ha_device.last_off_command_timestamp = datetime.utcnow()
                await db.commit()
                logger.info(f"✅ Energy Optimization: HA device {ha_device.name} turned {action}")
            else:
                logger.warning(f"❌ Energy Optimization: Failed to turn {action} HA device {ha_device.name}")
        
        except Exception as e:
            logger.error(f"❌ Failed to control HA device for {miner.name}: {e}")

    async def _control_ha_device_for_automation(self, db, miner, turn_on: bool):
        """Control Home Assistant device linked to miner for automation rules"""
        try:
            from core.database import HomeAssistantConfig, HomeAssistantDevice
            
            # Check if HA is configured
            result = await db.execute(select(HomeAssistantConfig).where(HomeAssistantConfig.enabled == True).limit(1))
            ha_config = result.scalar_one_or_none()
            if not ha_config:
                return
            
            # Find device linked to this miner
            result = await db.execute(
                select(HomeAssistantDevice)
                .where(HomeAssistantDevice.miner_id == miner.id)
                .where(HomeAssistantDevice.enrolled == True)
            )
            ha_device = result.scalar_one_or_none()
            if not ha_device:
                return
            
            # Import HA integration at runtime to avoid circular dependencies
            from integrations.homeassistant import HomeAssistantIntegration
            
            ha = HomeAssistantIntegration(ha_config.base_url, ha_config.access_token)
            
            action = "ON" if turn_on else "OFF"
            logger.info(f"🤖 Automation: turn_{action.lower()} HA device {ha_device.name} for miner {miner.name}")
            
            if turn_on:
                success = await ha.turn_on(ha_device.entity_id)
            else:
                success = await ha.turn_off(ha_device.entity_id)
            
            if success:
                ha_device.current_state = "on" if turn_on else "off"
                if not turn_on:  # Track when OFF command sent for reconciliation
                    ha_device.last_off_command_timestamp = datetime.utcnow()
                await db.commit()
                logger.info(f"✅ Automation: HA device {ha_device.name} turned {action}")
            else:
                logger.warning(f"❌ Automation: Failed to turn {action} HA device {ha_device.name}")
        
        except Exception as e:
            logger.error(f"❌ Failed to control HA device for {miner.name}: {e}")
    
    async def _update_miner_baselines(self):
        """Update statistical baselines for all miners"""
        from core.database import AsyncSessionLocal
        from core.anomaly_detection import update_baselines_for_all_miners
        
        try:
            async with AsyncSessionLocal() as db:
                await update_baselines_for_all_miners(db)
        except Exception as e:
            logger.error(f"❌ Failed to update miner baselines: {e}", exc_info=True)
    
    async def _check_miner_health(self):
        """Check health for all miners and detect anomalies"""
        from core.database import AsyncSessionLocal
        from core.anomaly_detection import check_all_miners_health
        
        try:
            async with AsyncSessionLocal() as db:
                await check_all_miners_health(db)
        except Exception as e:
            logger.error(f"❌ Failed to check miner health: {e}", exc_info=True)
    
    async def _train_ml_models(self):
        """Train ML anomaly detection models (weekly)"""
        from core.database import AsyncSessionLocal
        from core.ml_anomaly import train_all_models
        
        try:
            async with AsyncSessionLocal() as db:
                await train_all_models(db)
        except Exception as e:
            logger.error(f"❌ Failed to train ML models: {e}", exc_info=True)
    
    async def _backup_database(self):
        """Backup PostgreSQL database using pg_dump"""
        from core.database import engine
        from core.config import settings, app_config
        import subprocess
        from datetime import datetime
        
        try:
            backup_dir = settings.CONFIG_DIR / "backups"
            backup_dir.mkdir(exist_ok=True)
            
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            
            # PostgreSQL: Use pg_dump
            pg_config = _as_dict(app_config.get("database.postgresql", {}))
            
            host = pg_config.get("host", "localhost")
            port = pg_config.get("port", 5432)
            database = pg_config.get("database", "hmm")
            username = pg_config.get("username", "hmm")
            password = pg_config.get("password", "")
            
            backup_file = backup_dir / f"hmm_pg_{timestamp}.sql"
            
            # Use pg_dump via docker exec or direct command
            env = os.environ.copy()
            env['PGPASSWORD'] = password
            
            result = subprocess.run(
                ['pg_dump', '-h', host, '-p', str(port), '-U', username, '-d', database, '-f', str(backup_file)],
                env=env,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            if result.returncode == 0:
                # Compress backup
                subprocess.run(['gzip', str(backup_file)], check=True)
                backup_file_gz = Path(f"{backup_file}.gz")
                
                size_mb = os.path.getsize(backup_file_gz) / (1024 * 1024)
                logger.info(f"✅ PostgreSQL backup created: {backup_file_gz} ({size_mb:.2f} MB)")
                
                # Cleanup old backups (keep last 7 days)
                self._cleanup_old_backups(backup_dir, days=7)
                
                # Send notification
                from core.notifications import send_alert
                await send_alert(
                    f"💾 PostgreSQL backup complete\n\n"
                    f"File: {backup_file_gz.name}\n"
                    f"Size: {size_mb:.2f} MB\n"
                    f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
                    alert_type="backup_status"
                )
            else:
                logger.error(f"❌ pg_dump failed: {result.stderr}")
        
        except Exception as e:
            logger.error(f"❌ Database backup failed: {e}", exc_info=True)
            
            # Send alert on failure
            from core.notifications import send_alert
            await send_alert(
                f"⚠️ Database backup FAILED\n\n"
                f"Error: {str(e)}\n"
                f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
                alert_type="backup_failure"
            )
    
    def _cleanup_old_backups(self, backup_dir: Path, days: int):
        """Remove backups older than specified days"""
        import time
        
        cutoff = time.time() - (days * 86400)
        
        for backup_file in backup_dir.glob("hmm_*.gz"):
            if backup_file.stat().st_mtime < cutoff:
                backup_file.unlink()
                logger.info(f"🗑️ Removed old backup: {backup_file.name}")
    
    async def _monitor_database_health(self):
        """Monitor database connection pool and performance"""
        from core.database import engine
        from core.db_pool_metrics import update_peaks
        from sqlalchemy import text
        
        try:
            # Get connection pool stats
            pool: Any = engine.pool
            pool_size = pool.size()
            checked_out = pool.checkedout()
            overflow = pool.overflow() if hasattr(pool, 'overflow') else 0
            
            # Calculate pool utilization
            total_capacity = pool_size + overflow
            utilization_pct = (checked_out / total_capacity * 100) if total_capacity > 0 else 0
            
            # Update high-water marks
            update_peaks(in_use=checked_out)

            # Check for pool exhaustion warning
            if utilization_pct > 80:
                logger.warning(
                    f"⚠️ Database connection pool high utilization: {utilization_pct:.1f}% "
                    f"({checked_out}/{total_capacity} connections in use)"
                )
                
                # Send alert if critical (>90%)
                if utilization_pct > 90:
                    from core.notifications import send_alert
                    await send_alert(
                        f"🚨 Database connection pool critical\n\n"
                        f"Utilization: {utilization_pct:.1f}%\n"
                        f"In use: {checked_out}/{total_capacity}\n"
                        f"Consider increasing pool_size or investigating connection leaks",
                        alert_type="database_critical"
                    )
            
            # PostgreSQL-specific monitoring
            if 'postgresql' in str(engine.url):
                async with engine.begin() as conn:
                    # Check active connections
                    result = await conn.execute(text("""
                        SELECT count(*) as active_connections
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                        AND state = 'active'
                    """))
                    row = result.fetchone()
                    active_conns = row[0] if row else 0
                    
                    # Check for long-running queries (>1 minute)
                    result = await conn.execute(text("""
                        SELECT count(*) as long_queries
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                        AND state = 'active'
                        AND query_start < NOW() - INTERVAL '1 minute'
                        AND query NOT LIKE '%pg_stat_activity%'
                    """))
                    row = result.fetchone()
                    long_queries = row[0] if row else 0
                    
                    update_peaks(in_use=checked_out, active_queries=active_conns, slow_queries=long_queries)

                    if long_queries > 0:
                        logger.warning(f"⚠️ {long_queries} long-running PostgreSQL queries detected (>1min)")
                    
                    # Check database size
                    result = await conn.execute(text("""
                        SELECT pg_database_size(current_database()) as db_size
                    """))
                    row = result.fetchone()
                    db_size_mb = (row[0] / (1024 * 1024)) if row else 0
                    
                    # Log periodic summary
                    if datetime.utcnow().minute % 15 == 0:
                        logger.info(
                            f"📊 Database health: Pool {utilization_pct:.1f}% "
                            f"({checked_out}/{total_capacity}), "
                            f"Active queries: {active_conns}, "
                            f"Size: {db_size_mb:.1f} MB"
                        )
        
        except Exception as e:
            logger.error(f"❌ Database health check failed: {e}")
    
    async def _check_index_health(self):
        """Check PostgreSQL index health, bloat, and usage"""
        from core.database import engine
        from sqlalchemy import text

        if self._should_skip_non_critical_job("check_index_health"):
            return
        
        try:
            async with engine.begin() as conn:
                # Check for unused indexes
                result = await conn.execute(text("""
                    SELECT
                        schemaname,
                        tablename,
                        indexname,
                        idx_scan as scans,
                        pg_size_pretty(pg_relation_size(indexrelid)) as size
                    FROM pg_stat_user_indexes
                    WHERE idx_scan = 0
                    AND indexrelid::regclass::text NOT LIKE '%_pkey'
                    ORDER BY pg_relation_size(indexrelid) DESC
                    LIMIT 10
                """))
                
                unused_indexes = result.fetchall()
                
                if unused_indexes:
                    logger.warning(f"⚠️ Found {len(unused_indexes)} unused indexes:")
                    for idx in unused_indexes:
                        logger.warning(f"  - {idx[2]} on {idx[1]} ({idx[4]}, 0 scans)")
                
                # Check for bloated indexes (rough estimate)
                result = await conn.execute(text("""
                    SELECT
                        tablename,
                        indexname,
                        pg_size_pretty(pg_relation_size(indexrelid)) as size,
                        idx_scan as scans
                    FROM pg_stat_user_indexes
                    WHERE pg_relation_size(indexrelid) > 10485760  -- >10MB
                    ORDER BY pg_relation_size(indexrelid) DESC
                    LIMIT 10
                """))
                
                large_indexes = result.fetchall()
                
                if large_indexes:
                    logger.info("📊 Largest indexes:")
                    for idx in large_indexes:
                        logger.info(f"  - {idx[1]} on {idx[0]}: {idx[2]} ({idx[3]} scans)")
                
                # Check for missing indexes (sequential scans on large tables)
                result = await conn.execute(text("""
                    SELECT
                        schemaname,
                        tablename,
                        seq_scan,
                        seq_tup_read,
                        idx_scan,
                        pg_size_pretty(pg_relation_size(relid)) as size
                    FROM pg_stat_user_tables
                    WHERE seq_scan > 1000
                    AND pg_relation_size(relid) > 1048576  -- >1MB
                    AND (idx_scan = 0 OR seq_scan > idx_scan * 10)
                    ORDER BY seq_tup_read DESC
                    LIMIT 5
                """))
                
                seq_scan_tables = result.fetchall()
                
                if seq_scan_tables:
                    logger.warning("⚠️ Tables with excessive sequential scans (may need indexes):")
                    for tbl in seq_scan_tables:
                        logger.warning(
                            f"  - {tbl[1]}: {tbl[2]} seq scans ({tbl[3]} rows), "
                            f"{tbl[4]} index scans, size: {tbl[5]}"
                        )
                    
                    # Send notification
                    from core.notifications import send_alert
                    await send_alert(
                        f"🔍 Index health check\n\n"
                        f"Unused indexes: {len(unused_indexes)}\n"
                        f"Tables needing indexes: {len(seq_scan_tables)}\n"
                        f"Consider running REINDEX or adding indexes",
                        alert_type="index_health"
                    )
        
        except Exception as e:
            logger.error(f"❌ Index health check failed: {e}")
    
    async def _refresh_dashboard_materialized_view(self):
        """Refresh dashboard materialized view (PostgreSQL only)"""
        from core.database import AsyncSessionLocal
        from core.postgres_optimizations import refresh_dashboard_materialized_view

        if self._should_skip_non_critical_job("refresh_dashboard_materialized_view"):
            return
        
        try:
            async with AsyncSessionLocal() as db:
                await refresh_dashboard_materialized_view(db)
        except Exception as e:
            logger.error(f"Failed to refresh dashboard materialized view: {e}")
    
    async def _ensure_future_partitions(self):
        """Ensure future telemetry partitions exist (PostgreSQL only)"""
        from core.database import AsyncSessionLocal
        from core.postgres_optimizations import ensure_future_partitions
        
        try:
            async with AsyncSessionLocal() as db:
                await ensure_future_partitions(db)
        except Exception as e:
            logger.error(f"Failed to ensure future partitions: {e}")


scheduler = SchedulerService()

# Make scheduler accessible to adapters
from adapters import set_scheduler_service
set_scheduler_service(scheduler)
