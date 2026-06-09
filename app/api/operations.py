"""Operations dashboard API endpoints."""
from datetime import datetime, timedelta
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import (
    get_db,
    AutomationRule,
    PriceBandStrategyConfig,
    MinerStrategy,
    Miner,
    HomeAssistantConfig,
    Telemetry,
    engine
)
from core.db_pool_metrics import get_metrics as get_db_pool_metrics
from core.telemetry_metrics import get_metrics as get_telemetry_metrics, update_backlog
from api.time_utils import to_utc_iso8601

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/operations/status")
async def get_operations_status(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """Get operational status summary for the Operations dashboard."""
    try:
        # Active automation rules
        rules_result = await db.execute(
            select(AutomationRule)
            .where(AutomationRule.enabled == True)
            .order_by(AutomationRule.priority)
        )
        active_rules = rules_result.scalars().all()
        rules_payload: List[Dict[str, Any]] = [
            {
                "id": rule.id,
                "name": rule.name,
                "trigger_type": rule.trigger_type,
                "trigger_config": rule.trigger_config,
                "action_type": rule.action_type,
                "action_config": rule.action_config,
                "priority": rule.priority
            }
            for rule in active_rules
        ]

        # Price band strategy status
        strategy_result = await db.execute(select(PriceBandStrategyConfig).limit(1))
        strategy = strategy_result.scalar_one_or_none()
        enrolled_miners: List[Dict[str, Any]] = []
        if strategy:
            enrolled_result = await db.execute(
                select(MinerStrategy, Miner)
                .join(Miner, MinerStrategy.miner_id == Miner.id)
                .where(MinerStrategy.strategy_enabled == True)
            )
            enrolled_miners = [
                {
                    "id": miner.id,
                    "name": miner.name,
                    "type": miner.miner_type
                }
                for _, miner in enrolled_result.all()
            ]

        # Get champion miner name if set
        champion_miner_name = None
        if strategy and strategy.current_champion_miner_id:
            champion_result = await db.execute(
                select(Miner.name).where(Miner.id == strategy.current_champion_miner_id)
            )
            champion_miner_name = champion_result.scalar_one_or_none()
        
        strategy_payload = {
            "enabled": bool(strategy.enabled) if strategy else False,
            "current_price_band": strategy.current_price_band if strategy else None,
            "current_band_sort_order": strategy.current_band_sort_order if strategy else None,
            "champion_mode_enabled": strategy.champion_mode_enabled if strategy else False,
            "current_champion_miner_name": champion_miner_name,
            "last_action_time": to_utc_iso8601(strategy.last_action_time) if strategy else None,
            "last_price_checked": strategy.last_price_checked if strategy else None,
            "enrolled_miners": enrolled_miners
        }

        # Home Assistant keepalive status
        ha_result = await db.execute(select(HomeAssistantConfig).limit(1))
        ha_config = ha_result.scalar_one_or_none()
        now = datetime.utcnow()
        ha_unstable = False
        ha_detail = None
        if ha_config and ha_config.keepalive_enabled:
            if ha_config.keepalive_downtime_start is not None:
                ha_unstable = True
            elif ha_config.keepalive_last_success:
                if (now - ha_config.keepalive_last_success) > timedelta(minutes=5):
                    ha_unstable = True
            ha_detail = {
                "enabled": True,
                "last_success": to_utc_iso8601(ha_config.keepalive_last_success),
                "downtime_start": to_utc_iso8601(ha_config.keepalive_downtime_start),
                "alerts_sent": ha_config.keepalive_alerts_sent,
            }
        else:
            ha_detail = {"enabled": False}

        # Telemetry backlog
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
        telemetry_metrics = get_telemetry_metrics()

        # DB pool info (current + high-water marks)
        pool = engine.pool
        pool_size = pool.size() if hasattr(pool, "size") else 0
        checked_out = pool.checkedout() if hasattr(pool, "checkedout") else 0
        overflow = pool.overflow() if hasattr(pool, "overflow") else 0
        total_capacity = pool_size + overflow
        utilization_pct = (checked_out / total_capacity * 100) if total_capacity > 0 else 0

        throttling_writes = utilization_pct >= 90

        # Ramp-up heuristic
        ramp_up = backlog_count > 0

        response = {
            "automation_rules": rules_payload,
            "strategy": strategy_payload,
            "ha": {
                "unstable": ha_unstable,
                "detail": ha_detail
            },
            "telemetry": {
                "backlog_current": backlog_count,
                "metrics": {
                    "last_24h": telemetry_metrics.last_24h.to_dict(),
                    "since_boot": telemetry_metrics.since_boot.to_dict(),
                    "last_24h_date": telemetry_metrics.last_24h_date
                }
            },
            "db_pool": {
                "checked_out": checked_out,
                "total_capacity": total_capacity,
                "utilization_percent": round(utilization_pct, 1),
                "high_water": {
                    "last_24h": get_db_pool_metrics().last_24h.to_dict(),
                    "since_boot": get_db_pool_metrics().since_boot.to_dict(),
                    "last_24h_date": get_db_pool_metrics().last_24h_date
                }
            },
            "modes": {
                "ramp_up": ramp_up,
                "throttling_writes": throttling_writes,
                "ha_unstable": ha_unstable
            }
        }

        return response
    except Exception as e:
        logger.error(f"Failed to build operations status: {e}", exc_info=True)
        return {
            "error": str(e)
        }
