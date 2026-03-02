"""
Energy price band strategy API endpoints
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from pydantic import BaseModel
from typing import Dict, List, Optional
from datetime import datetime

from core.database import get_db, PriceBandStrategyConfig, MinerStrategy, Miner

router = APIRouter()

NO_POOL_CHANGE_POOL_ID = -1

# Large offset used when re-indexing bands to avoid transient UNIQUE collisions
SHIFT_OFFSET = 1000


class PriceBandStrategySettings(BaseModel):
    enabled: bool
    miner_ids: List[int]
    champion_mode_enabled: bool = False


class PriceBandStrategyStatus(BaseModel):
    enabled: bool
    current_price_band: Optional[str]
    last_action_time: Optional[datetime]
    last_price_checked: Optional[float]
    enrolled_miners: List[dict]  # List of {id, name, type}


@router.get("/price-band-strategy")
async def get_price_band_strategy_settings(db: AsyncSession = Depends(get_db)):
    """Get current price band strategy settings"""
    # Get strategy config
    result = await db.execute(select(PriceBandStrategyConfig))
    strategy = result.scalar_one_or_none()
    
    if not strategy:
        # Create default strategy config
        strategy = PriceBandStrategyConfig(
            enabled=False,
            current_price_band=None,
            hysteresis_counter=0
        )
        db.add(strategy)
        await db.commit()
        await db.refresh(strategy)
    
    # Get enrolled miners
    miner_strategy_result = await db.execute(
        select(MinerStrategy, Miner)
        .join(Miner, MinerStrategy.miner_id == Miner.id)
        .where(MinerStrategy.strategy_enabled == True)
    )
    enrolled = miner_strategy_result.all()
    
    enrolled_miners = [
        {
            "id": miner.id,
            "name": miner.name,
            "type": miner.miner_type
        }
        for _, miner in enrolled
    ]
    
    # Get all miners for selection
    all_miners_result = await db.execute(
        select(Miner)
        .where(Miner.enabled == True)
        .order_by(Miner.miner_type, Miner.name)
    )
    all_miners = all_miners_result.scalars().all()
    
    miners_by_type = {}
    
    for miner in all_miners:
        miner_dict = {
            "id": miner.id,
            "name": miner.name,
            "type": miner.miner_type,
            "enrolled": miner.id in [m["id"] for m in enrolled_miners]
        }
        
        miners_by_type.setdefault(miner.miner_type, []).append(miner_dict)
    
    return {
        "enabled": strategy.enabled,
        "current_price_band": strategy.current_price_band,
        "last_action_time": strategy.last_action_time.isoformat() if strategy.last_action_time else None,
        "last_price_checked": strategy.last_price_checked,
        "hysteresis_counter": strategy.hysteresis_counter,
        "champion_mode_enabled": strategy.champion_mode_enabled,
        "current_champion_miner_id": strategy.current_champion_miner_id,
        "enrolled_miners": enrolled_miners,
        "miners_by_type": miners_by_type
    }


@router.post("/price-band-strategy")
async def save_price_band_strategy_settings(
    settings: PriceBandStrategySettings,
    db: AsyncSession = Depends(get_db)
):
    """Save price band strategy settings"""
    # Get or create strategy
    result = await db.execute(select(PriceBandStrategyConfig))
    strategy = result.scalar_one_or_none()
    
    if not strategy:
        strategy = PriceBandStrategyConfig(
            enabled=settings.enabled,
            champion_mode_enabled=settings.champion_mode_enabled
        )
        db.add(strategy)
    else:
        strategy.enabled = settings.enabled
        strategy.champion_mode_enabled = settings.champion_mode_enabled
    
    strategy.updated_at = datetime.utcnow()
    
    # Clear existing miner strategy entries
    existing_result = await db.execute(select(MinerStrategy))
    existing = existing_result.scalars().all()
    
    for ms in existing:
        await db.delete(ms)
    
    await db.flush()  # Ensure deletions are processed
    
    # Add new miner strategy entries
    for miner_id in settings.miner_ids:
        ms = MinerStrategy(
            miner_id=miner_id,
            strategy_enabled=True
        )
        db.add(ms)
    
    await db.commit()
    
    return {
        "message": "Price band strategy settings saved successfully",
        "enabled": settings.enabled,
        "enrolled_count": len(settings.miner_ids)
    }


@router.post("/price-band-strategy/execute")
async def execute_price_band_strategy_manual(db: AsyncSession = Depends(get_db)):
    """Manually trigger strategy execution"""
    from core.price_band_strategy import PriceBandStrategy
    
    try:
        report = await PriceBandStrategy.execute_strategy(db)
        return report
    except Exception as e:
        import traceback
        return {
            "error": str(e),
            "traceback": traceback.format_exc()
        }


@router.post("/price-band-strategy/reconcile")
async def reconcile_price_band_strategy_manual(db: AsyncSession = Depends(get_db)):
    """Manually trigger strategy reconciliation"""
    from core.price_band_strategy import PriceBandStrategy
    
    try:
        report = await PriceBandStrategy.reconcile_strategy(db)
        return report
    except Exception as e:
        import traceback
        return {
            "error": str(e),
            "traceback": traceback.format_exc()
        }


class BandUpdate(BaseModel):
    model_config = {"extra": "forbid"}  # Pydantic v2 syntax
    
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    target_pool_id: int | None = None  # Pool ID, None for OFF, -1 for Do Not Change Pool
    mode_targets: Optional[Dict[str, str]] = None  # dynamic miner_type -> mode map


def validate_band_update(update: BandUpdate) -> Optional[str]:
    """
    Validate band update values
    
    Returns:
        Error message if validation fails, None if valid
    """
    from core.price_band_bands import get_valid_modes
    valid_modes = get_valid_modes()
    
    # Validate price thresholds
    if update.min_price is not None and update.min_price < 0:
        return "Minimum price cannot be negative"
    
    if update.max_price is not None and update.max_price < 0:
        return "Maximum price cannot be negative"
    
    if update.min_price is not None and update.max_price is not None:
        if update.min_price >= update.max_price:
            return "Minimum price must be less than maximum price"
    
    # Validate pool ID (preferred method)
    if update.target_pool_id is not None:
        if update.target_pool_id != NO_POOL_CHANGE_POOL_ID and update.target_pool_id <= 0:
            return "target_pool_id must be a positive pool ID, null (OFF), or -1 (Do Not Change Pool)"
    
    # Validate dynamic mode targets
    if update.mode_targets is not None:
        for miner_type, mode in update.mode_targets.items():
            allowed = valid_modes.get(miner_type)
            if not allowed:
                return f"Unsupported miner type '{miner_type}' for mode_targets"
            if mode not in allowed:
                return (
                    f"Invalid mode '{mode}' for {miner_type}. "
                    f"Must be one of: {', '.join(allowed)}"
                )
    
    return None


async def _load_mode_targets_by_band(db: AsyncSession, band_ids: List[int]) -> Dict[int, Dict[str, str]]:
    """Load persisted dynamic mode targets keyed by band_id then miner_type."""
    if not band_ids:
        return {}

    from core.database import StrategyBandModeTarget

    result = await db.execute(
        select(StrategyBandModeTarget).where(StrategyBandModeTarget.band_id.in_(band_ids))
    )
    rows = result.scalars().all()

    mode_targets_by_band: Dict[int, Dict[str, str]] = {}
    for row in rows:
        mode_targets_by_band.setdefault(row.band_id, {})[row.miner_type] = row.mode

    return mode_targets_by_band


def _build_band_mode_targets_from_sources(band, persisted: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Build mode targets from persisted dynamic rows only, with default fill for missing miner types."""
    from core.miner_capabilities import get_strategy_mode_map

    mode_targets: Dict[str, str] = dict(persisted or {})
    valid_mode_map = get_strategy_mode_map()

    for miner_type, allowed in valid_mode_map.items():
        if miner_type in mode_targets:
            continue
        if "managed_externally" in allowed:
            mode_targets[miner_type] = "managed_externally"
        elif allowed:
            mode_targets[miner_type] = allowed[0]

    return mode_targets


async def _upsert_mode_target(db: AsyncSession, band_id: int, miner_type: str, mode: str) -> None:
    from core.database import StrategyBandModeTarget

    existing_result = await db.execute(
        select(StrategyBandModeTarget)
        .where(StrategyBandModeTarget.band_id == band_id)
        .where(StrategyBandModeTarget.miner_type == miner_type)
    )
    existing = existing_result.scalar_one_or_none()
    if existing:
        existing.mode = mode
    else:
        db.add(
            StrategyBandModeTarget(
                band_id=band_id,
                miner_type=miner_type,
                mode=mode,
            )
        )


async def _set_all_mode_targets_managed_externally(db: AsyncSession, band_id: int) -> None:
    from core.miner_capabilities import get_strategy_mode_map

    for miner_type in get_strategy_mode_map().keys():
        await _upsert_mode_target(db, band_id, miner_type, "managed_externally")


@router.get("/price-band-strategy/capabilities")
async def get_strategy_capabilities():
    """Expose miner strategy capabilities for dynamic UI rendering."""
    from core.miner_capabilities import get_miner_capabilities

    caps = get_miner_capabilities()
    return {
        "miner_types": {
            miner_type: {
                "display_name": capability.display_name,
                "available_modes": capability.available_modes,
                "champion_lowest_mode": capability.champion_lowest_mode,
            }
            for miner_type, capability in caps.items()
        }
    }


@router.get("/price-band-strategy/bands")
async def get_strategy_bands_api(db: AsyncSession = Depends(get_db)):
    """Get configured price bands for strategy"""
    from core.database import PriceBandStrategyBand
    from core.price_band_bands import ensure_strategy_bands
    
    # Get strategy
    result = await db.execute(select(PriceBandStrategyConfig))
    strategy = result.scalar_one_or_none()
    
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    
    # Ensure bands exist
    await ensure_strategy_bands(db, strategy.id)
    
    # Get bands
    bands_result = await db.execute(
        select(PriceBandStrategyBand)
        .where(PriceBandStrategyBand.strategy_id == strategy.id)
        .order_by(PriceBandStrategyBand.sort_order)
    )
    bands = bands_result.scalars().all()
    
    mode_targets_by_band = await _load_mode_targets_by_band(db, [band.id for band in bands])

    return {
        "bands": [
            {
                "id": band.id,
                "sort_order": band.sort_order,
                "min_price": band.min_price,
                "max_price": band.max_price,
                "target_pool_id": band.target_pool_id,  # Preferred
                "mode_targets": _build_band_mode_targets_from_sources(
                    band,
                    mode_targets_by_band.get(band.id),
                ),
            }
            for band in bands
        ]
    }


@router.patch("/price-band-strategy/bands/{band_id}")
async def update_strategy_band(
    band_id: int,
    update: BandUpdate,
    db: AsyncSession = Depends(get_db)
):
    """Update a specific band's settings"""
    from core.database import PriceBandStrategyBand
    
    # Validate input
    validation_error = validate_band_update(update)
    if validation_error:
        raise HTTPException(status_code=400, detail=validation_error)
    
    # Get band
    result = await db.execute(
        select(PriceBandStrategyBand).where(PriceBandStrategyBand.id == band_id)
    )
    band = result.scalar_one_or_none()
    
    if not band:
        raise HTTPException(status_code=404, detail="Band not found")
    
    # Update fields if provided
    if update.min_price is not None:
        band.min_price = update.min_price
    
    if update.max_price is not None:
        band.max_price = update.max_price
    
    # Validate price range after updates
    if band.min_price is not None and band.max_price is not None:
        if band.min_price >= band.max_price:
            raise HTTPException(
                status_code=400, 
                detail=f"Minimum price ({band.min_price}) must be less than maximum price ({band.max_price})"
            )
    
    # Handle pool selection (preferred method)
    # Pydantic 2.x includes explicitly set fields (even if null) in model_fields_set
    if 'target_pool_id' in update.model_fields_set:
        # Validate pool exists and is enabled (None/null = OFF is valid)
        if update.target_pool_id not in (None, NO_POOL_CHANGE_POOL_ID):
            from core.database import Pool
            pool_result = await db.execute(
                select(Pool).where(Pool.id == update.target_pool_id, Pool.enabled == True)
            )
            pool = pool_result.scalar_one_or_none()
            if not pool:
                raise HTTPException(status_code=400, detail=f"Pool #{update.target_pool_id} not found or disabled")
        
        band.target_pool_id = update.target_pool_id
        
        # If setting to None (OFF), force all dynamic mode targets to managed_externally
        if update.target_pool_id is None:
            await _set_all_mode_targets_managed_externally(db, band.id)
    
    # Apply dynamic mode map when provided
    if update.mode_targets:
        for miner_type, mode in update.mode_targets.items():
            await _upsert_mode_target(db, band.id, miner_type, mode)
    
    # Retry commit on database lock
    import asyncio
    from sqlalchemy.exc import OperationalError
    max_retries = 3
    for attempt in range(max_retries):
        try:
            await db.commit()
            break
        except OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                await asyncio.sleep(0.5 * (attempt + 1))  # Exponential backoff
                continue
            raise
    
    await db.refresh(band)
    
    return {
        "id": band.id,
        "sort_order": band.sort_order,
        "min_price": band.min_price,
        "max_price": band.max_price,
        "target_pool_id": band.target_pool_id,
        "mode_targets": _build_band_mode_targets_from_sources(
            band,
            (await _load_mode_targets_by_band(db, [band.id])).get(band.id),
        ),
    }


@router.post("/price-band-strategy/bands/reset")
async def reset_strategy_bands_api(db: AsyncSession = Depends(get_db)):
    """Reset all bands to default configuration"""
    from core.price_band_bands import reset_bands_to_default
    
    # Get strategy
    result = await db.execute(select(PriceBandStrategyConfig))
    strategy = result.scalar_one_or_none()
    
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    
    success = await reset_bands_to_default(db, strategy.id)
    
    if not success:
        raise HTTPException(status_code=500, detail="Failed to reset bands")
    
    return {"message": "Bands reset to defaults"}

