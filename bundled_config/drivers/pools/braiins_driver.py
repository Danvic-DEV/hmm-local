"""
Braiins Pool Integration Plugin
BTC pool with comprehensive API for workers, rewards, and profile data
"""
import logging
import aiohttp
import asyncio
from typing import Optional, List, Dict, Any
from datetime import datetime

from integrations.base_pool import (
    BasePoolIntegration,
    PoolHealthStatus,
    PoolStats,
    PoolBlock,
    DashboardTileData,
    PoolTemplate,
    MiningModel
)
from core.utils import format_hashrate

logger = logging.getLogger(__name__)

__version__ = "1.0.5"


class BraiinsIntegration(BasePoolIntegration):
    """Integration for Braiins Pool (formerly SlushPool)."""
    
    # Class attributes
    pool_type = "braiins"
    display_name = "Braiins Pool"
    driver_version = __version__
    documentation_url = "https://braiins.com/pool"
    supports_coins = ["BTC"]
    requires_api_key = True
    
    BASE_URL = "https://pool.braiins.com/accounts"
    POOL_URL = "stratum.braiins.com"
    POOL_PORT = 3333
    API_TIMEOUT = 10
    WORKER_HASHRATE_UNIT = "GH/s"
    
    def get_pool_templates(self) -> List[PoolTemplate]:
        """
        Return Braiins Pool configuration.
        
        Braiins Pool is a global BTC pool with PPLNS reward system.
        """
        return [
            PoolTemplate(
                template_id="btc_global",
                display_name="Braiins Pool BTC (Global)",
                url="stratum.braiins.com",
                port=3333,
                coin="BTC",
                mining_model=MiningModel.POOL,
                region="Global",
                requires_auth=True,
                supports_shares=True,
                supports_earnings=True,
                supports_balance=True,
                description="Bitcoin pool mining with PPLNS rewards (requires API key)",
                fee_percent=2.0
            ),
        ]
    
    async def detect(self, url: str, port: int) -> bool:
        """Auto-detect Braiins Pool by URL pattern."""
        return "braiins.com" in url.lower() and port == self.POOL_PORT
    
    async def get_health(self, url: str, port: int, **kwargs) -> Optional[PoolHealthStatus]:
        """
        Check Braiins Pool health.
        If API token provided, check API connectivity. Otherwise just check URL/port match.
        """
        api_token = kwargs.get("api_token")
        
        if not api_token:
            # Basic check: is it a Braiins pool?
            is_braiins = await self.detect(url, port)
            return PoolHealthStatus(
                is_healthy=is_braiins,
                error_message=None if is_braiins else "Not a Braiins Pool"
            )
        
        # With API token, check actual connectivity
        try:
            start_time = datetime.utcnow()
            headers = {
                "SlushPool-Auth-Token": api_token,
                "Accept": "application/json"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.BASE_URL}/profile/json/btc/",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.API_TIMEOUT)
                ) as response:
                    latency_ms = (datetime.utcnow() - start_time).total_seconds() * 1000
                    
                    if response.status == 200:
                        return PoolHealthStatus(
                            is_healthy=True,
                            latency_ms=latency_ms
                        )
                    elif response.status == 401:
                        return PoolHealthStatus(
                            is_healthy=False,
                            latency_ms=latency_ms,
                            error_message="Invalid API token"
                        )
                    else:
                        return PoolHealthStatus(
                            is_healthy=False,
                            latency_ms=latency_ms,
                            error_message=f"HTTP {response.status}"
                        )
        except Exception as e:
            logger.error(f"Braiins health check failed: {e}")
            return PoolHealthStatus(
                is_healthy=False,
                error_message=str(e)
            )
    
    async def get_network_difficulty(self, coin: str, **kwargs) -> Optional[float]:
        """
        Braiins doesn't expose network difficulty in public API.
        Would need authenticated access to pool stats.
        """
        return None
    
    async def get_pool_stats(
        self,
        url: str,
        coin: str,
        username: Optional[str] = None,
        **kwargs
    ) -> Optional[PoolStats]:
        """
        Get pool statistics from Braiins workers API.
        Requires api_token in kwargs.
        """
        api_token = kwargs.get("api_token")
        if not api_token:
            logger.warning("Braiins API token required for stats")
            return None
        
        workers_data = await self._get_workers(api_token)
        if not workers_data:
            return None
        
        # Parse workers data (current API: btc.workers.<name> with state/hash fields)
        workers_online = 0
        workers_offline = 0
        total_hashrate = 0
        
        if "btc" in workers_data:
            workers_btc = workers_data["btc"]
            workers_map = workers_btc.get("workers", {}) if isinstance(workers_btc, dict) else {}
            for worker_name, worker_info in workers_map.items():
                state = (worker_info.get("state") or "").lower()
                if state in {"ok", "low"}:
                    workers_online += 1
                    # Use 5m hashrate if available, fall back to 60m
                    hashrate_5m = worker_info.get("hash_rate_5m", 0)
                    hashrate_60m = worker_info.get("hash_rate_60m", 0)
                    total_hashrate += hashrate_5m if hashrate_5m else hashrate_60m
                else:
                    workers_offline += 1
        
        return PoolStats(
            hashrate=format_hashrate(total_hashrate, self.WORKER_HASHRATE_UNIT),
            active_workers=workers_online,
            blocks_found=None,  # Not available in workers API
            network_difficulty=None,
            additional_stats={
                "workers_offline": workers_offline
            }
        )
    
    async def get_blocks(
        self,
        url: str,
        coin: str,
        limit: int = 10,
        **kwargs
    ) -> List[PoolBlock]:
        """
        Braiins doesn't provide block history in public API.
        Would need authenticated access to pool stats.
        """
        return []
    
    async def get_worker_stats(
        self,
        url: str,
        coin: str,
        username: str,
        **kwargs
    ) -> Optional[Dict[str, Any]]:
        """
        Get worker-specific stats from Braiins.
        Returns workers data for the account.
        """
        api_token = kwargs.get("api_token")
        if not api_token:
            return None
        
        return await self._get_workers(api_token)
    
    async def get_dashboard_data(
        self,
        url: str,
        coin: str,
        username: Optional[str] = None,
        **kwargs
    ) -> Optional[DashboardTileData]:
        """
        Get all dashboard tile data from Braiins Pool.
        
        Fetches:
        - Workers (hashrate, online/offline status)
        - Profile (balance, username)
        - Rewards (24h earnings)
        
        Requires api_token in kwargs.
        """
        logger.info(f"Braiins get_dashboard_data received kwargs: {list(kwargs.keys())}")
        logger.debug(f"Braiins kwargs full content: {kwargs}")
        
        api_token = kwargs.get("api_token")
        if not api_token:
            logger.warning(f"Braiins API token required for dashboard data. Received kwargs: {list(kwargs.keys())}")
            return DashboardTileData(
                health_status=False,
                health_message="API token required",
                currency="BTC"
            )
        
        try:
            # Fetch all endpoints in parallel
            start_time = datetime.utcnow()
            
            workers_data, profile_data, rewards_data = await asyncio.gather(
                self._get_workers(api_token),
                self._get_profile(api_token),
                self._get_rewards(api_token),
                return_exceptions=True
            )
            
            latency_ms = (datetime.utcnow() - start_time).total_seconds() * 1000
            
            # Check for errors and ensure proper types
            workers_dict = workers_data if isinstance(workers_data, dict) else None
            profile_dict = profile_data if isinstance(profile_data, dict) else None
            rewards_dict = rewards_data if isinstance(rewards_data, dict) else None
            
            # Parse workers data (current API: btc.workers.<name> with state/hash fields)
            workers_online = 0
            workers_offline = 0
            total_hashrate_5m = 0.0  # Braiins worker endpoint values are GH/s-scale
            shares_valid_24h = 0
            
            if workers_dict and "btc" in workers_dict:
                workers_btc = workers_dict["btc"]
                workers_map = workers_btc.get("workers", {}) if isinstance(workers_btc, dict) else {}
                for worker_name, worker_info in workers_map.items():
                    state = (worker_info.get("state") or "").lower()
                    if state in {"ok", "low"}:
                        workers_online += 1
                        hashrate_5m = worker_info.get("hash_rate_5m", 0)
                        total_hashrate_5m += hashrate_5m if hashrate_5m else 0
                    else:
                        workers_offline += 1
                    shares_valid_24h += int(worker_info.get("shares_24h", 0) or 0)
            
            # Parse profile data (current API returns string BTC values)
            current_balance = 0
            confirmed_balance = 0
            today_reward = 0
            if profile_dict and "btc" in profile_dict:
                profile_btc = profile_dict["btc"]
                # Keep semantic compatibility with existing tile fields:
                # confirmed_balance -> current balance in account
                # pending_balance   -> estimated (yet to be finalized) reward
                try:
                    confirmed_balance = float(profile_btc.get("current_balance", 0) or 0)
                except (TypeError, ValueError):
                    confirmed_balance = 0
                try:
                    current_balance = float(profile_btc.get("estimated_reward", 0) or 0)
                except (TypeError, ValueError):
                    current_balance = 0
                try:
                    today_reward = float(profile_btc.get("today_reward", 0) or 0)
                except (TypeError, ValueError):
                    today_reward = 0
            
            # Parse rewards data fallback (current API: btc.daily_rewards[])
            # If profile already provided today_reward, keep it.
            if rewards_dict and "btc" in rewards_dict:
                rewards_btc = rewards_dict["btc"]
                daily_rewards = rewards_btc.get("daily_rewards", []) if isinstance(rewards_btc, dict) else []
                if not today_reward and isinstance(daily_rewards, list) and daily_rewards:
                    latest = max(
                        (entry for entry in daily_rewards if isinstance(entry, dict)),
                        key=lambda entry: entry.get("date", 0),
                        default=None,
                    )
                    if latest:
                        try:
                            today_reward = float(latest.get("total_reward", 0) or 0)
                        except (TypeError, ValueError):
                            today_reward = 0
            
            return DashboardTileData(
                # Tile 1: Health
                health_status=workers_dict is not None,
                health_message=f"{workers_online} workers online" if workers_dict else "API error",
                latency_ms=latency_ms,
                
                # Tile 2: Network Stats
                # For pool mining, show YOUR hashrate (not pool-wide)
                network_difficulty=None,
                pool_hashrate=format_hashrate(total_hashrate_5m, self.WORKER_HASHRATE_UNIT),  # User's total hashrate
                estimated_time_to_block=None,
                pool_percentage=None,
                active_workers=workers_online,
                
                # Tile 3: Shares
                shares_valid=shares_valid_24h,
                shares_invalid=0,
                shares_stale=None,
                reject_rate=0.0,
                
                # Tile 4: Blocks & Earnings
                blocks_found_24h=None,  # Not available in account API
                estimated_earnings_24h=today_reward,
                currency="BTC",
                confirmed_balance=confirmed_balance,
                pending_balance=current_balance,
                
                # Metadata
                last_updated=datetime.utcnow(),
                supports_earnings=True,
                supports_balance=True
            )
        
        except Exception as e:
            logger.error(f"Failed to get Braiins dashboard data: {e}")
            return None
    
    def get_config_schema(self) -> Dict[str, Any]:
        """
        Braiins requires an API token for authenticated access.
        """
        return {
            "type": "object",
            "properties": {
                "api_token": {
                    "type": "string",
                    "title": "API Token",
                    "description": "SlushPool-Auth-Token from Braiins Pool account settings (required)"
                },
                "username": {
                    "type": "string",
                    "title": "Username",
                    "description": "Your Braiins Pool username/account name"
                }
            },
            "required": ["api_token"]
        }
    
    async def _get_workers(self, api_token: str) -> Optional[Dict[str, Any]]:
        """Get workers information from Braiins Pool API."""
        headers = {
            "SlushPool-Auth-Token": api_token,
            "Accept": "application/json"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.BASE_URL}/workers/json/btc",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.API_TIMEOUT)
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        logger.warning(f"Braiins workers API returned {response.status}")
                        return None
        except Exception as e:
            logger.error(f"Failed to fetch Braiins workers: {e}")
            return None
    
    async def _get_profile(self, api_token: str) -> Optional[Dict[str, Any]]:
        """Get profile/balance information from Braiins Pool API."""
        headers = {
            "SlushPool-Auth-Token": api_token,
            "Accept": "application/json"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.BASE_URL}/profile/json/btc/",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.API_TIMEOUT)
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        logger.warning(f"Braiins profile API returned {response.status}")
                        return None
        except Exception as e:
            logger.error(f"Failed to fetch Braiins profile: {e}")
            return None
    
    async def _get_rewards(self, api_token: str) -> Optional[Dict[str, Any]]:
        """Get daily rewards from Braiins Pool API."""
        headers = {
            "SlushPool-Auth-Token": api_token,
            "Accept": "application/json"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.BASE_URL}/rewards/json/btc/",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.API_TIMEOUT)
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        logger.warning(f"Braiins rewards API returned {response.status}")
                        return None
        except Exception as e:
            logger.error(f"Failed to fetch Braiins rewards: {e}")
            return None


# Auto-register this plugin
# Pool driver loaded dynamically by PoolDriverLoader
