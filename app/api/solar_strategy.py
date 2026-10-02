"""
Solar Strategy API endpoints

Mirrors app/api/price_band_strategy.py's GET/POST shape for consistency,
but fully independent state (SolarStrategyConfig/SolarMinerEnrollment,
not PriceBandStrategyConfig/MinerStrategy).
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

from core.database import get_db, Miner, SolarStrategyConfig, SolarMinerEnrollment, HomeAssistantConfig
from core.strategy_enrollment import reject_conflicting_price_band_enrollment

router = APIRouter()


class SolarStrategySettings(BaseModel):
    enabled: bool
    solar_surplus_entity_id: Optional[str] = None
    miner_ids: List[int] = []


@router.get("/solar-strategy")
async def get_solar_strategy_settings(db: AsyncSession = Depends(get_db)):
    """Get current Solar Strategy settings"""
    result = await db.execute(select(SolarStrategyConfig))
    strategy = result.scalar_one_or_none()

    if not strategy:
        strategy = SolarStrategyConfig(enabled=False)
        db.add(strategy)
        await db.commit()
        await db.refresh(strategy)

    enrollment_result = await db.execute(
        select(SolarMinerEnrollment, Miner)
        .join(Miner, SolarMinerEnrollment.miner_id == Miner.id)
        .where(SolarMinerEnrollment.enabled == True)
    )
    enrolled = enrollment_result.all()

    enrolled_miners = [
        {"id": miner.id, "name": miner.name, "type": miner.miner_type}
        for _, miner in enrolled
    ]

    all_miners_result = await db.execute(
        select(Miner).where(Miner.enabled == True).order_by(Miner.miner_type, Miner.name)
    )
    all_miners = all_miners_result.scalars().all()

    enrolled_ids = {m["id"] for m in enrolled_miners}
    miners_by_type = {}
    for miner in all_miners:
        miner_dict = {
            "id": miner.id,
            "name": miner.name,
            "type": miner.miner_type,
            "enrolled": miner.id in enrolled_ids,
        }
        miners_by_type.setdefault(miner.miner_type, []).append(miner_dict)

    return {
        "enabled": strategy.enabled,
        "solar_surplus_entity_id": strategy.solar_surplus_entity_id,
        "enrolled_miners": enrolled_miners,
        "miners_by_type": miners_by_type,
    }


@router.post("/solar-strategy")
async def save_solar_strategy_settings(
    settings: SolarStrategySettings,
    db: AsyncSession = Depends(get_db),
):
    """Save Solar Strategy settings"""
    await reject_conflicting_price_band_enrollment(db, settings.miner_ids)

    result = await db.execute(select(SolarStrategyConfig))
    strategy = result.scalar_one_or_none()

    if not strategy:
        strategy = SolarStrategyConfig(
            enabled=settings.enabled,
            solar_surplus_entity_id=settings.solar_surplus_entity_id,
        )
        db.add(strategy)
    else:
        strategy.enabled = settings.enabled
        strategy.solar_surplus_entity_id = settings.solar_surplus_entity_id

    strategy.updated_at = datetime.utcnow()

    existing_result = await db.execute(select(SolarMinerEnrollment))
    for row in existing_result.scalars().all():
        await db.delete(row)
    await db.flush()

    for miner_id in settings.miner_ids:
        db.add(SolarMinerEnrollment(miner_id=miner_id, enabled=True))

    await db.commit()

    return {
        "message": "Solar Strategy settings saved successfully",
        "enabled": settings.enabled,
        "enrolled_count": len(settings.miner_ids),
    }


@router.get("/solar-strategy/surplus")
async def get_solar_surplus_reading(db: AsyncSession = Depends(get_db)):
    """Live (unsmoothed) read of the configured surplus sensor, for display
    purposes only (e.g. the header price ticker) - independent of whether
    Solar Strategy itself is enabled. Not the EMA-smoothed value the
    strategy's own decision-making uses (see bundled_config/strategies/
    solar_strategy.py); this is a direct poll, so it can show a reading as
    soon as a sensor is selected, even before the strategy is turned on."""
    result = await db.execute(select(SolarStrategyConfig))
    strategy = result.scalar_one_or_none()

    if not strategy or not strategy.solar_surplus_entity_id:
        return {"configured": False, "surplus_watts": None}

    config_result = await db.execute(select(HomeAssistantConfig))
    ha_config = config_result.scalar_one_or_none()
    if not ha_config or not ha_config.enabled:
        return {"configured": True, "surplus_watts": None, "entity_id": strategy.solar_surplus_entity_id}

    from integrations.homeassistant import HomeAssistantIntegration

    ha = HomeAssistantIntegration(base_url=ha_config.base_url, access_token=ha_config.access_token)
    raw_value = await ha.get_device_state_value(strategy.solar_surplus_entity_id)

    surplus_watts = None
    if raw_value is not None:
        try:
            surplus_watts = float(raw_value)
        except (TypeError, ValueError):
            surplus_watts = None

    return {
        "configured": True,
        "entity_id": strategy.solar_surplus_entity_id,
        "surplus_watts": surplus_watts,
    }


@router.post("/solar-strategy/execute")
async def execute_solar_strategy_manual(db: AsyncSession = Depends(get_db)):
    """Manually trigger Solar Strategy execution (for testing/debugging)"""
    try:
        from core.strategy_loader import get_strategy_loader
        plugin = get_strategy_loader().get_plugin("solar")
        if not plugin:
            return {"error": "NOT_LOADED", "message": "Solar Strategy plugin is not loaded"}

        result = await plugin.execute(db)
        return {
            "enabled": result.enabled,
            "actions": result.actions,
            "details": result.details,
            "error": result.error,
        }
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()}
