"""
Dashboard Pool Service

Orchestrates fetching dashboard data from pool drivers.
Handles caching, error recovery, and multi-pool aggregation.
"""
import asyncio
import logging
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.database import Pool, BlockFound, Event
from core.pool_loader import get_pool_loader
from integrations.base_pool import DashboardTileData

logger = logging.getLogger(__name__)
RECOVERY_EVENT_SOURCE = "pool_driver_recovery"


def _normalize_pool_endpoint(url: str, port: Optional[int]) -> str:
    normalized = (url or "").lower().replace("stratum+tcp://", "").replace("stratum+ssl://", "")
    normalized = normalized.replace("http://", "").replace("https://", "").rstrip("/")
    if port:
        return f"{normalized}:{port}"
    return normalized


def _get_matching_driver_settings(pool_loader, pool_type: str, url: str, port: Optional[int]) -> Dict[str, Any]:
    """
    Resolve driver-specific settings from YAML pool config by matching driver + endpoint.

    Returns a dict of driver settings (e.g. api_token/api_key) or empty dict if no match.
    """
    pool_endpoint = _normalize_pool_endpoint(url, port)

    for config in pool_loader.pool_configs.values():
        if config.driver != pool_type:
            continue

        config_endpoint = _normalize_pool_endpoint(config.url, config.port)
        if config_endpoint != pool_endpoint:
            continue

        return dict(config.driver_settings or {})

    return {}

# Cache dashboard data for 30 seconds
_POOL_DASHBOARD_CACHE: Dict[str, tuple[float, DashboardTileData]] = {}
_POOL_DASHBOARD_CACHE_TTL = 30.0


class DashboardPoolService:
    """
    Service for fetching and caching pool dashboard data.
    """
    
    @staticmethod
    async def get_pool_dashboard_data(
        db: AsyncSession,
        pool_id: Optional[str] = None
    ) -> Dict[str, DashboardTileData]:
        """
        Get dashboard data for all active pools (or specific pool).
        
        Args:
            db: Database session
            pool_id: Optional - get data for specific pool only
        
        Returns:
            Dict mapping pool_id -> DashboardTileData
        """
        # Get active pools from database that are enabled for dashboard display
        query = select(Pool).where(Pool.enabled == True, Pool.show_on_dashboard == True)
        if pool_id:
            query = query.where(Pool.id == pool_id)
        
        result = await db.execute(query)
        pools = result.scalars().all()
        
        if not pools:
            logger.debug("No active pools found for dashboard")
            return {}
        
        dashboard_data = {}
        
        for pool in pools:
            # Check cache first
            cache_key = f"{pool.id}"
            cached = _POOL_DASHBOARD_CACHE.get(cache_key)
            
            if cached:
                cached_at, cached_data = cached
                if (datetime.utcnow().timestamp() - cached_at) < _POOL_DASHBOARD_CACHE_TTL:
                    dashboard_data[str(pool.id)] = cached_data
                    continue
            
            # Fetch fresh data from plugin
            try:
                tile_data = await DashboardPoolService._fetch_pool_data(pool, db)
                
                if tile_data:
                    # If a refresh intermittently fails to populate network difficulty, keep the
                    # last-known value so the dashboard doesn't flicker between a value and N/A.
                    if cached and tile_data.network_difficulty is None:
                        try:
                            _, cached_data = cached
                            tile_data.network_difficulty = cached_data.network_difficulty
                        except Exception:
                            pass

                    # Cache it
                    _POOL_DASHBOARD_CACHE[cache_key] = (
                        datetime.utcnow().timestamp(),
                        tile_data
                    )
                    dashboard_data[str(pool.id)] = tile_data
                else:
                    logger.warning(f"No dashboard data returned for pool {pool.name}")
            
            except Exception as e:
                logger.error(f"Failed to fetch dashboard data for pool {pool.name}: {e}")
                # Return error state
                dashboard_data[str(pool.id)] = DashboardTileData(
                    health_status=False,
                    health_message=f"Error: {str(e)[:100]}",
                    currency=getattr(pool, 'coin', None) or "UNKNOWN"
                )
        
        return dashboard_data
    
    @staticmethod
    async def _fetch_pool_data(pool: Pool, db: AsyncSession) -> Optional[DashboardTileData]:
        """
        Fetch dashboard data from a pool using its plugin.
        
        Args:
            pool: Pool database model
            db: Database session for querying our blocks
        
        Returns:
            DashboardTileData or None
        """
        pool_loader = get_pool_loader()
        pool_type = pool.pool_type

        if not pool_type or pool_type == "unknown":
            pool_type = await DashboardPoolService._recover_pool_driver(pool, db, pool_loader)
            if not pool_type:
                logger.warning(f"Pool {pool.name} driver unresolved; returning degraded dashboard tile")
                return DashboardTileData(
                    health_status=False,
                    health_message="driver unresolved",
                    currency=(pool.pool_config or {}).get("coin") or "UNKNOWN",
                )

        # Get driver from new driver system
        driver = pool_loader.get_driver(pool_type)
        
        if not driver:
            logger.warning(f"No driver found for pool type: {pool_type}")
            return DashboardTileData(
                health_status=False,
                health_message=f"driver not loaded: {pool_type}",
                currency=(pool.pool_config or {}).get("coin") or "UNKNOWN",
            )
        
        # Parse pool config JSON (if exists) and merge driver settings from YAML config
        # YAML driver settings are the source of truth for auth secrets.
        pool_config = dict(pool.pool_config or {})
        matched_driver_settings = _get_matching_driver_settings(
            pool_loader,
            pool_type=pool_type,
            url=pool.url,
            port=pool.port,
        )
        if matched_driver_settings:
            pool_config.update(matched_driver_settings)
            redacted_keys = [
                key for key in matched_driver_settings.keys()
                if "token" in key.lower() or "key" in key.lower() or "secret" in key.lower()
            ]
            if redacted_keys:
                logger.debug(
                    "Injected driver auth/settings from YAML for pool %s (keys=%s)",
                    pool.name,
                    redacted_keys,
                )
            else:
                logger.debug("Injected driver settings from YAML for pool %s", pool.name)
        
        logger.debug(f"Pool {pool.name} config type: {type(pool.pool_config)}, value: {pool.pool_config}")
        logger.debug(f"Pool config dict: {pool_config}, keys: {list(pool_config.keys())}")
        
        # Get username/worker from config or pool fields
        username = pool_config.get("username") or pool.user
        
        # Call driver's dashboard method
        try:
            # Detect coin from pool config, or try to detect from port/URL
            coin = pool_config.get("coin")
            
            if not coin:
                # Try to detect coin from port (for Solopool and other multi-coin pools)
                if hasattr(driver, '_get_coin_from_port'):
                    coin = driver._get_coin_from_port(pool.port)
                
                # Fallback: try to parse from pool name or URL
                if not coin:
                    pool_identifier = f"{pool.name} {pool.url}".upper()
                    for test_coin in ["DGB", "BCH", "BTC", "BC2", "LTC"]:
                        if test_coin in pool_identifier:
                            coin = test_coin
                            break
                
                # Final fallback
                if not coin:
                    coin = "BTC"
            
            logger.debug(f"Calling {pool_type} driver for pool {pool.name} with coin={coin}, username={username}")
            
            dashboard_data = await driver.get_dashboard_data(
                url=pool.url,
                coin=coin,
                username=username,
                **pool_config
            )
            
            # Override blocks_found_24h with OUR actual blocks from database
            if dashboard_data:
                cutoff = datetime.utcnow() - timedelta(hours=24)
                
                # Query blocks found by our miners on this pool in last 24h
                blocks_query = select(func.count(BlockFound.id)).where(
                    BlockFound.pool_name == pool.name,
                    BlockFound.coin == coin.upper(),
                    BlockFound.timestamp >= cutoff
                )
                result = await db.execute(blocks_query)
                our_blocks_24h = result.scalar() or 0
                
                # Query timestamp of most recent block
                last_block_query = select(BlockFound.timestamp).where(
                    BlockFound.pool_name == pool.name,
                    BlockFound.coin == coin.upper()
                ).order_by(BlockFound.timestamp.desc()).limit(1)
                result = await db.execute(last_block_query)
                last_block_ts = result.scalar()
                
                # Replace with our actual data
                dashboard_data.blocks_found_24h = our_blocks_24h
                dashboard_data.last_block_found = last_block_ts
                logger.debug(f"Pool {pool.name}: Found {our_blocks_24h} blocks by our miners in last 24h")
            
            return dashboard_data
        
        except NotImplementedError:
            logger.warning(f"Pool driver {pool_type} does not implement get_dashboard_data()")
            return None
        
        except Exception as e:
            logger.error(f"Error calling driver {pool_type}.get_dashboard_data(): {e}")
            raise

    @staticmethod
    async def _recover_pool_driver(pool: Pool, db: AsyncSession, pool_loader) -> Optional[str]:
        """
        Attempt to auto-recover unknown pool driver type and persist it.
        Returns resolved driver type or None if unresolved.
        """
        max_attempts = 2
        retry_delay_seconds = 0.2
        for attempt in range(1, max_attempts + 1):
            for driver_type, driver in pool_loader.drivers.items():
                try:
                    if await driver.detect(pool.url, pool.port):
                        logger.info(
                            "Auto-recovered pool driver for %s (%s:%s): %s (attempt %s/%s)",
                            pool.name,
                            pool.url,
                            pool.port,
                            driver_type,
                            attempt,
                            max_attempts,
                        )
                        pool.pool_type = driver_type
                        pool_config = dict(pool.pool_config or {})
                        pool_config["driver"] = driver_type
                        pool.pool_config = pool_config
                        await DashboardPoolService._emit_recovery_event(
                            db,
                            event_type="info",
                            message=(
                                f"Recovered pool driver '{driver_type}' via dashboard reconciliation "
                                f"for {pool.url}:{pool.port}"
                            ),
                            pool=pool,
                            context="dashboard_reconciliation",
                            old_pool_type="unknown",
                            resolved_pool_type=driver_type,
                            dedupe_seconds=300,
                        )
                        await db.commit()
                        await db.refresh(pool)
                        return driver_type
                except Exception as e:
                    logger.debug(
                        "Driver recovery detect error for pool %s via '%s' (attempt %s/%s): %s",
                        pool.name,
                        driver_type,
                        attempt,
                        max_attempts,
                        e,
                    )

            if attempt < max_attempts:
                await asyncio.sleep(retry_delay_seconds)

        # Persist explicit unknown marker for observability/reconciliation.
        try:
            pool.pool_type = "unknown"
            pool_config = dict(pool.pool_config or {})
            pool_config["driver"] = "unknown"
            pool.pool_config = pool_config
            await DashboardPoolService._emit_recovery_event(
                db,
                event_type="warning",
                message=f"Pool driver unresolved after retries for {pool.url}:{pool.port}",
                pool=pool,
                context="dashboard_reconciliation",
                old_pool_type="unknown",
                resolved_pool_type="unknown",
                dedupe_seconds=900,
            )
            await db.commit()
        except Exception as e:
            logger.error("Failed persisting unknown driver marker for pool %s: %s", pool.name, e)
            await db.rollback()

        return None

    @staticmethod
    async def _emit_recovery_event(
        db: AsyncSession,
        *,
        event_type: str,
        message: str,
        pool: Pool,
        context: str,
        old_pool_type: str | None,
        resolved_pool_type: str | None,
        dedupe_seconds: int,
    ) -> None:
        """Emit driver recovery event with lightweight time-based dedupe."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(0, dedupe_seconds))
        recent_result = await db.execute(
            select(Event.id)
            .where(Event.source == RECOVERY_EVENT_SOURCE)
            .where(Event.message == message)
            .where(Event.timestamp >= cutoff)
            .limit(1)
        )
        if recent_result.scalar_one_or_none() is not None:
            return

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
    
    @staticmethod
    async def invalidate_cache(pool_id: Optional[str] = None):
        """
        Invalidate cached dashboard data.
        
        Args:
            pool_id: Optional - clear specific pool only, or all if None
        """
        if pool_id:
            cache_key = f"{pool_id}"
            if cache_key in _POOL_DASHBOARD_CACHE:
                del _POOL_DASHBOARD_CACHE[cache_key]
                logger.debug(f"Invalidated cache for pool {pool_id}")
        else:
            _POOL_DASHBOARD_CACHE.clear()
            logger.debug("Cleared all pool dashboard cache")
    
    @staticmethod
    async def get_platform_tiles(
        db: AsyncSession
    ) -> Dict[str, Any]:
        """
        Get PLATFORM TILES - consolidated data across ALL pools.
        
        These are the top 4 tiles on the dashboard showing aggregate view.
        
        Returns:
            Dict with 4 platform tiles:
            - tile_1_health: Overall pool health status
            - tile_2_network: Consolidated network stats
            - tile_3_shares: Combined shares across all pools
            - tile_4_blocks: Total blocks/earnings across all pools
        """
        pool_data = await DashboardPoolService.get_pool_dashboard_data(db)
        
        if not pool_data:
            return {
                "tile_1_health": {
                    "total_pools": 0,
                    "healthy_pools": 0,
                    "unhealthy_pools": 0,
                    "avg_latency_ms": None,
                    "status": "no_pools"
                },
                "tile_2_network": {
                    "total_pool_hashrate": 0.0,
                    "total_network_difficulty": 0.0,
                    "avg_pool_percentage": 0.0,
                    "estimated_time_to_block": None
                },
                "tile_3_shares": {
                    "total_valid": 0,
                    "total_invalid": 0,
                    "total_stale": 0,
                    "avg_reject_rate": 0.0
                },
                "tile_4_blocks": {
                    "total_blocks_24h": 0,
                    "total_earnings_24h": 0.0,
                    "currencies": []
                }
            }
        
        # TILE 1: Health - aggregate health across all pools
        healthy_count = sum(1 for d in pool_data.values() if d.health_status)
        unhealthy_count = len(pool_data) - healthy_count
        latencies = [d.latency_ms for d in pool_data.values() if d.latency_ms is not None]
        avg_latency = sum(latencies) / len(latencies) if latencies else None
        
        overall_status = "healthy" if unhealthy_count == 0 else "degraded" if healthy_count > 0 else "unhealthy"
        
        # TILE 2: Network - combined network stats
        # Extract value from structured hashrate format {display, value, unit} or use float directly
        total_pool_hashrate = sum(
            (d.pool_hashrate.get('value') if isinstance(d.pool_hashrate, dict) else d.pool_hashrate) or 0.0
            for d in pool_data.values()
        )
        network_diffs = [d.network_difficulty for d in pool_data.values() if d.network_difficulty]
        total_network_diff = sum(network_diffs) if network_diffs else 0.0
        pool_percentages = [d.pool_percentage for d in pool_data.values() if d.pool_percentage]
        avg_pool_pct = sum(pool_percentages) / len(pool_percentages) if pool_percentages else 0.0
        
        # Format hashrate using utility
        from core.utils import format_hashrate
        total_pool_hashrate_formatted = format_hashrate(total_pool_hashrate, "GH/s")
        
        # TILE 3: Shares - combined shares data
        total_valid = sum(d.shares_valid or 0 for d in pool_data.values())
        total_invalid = sum(d.shares_invalid or 0 for d in pool_data.values())
        total_stale = sum(d.shares_stale or 0 for d in pool_data.values())
        reject_rates = [d.reject_rate for d in pool_data.values() if d.reject_rate is not None]
        avg_reject = sum(reject_rates) / len(reject_rates) if reject_rates else 0.0
        
        # TILE 4: Blocks - combined blocks data
        total_blocks = sum(d.blocks_found_24h or 0 for d in pool_data.values())
        currencies = list(set(d.currency for d in pool_data.values() if d.currency))
        
        return {
            "tile_1_health": {
                "total_pools": len(pool_data),
                "healthy_pools": healthy_count,
                "unhealthy_pools": unhealthy_count,
                "avg_latency_ms": round(avg_latency, 1) if avg_latency else None,
                "status": overall_status
            },
            "tile_2_network": {
                "total_pool_hashrate": total_pool_hashrate_formatted,  # Structured format
                "total_network_difficulty": round(total_network_diff, 2),
                "avg_pool_percentage": round(avg_pool_pct, 2),
                "estimated_time_to_block": None  # Would need complex calculation
            },
            "tile_3_shares": {
                "total_valid": total_valid,
                "total_invalid": total_invalid,
                "total_stale": total_stale,
                "avg_reject_rate": round(avg_reject, 2)
            },
            "tile_4_blocks": {
                "total_blocks_24h": total_blocks,
                "currencies": currencies
            }
        }
