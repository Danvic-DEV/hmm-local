"""
Miner management API endpoints
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from typing import List
from pydantic import BaseModel
from datetime import datetime
import logging
import copy

from core.database import get_db, Miner, Pool, Telemetry, MinerModePowerStats
from adapters import create_adapter, get_supported_types
from api.time_utils import to_utc_iso8601


router = APIRouter()


def sanitize_miner_config(config: dict) -> dict:
    """Remove sensitive data from miner config before returning in API responses"""
    if not config:
        return config
    
    sanitized = copy.deepcopy(config)
    
    # Mask admin_password for Avalon Nano miners
    if "admin_password" in sanitized and sanitized["admin_password"]:
        sanitized["admin_password"] = "****"
    
    return sanitized


class MinerCreate(BaseModel):
    name: str
    miner_type: str
    ip_address: str
    port: int | None = None
    config: dict | None = None


class MinerUpdate(BaseModel):
    name: str | None = None
    ip_address: str | None = None
    port: int | None = None
    current_mode: str | None = None
    enabled: bool | None = None
    manual_power_watts: int | None = None
    config: dict | None = None


class MinerResponse(BaseModel):
    id: int
    name: str
    miner_type: str
    ip_address: str
    port: int | None
    effective_port: int  # The actual port being used (including defaults)
    current_mode: str | None
    enabled: bool
    manual_power_watts: int | None = None
    config: dict | None
    
    class Config:
        from_attributes = True


class ModePowerStatsRow(BaseModel):
    mode: str
    sample_count: int
    avg_power_watts: float | None = None
    ema_power_watts: float | None = None
    min_power_watts: float | None = None
    max_power_watts: float | None = None
    last_power_watts: float | None = None
    last_sample_at: datetime | None = None
    resets_count: int = 0


class MinerModePowerStatsResponse(BaseModel):
    miner_id: int
    current_mode: str | None = None
    current_mode_stats: ModePowerStatsRow | None = None
    modes: list[ModePowerStatsRow]


def _to_mode_stats_row(row: MinerModePowerStats) -> ModePowerStatsRow:
    return ModePowerStatsRow(
        mode=row.mode,
        sample_count=int(row.sample_count or 0),
        avg_power_watts=row.avg_power_watts,
        ema_power_watts=row.ema_power_watts,
        min_power_watts=row.min_power_watts,
        max_power_watts=row.max_power_watts,
        last_power_watts=row.last_power_watts,
        last_sample_at=row.last_sample_at,
        resets_count=int(row.resets_count or 0),
    )


@router.get("/mode-power-stats")
async def get_all_mode_power_stats(db: AsyncSession = Depends(get_db)):
    """Get running power stats for all miners grouped by miner and mode."""
    miner_result = await db.execute(select(Miner))
    miners = miner_result.scalars().all()
    miners_by_id = {m.id: m for m in miners}

    stats_result = await db.execute(
        select(MinerModePowerStats)
        .order_by(MinerModePowerStats.miner_id, MinerModePowerStats.mode)
    )
    stats_rows = stats_result.scalars().all()

    by_miner: dict[int, dict] = {
        miner.id: {
            "miner_id": miner.id,
            "current_mode": miner.current_mode,
            "current_mode_stats": None,
            "modes": [],
        }
        for miner in miners
    }

    for row in stats_rows:
        entry = by_miner.get(row.miner_id)
        if entry is None:
            continue
        row_data = _to_mode_stats_row(row)
        entry["modes"].append(row_data.model_dump())

    for miner_id, payload in by_miner.items():
        current_mode = (payload.get("current_mode") or "").strip().lower()
        if not current_mode:
            continue
        for row_data in payload["modes"]:
            if row_data.get("mode") == current_mode:
                payload["current_mode_stats"] = row_data
                break

    return {"miners": by_miner}


@router.get("/types")
async def get_miner_types():
    """Get supported miner types"""
    return {"types": get_supported_types()}


@router.get("/", response_model=List[MinerResponse])
async def list_miners(db: AsyncSession = Depends(get_db)):
    """List all miners"""
    from sqlalchemy import func
    result = await db.execute(select(Miner).order_by(func.lower(Miner.name)))
    miners = result.scalars().all()
    
    # Add effective_port to each miner
    miners_with_effective_port = []
    for miner in miners:
        adapter = create_adapter(miner.miner_type, miner.id, miner.name, miner.ip_address, miner.port, miner.config)
        miner_dict = {
            "id": miner.id,
            "name": miner.name,
            "miner_type": miner.miner_type,
            "ip_address": miner.ip_address,
            "port": miner.port,
            "effective_port": adapter.port,
            "current_mode": miner.current_mode,
            "enabled": miner.enabled,
            "manual_power_watts": miner.manual_power_watts,
            "config": sanitize_miner_config(miner.config)
        }
        miners_with_effective_port.append(miner_dict)
    
    return miners_with_effective_port


@router.get("/{miner_id}", response_model=MinerResponse)
async def get_miner(miner_id: int, db: AsyncSession = Depends(get_db)):
    """Get miner by ID"""
    result = await db.execute(select(Miner).where(Miner.id == miner_id))
    miner = result.scalar_one_or_none()
    
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")
    
    # Calculate effective_port using the adapter
    adapter = create_adapter(miner.miner_type, miner.id, miner.name, miner.ip_address, miner.port, miner.config)
    
    return {
        "id": miner.id,
        "name": miner.name,
        "miner_type": miner.miner_type,
        "ip_address": miner.ip_address,
        "port": miner.port,
        "effective_port": adapter.port,
        "current_mode": miner.current_mode,
        "enabled": miner.enabled,
        "manual_power_watts": miner.manual_power_watts,
        "config": sanitize_miner_config(miner.config)
    }


@router.get("/{miner_id}/mode-power-stats", response_model=MinerModePowerStatsResponse)
async def get_miner_mode_power_stats(miner_id: int, db: AsyncSession = Depends(get_db)):
    """Get running power stats for one miner grouped by mode."""
    result = await db.execute(select(Miner).where(Miner.id == miner_id))
    miner = result.scalar_one_or_none()
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")

    stats_result = await db.execute(
        select(MinerModePowerStats)
        .where(MinerModePowerStats.miner_id == miner_id)
        .order_by(MinerModePowerStats.mode)
    )
    rows = stats_result.scalars().all()

    mode_rows = [_to_mode_stats_row(row) for row in rows]
    current_mode_key = (miner.current_mode or "").strip().lower()
    current_mode_stats = next((r for r in mode_rows if r.mode == current_mode_key), None)

    return MinerModePowerStatsResponse(
        miner_id=miner.id,
        current_mode=miner.current_mode,
        current_mode_stats=current_mode_stats,
        modes=mode_rows,
    )


@router.post("/", response_model=MinerResponse)
async def create_miner(miner: MinerCreate, db: AsyncSession = Depends(get_db)):
    """Create new miner"""
    # Validate miner type
    if miner.miner_type not in get_supported_types():
        raise HTTPException(status_code=400, detail=f"Invalid miner type: {miner.miner_type}")
    
    db_miner = Miner(
        name=miner.name,
        miner_type=miner.miner_type,
        ip_address=miner.ip_address,
        port=miner.port,
        config=miner.config
    )
    
    db.add(db_miner)
    await db.commit()
    await db.refresh(db_miner)
    
    # If NMMiner, reload UDP listener adapters
    if miner.miner_type == "nmminer":
        from core.scheduler import scheduler
        await scheduler.reload_nmminer_adapters()
    
    # Calculate effective_port using the adapter
    adapter = create_adapter(db_miner.miner_type, db_miner.id, db_miner.name, db_miner.ip_address, db_miner.port, db_miner.config)
    
    return {
        "id": db_miner.id,
        "name": db_miner.name,
        "miner_type": db_miner.miner_type,
        "ip_address": db_miner.ip_address,
        "port": db_miner.port,
        "effective_port": adapter.port,
        "current_mode": db_miner.current_mode,
        "enabled": db_miner.enabled,
        "manual_power_watts": db_miner.manual_power_watts,
        "config": sanitize_miner_config(db_miner.config)
    }


@router.put("/{miner_id}", response_model=MinerResponse)
async def update_miner(miner_id: int, miner_update: MinerUpdate, db: AsyncSession = Depends(get_db)):
    """Update miner configuration"""
    result = await db.execute(select(Miner).where(Miner.id == miner_id))
    miner = result.scalar_one_or_none()
    
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")
    
    # Track if this is a NMMiner and if we need to reload adapters
    is_nmminer = miner.miner_type == "nmminer"
    needs_reload = False
    
    # Update fields
    if miner_update.name is not None:
        miner.name = miner_update.name
    if miner_update.ip_address is not None:
        miner.ip_address = miner_update.ip_address
        needs_reload = is_nmminer  # IP change requires reload
    if miner_update.port is not None:
        miner.port = miner_update.port
    if miner_update.current_mode is not None:
        miner.current_mode = miner_update.current_mode
    if miner_update.enabled is not None:
        miner.enabled = miner_update.enabled
        needs_reload = is_nmminer  # Enable/disable requires reload
    if miner_update.manual_power_watts is not None:
        # Validate range
        if miner_update.manual_power_watts < 1 or miner_update.manual_power_watts > 5000:
            raise HTTPException(status_code=400, detail="manual_power_watts must be between 1 and 5000")
        miner.manual_power_watts = miner_update.manual_power_watts
    if miner_update.config is not None:
        # Merge config to preserve existing values (especially admin_password)
        if miner.config:
            merged_config = copy.deepcopy(miner.config)
            merged_config.update(miner_update.config)
            miner.config = merged_config
        else:
            miner.config = miner_update.config
    
    await db.commit()
    await db.refresh(miner)
    
    # Reload NMMiner adapters if needed
    if needs_reload:
        from core.scheduler import scheduler
        await scheduler.reload_nmminer_adapters()
    
    # Calculate effective_port using the adapter
    adapter = create_adapter(miner.miner_type, miner.id, miner.name, miner.ip_address, miner.port, miner.config)
    
    return {
        "id": miner.id,
        "name": miner.name,
        "miner_type": miner.miner_type,
        "ip_address": miner.ip_address,
        "port": miner.port,
        "effective_port": adapter.port,
        "current_mode": miner.current_mode,
        "enabled": miner.enabled,
        "manual_power_watts": miner.manual_power_watts,
        "config": sanitize_miner_config(miner.config)
    }


@router.delete("/{miner_id}")
async def delete_miner(miner_id: int, db: AsyncSession = Depends(get_db)):
    """Delete miner"""
    result = await db.execute(select(Miner).where(Miner.id == miner_id))
    miner = result.scalar_one_or_none()
    
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")
    
    # Track if this is a NMMiner (need to reload adapters after deletion)
    is_nmminer = miner.miner_type == "nmminer"
    
    await db.delete(miner)
    await db.commit()
    
    # Reload NMMiner adapters if needed
    if is_nmminer:
        from core.scheduler import scheduler
        await scheduler.reload_nmminer_adapters()
    
    return {"status": "deleted"}


@router.get("/{miner_id}/telemetry")
async def get_miner_telemetry(
    miner_id: int, 
    live: bool = Query(default=False, description="Fetch live data from device instead of cached"),
    db: AsyncSession = Depends(get_db)
):
    """
    Get telemetry for a miner.
    By default returns cached data from database (updated every 60s).
    Set live=true to query the device directly.
    """
    result = await db.execute(select(Miner).where(Miner.id == miner_id))
    miner = result.scalar_one_or_none()
    
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")
    
    # Live query - hit device directly
    if live:
        adapter = create_adapter(miner.miner_type, miner.id, miner.name, miner.ip_address, miner.port, miner.config)
        if not adapter:
            raise HTTPException(status_code=500, detail="Failed to create miner adapter")
        
        telemetry = await adapter.get_telemetry()
        if not telemetry:
            raise HTTPException(status_code=503, detail="Failed to get telemetry from miner")
        
        return telemetry.to_dict()
    
    # Cached query - read from database
    result = await db.execute(
        select(Telemetry)
        .where(Telemetry.miner_id == miner_id)
        .order_by(desc(Telemetry.timestamp))
        .limit(1)
    )
    cached_telemetry = result.scalar_one_or_none()
    
    if not cached_telemetry:
        raise HTTPException(status_code=503, detail="No cached telemetry available. Try again with live=true")
    
    # Import format_hashrate utility
    from core.utils import format_hashrate
    
    # Format hashrate using utility
    hashrate_formatted = format_hashrate(
        cached_telemetry.hashrate, 
        cached_telemetry.hashrate_unit or "GH/s"
    )
    
    # Convert database model to dict matching adapter format
    return {
        "timestamp": to_utc_iso8601(cached_telemetry.timestamp),
        "hashrate": hashrate_formatted,  # Structured format with display/value/unit
        "temperature": cached_telemetry.temperature,
        "power_watts": cached_telemetry.power_watts,
        "shares_accepted": cached_telemetry.shares_accepted,
        "shares_rejected": cached_telemetry.shares_rejected,
        "pool_in_use": cached_telemetry.pool_in_use,
        "extra_data": cached_telemetry.data or {}
    }


@router.post("/{miner_id}/mode")
async def set_miner_mode(miner_id: int, mode: str, db: AsyncSession = Depends(get_db)):
    """Set miner operating mode"""
    result = await db.execute(select(Miner).where(Miner.id == miner_id))
    miner = result.scalar_one_or_none()
    
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")
    
    adapter = create_adapter(miner.miner_type, miner.id, miner.name, miner.ip_address, miner.port, miner.config)
    if not adapter:
        raise HTTPException(status_code=500, detail="Failed to create miner adapter")
    
    success = await adapter.set_mode(mode)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to set mode")
    
    # Update database
    miner.current_mode = mode
    miner.last_mode_change = datetime.utcnow()
    await db.commit()
    
    # Wait for device to stabilize and get updated telemetry
    import asyncio
    await asyncio.sleep(3)
    
    # Get fresh telemetry to capture new power consumption
    telemetry = await adapter.get_telemetry()
    if telemetry and telemetry.power_watts:
        from core.database import Telemetry, EnergyPrice
        from sqlalchemy import select
        
        # Calculate energy cost if we have power data
        energy_cost = None
        if telemetry.power_watts is not None and telemetry.power_watts > 0:
            try:
                # Query Agile price for this timestamp
                price_query = select(EnergyPrice).where(
                    EnergyPrice.valid_from <= telemetry.timestamp,
                    EnergyPrice.valid_to > telemetry.timestamp
                ).limit(1)
                result = await db.execute(price_query)
                price_row = result.scalar_one_or_none()
                
                if price_row:
                    # Calculate cost for 1 minute: (watts / 60 / 1000) * price_pence
                    energy_cost = (telemetry.power_watts / 60.0 / 1000.0) * price_row.price_pence
            except Exception as e:
                print(f"⚠️ Could not calculate energy cost: {e}")
        
        db_telemetry = Telemetry(
            miner_id=miner.id,
            timestamp=telemetry.timestamp,
            hashrate=telemetry.hashrate,
            temperature=telemetry.temperature,
            power_watts=telemetry.power_watts,
            energy_cost=energy_cost,
            shares_accepted=telemetry.shares_accepted,
            shares_rejected=telemetry.shares_rejected,
            pool_in_use=telemetry.pool_in_use,
            mode=miner.current_mode,
            data=telemetry.extra_data
        )
        db.add(db_telemetry)
        await db.commit()
    
    return {"status": "success", "mode": mode}


@router.post("/{miner_id}/pool")
async def switch_miner_pool(miner_id: int, pool_id: int, db: AsyncSession = Depends(get_db)):
    """Switch miner to a different pool"""
    from core.database import Pool
    
    # Get miner
    result = await db.execute(select(Miner).where(Miner.id == miner_id))
    miner = result.scalar_one_or_none()
    
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")
    
    # Get pool
    result = await db.execute(select(Pool).where(Pool.id == pool_id))
    pool = result.scalar_one_or_none()
    
    if not pool:
        raise HTTPException(status_code=404, detail="Pool not found")
    
    if not pool.enabled:
        raise HTTPException(status_code=400, detail="Pool is disabled")
    
    # Create adapter and switch pool
    adapter = create_adapter(miner.miner_type, miner.id, miner.name, miner.ip_address, miner.port, miner.config)
    if not adapter:
        raise HTTPException(status_code=500, detail="Failed to create miner adapter")
    
    success = await adapter.switch_pool(pool.url, pool.port, pool.user, pool.password)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to switch pool")
    
    return {"status": "success", "pool": pool.name}


@router.get("/{miner_id}/device-pools")
async def get_device_pools(miner_id: int, db: AsyncSession = Depends(get_db)):
    """Get the pools available for a miner (Avalon Nano uses cached slots from database)"""
    from core.database import MinerPoolSlot
    from sqlalchemy import and_
    
    result = await db.execute(select(Miner).where(Miner.id == miner_id))
    miner = result.scalar_one_or_none()
    
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")
    
    # For Avalon Nano, get pools from cached MinerPoolSlot table (synced every 15min by scheduler)
    if miner.miner_type == "avalon_nano":
        result = await db.execute(
            select(MinerPoolSlot)
            .where(MinerPoolSlot.miner_id == miner_id)
            .order_by(MinerPoolSlot.slot_number)
        )
        slots = result.scalars().all()
        
        if slots:
            device_pools = []
            for slot in slots:
                device_pools.append({
                    "slot": slot.slot_number,
                    "url": slot.pool_url,
                    "port": slot.pool_port,
                    "user": slot.pool_user,
                    "active": slot.is_active
                })
            return {"pools": device_pools, "type": "device"}
        else:
            # No cached slots yet - return empty with message
            return {"pools": [], "type": "device", "message": "Pool slots not yet synced. Click 'Sync Pool Slots' button."}
    
    # For other miners (Bitaxe, etc), return all database pools
    result = await db.execute(select(Pool).where(Pool.enabled == True).order_by(Pool.name))
    pools = result.scalars().all()
    
    return {
        "pools": [
            {
                "id": p.id,
                "name": p.name,
                "url": p.url,
                "port": p.port,
                "user": p.user
            } for p in pools
        ],
        "type": "database"
    }


@router.post("/{miner_id}/restart")
async def restart_miner(miner_id: int, db: AsyncSession = Depends(get_db)):
    """Restart miner"""
    result = await db.execute(select(Miner).where(Miner.id == miner_id))
    miner = result.scalar_one_or_none()
    
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")
    
    adapter = create_adapter(miner.miner_type, miner.id, miner.name, miner.ip_address, miner.port, miner.config)
    if not adapter:
        raise HTTPException(status_code=500, detail="Failed to create miner adapter")
    
    success = await adapter.restart()
    if not success:
        raise HTTPException(status_code=500, detail="Failed to restart miner")
    
    return {"status": "restarting"}


@router.get("/{miner_id}/modes")
async def get_miner_modes(miner_id: int, db: AsyncSession = Depends(get_db)):
    """Get available modes for miner"""
    result = await db.execute(select(Miner).where(Miner.id == miner_id))
    miner = result.scalar_one_or_none()
    
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")
    
    adapter = create_adapter(miner.miner_type, miner.id, miner.name, miner.ip_address, miner.port, miner.config)
    if not adapter:
        raise HTTPException(status_code=500, detail="Failed to create miner adapter")
    
    modes = await adapter.get_available_modes()
    
    return {"modes": modes}


@router.get("/{miner_id}/cost/24h")
async def get_miner_24h_cost(miner_id: int, db: AsyncSession = Depends(get_db)):
    """Calculate rolling 24-hour cost for a miner based on power consumption and Octopus Agile prices"""
    from datetime import datetime, timedelta
    from core.database import Telemetry, EnergyPrice
    from core.config import app_config
    
    # Get miner
    result = await db.execute(select(Miner).where(Miner.id == miner_id))
    miner = result.scalar_one_or_none()
    
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")
    
    # Get time range (last 24 hours)
    now = datetime.utcnow()
    start_time = now - timedelta(hours=24)
    
    # Get all telemetry records for this miner in the last 24 hours
    result = await db.execute(
        select(Telemetry)
        .where(Telemetry.miner_id == miner_id)
        .where(Telemetry.timestamp > start_time)
        .order_by(Telemetry.timestamp)
    )
    telemetry_records = result.scalars().all()
    
    if not telemetry_records:
        return {
            "miner_id": miner_id,
            "miner_name": miner.name,
            "period_hours": 24,
            "cost_pence": 0,
            "cost_pounds": 0,
            "avg_power_watts": 0,
            "total_kwh": 0,
            "message": "No telemetry data available"
        }
    
    # Pre-fetch all energy prices for the last 24 hours (same as dashboard)
    result = await db.execute(
        select(EnergyPrice)
        .where(EnergyPrice.valid_from >= start_time)
        .order_by(EnergyPrice.valid_from)
    )
    energy_prices = result.scalars().all()
    
    # Create a lookup function for energy prices (same as dashboard)
    def get_price_for_timestamp(ts):
        for price in energy_prices:
            if price.valid_from <= ts < price.valid_to:
                return price.price_pence
        return None
    
    # Calculate total cost by matching telemetry records with energy prices
    total_cost_pence = 0
    total_power_readings = 0
    total_power_sum = 0
    
    for i, telem in enumerate(telemetry_records):
        power = telem.power_watts
        
        # Fallback to manual power if no auto-detected power
        if not power or power <= 0:
            if miner.manual_power_watts:
                power = miner.manual_power_watts
            else:
                continue
        
        # Find the energy price for this timestamp using cached lookup
        price_pence = get_price_for_timestamp(telem.timestamp)
        
        if price_pence:
            # Calculate duration until next reading (same logic as dashboard)
            if i < len(telemetry_records) - 1:
                next_timestamp = telemetry_records[i + 1].timestamp
                duration_seconds = (next_timestamp - telem.timestamp).total_seconds()
                duration_hours = duration_seconds / 3600.0
                
                # Cap duration at 10 minutes to prevent counting offline gaps
                # Telemetry is recorded every 30s, so >10min gap = miner was offline
                max_duration_hours = 10.0 / 60.0  # 10 minutes in hours
                if duration_hours > max_duration_hours:
                    duration_hours = max_duration_hours
            else:
                # Last reading: assume 30 seconds
                duration_hours = 30.0 / 3600.0
            
            # Calculate energy consumed during this period
            energy_kwh = (power / 1000) * duration_hours
            cost_pence = energy_kwh * price_pence
            total_cost_pence += cost_pence
        
        total_power_sum += power
        total_power_readings += 1
    
    # Calculate averages
    avg_power_watts = total_power_sum / total_power_readings if total_power_readings > 0 else 0
    
    # Calculate total kWh (average power over 24 hours)
    total_kwh = (avg_power_watts / 1000) * 24
    
    return {
        "miner_id": miner_id,
        "miner_name": miner.name,
        "period_hours": 24,
        "cost_pence": round(total_cost_pence, 2),
        "cost_pounds": round(total_cost_pence / 100, 2),
        "avg_power_watts": round(avg_power_watts, 2),
        "total_kwh": round(total_kwh, 3),
        "data_points": total_power_readings
    }


class BulkOperationRequest(BaseModel):
    miner_ids: List[int]
    mode: str | None = None
    pool_id: int | None = None


@router.post("/bulk/enable")
async def bulk_enable_miners(
    request: BulkOperationRequest,
    db: AsyncSession = Depends(get_db)
):
    """Enable multiple miners"""
    success = 0
    failed = 0
    
    for miner_id in request.miner_ids:
        try:
            result = await db.execute(select(Miner).where(Miner.id == miner_id))
            miner = result.scalar_one_or_none()
            if miner:
                miner.enabled = True
                success += 1
            else:
                failed += 1
        except Exception as e:
            print(f"Failed to enable miner {miner_id}: {e}")
            failed += 1
    
    await db.commit()
    return {"success": success, "failed": failed}


@router.post("/bulk/disable")
async def bulk_disable_miners(
    request: BulkOperationRequest,
    db: AsyncSession = Depends(get_db)
):
    """Disable multiple miners"""
    success = 0
    failed = 0
    
    for miner_id in request.miner_ids:
        try:
            result = await db.execute(select(Miner).where(Miner.id == miner_id))
            miner = result.scalar_one_or_none()
            if miner:
                miner.enabled = False
                success += 1
            else:
                failed += 1
        except Exception as e:
            print(f"Failed to disable miner {miner_id}: {e}")
            failed += 1
    
    await db.commit()
    return {"success": success, "failed": failed}


@router.post("/bulk/mode")
async def bulk_set_mode(
    request: BulkOperationRequest,
    db: AsyncSession = Depends(get_db)
):
    """Set mode for multiple miners"""
    if not request.mode:
        raise HTTPException(status_code=400, detail="Mode is required")
    
    success = 0
    failed = 0
    
    for miner_id in request.miner_ids:
        try:
            result = await db.execute(select(Miner).where(Miner.id == miner_id))
            miner = result.scalar_one_or_none()
            if not miner:
                failed += 1
                continue
            
            adapter = create_adapter(
                miner.miner_type,
                miner.id,
                miner.name,
                miner.ip_address,
                miner.port,
                miner.config or {}
            )
            
            await adapter.set_mode(request.mode)
            miner.current_mode = request.mode
            miner.last_mode_change = datetime.utcnow()
            success += 1
        except Exception as e:
            print(f"Failed to set mode for miner {miner_id}: {e}")
            failed += 1
    
    await db.commit()
    return {"success": success, "failed": failed}


@router.post("/bulk/pool")
async def bulk_switch_pool(
    request: BulkOperationRequest,
    db: AsyncSession = Depends(get_db)
):
    """Switch pool for multiple miners"""
    if not request.pool_id:
        raise HTTPException(status_code=400, detail="Pool ID is required")
    
    # Get pool details
    result = await db.execute(select(Pool).where(Pool.id == request.pool_id))
    pool = result.scalar_one_or_none()
    if not pool:
        raise HTTPException(status_code=404, detail="Pool not found")
    
    success = 0
    failed = 0
    
    for miner_id in request.miner_ids:
        try:
            result = await db.execute(select(Miner).where(Miner.id == miner_id))
            miner = result.scalar_one_or_none()
            if not miner:
                failed += 1
                continue
            
            adapter = create_adapter(
                miner.miner_type,
                miner.id,
                miner.name,
                miner.ip_address,
                miner.port,
                miner.config or {}
            )
            
            await adapter.switch_pool(pool.url, pool.port, pool.username, pool.password or "")
            success += 1
        except Exception as e:
            print(f"Failed to switch pool for miner {miner_id}: {e}")
            failed += 1
    
    return {"success": success, "failed": failed}


@router.post("/bulk/restart")
async def bulk_restart_miners(
    request: BulkOperationRequest,
    db: AsyncSession = Depends(get_db)
):
    """Restart multiple miners"""
    success = 0
    failed = 0
    
    for miner_id in request.miner_ids:
        try:
            result = await db.execute(select(Miner).where(Miner.id == miner_id))
            miner = result.scalar_one_or_none()
            if not miner:
                failed += 1
                continue
            
            adapter = create_adapter(
                miner.miner_type,
                miner.id,
                miner.name,
                miner.ip_address,
                miner.port,
                miner.config or {}
            )
            
            await adapter.restart()
            success += 1
        except Exception as e:
            print(f"Failed to restart miner {miner_id}: {e}")
            failed += 1
    
    return {"success": success, "failed": failed}


@router.post("/{miner_id}/sync-pool-slots")
async def sync_miner_pool_slots(
    miner_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Sync pool slots for a specific Avalon Nano miner"""
    from core.pool_slots import sync_avalon_nano_pool_slots
    from sqlalchemy import and_
    
    # Verify miner exists and is an Avalon Nano
    result = await db.execute(select(Miner).where(Miner.id == miner_id))
    miner = result.scalar_one_or_none()
    
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")
    
    if miner.miner_type != "avalon_nano":
        raise HTTPException(status_code=400, detail="Only Avalon Nano miners have pool slots")
    
    try:
        # Run the sync for just this miner by temporarily filtering
        await sync_avalon_nano_pool_slots(db)
        return {"success": True, "message": f"Pool slots synced for {miner.name}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sync pool slots: {str(e)}")
