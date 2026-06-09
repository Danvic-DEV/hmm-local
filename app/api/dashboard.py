"""
Dashboard and analytics API endpoints
"""
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text
from datetime import datetime, timedelta, timezone
import time
import logging

from core.database import get_db, Miner, Telemetry, EnergyPrice, Event, HighDiffShare, PriceBandStrategyConfig, PoolBlockEffort
from core.dashboard_pool_service import DashboardPoolService
from core.pool_loader import get_pool_loader
from core.pool_warnings import derive_pool_warnings
from core.utils import format_hashrate

logger = logging.getLogger(__name__)
router = APIRouter()

_DASHBOARD_ALL_CACHE: dict = {}
_DASHBOARD_ALL_CACHE_TTL_SECONDS = 30
_DASHBOARD_ALL_STALE_MAX_SECONDS = 180
_DASHBOARD_ALL_COMPUTE_LOCKS: dict[str, asyncio.Lock] = {}
_DASHBOARD_EARNINGS_CACHE: dict = {}
_DASHBOARD_EARNINGS_CACHE_TTL_SECONDS = 60


# Helper functions for pool stats (migrated from SolopoolService)
def extract_username(pool_user: str) -> str:
    """Extract username from pool.user field (format: username.worker or just username)"""
    if not pool_user:
        return ""
    return pool_user.split(".")[0] if "." in pool_user else pool_user


def format_stats_summary(stats: dict) -> dict:
    """
    Format pool stats into a standardized summary structure.
    Uses format_hashrate() for consistent formatting.
    """
    if not stats:
        return {}
    
    # Import format_hashrate utility
    from core.utils import format_hashrate
    
    # Extract hashrate and format using utility
    hashrate_raw = stats.get("hashrate", 0)
    hashrate_formatted = format_hashrate(hashrate_raw, "H/s")  # Pool stats typically in H/s
    
    # Format workers data
    workers = stats.get("workers", {})
    workers_online = 0
    workers_offline = 0
    
    if isinstance(workers, dict):
        for worker_name, worker_data in workers.items():
            if isinstance(worker_data, dict):
                if worker_data.get("lastShare", 0) > 0:
                    workers_online += 1
                else:
                    workers_offline += 1
    
    # Extract earnings
    earnings = stats.get("stats", {})
    paid_24h = earnings.get("paid24h", 0) if isinstance(earnings, dict) else 0
    paid_7d = earnings.get("paid7d", 0) if isinstance(earnings, dict) else 0
    paid_30d = earnings.get("paid", 0) if isinstance(earnings, dict) else 0
    
    # Extract blocks
    blocks_24h = stats.get("24hBlocks", 0)
    blocks_7d = stats.get("7dBlocks", 0)
    blocks_30d = stats.get("blocks", 0)
    
    # Luck
    luck = stats.get("luck", {})
    luck_24h = luck.get("24h", 0) if isinstance(luck, dict) else 0
    luck_7d = luck.get("7d", 0) if isinstance(luck, dict) else 0
    
    return {
        "hashrate": hashrate_formatted,  # Structured format
        "workers_online": workers_online,
        "workers_offline": workers_offline,
        "paid_24h": paid_24h,
        "paid_7d": paid_7d,
        "paid_30d": paid_30d,
        "blocks_24h": blocks_24h,
        "blocks_7d": blocks_7d,
        "blocks_30d": blocks_30d,
        "luck_24h": luck_24h,
        "luck_7d": luck_7d,
    }


def parse_coin_from_pool(pool_url: str) -> str:
    """Extract coin symbol from pool URL"""
    if not pool_url:
        return None
    
    pool_url = pool_url.lower()
    
    # Braiins Pool patterns
    if "braiins" in pool_url or "slushpool" in pool_url:
        return "BTC"
    
    # NerdMiners Pool patterns
    if "nerdminers" in pool_url:
        return "BTC"
    
    # Solopool.org patterns
    if "dgb" in pool_url:
        return "DGB"
    elif "bch" in pool_url or "eu2.solopool.org" in pool_url:
        return "BCH"
    elif "bc2" in pool_url:
        return "BC2"
    elif "btc" in pool_url:
        return "BTC"
    elif "eu1.solopool.org" in pool_url or "us1.solopool.org" in pool_url:
        # Default to DGB for shared pools
        return "DGB"
    
    return None


def _to_utc_iso8601(dt):
    """Serialize datetimes as explicit UTC ISO8601 to avoid client timezone ambiguity."""
    if not dt:
        return None

    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt.isoformat().replace("+00:00", "Z")


async def get_best_share_24h(db: AsyncSession) -> dict:
    """
    Get best difficulty share in last 24 hours for ASIC dashboard
    Uses HighDiffShare table which tracks when shares were actually found
    """
    from core.high_diff_tracker import get_network_difficulty
    
    # Query HighDiffShare table for actual share finds in last 24h
    cutoff_24h = datetime.utcnow() - timedelta(hours=24)
    
    result = await db.execute(
        select(HighDiffShare)
        .where(HighDiffShare.timestamp > cutoff_24h)
        .order_by(HighDiffShare.difficulty.desc())
        .limit(1)
    )
    best_share = result.scalar_one_or_none()
    
    if not best_share:
        return {
            "difficulty": 0,
            "coin": None,
            "network_difficulty": None,
            "percentage": 0.0,
            "timestamp": None,
            "time_ago_seconds": None
        }
    
    # Get current network difficulty for the coin (use cached value if recent)
    network_diff = best_share.network_difficulty
    if not network_diff or network_diff == 0:
        network_diff = await get_network_difficulty(best_share.coin)
    
    # Calculate percentage
    percentage = 0.0
    if network_diff and network_diff > 0:
        percentage = (best_share.difficulty / network_diff) * 100
    
    # Calculate time ago
    time_ago_seconds = int((datetime.utcnow() - best_share.timestamp).total_seconds())
    
    return {
        "difficulty": best_share.difficulty,
        "coin": best_share.coin,
        "network_difficulty": network_diff,
        "percentage": round(percentage, 2),
        "timestamp": best_share.timestamp.isoformat(),
        "time_ago_seconds": time_ago_seconds
    }


@router.get("/stats")
async def get_dashboard_stats(db: AsyncSession = Depends(get_db)):
    """Get overall dashboard statistics"""
    from core.database import engine
    
    # Check if PostgreSQL with materialized view
    is_postgresql = 'postgresql' in str(engine.url)
    miners = []  # Initialize miners list for both paths
    
    if is_postgresql:
        # Fast path: Use materialized view (refreshed every 5min)
        try:
            mv_query = text("""
                SELECT 
                    COUNT(*) as total_miners,
                    COUNT(*) FILTER (WHERE enabled = true) as active_miners,
                    COUNT(*) FILTER (WHERE seconds_since_last_telemetry < 300) as online_miners,
                    COALESCE(SUM(current_hashrate), 0) as total_hashrate,
                    COALESCE(SUM(current_power), 0) as total_power_watts
                FROM dashboard_stats_mv
            """)
            result = await db.execute(mv_query)
            row = result.fetchone()
            
            if row:
                total_miners = row[0]
                active_miners = row[1]
                online_miners = row[2]
                total_hashrate = float(row[3] or 0)
                total_power_watts = float(row[4] or 0)
            else:
                # Fallback if view is empty
                total_miners = active_miners = online_miners = 0
                total_hashrate = total_power_watts = 0.0
            
            # For PostgreSQL, still need to fetch miners list for later processing
            result = await db.execute(select(Miner).where(Miner.enabled == True))
            miners = result.scalars().all()
                
        except Exception as e:
            logger.warning(f"Materialized view query failed, using fallback: {e}")
            is_postgresql = False  # Fall through to direct-query path
    
    if not is_postgresql:
        # Direct-query fallback when materialized view is unavailable
        # Count miners
        result = await db.execute(select(func.count(Miner.id)))
        total_miners = result.scalar()
        
        result = await db.execute(select(func.count(Miner.id)).where(Miner.enabled == True))
        active_miners = result.scalar()
        
        # Get latest telemetry for each miner for total hashrate
        # Use a subquery to get the latest timestamp per miner, then sum their hashrates
        from sqlalchemy import and_
        
        # Get latest telemetry for each enabled miner
        total_hashrate = 0.0
        total_power_watts = 0.0
        online_miners = 0
        result = await db.execute(select(Miner).where(Miner.enabled == True))
        miners = result.scalars().all()
        
        cutoff = datetime.utcnow() - timedelta(minutes=5)
        for miner in miners:
            result = await db.execute(
                select(Telemetry.hashrate, Telemetry.power_watts)
                .where(Telemetry.miner_id == miner.id)
                .where(Telemetry.timestamp > cutoff)
                .order_by(Telemetry.timestamp.desc())
                .limit(1)
            )
            latest_data = result.first()
            if latest_data and latest_data[0]:  # If hashrate exists
                latest_hashrate, latest_power = latest_data
                total_hashrate += latest_hashrate
                # Count power usage
                if latest_power:
                    total_power_watts += latest_power
                online_miners += 1
    
    # Calculate average efficiency (W/TH) for ASIC miners
    # Efficiency = Watts / Hashrate_TH = Watts per Terahash
    avg_efficiency_wth = None
    if total_hashrate > 0 and total_power_watts > 0:
        hashrate_ths = total_hashrate / 1000.0  # Convert GH/s to TH/s
        avg_efficiency_wth = total_power_watts / hashrate_ths

    # Get pool hashrate from DashboardPoolService
    pool_tiles = await DashboardPoolService.get_platform_tiles(db)
    total_pool_hashrate_ghs = 0.0
    for pool_data in pool_tiles.values():
        if pool_data.get("tile_2_network") and pool_data["tile_2_network"].get("pool_hashrate"):
            pool_hr = pool_data["tile_2_network"]["pool_hashrate"]
            # Handle structured format {value: X, unit: "GH/s"} or plain numeric value
            if isinstance(pool_hr, dict) and "value" in pool_hr:
                total_pool_hashrate_ghs += pool_hr["value"]
            elif isinstance(pool_hr, (int, float)):
                total_pool_hashrate_ghs += pool_hr
    
    # Calculate pool efficiency
    pool_efficiency_percent = None
    if total_hashrate > 0 and total_pool_hashrate_ghs > 0:
        pool_efficiency_percent = (total_pool_hashrate_ghs / total_hashrate) * 100.0
    
    # Get current energy price
    now = datetime.utcnow()
    result = await db.execute(
        select(EnergyPrice.price_pence)
        .where(EnergyPrice.valid_from <= now)
        .where(EnergyPrice.valid_to > now)
        .limit(1)
    )
    current_price = result.scalar()
    
    # Count recent events
    cutoff_24h = datetime.utcnow() - timedelta(hours=24)
    result = await db.execute(
        select(func.count(Event.id))
        .where(Event.timestamp > cutoff_24h)
    )
    recent_events = result.scalar()
    
    # Pre-fetch all energy prices for the last 24 hours (avoid N queries per telemetry record)
    result = await db.execute(
        select(EnergyPrice)
        .where(EnergyPrice.valid_from >= cutoff_24h)
        .order_by(EnergyPrice.valid_from)
    )
    energy_prices = result.scalars().all()

    miner_ids = [miner.id for miner in miners]
    telemetry_by_miner = {miner_id: [] for miner_id in miner_ids}
    if miner_ids:
        result = await db.execute(
            select(Telemetry)
            .where(Telemetry.miner_id.in_(miner_ids))
            .where(Telemetry.timestamp > cutoff_24h)
            .order_by(Telemetry.miner_id, Telemetry.timestamp.asc())
        )
        for telemetry in result.scalars().all():
            telemetry_by_miner[telemetry.miner_id].append(telemetry)
    
    # Create a lookup function for energy prices
    def get_price_for_timestamp(ts):
        for price in energy_prices:
            if price.valid_from <= ts < price.valid_to:
                return price.price_pence
        return None

    miner_ids = [miner.id for miner in miners]
    telemetry_by_miner = {miner_id: [] for miner_id in miner_ids}
    if miner_ids:
        result = await db.execute(
            select(Telemetry)
            .where(Telemetry.miner_id.in_(miner_ids))
            .where(Telemetry.timestamp > cutoff_24h)
            .order_by(Telemetry.miner_id, Telemetry.timestamp.asc())
        )
        for telemetry in result.scalars().all():
            telemetry_by_miner[telemetry.miner_id].append(telemetry)

    miner_ids = [miner.id for miner in miners]
    telemetry_by_miner = {miner_id: [] for miner_id in miner_ids}
    if miner_ids:
        result = await db.execute(
            select(Telemetry)
            .where(Telemetry.miner_id.in_(miner_ids))
            .where(Telemetry.timestamp > cutoff_24h)
            .order_by(Telemetry.miner_id, Telemetry.timestamp.asc())
        )
        for telemetry in result.scalars().all():
            telemetry_by_miner[telemetry.miner_id].append(telemetry)
    
    # Calculate total 24h cost across all miners using actual telemetry + energy prices
    total_cost_pence = 0.0
    total_kwh_consumed_24h = 0.0
    for miner in miners:
        # Get telemetry for last 24 hours
        result = await db.execute(
            select(Telemetry.power_watts, Telemetry.timestamp)
            .where(Telemetry.miner_id == miner.id)
            .where(Telemetry.timestamp > cutoff_24h)
            .order_by(Telemetry.timestamp)
        )
        telemetry_records = result.all()
        
        if not telemetry_records:
            continue
        
        # Calculate cost by matching each telemetry reading with the energy price that was active at that time
        for i, (power, timestamp) in enumerate(telemetry_records):
            if power is None or power <= 0:
                continue
            
            # Find the energy price that was active when this telemetry was recorded
            price_pence = get_price_for_timestamp(timestamp)
            
            if price_pence is None:
                # No price data for this timestamp, skip
                continue
            
            # Calculate duration until next telemetry reading (or assume 30s if it's the last one)
            if i < len(telemetry_records) - 1:
                next_timestamp = telemetry_records[i + 1][1]
                duration_seconds = (next_timestamp - timestamp).total_seconds()
                duration_hours = duration_seconds / 3600.0
                
                # Cap duration at 10 minutes to prevent counting offline gaps
                # Telemetry is recorded every 30s, so >10min gap = miner was offline
                max_duration_hours = 10.0 / 60.0  # 10 minutes in hours
                if duration_hours > max_duration_hours:
                    duration_hours = max_duration_hours
            else:
                # Last reading, assume 30 second interval
                duration_hours = 30.0 / 3600.0
            
            # Calculate cost: (power_watts / 1000) * duration_hours * price_pence_per_kwh
            kwh = (power / 1000.0) * duration_hours
            cost = kwh * price_pence
            total_cost_pence += cost
            total_kwh_consumed_24h += kwh
    
    # Calculate average price per kWh (weighted by consumption)
    avg_price_per_kwh = None
    if total_kwh_consumed_24h > 0:
        avg_price_per_kwh = total_cost_pence / total_kwh_consumed_24h
    
    # Calculate average miner health (using latest health score for each miner)
    from core.database import HealthScore
    avg_miner_health = None
    
    # Get all miners
    result = await db.execute(select(Miner))
    miners_list = result.scalars().all()
    
    # Get latest health score for each miner
    miner_health_scores = []
    for miner in miners_list:
        result = await db.execute(
            select(HealthScore.overall_score)
            .where(HealthScore.miner_id == miner.id)
            .order_by(HealthScore.timestamp.desc())
            .limit(1)
        )
        latest_score = result.scalar()
        if latest_score is not None:
            miner_health_scores.append(latest_score)
    
    # Calculate average of latest scores
    if miner_health_scores:
        avg_miner_health = sum(miner_health_scores) / len(miner_health_scores)
    
    # Calculate average pool health (using latest health score for each pool)
    from core.database import PoolHealth, Pool
    avg_pool_health = None
    
    # Get all pools
    result = await db.execute(select(Pool))
    all_pools = result.scalars().all()
    
    # Get latest health score for each pool
    pool_health_scores = []
    for pool in all_pools:
        result = await db.execute(
            select(PoolHealth.health_score)
            .where(PoolHealth.pool_id == pool.id)
            .order_by(PoolHealth.timestamp.desc())
            .limit(1)
        )
        latest_score = result.scalar()
        if latest_score is not None:
            pool_health_scores.append(latest_score)
    
    # Calculate average of latest scores
    if pool_health_scores:
        avg_pool_health = sum(pool_health_scores) / len(pool_health_scores)
    
    # Get best share in last 24h (ASIC only)
    best_share_24h = await get_best_share_24h(db)
    
    # Import format_hashrate for consistent formatting
    from core.utils import format_hashrate
    total_hashrate_formatted = format_hashrate(total_hashrate, "GH/s")
    total_pool_hashrate_formatted = format_hashrate(total_pool_hashrate_ghs, "GH/s") if total_pool_hashrate_ghs > 0 else None
    
    return {
        "total_miners": total_miners,
        "active_miners": active_miners,
        "online_miners": online_miners,
        "total_hashrate": total_hashrate_formatted,  # Structured format
        "total_pool_hashrate_ghs": total_pool_hashrate_formatted,  # Structured format
        "pool_efficiency_percent": round(pool_efficiency_percent, 1) if pool_efficiency_percent is not None else None,
        "total_power_watts": round(total_power_watts, 1),
        "avg_efficiency_wth": round(avg_efficiency_wth, 1) if avg_efficiency_wth is not None else None,
        "current_energy_price_pence": current_price,
        "avg_price_per_kwh_pence": round(avg_price_per_kwh, 2) if avg_price_per_kwh is not None else None,
        "recent_events_24h": recent_events,
        "total_cost_24h_pence": round(total_cost_pence, 2),
        "total_cost_24h_pounds": round(total_cost_pence / 100, 2),
        "avg_miner_health": round(avg_miner_health, 1) if avg_miner_health is not None else None,
        "avg_pool_health": round(avg_pool_health, 1) if avg_pool_health is not None else None,
        "best_share_24h": best_share_24h
    }


@router.get("/pools/platform-tiles")
async def get_platform_tiles(db: AsyncSession = Depends(get_db)):
    """
    Get PLATFORM TILES - the top 4 dashboard tiles showing consolidated view across ALL pools.
    
    These are the main dashboard tiles that aggregate data from all pool integrations.
    
    Returns:
        {
            tile_1_health: {
                total_pools: int,
                healthy_pools: int,
                unhealthy_pools: int,
                avg_latency_ms: float,
                status: "healthy" | "degraded" | "unhealthy" | "no_pools"
            },
            tile_2_network: {
                total_pool_hashrate: float,
                total_network_difficulty: float,
                avg_pool_percentage: float,
                estimated_time_to_block: str | null
            },
            tile_3_shares: {
                total_valid: int,
                total_invalid: int,
                total_stale: int,
                avg_reject_rate: float
            },
            tile_4_blocks: {
                total_blocks_24h: int,
                total_earnings_24h: float | null,
                currencies: List[str]
            }
        }
    """
    try:
        tiles = await DashboardPoolService.get_platform_tiles(db)
        return tiles
    except Exception as e:
        logger.error(f"Failed to get platform tiles: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pools")
async def get_pool_tiles(pool_id: str = None, db: AsyncSession = Depends(get_db)):
    """
    Get POOL TILES - individual 4-tile breakdown per pool.
    
    Each active pool gets its own set of 4 tiles showing pool-specific data.
    This is used to display per-pool sections below the platform tiles.
    
    Args:
        pool_id: Optional - get tiles for specific pool only
    
    Returns:
        Dict mapping pool_id -> {
            display_name, pool_type, supports_coins, supports_earnings, supports_balance,
            tile_1_health: {...},
            tile_2_network: {...},
            tile_3_shares: {...},
            tile_4_blocks: {...}
        }
    """
    try:
        from core.database import Pool
        from core.pool_loader import get_pool_loader
        
        pool_data = await DashboardPoolService.get_pool_dashboard_data(db, pool_id)
        
        # Get pool metadata from database
        if pool_id:
            result = await db.execute(select(Pool).where(Pool.id == int(pool_id)))
            pools = [result.scalar_one_or_none()]
        else:
            # Sort by sort_order ascending, then by id for consistent fallback
            result = await db.execute(
                select(Pool)
                .where(Pool.enabled == True)
                .order_by(Pool.sort_order.asc(), Pool.id.asc())
            )
            pools = result.scalars().all()
        
        # Query all pool effort data for luck percentage
        effort_result = await db.execute(select(PoolBlockEffort))
        pool_efforts = {effort.pool_name: effort for effort in effort_result.scalars().all()}
        
        # Create pool ID to metadata mapping
        pool_loader = get_pool_loader()
        pool_metadata = {}
        for pool in pools:
            if pool:
                # Get driver for metadata
                driver = pool_loader.get_driver(pool.pool_type) if pool.pool_type else None
                
                # Determine actual coin being mined (not all supported coins)
                actual_coin = None
                if driver and hasattr(driver, '_get_coin_from_port'):
                    actual_coin = driver._get_coin_from_port(pool.port)
                
                # Fallback: try to parse coin from pool name/URL
                if not actual_coin:
                    pool_identifier = f"{pool.name} {pool.url}".upper()
                    for test_coin in ["DGB", "BCH", "BTC", "BC2", "LTC"]:
                        if test_coin in pool_identifier:
                            actual_coin = test_coin
                            break
                
                pool_metadata[str(pool.id)] = {
                    "display_name": pool.name,
                    "pool_type": pool.pool_type or "unknown",
                    "supports_coins": [actual_coin] if actual_coin else (driver.supports_coins if driver else []),
                    "supports_earnings": False,  # Will be overridden by tile data
                    "supports_balance": False,    # Will be overridden by tile data
                    "warnings": derive_pool_warnings(pool.pool_type, driver is not None),
                }
        
        # Convert DashboardTileData models to structured tile format with metadata
        response = {}
        for pid, data in pool_data.items():
            metadata = pool_metadata.get(str(pid), {
                "display_name": "Unknown Pool",
                "pool_type": "unknown",
                "supports_coins": [],
                "supports_earnings": False,
                "supports_balance": False,
                "warnings": ["driver_unresolved"],
            })
            
            # Find the corresponding pool object to get sort_order
            pool_obj = next((p for p in pools if p and str(p.id) == str(pid)), None)
            sort_order = pool_obj.sort_order if pool_obj and pool_obj.sort_order is not None else 0
            
            response[pid] = {
                # Pool metadata
                "pool_id": str(pid),
                "display_name": metadata["display_name"],
                "pool_type": metadata["pool_type"],
                "supports_coins": metadata["supports_coins"],
                "supports_earnings": data.supports_earnings,
                "supports_balance": data.supports_balance,
                "warnings": metadata["warnings"],
                "sort_order": sort_order,  # Add sort_order for frontend
                
                # Tile data
                "tile_1_health": {
                    "health_status": data.health_status,
                    "health_message": data.health_message,
                    "latency_ms": data.latency_ms
                },
                "tile_2_network": {
                    "network_difficulty": data.network_difficulty,
                    "pool_hashrate": data.pool_hashrate,
                    "estimated_time_to_block": data.estimated_time_to_block,
                    "pool_percentage": data.pool_percentage,
                },
                "tile_3_shares": {
                    "shares_valid": data.shares_valid,
                    "shares_invalid": data.shares_invalid,
                    "shares_stale": data.shares_stale,
                    "reject_rate": data.reject_rate
                },
                "tile_4_blocks": {
                    "blocks_found_24h": data.blocks_found_24h,
                    "last_block_found": data.last_block_found.isoformat() if data.last_block_found else None,
                    "currency": data.currency,
                    "confirmed_balance": data.confirmed_balance,
                    "pending_balance": data.pending_balance,
                    "luck_percentage": (pool_efforts[metadata["display_name"]].blocks_equivalent * 100) if metadata["display_name"] in pool_efforts else None
                },
                "last_updated": _to_utc_iso8601(data.last_updated)
            }
        
        return response
    
    except Exception as e:
        logger.error(f"Failed to get pool tiles: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/energy/current")
async def get_current_energy_price(db: AsyncSession = Depends(get_db)):
    """Get current energy price slot"""
    from core.config import app_config

    provider_id = app_config.get("energy.provider_id", "octopus_agile")
    region = app_config.get(f"energy.providers.{provider_id}.region", app_config.get("octopus_agile.region", "H"))
    now = datetime.utcnow()
    result = await db.execute(
        select(EnergyPrice)
        .where(EnergyPrice.region == region)
        .where(EnergyPrice.valid_from <= now)
        .where(EnergyPrice.valid_to > now)
        .limit(1)
    )
    price = result.scalar_one_or_none()
    
    if not price:
        return {"price_pence": None, "valid_from": None, "valid_to": None}
    
    return {
        "price_pence": price.price_pence,
        "valid_from": price.valid_from.isoformat(),
        "valid_to": price.valid_to.isoformat()
    }


@router.get("/energy/next")
async def get_next_energy_price(db: AsyncSession = Depends(get_db)):
    """Get next energy price slot"""
    from core.config import app_config

    provider_id = app_config.get("energy.provider_id", "octopus_agile")
    region = app_config.get(f"energy.providers.{provider_id}.region", app_config.get("octopus_agile.region", "H"))
    now = datetime.utcnow()
    result = await db.execute(
        select(EnergyPrice)
        .where(EnergyPrice.region == region)
        .where(EnergyPrice.valid_from > now)
        .order_by(EnergyPrice.valid_from)
        .limit(1)
    )
    price = result.scalar_one_or_none()
    
    if not price:
        return {"price_pence": None, "valid_from": None, "valid_to": None}
    
    return {
        "price_pence": price.price_pence,
        "valid_from": price.valid_from.isoformat(),
        "valid_to": price.valid_to.isoformat()
    }


@router.get("/energy/timeline")
async def get_energy_timeline(db: AsyncSession = Depends(get_db)):
    """Get energy price timeline grouped by today and tomorrow"""
    from core.config import app_config
    import logging
    logger = logging.getLogger(__name__)
    
    provider_id = app_config.get("energy.provider_id", "octopus_agile")
    region = app_config.get(f"energy.providers.{provider_id}.region", app_config.get("octopus_agile.region", "H"))
    now = datetime.utcnow()
    
    # Calculate day boundaries (timezone-aware)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)
    day_after_start = today_start + timedelta(days=2)
    
    logger.info(f"Energy timeline query - Region: {region}, Tomorrow: {tomorrow_start} to {day_after_start}")
    
    # Get today's prices
    result = await db.execute(
        select(EnergyPrice)
        .where(EnergyPrice.region == region)
        .where(EnergyPrice.valid_from >= today_start)
        .where(EnergyPrice.valid_from < tomorrow_start)
        .order_by(EnergyPrice.valid_from)
    )
    today_prices = result.scalars().all()
    
    # Get tomorrow's prices
    result = await db.execute(
        select(EnergyPrice)
        .where(EnergyPrice.region == region)
        .where(EnergyPrice.valid_from >= tomorrow_start)
        .where(EnergyPrice.valid_from < day_after_start)
        .order_by(EnergyPrice.valid_from)
    )
    tomorrow_prices = result.scalars().all()
    
    # Debug: check total count in DB
    debug_result = await db.execute(
        select(func.count()).select_from(EnergyPrice).where(EnergyPrice.region == region)
    )
    total_count = debug_result.scalar()
    logger.info(f"Total prices in DB for region {region}: {total_count}")
    logger.info(f"Found {len(today_prices)} today prices, {len(tomorrow_prices)} tomorrow prices")
    
    return {
        "today": {
            "date": today_start.strftime("%A, %d %B %Y"),
            "prices": [
                {
                    "valid_from": p.valid_from.isoformat(),
                    "valid_to": p.valid_to.isoformat(),
                    "price_pence": p.price_pence
                }
                for p in today_prices
            ]
        },
        "tomorrow": {
            "date": tomorrow_start.strftime("%A, %d %B %Y"),
            "prices": [
                {
                    "valid_from": p.valid_from.isoformat(),
                    "valid_to": p.valid_to.isoformat(),
                    "price_pence": p.price_pence
                }
                for p in tomorrow_prices
            ]
        }
    }


@router.get("/energy/config")
async def get_energy_config():
    """Get current energy pricing configuration (provider-aware)."""
    from core.config import app_config

    provider_id = app_config.get("energy.provider_id", "octopus_agile")
    provider_config = app_config.get(f"energy.providers.{provider_id}", {}) or {}
    region = provider_config.get("region") or app_config.get("octopus_agile.region", "H")

    return {
        "enabled": app_config.get("octopus_agile.enabled", False),
        "provider_id": provider_id,
        "provider_config": provider_config,
        # Backward-compatible flat field for UI
        "region": region,
    }


@router.post("/energy/provider")
async def set_energy_provider(provider_id: str):
    """Set active energy provider and trigger immediate refresh."""
    from core.config import save_config, app_config
    from core.scheduler import scheduler
    from providers.energy.loader import get_energy_provider_loader

    try:
        loader = get_energy_provider_loader()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Energy provider loader not ready: {e}")

    if provider_id not in loader.get_provider_ids():
        raise HTTPException(status_code=400, detail=f"Unknown provider_id: {provider_id}")

    # Ensure provider config map exists
    existing_provider_config = app_config.get(f"energy.providers.{provider_id}", {}) or {}
    if provider_id == "octopus_agile" and "region" not in existing_provider_config:
        existing_provider_config["region"] = app_config.get("octopus_agile.region", "H")

    save_config("energy.provider_id", provider_id)
    save_config(f"energy.providers.{provider_id}", existing_provider_config)

    scheduler.scheduler.add_job(
        scheduler._update_energy_prices,
        id=f"update_energy_prices_provider_change_{provider_id}",
        name=f"Fetch prices for provider {provider_id}",
        replace_existing=True,
    )

    return {"status": "success", "provider_id": provider_id}


@router.get("/energy/provider-status")
async def get_energy_provider_runtime_status():
    """Get runtime status of last energy provider synchronization."""
    from core.config import app_config
    from core.scheduler import scheduler
    from providers.energy.loader import get_energy_provider_loader

    try:
        loader = get_energy_provider_loader()
        available_provider_ids = loader.get_provider_ids()
    except Exception:
        available_provider_ids = []

    status = scheduler.get_energy_provider_status()
    configured_provider_id = app_config.get("energy.provider_id", "octopus_agile")

    return {
        "configured_provider_id": configured_provider_id,
        "available_provider_ids": available_provider_ids,
        "runtime": status,
    }


@router.get("/energy/provider-health")
async def get_energy_provider_health(provider_id: str | None = None):
    """Run health check for active (or specified) energy provider."""
    from core.config import app_config
    from providers.energy.loader import get_energy_provider_loader

    configured_provider_id = app_config.get("energy.provider_id", "octopus_agile")
    target_provider_id = provider_id or configured_provider_id

    try:
        loader = get_energy_provider_loader()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Energy provider loader not ready: {e}")

    provider = loader.get_provider(target_provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail=f"Unknown provider_id: {target_provider_id}")

    provider_config = app_config.get(f"energy.providers.{target_provider_id}", {}) or {}
    region = provider_config.get("region") or app_config.get("octopus_agile.region", "H")
    validation_errors = provider.validate_config({**provider_config, "region": region})

    try:
        health = await provider.health_check({**provider_config, "region": region})
    except Exception as e:
        health = {
            "status": "error",
            "provider_id": target_provider_id,
            "message": str(e),
        }

    return {
        "configured_provider_id": configured_provider_id,
        "provider_id": target_provider_id,
        "region": region,
        "validation_errors": validation_errors,
        "health": health,
        "checked_at": datetime.utcnow().isoformat(),
    }


@router.post("/energy/region")
async def set_energy_region(region: str):
    """Set energy provider region (for region-based providers like Octopus Agile)."""
    from core.config import app_config, save_config
    from core.scheduler import scheduler
    
    # Validate region
    valid_regions = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'J', 'K', 'L', 'M', 'N', 'P']
    if region not in valid_regions:
        raise HTTPException(status_code=400, detail=f"Invalid region: {region}")
    
    provider_id = app_config.get("energy.provider_id", "octopus_agile")

    # Update provider-specific config and keep Octopus compatibility keys in sync
    save_config(f"energy.providers.{provider_id}.region", region)
    save_config("octopus_agile.region", region)
    save_config("octopus_agile.enabled", True)
    
    # Trigger immediate energy price fetch for the new region
    scheduler.scheduler.add_job(
        scheduler._update_energy_prices,
        id=f"update_energy_prices_region_change_{provider_id}_{region}",
        name=f"Fetch prices for {provider_id} region {region}",
        replace_existing=True
    )
    
    return {"status": "success", "provider_id": provider_id, "region": region}


@router.post("/energy/toggle")
async def toggle_energy_pricing(enabled: bool):
    """Enable or disable Octopus Agile energy pricing"""
    from core.config import save_config
    
    save_config("octopus_agile.enabled", enabled)
    
    return {"status": "success", "enabled": enabled}


@router.get("/events/recent")
async def get_recent_events(limit: int = 50, db: AsyncSession = Depends(get_db)):
    """Get recent events"""
    result = await db.execute(
        select(Event)
        .order_by(Event.timestamp.desc())
        .limit(limit)
    )
    events = result.scalars().all()
    
    return {
        "events": [
            {
                "id": e.id,
                "timestamp": e.timestamp.isoformat(),
                "event_type": e.event_type,
                "source": e.source,
                "message": e.message,
                "data": e.data
            }
            for e in events
        ]
    }


async def _build_dashboard_all_payload(dashboard_type: str, db: AsyncSession) -> dict:
    """Build uncached payload for /dashboard/all."""
    from core.database import Pool

    # Define miner type filters
    ASIC_TYPES = ["avalon_nano", "bitaxe", "nerdqaxe", "nmminer"]

    # Get all miners
    result = await db.execute(select(Miner))
    all_miners = result.scalars().all()

    # Filter miners based on dashboard type
    if dashboard_type == "asic":
        miners = [m for m in all_miners if m.miner_type in ASIC_TYPES]
    else:
        miners = all_miners

    # Get all pools for name mapping and coin filtering
    result = await db.execute(select(Pool).where(Pool.enabled == True))
    pools = result.scalars().all()
    pools_dict = {(p.url, p.port): p.name for p in pools}

    # Extract pool coins for ticker filtering
    from core.high_diff_tracker import extract_coin_from_pool_name
    pools_with_coins = []
    for pool in pools:
        coin = extract_coin_from_pool_name(pool.name)
        if coin:
            pools_with_coins.append({
                "name": pool.name,
                "coin": coin
            })

    # Get current energy price
    now = datetime.utcnow()
    result = await db.execute(
        select(EnergyPrice.price_pence)
        .where(EnergyPrice.valid_from <= now)
        .where(EnergyPrice.valid_to > now)
        .limit(1)
    )
    current_energy_price = result.scalar()

    # Get recent events (limit 200 for dashboard payload)
    result = await db.execute(
        select(Event)
        .order_by(Event.timestamp.desc())
        .limit(200)
    )
    events = result.scalars().all()

    # Get latest telemetry and calculate costs for each miner
    cutoff_5min = datetime.utcnow() - timedelta(minutes=5)
    cutoff_24h = datetime.utcnow() - timedelta(hours=24)

    # Pre-fetch all energy prices for the last 24 hours (optimization)
    result = await db.execute(
        select(EnergyPrice)
        .where(EnergyPrice.valid_from >= cutoff_24h)
        .order_by(EnergyPrice.valid_from)
    )
    energy_prices = result.scalars().all()

    # Create a lookup function for energy prices
    def get_price_for_timestamp(ts):
        for price in energy_prices:
            if price.valid_from <= ts < price.valid_to:
                return price.price_pence
        return None

    miner_ids = [miner.id for miner in miners]
    telemetry_by_miner = {miner_id: [] for miner_id in miner_ids}
    if miner_ids:
        result = await db.execute(
            select(Telemetry)
            .where(Telemetry.miner_id.in_(miner_ids))
            .where(Telemetry.timestamp > cutoff_24h)
            .order_by(Telemetry.miner_id, Telemetry.timestamp.asc())
        )
        for telemetry in result.scalars().all():
            telemetry_by_miner[telemetry.miner_id].append(telemetry)

    miners_data = []
    total_hashrate = 0.0
    total_power_watts = 0.0
    total_cost_24h_pence = 0.0
    total_kwh_consumed_24h = 0.0
    total_pool_hashrate_ghs = 0.0

    for miner in miners:
        telemetry_records = telemetry_by_miner.get(miner.id, [])
        latest_telemetry = None
        if telemetry_records:
            for entry in reversed(telemetry_records):
                if entry.timestamp > cutoff_5min:
                    latest_telemetry = entry
                    break

        hashrate = 0.0
        hashrate_unit = "GH/s"  # Default for ASIC miners
        power = 0.0
        pool_display = '--'

        if latest_telemetry:
            hashrate = latest_telemetry.hashrate or 0.0
            hashrate_unit = latest_telemetry.hashrate_unit or "GH/s"
            power = latest_telemetry.power_watts or 0.0

            # Fallback to manual power if telemetry has no power
            if not power and miner.manual_power_watts:
                power = miner.manual_power_watts

            # Map pool URL to name
            if latest_telemetry.pool_in_use:
                pool_str = latest_telemetry.pool_in_use
                # Remove protocol
                if '://' in pool_str:
                    pool_str = pool_str.split('://')[1]
                # Extract host and port
                if ':' in pool_str:
                    parts = pool_str.split(':')
                    host = parts[0]
                    port = int(parts[1])
                    pool_display = pools_dict.get((host, port), latest_telemetry.pool_in_use)
                else:
                    pool_display = latest_telemetry.pool_in_use

            # Only add to total if it's in GH/s (ASIC miners)
            # CPU miners (KH/s) are summed separately
            if miner.enabled:
                if hashrate_unit == "GH/s":
                    total_hashrate += hashrate
                    # Only count power for ASIC miners
                    if miner.miner_type in ASIC_TYPES and power:
                        total_power_watts += power
                elif hashrate_unit == "KH/s":
                    # Convert KH/s to GH/s for consistent storage
                    total_hashrate += hashrate / 1000000
                    # Count power for CPU miners too
                    if miner.miner_type in CPU_TYPES and power:
                        total_power_watts += power

        # Calculate accurate 24h cost using historical telemetry + energy prices (using cached prices)
        miner_cost_24h = 0.0
        telemetry_power_records = [
            (tel.power_watts, tel.timestamp)
            for tel in telemetry_records
            if tel.timestamp > cutoff_24h
        ]

        for i, (tel_power, tel_timestamp) in enumerate(telemetry_power_records):
            power = tel_power

            # Fallback to manual power if no auto-detected power
            if not power or power <= 0:
                if miner.manual_power_watts:
                    power = miner.manual_power_watts
                else:
                    continue

            # Find the energy price that was active at this telemetry timestamp (from cached prices)
            price_pence = get_price_for_timestamp(tel_timestamp)

            if price_pence is None:
                continue

            # Calculate duration until next reading
            if i < len(telemetry_power_records) - 1:
                next_timestamp = telemetry_power_records[i + 1][1]
                duration_seconds = (next_timestamp - tel_timestamp).total_seconds()
                duration_hours = duration_seconds / 3600.0

                # Cap duration at 10 minutes to prevent counting offline gaps
                # Telemetry is recorded every 30s, so >10min gap = miner was offline
                max_duration_hours = 10.0 / 60.0  # 10 minutes in hours
                if duration_hours > max_duration_hours:
                    duration_hours = max_duration_hours
            else:
                duration_hours = 30.0 / 3600.0

            # Calculate cost for this period
            kwh = (power / 1000.0) * duration_hours
            cost = kwh * price_pence
            miner_cost_24h += cost

        if miner.enabled:
            total_cost_24h_pence += miner_cost_24h
            # Track total kWh from telemetry records
            for i, (tel_power, tel_timestamp) in enumerate(telemetry_power_records):
                power = tel_power
                if not power or power <= 0:
                    if miner.manual_power_watts:
                        power = miner.manual_power_watts
                    else:
                        continue

                if i < len(telemetry_power_records) - 1:
                    next_timestamp = telemetry_power_records[i + 1][1]
                    duration_seconds = (next_timestamp - tel_timestamp).total_seconds()
                    duration_hours = duration_seconds / 3600.0
                    max_duration_hours = 10.0 / 60.0
                    if duration_hours > max_duration_hours:
                        duration_hours = max_duration_hours
                else:
                    duration_hours = 30.0 / 3600.0

                kwh = (power / 1000.0) * duration_hours
                total_kwh_consumed_24h += kwh

        # Get latest health score for this miner
        health_score = None
        try:
            result = await db.execute(
                select(HealthScore.overall_score)
                .where(HealthScore.miner_id == miner.id)
                .order_by(HealthScore.timestamp.desc())
                .limit(1)
            )
            health_score = result.scalar()
        except Exception:
            pass

        # Determine if miner is offline (no telemetry in last 5 minutes)
        is_offline = latest_telemetry is None

        # Get best session diff/share for tile display
        best_diff = None
        device_reported_mode = None
        mode_switch_state = None
        mode_switch_last_code = None
        mode_switch_last_message = None
        if latest_telemetry and latest_telemetry.data:
            device_reported_mode = latest_telemetry.data.get("current_mode")
            mode_switch_state = latest_telemetry.data.get("mode_switch_state")
            mode_switch_last_code = latest_telemetry.data.get("mode_switch_last_code")
            mode_switch_last_message = latest_telemetry.data.get("mode_switch_last_message")
            if miner.miner_type in ["bitaxe", "nerdqaxe"]:
                best_diff = latest_telemetry.data.get("best_session_diff")
            elif miner.miner_type in ["avalon_nano"]:
                best_diff = latest_telemetry.data.get("best_share")
            elif miner.miner_type == "nmminer":
                best_diff = latest_telemetry.data.get("best_share_diff")

        nano_state = None
        if miner.miner_type == "avalon_nano":
            if mode_switch_state == "calibration_lock":
                nano_state = "calibration"
            elif mode_switch_state == "rejected":
                nano_state = "rejected"
            elif miner.current_mode and device_reported_mode:
                nano_state = "ok" if miner.current_mode == device_reported_mode else "drift"
            else:
                nano_state = "unknown"

        miners_data.append({
            "id": miner.id,
            "name": miner.name,
            "miner_type": miner.miner_type,
            "enabled": miner.enabled,
            "current_mode": miner.current_mode,
            "firmware_version": miner.firmware_version,
            "best_diff": best_diff,
            "hashrate": format_hashrate(hashrate, hashrate_unit),
            "hashrate_unit": hashrate_unit,
            "power": power,
            "pool": pool_display,
            "cost_24h": round(miner_cost_24h / 100, 2),  # Convert to pounds
            "health_score": health_score,
            "is_offline": is_offline,
            "device_reported_mode": device_reported_mode,
            "mode_switch_state": mode_switch_state,
            "mode_switch_last_code": mode_switch_last_code,
            "mode_switch_last_message": mode_switch_last_message,
            "nano_state": nano_state,
        })

    # ============================================================================
    # Get pool hashrate using plugin-based pool system
    # ============================================================================
    total_pool_hashrate_ghs = 0.0
    hashrate_cache_key = f"{dashboard_type}"
    hashrate_cached = _DASHBOARD_EARNINGS_CACHE.get(hashrate_cache_key)

    if hashrate_cached:
        cached_at, cached_payload = hashrate_cached
        if time.time() - cached_at <= _DASHBOARD_EARNINGS_CACHE_TTL_SECONDS:
            total_pool_hashrate_ghs = cached_payload.get("total_pool_hashrate_ghs", 0.0)
        else:
            hashrate_cached = None

    if not hashrate_cached:
        try:
            # Fetch all pool dashboard data from plugins
            pool_dashboard_data = await DashboardPoolService.get_pool_dashboard_data(db)

            for pool_id, tile_data in pool_dashboard_data.items():
                # Aggregate pool hashrate
                if tile_data.pool_hashrate:
                    if isinstance(tile_data.pool_hashrate, dict):
                        total_pool_hashrate_ghs += tile_data.pool_hashrate.get('value', 0.0)
                    else:
                        total_pool_hashrate_ghs += float(tile_data.pool_hashrate)

            _DASHBOARD_EARNINGS_CACHE[hashrate_cache_key] = (
                time.time(),
                {
                    "total_pool_hashrate_ghs": total_pool_hashrate_ghs
                }
            )
        except Exception as e:
            logging.error(f"Error fetching pool hashrate from plugins in /all: {e}")

    # Calculate average price per kWh (weighted by consumption)
    avg_price_per_kwh = None
    if total_kwh_consumed_24h > 0:
        avg_price_per_kwh = total_cost_24h_pence / total_kwh_consumed_24h

    # Count offline/online miners
    offline_miners_count = sum(1 for m in miners_data if m["is_offline"])
    online_miners_count = sum(1 for m in miners_data if not m["is_offline"])

    # Calculate average efficiency (W/TH) for ASIC miners
    # Efficiency = Watts / Hashrate_TH = Watts per Terahash
    avg_efficiency_wth = None
    if total_hashrate > 0 and total_power_watts > 0:
        hashrate_ths = total_hashrate / 1000.0  # Convert GH/s to TH/s
        avg_efficiency_wth = total_power_watts / hashrate_ths

    pool_efficiency_percent = None
    if total_hashrate > 0 and total_pool_hashrate_ghs > 0:
        pool_efficiency_percent = (total_pool_hashrate_ghs / total_hashrate) * 100.0

    # Calculate average pool health
    avg_pool_health = None

    try:
        from core.database import HealthScore, PoolHealth

        # Calculate average pool health (using latest health score for each pool)
        # Get all pools
        result = await db.execute(select(Pool))
        all_pools = result.scalars().all()

        # Get latest health score for each pool
        pool_health_scores = []
        for pool in all_pools:
            result = await db.execute(
                select(PoolHealth.health_score)
                .where(PoolHealth.pool_id == pool.id)
                .order_by(PoolHealth.timestamp.desc())
                .limit(1)
            )
            latest_score = result.scalar()
            if latest_score is not None:
                pool_health_scores.append(latest_score)

        # Calculate average of latest scores
        if pool_health_scores:
            avg_pool_health = round(sum(pool_health_scores) / len(pool_health_scores), 1)
    except Exception as e:
        logging.error(f"Error calculating health scores in /all: {e}")

    # Get best share in last 24h (ASIC only)
    best_share_24h = await get_best_share_24h(db)

    return {
        "stats": {
            "total_miners": len(miners),
            "active_miners": sum(1 for m in miners if m.enabled),
            "online_miners": online_miners_count,
            "offline_miners": offline_miners_count,
            "total_hashrate_ghs": total_hashrate,  # Don't round - preserve precision for KH/s miners
            # Format pool hashrate with structured format (display, value, unit)
            "total_pool_hashrate_ghs": format_hashrate(total_pool_hashrate_ghs, "GH/s") if total_pool_hashrate_ghs > 0 else None,
            "pool_efficiency_percent": round(pool_efficiency_percent, 1) if pool_efficiency_percent is not None else None,
            "total_power_watts": round(total_power_watts, 1),
            "avg_efficiency_wth": round(avg_efficiency_wth, 1) if avg_efficiency_wth is not None else None,
            "current_energy_price_pence": current_energy_price,
            "avg_price_per_kwh_pence": round(avg_price_per_kwh, 2) if avg_price_per_kwh is not None else None,
            "total_cost_24h_pence": round(total_cost_24h_pence, 2),
            "total_cost_24h_pounds": round(total_cost_24h_pence / 100, 2),
            "avg_pool_health": avg_pool_health,
            "best_share_24h": best_share_24h
        },
        "pools": pools_with_coins,
        "miners": miners_data,
        "events": [
            {
                "id": e.id,
                "timestamp": e.timestamp.isoformat(),
                "event_type": e.event_type,
                "source": e.source,
                "message": e.message
            }
            for e in events
        ]
    }


@router.get("/all")
async def get_dashboard_all(dashboard_type: str = "all", db: AsyncSession = Depends(get_db)):
    """
    Optimized bulk endpoint - returns all dashboard data in one call
    Uses cached telemetry from database instead of live polling

    Args:
        dashboard_type: Filter by miner type - "asic" or "all"
    """
    cache_key = f"{dashboard_type}"
    cached = _DASHBOARD_ALL_CACHE.get(cache_key)
    if cached:
        cached_at, cached_payload = cached
        if time.time() - cached_at <= _DASHBOARD_ALL_CACHE_TTL_SECONDS:
            return cached_payload

    compute_lock = _DASHBOARD_ALL_COMPUTE_LOCKS.setdefault(cache_key, asyncio.Lock())
    if compute_lock.locked():
        if cached:
            cached_at, cached_payload = cached
            stale_age = time.time() - cached_at
            if stale_age <= _DASHBOARD_ALL_STALE_MAX_SECONDS:
                logger.warning(
                    "Load shedding /dashboard/all (%s): serving stale cache age=%.1fs",
                    cache_key,
                    stale_age,
                )
                return cached_payload

        logger.warning("Load shedding /dashboard/all (%s): rejecting with 429", cache_key)
        raise HTTPException(status_code=429, detail="Dashboard is busy, retry in a few seconds")

    await compute_lock.acquire()
    try:
        cached = _DASHBOARD_ALL_CACHE.get(cache_key)
        if cached:
            cached_at, cached_payload = cached
            if time.time() - cached_at <= _DASHBOARD_ALL_CACHE_TTL_SECONDS:
                return cached_payload

        payload = await _build_dashboard_all_payload(dashboard_type, db)
        _DASHBOARD_ALL_CACHE[cache_key] = (time.time(), payload)
        return payload
    finally:
        if compute_lock.locked():
            compute_lock.release()


@router.delete("/events")
async def clear_events(db: AsyncSession = Depends(get_db)):
    """Clear all events"""
    from sqlalchemy import delete
    
    await db.execute(delete(Event))
    await db.commit()
    
    return {"message": "All events cleared"}
