"""
Pool management API endpoints
"""
import asyncio
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List
from pydantic import BaseModel

from core.database import get_db, Pool, PoolBlockEffort, Event


router = APIRouter()
RECOVERY_EVENT_SOURCE = "pool_driver_recovery"


async def _detect_pool_driver(
    pool_loader,
    url: str,
    port: int,
    logger,
    attempts: int = 2,
    retry_delay_seconds: float = 0.2,
) -> str | None:
    """Best-effort driver detection for a pool endpoint with retry/backoff."""
    effective_attempts = max(1, attempts)
    for attempt in range(1, effective_attempts + 1):
        for driver_type, driver in pool_loader.drivers.items():
            try:
                if await driver.detect(url, port):
                    logger.info(f"Detected driver '{driver_type}' for {url}:{port} (attempt {attempt}/{effective_attempts})")
                    return driver_type
            except Exception as e:
                logger.debug(
                    f"Driver detection error for '{driver_type}' on {url}:{port} "
                    f"(attempt {attempt}/{effective_attempts}): {e}"
                )

        if attempt < effective_attempts:
            await asyncio.sleep(retry_delay_seconds)

    return None


def _ensure_pool_config_driver(pool_config: dict | None, driver_type: str) -> dict:
    """Ensure pool_config always carries the resolved driver value."""
    config = dict(pool_config or {})
    config["driver"] = driver_type
    return config


def _record_recovery_event(
    db: AsyncSession,
    *,
    event_type: str,
    message: str,
    pool: Pool,
    context: str,
    old_pool_type: str | None,
    resolved_pool_type: str | None,
) -> None:
    """Append a structured pool driver recovery event."""
    db.add(
        Event(
            event_type=event_type,
            source=RECOVERY_EVENT_SOURCE,
            message=message,
            data={
                "pool_id": pool.id,
                "pool_name": pool.name,
                "url": pool.url,
                "port": pool.port,
                "context": context,
                "from_pool_type": old_pool_type,
                "to_pool_type": resolved_pool_type,
            },
        )
    )


class PoolCreate(BaseModel):
    name: str
    url: str
    port: int
    user: str
    password: str
    enabled: bool = True
    pool_config: dict | None = None
    show_on_dashboard: bool = True


class PoolUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    enabled: bool | None = None
    pool_config: dict | None = None
    show_on_dashboard: bool | None = None
    sort_order: int | None = None


class PoolResponse(BaseModel):
    id: int
    name: str
    url: str
    port: int
    user: str
    password: str
    enabled: bool
    pool_config: dict | None = None
    show_on_dashboard: bool = True
    sort_order: int = 0
    
    class Config:
        from_attributes = True


@router.get("/", response_model=List[PoolResponse])
async def list_pools(db: AsyncSession = Depends(get_db)):
    """List all pools"""
    from sqlalchemy import func
    result = await db.execute(select(Pool).order_by(func.lower(Pool.name)))
    pools = result.scalars().all()
    return pools


@router.get("/for-bands")
async def list_pools_for_bands(db: AsyncSession = Depends(get_db)):
    """
    List pools suitable for price band strategy selection.
    Returns simplified pool data with supported coins.
    """
    result = await db.execute(
        select(Pool)
        .where(Pool.enabled == True)
        .order_by(Pool.name)
    )
    pools = result.scalars().all()
    
    pool_options = []
    for pool in pools:
        # Get supported coin from pool_config
        supported_coins = []
        if pool.pool_config and pool.pool_config.get("coin"):
            supported_coins = [pool.pool_config.get("coin").upper()]
        
        pool_options.append({
            "id": pool.id,
            "name": pool.name,
            "pool_type": pool.pool_type,
            "supported_coins": supported_coins
        })
    
    return pool_options


@router.get("/performance")
async def get_pool_performance(range: str = "24h", db: AsyncSession = Depends(get_db)):
    """Get pool performance comparison data"""
    from datetime import datetime, timedelta
    from sqlalchemy import func
    from core.database import PoolHealth
    
    # Parse time range
    range_hours = {
        "24h": 24,
        "3d": 72,
        "7d": 168,
        "30d": 720
    }.get(range, 24)
    
    cutoff_time = datetime.utcnow() - timedelta(hours=range_hours)
    
    # Get all enabled pools
    result = await db.execute(select(Pool).where(Pool.enabled == True))
    pools = result.scalars().all()
    
    pool_data = []
    
    for pool in pools:
        # Get health history for this pool
        history_result = await db.execute(
            select(PoolHealth)
            .where(PoolHealth.pool_id == pool.id)
            .where(PoolHealth.timestamp >= cutoff_time)
            .order_by(PoolHealth.timestamp)
        )
        history = history_result.scalars().all()
        
        # Calculate averages
        if history:
            avg_luck = sum(h.luck_percentage for h in history if h.luck_percentage is not None) / len([h for h in history if h.luck_percentage is not None]) if any(h.luck_percentage is not None for h in history) else None
            avg_latency = sum(h.response_time_ms for h in history if h.response_time_ms is not None) / len([h for h in history if h.response_time_ms is not None]) if any(h.response_time_ms is not None for h in history) else None
            avg_health = sum(h.health_score for h in history if h.health_score is not None) / len([h for h in history if h.health_score is not None]) if any(h.health_score is not None for h in history) else None
            avg_reject = sum(h.reject_rate for h in history if h.reject_rate is not None) / len([h for h in history if h.reject_rate is not None]) if any(h.reject_rate is not None for h in history) else None
        else:
            avg_luck = avg_latency = avg_health = avg_reject = None
        
        pool_data.append({
            "id": pool.id,
            "name": pool.name,
            "avg_luck": avg_luck,
            "avg_latency": avg_latency,
            "avg_health": avg_health,
            "avg_reject": avg_reject,
            "history": [
                {
                    "timestamp": h.timestamp.isoformat(),
                    "luck": h.luck_percentage or 0,
                    "latency": h.response_time_ms or 0,
                    "health": h.health_score or 0,
                    "reject_rate": h.reject_rate or 0
                }
                for h in history
            ]
        })
    
    return {"pools": pool_data, "range": range}


@router.post("/", response_model=PoolResponse)
async def create_pool(pool: PoolCreate, db: AsyncSession = Depends(get_db)):
    """Create new pool"""
    from core.pool_loader import get_pool_loader
    import logging
    
    logger = logging.getLogger(__name__)
    
    # Auto-detect pool type using drivers
    pool_loader = get_pool_loader()
    detected_driver = await _detect_pool_driver(pool_loader, pool.url, pool.port, logger)
    
    if not detected_driver:
        logger.warning(f"Could not detect driver for {pool.url}:{pool.port}, defaulting to 'unknown'")
        detected_driver = "unknown"
    
    # Create pool config based on detected driver
    pool_config = {"driver": detected_driver}
    if detected_driver != "unknown":
        driver_instance = pool_loader.get_driver(detected_driver)
        if driver_instance:
            # Get config schema and populate with defaults
            config_schema = driver_instance.get_config_schema()
            if config_schema:
                for k, v in config_schema.items():
                    if "default" in v:
                        pool_config[k] = v.get("default")
    
    # DEBUG: Log received pool data
    logger.info(f"[DEBUG] Creating pool - name={pool.name}, user={pool.user}, url={pool.url}, port={pool.port}, detected_driver={detected_driver}")
    
    db_pool = Pool(
        name=pool.name,
        url=pool.url,
        port=pool.port,
        user=pool.user,
        password=pool.password,
        enabled=pool.enabled,
        pool_type=detected_driver or "unknown",
        pool_config=pool_config if pool_config else None,
        show_on_dashboard=pool.show_on_dashboard,
    )
    
    db.add(db_pool)
    await db.commit()
    await db.refresh(db_pool)

    if detected_driver == "unknown":
        _record_recovery_event(
            db,
            event_type="warning",
            message=f"Pool driver unresolved after detection retries for {pool.url}:{pool.port}",
            pool=db_pool,
            context="create",
            old_pool_type=None,
            resolved_pool_type="unknown",
        )
        await db.commit()
    
    return db_pool


# Placement note: dynamic pool routes must come last so they don't intercept
# other named paths.


@router.get("/effort")
async def get_pool_efforts(db: AsyncSession = Depends(get_db)):
    """
    Get block effort statistics for all pools
    Returns cumulative effort (blocks equivalent) per pool
    """
    result = await db.execute(select(PoolBlockEffort))
    efforts = result.scalars().all()
    
    return {
        "efforts": [
            {
                "pool_name": effort.pool_name,
                "coin": effort.coin,
                "effort_start": effort.effort_start.isoformat(),
                "last_reset": effort.last_reset.isoformat() if effort.last_reset else None,
                "total_shares_accepted": effort.total_shares_accepted,
                "total_hashes": effort.total_hashes,
                "current_network_difficulty": effort.current_network_difficulty,
                "blocks_equivalent": effort.blocks_equivalent,
                "last_updated": effort.last_updated.isoformat()
            }
            for effort in efforts
        ]
    }


@router.get("/effort/{pool_name}")
async def get_pool_effort(pool_name: str, db: AsyncSession = Depends(get_db)):
    """
    Get block effort statistics for a specific pool
    """
    result = await db.execute(
        select(PoolBlockEffort).where(PoolBlockEffort.pool_name == pool_name)
    )
    effort = result.scalar_one_or_none()
    
    if not effort:
        raise HTTPException(status_code=404, detail="Pool effort not found")
    
    return {
        "pool_name": effort.pool_name,
        "coin": effort.coin,
        "effort_start": effort.effort_start.isoformat(),
        "last_reset": effort.last_reset.isoformat() if effort.last_reset else None,
        "total_shares_accepted": effort.total_shares_accepted,
        "total_hashes": effort.total_hashes,
        "current_network_difficulty": effort.current_network_difficulty,
        "blocks_equivalent": effort.blocks_equivalent,
        "last_updated": effort.last_updated.isoformat()
    }


@router.post("/effort/{pool_name}/reset")
async def reset_pool_effort_manual(pool_name: str, db: AsyncSession = Depends(get_db)):
    """
    Manually reset pool effort counter (admin use)
    """
    from core.high_diff_tracker import reset_pool_block_effort
    
    await reset_pool_block_effort(db, pool_name)
    await db.commit()
    
    return {"status": "reset", "pool_name": pool_name}


@router.get("/{pool_id:int}", response_model=PoolResponse)
async def get_pool(pool_id: int, db: AsyncSession = Depends(get_db)):
    """Get pool by ID"""
    result = await db.execute(select(Pool).where(Pool.id == pool_id))
    pool = result.scalar_one_or_none()
    
    if not pool:
        raise HTTPException(status_code=404, detail="Pool not found")
    
    return pool


@router.put("/{pool_id:int}", response_model=PoolResponse)
async def update_pool(pool_id: int, pool_update: PoolUpdate, db: AsyncSession = Depends(get_db)):
    """Update pool configuration"""
    from core.pool_loader import get_pool_loader
    import logging

    logger = logging.getLogger(__name__)

    result = await db.execute(select(Pool).where(Pool.id == pool_id))
    pool = result.scalar_one_or_none()
    
    if not pool:
        raise HTTPException(status_code=404, detail="Pool not found")
    
    original_pool_type = pool.pool_type
    pool_loader = get_pool_loader()

    # Track endpoint changes for auto re-detection
    endpoint_changed = False
    requested_driver: str | None = None
    requested_driver_valid = False

    # Update fields
    if pool_update.name is not None:
        pool.name = pool_update.name
    if pool_update.url is not None:
        pool.url = pool_update.url
        endpoint_changed = True
    if pool_update.port is not None:
        pool.port = pool_update.port
        endpoint_changed = True
    if pool_update.user is not None:
        pool.user = pool_update.user
    if pool_update.password is not None:
        pool.password = pool_update.password
    if pool_update.enabled is not None:
        pool.enabled = pool_update.enabled
    if pool_update.pool_config is not None:
        pool.pool_config = pool_update.pool_config
        requested_driver = (pool.pool_config or {}).get("driver")
        if requested_driver:
            requested_driver_valid = pool_loader.get_driver(requested_driver) is not None
            if requested_driver_valid:
                if pool.pool_type != requested_driver:
                    logger.info(
                        "Aligning pool_type to requested pool_config.driver '%s' for pool %s",
                        requested_driver,
                        pool.name,
                    )
                pool.pool_type = requested_driver
                pool.pool_config = _ensure_pool_config_driver(pool.pool_config, requested_driver)
            else:
                logger.warning(
                    "Requested pool_config.driver '%s' is not loaded; falling back to auto-detection for %s:%s",
                    requested_driver,
                    pool.url,
                    pool.port,
                )
    if pool_update.show_on_dashboard is not None:
        pool.show_on_dashboard = pool_update.show_on_dashboard
    if pool_update.sort_order is not None:
        pool.sort_order = pool_update.sort_order

    # Auto-recover pool_type if unknown or endpoint changed.
    if (not requested_driver_valid) and (endpoint_changed or not pool.pool_type or pool.pool_type == "unknown"):
        detected_driver = await _detect_pool_driver(pool_loader, pool.url, pool.port, logger)
        if detected_driver:
            pool.pool_type = detected_driver
            pool.pool_config = _ensure_pool_config_driver(pool.pool_config, detected_driver)
            _record_recovery_event(
                db,
                event_type="info",
                message=f"Recovered pool driver '{detected_driver}' for {pool.url}:{pool.port}",
                pool=pool,
                context="update",
                old_pool_type=original_pool_type,
                resolved_pool_type=detected_driver,
            )
        else:
            logger.warning(f"Could not detect driver for {pool.url}:{pool.port}, keeping 'unknown'")
            pool.pool_type = "unknown"
            pool.pool_config = _ensure_pool_config_driver(pool.pool_config, "unknown")
            _record_recovery_event(
                db,
                event_type="warning",
                message=f"Pool driver unresolved after detection retries for {pool.url}:{pool.port}",
                pool=pool,
                context="update",
                old_pool_type=original_pool_type,
                resolved_pool_type="unknown",
            )
    elif pool.pool_config is not None:
        # Keep driver key aligned when caller updates pool_config explicitly.
        pool.pool_config = _ensure_pool_config_driver(pool.pool_config, pool.pool_type)
    
    await db.commit()
    await db.refresh(pool)
    
    return pool


class PoolReorderItem(BaseModel):
    pool_id: int
    sort_order: int


class PoolRecoveryStatusPool(BaseModel):
    pool_id: int
    pool_name: str
    recovered_count: int
    unresolved_count: int
    last_event_at: str | None = None
    last_message: str | None = None


class PoolRecoveryStatusResponse(BaseModel):
    window_hours: int
    totals: dict
    pools: List[PoolRecoveryStatusPool]


@router.patch("/reorder")
async def reorder_pools(items: List[PoolReorderItem], db: AsyncSession = Depends(get_db)):
    """
    Reorder dashboard pool tiles.
    
    Accepts an array of {pool_id, sort_order} items and updates the sort_order
    for each pool. Lower sort_order values appear first on the dashboard.
    
    Example: [{"pool_id": 1, "sort_order": 0}, {"pool_id": 3, "sort_order": 1}]
    """
    try:
        # Validate all pool IDs exist
        pool_ids = [item.pool_id for item in items]
        result = await db.execute(select(Pool).where(Pool.id.in_(pool_ids)))
        existing_pools = {pool.id: pool for pool in result.scalars().all()}
        
        # Check for missing pools
        missing_ids = set(pool_ids) - set(existing_pools.keys())
        if missing_ids:
            raise HTTPException(
                status_code=404, 
                detail=f"Pools not found: {', '.join(map(str, missing_ids))}"
            )
        
        # Update sort_order for each pool
        for item in items:
            pool = existing_pools[item.pool_id]
            pool.sort_order = item.sort_order
        
        await db.commit()
        
        return {
            "success": True,
            "updated_count": len(items),
            "message": f"Successfully reordered {len(items)} pools"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to reorder pools: {str(e)}")


@router.get("/recovery-status", response_model=PoolRecoveryStatusResponse)
async def get_pool_recovery_status(window_hours: int = 24, db: AsyncSession = Depends(get_db)):
    """
    Summarize pool driver recovery outcomes from event logs.
    """
    hours = max(1, min(window_hours, 168))
    cutoff = datetime.utcnow() - timedelta(hours=hours)

    result = await db.execute(
        select(Event)
        .where(Event.source == RECOVERY_EVENT_SOURCE)
        .where(Event.timestamp >= cutoff)
        .order_by(Event.timestamp.desc())
    )
    events = result.scalars().all()

    totals = {"recovered": 0, "unresolved": 0}
    by_pool: dict[int, dict] = {}

    for event in events:
        event_data = event.data or {}
        pool_id = event_data.get("pool_id")
        if pool_id is None:
            continue

        try:
            pool_id = int(pool_id)
        except Exception:
            continue

        bucket = by_pool.setdefault(
            pool_id,
            {
                "pool_id": pool_id,
                "pool_name": str(event_data.get("pool_name") or f"Pool {pool_id}"),
                "recovered_count": 0,
                "unresolved_count": 0,
                "last_event_at": event.timestamp,
                "last_message": event.message,
            },
        )

        if event.event_type == "info":
            bucket["recovered_count"] += 1
            totals["recovered"] += 1
        else:
            bucket["unresolved_count"] += 1
            totals["unresolved"] += 1

        if bucket["last_event_at"] is None or event.timestamp > bucket["last_event_at"]:
            bucket["last_event_at"] = event.timestamp
            bucket["last_message"] = event.message

    pools = [
        PoolRecoveryStatusPool(
            pool_id=entry["pool_id"],
            pool_name=entry["pool_name"],
            recovered_count=entry["recovered_count"],
            unresolved_count=entry["unresolved_count"],
            last_event_at=(entry["last_event_at"].isoformat() if entry["last_event_at"] else None),
            last_message=entry["last_message"],
        )
        for entry in by_pool.values()
    ]
    pools.sort(key=lambda item: (item.unresolved_count, item.recovered_count), reverse=True)

    return PoolRecoveryStatusResponse(window_hours=hours, totals=totals, pools=pools)


@router.delete("/{pool_id:int}")
async def delete_pool(pool_id: int, db: AsyncSession = Depends(get_db)):
    """Delete pool"""
    result = await db.execute(select(Pool).where(Pool.id == pool_id))
    pool = result.scalar_one_or_none()
    
    if not pool:
        raise HTTPException(status_code=404, detail="Pool not found")
    
    await db.delete(pool)
    await db.commit()
    
    return {"status": "deleted"}
