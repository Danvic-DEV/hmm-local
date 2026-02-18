"""
HMM-Local Stratum Integration Plugin
Uses HMM-Local Stratum APIs (/api/pool-snapshot + /stats) as data source for dashboard tiles.
"""

__version__ = "1.0.5"

import logging
import aiohttp
from typing import Optional, List, Dict, Any
from datetime import datetime

from integrations.base_pool import (
    BasePoolIntegration,
    PoolHealthStatus,
    PoolStats,
    PoolBlock,
    DashboardTileData,
    PoolTemplate,
    MiningModel,
)
from core.utils import format_hashrate

logger = logging.getLogger(__name__)


class HMMLocalStratumIntegration(BasePoolIntegration):
    """Pool driver that reads telemetry from a local HMM-Local Stratum instance."""

    pool_type = "hmm_local_stratum"
    display_name = "HMM-Local Stratum"
    driver_version = __version__
    documentation_url = "https://github.com/RenegadeUK/hmm-local"
    supports_coins = ["BTC", "BCH", "DGB"]
    requires_api_key = False

    DEFAULT_API_PORT = 8082
    API_TIMEOUT = 10

    def get_pool_templates(self) -> List[PoolTemplate]:
        """Return default templates for local HMM-Local Stratum endpoints."""
        return [
            PoolTemplate(
                template_id="hmm_local_stratum_dgb",
                display_name="HMM-Local Stratum DGB",
                url="hmm-local-stratum",
                port=3335,
                coin="DGB",
                mining_model=MiningModel.SOLO,
                region="LOCAL",
                requires_auth=False,
                supports_shares=True,
                supports_earnings=False,
                supports_balance=False,
                description="Local DGB stratum gateway with internal snapshot API",
                fee_percent=0.0,
            ),
            PoolTemplate(
                template_id="hmm_local_stratum_btc",
                display_name="HMM-Local Stratum BTC",
                url="hmm-local-stratum",
                port=3333,
                coin="BTC",
                mining_model=MiningModel.SOLO,
                region="LOCAL",
                requires_auth=False,
                supports_shares=True,
                supports_earnings=False,
                supports_balance=False,
                description="Local BTC stratum gateway with internal snapshot API",
                fee_percent=0.0,
            ),
            PoolTemplate(
                template_id="hmm_local_stratum_bch",
                display_name="HMM-Local Stratum BCH",
                url="hmm-local-stratum",
                port=3334,
                coin="BCH",
                mining_model=MiningModel.SOLO,
                region="LOCAL",
                requires_auth=False,
                supports_shares=True,
                supports_earnings=False,
                supports_balance=False,
                description="Local BCH stratum gateway with internal snapshot API",
                fee_percent=0.0,
            ),
        ]

    async def detect(self, url: str, port: int) -> bool:
        """
        Detect HMM-Local Stratum endpoint.

        Supports both container hostnames and static IP deployments by probing
        the snapshot API at http://{url}:8082/api/pool-snapshot/{coin}.
        """
        known_ports = {3333: "BTC", 3334: "BCH", 3335: "DGB"}
        coin = known_ports.get(int(port))
        if not coin:
            return False

        host = (url or "").strip().lower()

        # Fast positive path for expected local hostnames.
        if any(token in host for token in ["hmm-local-stratum", "localhost", "127.0.0.1", "host.docker.internal"]):
            return True

        # Static-IP / custom hostname path: prove by live snapshot endpoint.
        api_base = f"http://{host}:{self.DEFAULT_API_PORT}"
        endpoint = f"{api_base}/api/pool-snapshot/{coin}?window_minutes=15"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    endpoint,
                    timeout=aiohttp.ClientTimeout(total=min(self.API_TIMEOUT, 4)),
                ) as response:
                    if response.status != 200:
                        return False
                    payload = await response.json()
                    return bool(payload.get("ok"))
        except Exception:
            return False

    def _resolve_api_base(self, url: str, **kwargs) -> str:
        """
        Resolve API base URL for snapshot calls.

        Priority:
        1) `api_base_url` from pool YAML driver settings
        2) `http://{url}:8082`
        """
        api_base = str(kwargs.get("api_base_url") or "").strip().rstrip("/")
        if api_base:
            return api_base
        host = (url or "hmm-local-stratum").strip()
        return f"http://{host}:{self.DEFAULT_API_PORT}"

    @staticmethod
    def _format_eta(seconds: Optional[float]) -> Optional[str]:
        if seconds is None or seconds <= 0:
            return None
        if seconds < 3600:
            return f"{int(seconds / 60)} minutes"
        if seconds < 86400:
            return f"{int(seconds / 3600)} hours"
        return f"{int(seconds / 86400)} days"

    @staticmethod
    def _as_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def _fetch_snapshot(
        self,
        *,
        url: str,
        coin: str,
        window_minutes: int,
        **kwargs,
    ) -> tuple[Optional[Dict[str, Any]], Optional[float], Optional[str]]:
        """
        Fetch `/api/pool-snapshot/{coin}`.

        Returns: (payload, latency_ms, error_message)
        """
        api_base = self._resolve_api_base(url, **kwargs)
        endpoint = f"{api_base}/api/pool-snapshot/{coin.upper()}?window_minutes={window_minutes}"

        try:
            start_time = datetime.utcnow()
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    endpoint,
                    timeout=aiohttp.ClientTimeout(total=self.API_TIMEOUT),
                ) as response:
                    latency_ms = (datetime.utcnow() - start_time).total_seconds() * 1000.0
                    if response.status != 200:
                        return None, latency_ms, f"HTTP {response.status}"
                    payload = await response.json()
                    if not payload.get("ok"):
                        return payload, latency_ms, str(payload.get("message") or "snapshot_error")
                    return payload, latency_ms, None
        except Exception as exc:
            logger.error("HMM-Local Stratum snapshot fetch failed: %s", exc)
            return None, None, str(exc)

    async def _fetch_stats(
        self,
        *,
        url: str,
        **kwargs,
    ) -> tuple[Optional[Dict[str, Any]], Optional[float], Optional[str]]:
        """
        Fetch `/stats` for live coin runtime metrics.

        Returns: (payload, latency_ms, error_message)
        """
        api_base = self._resolve_api_base(url, **kwargs)
        endpoint = f"{api_base}/stats"

        try:
            start_time = datetime.utcnow()
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    endpoint,
                    timeout=aiohttp.ClientTimeout(total=min(self.API_TIMEOUT, 5)),
                ) as response:
                    latency_ms = (datetime.utcnow() - start_time).total_seconds() * 1000.0
                    if response.status != 200:
                        return None, latency_ms, f"HTTP {response.status}"
                    payload = await response.json()
                    return payload if isinstance(payload, dict) else None, latency_ms, None
        except Exception as exc:
            logger.debug("HMM-Local Stratum stats fetch failed: %s", exc)
            return None, None, str(exc)

    @staticmethod
    def _live_connected_workers(stats_payload: Optional[Dict[str, Any]], coin: str) -> Optional[int]:
        if not stats_payload:
            return None
        coins = stats_payload.get("coins") if isinstance(stats_payload, dict) else None
        if not isinstance(coins, dict):
            return None
        coin_data = coins.get(str(coin).upper())
        if not isinstance(coin_data, dict):
            return None
        value = coin_data.get("connected_workers")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _stats_coin_int(stats_payload: Optional[Dict[str, Any]], coin: str, field_name: str) -> Optional[int]:
        if not stats_payload:
            return None
        coins = stats_payload.get("coins") if isinstance(stats_payload, dict) else None
        if not isinstance(coins, dict):
            return None
        coin_data = coins.get(str(coin).upper())
        if not isinstance(coin_data, dict):
            return None
        value = coin_data.get(field_name)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    async def get_health(self, url: str, port: int, **kwargs) -> Optional[PoolHealthStatus]:
        """Health status from snapshot readiness + transport latency."""
        coin = str(kwargs.get("coin") or self._coin_from_port(port) or "DGB").upper()
        window_minutes = int(kwargs.get("snapshot_window_minutes", 15) or 15)

        payload, latency_ms, error = await self._fetch_snapshot(
            url=url,
            coin=coin,
            window_minutes=window_minutes,
            **kwargs,
        )

        if payload and payload.get("ok"):
            quality = payload.get("quality") or {}
            readiness = str(quality.get("readiness") or "unready")
            has_required_inputs = bool(quality.get("has_required_inputs"))
            stale = bool(quality.get("stale"))
            is_healthy = has_required_inputs and not stale
            message = f"readiness={readiness}"
            missing = quality.get("missing_inputs") or []
            if missing:
                message += f" missing={','.join(str(m) for m in missing)}"
            return PoolHealthStatus(
                is_healthy=is_healthy,
                latency_ms=latency_ms,
                error_message=None if is_healthy else message,
                additional_info={"readiness": readiness, "missing_inputs": missing},
            )

        return PoolHealthStatus(
            is_healthy=False,
            latency_ms=latency_ms,
            error_message=error or "snapshot_unavailable",
        )

    async def get_network_difficulty(self, coin: str, **kwargs) -> Optional[float]:
        """Network difficulty from snapshot network section."""
        url = str(kwargs.get("url") or "hmm-local-stratum")
        window_minutes = int(kwargs.get("snapshot_window_minutes", 15) or 15)
        payload, _, _ = await self._fetch_snapshot(
            url=url,
            coin=coin,
            window_minutes=window_minutes,
            **kwargs,
        )
        if not payload:
            return None
        network = payload.get("network") or {}
        return self._as_float(network.get("network_difficulty"))

    async def get_pool_stats(
        self,
        url: str,
        coin: str,
        username: Optional[str] = None,
        **kwargs,
    ) -> Optional[PoolStats]:
        """Pool-level stats from snapshot payload."""
        window_minutes = int(kwargs.get("snapshot_window_minutes", 15) or 15)
        payload, _, _ = await self._fetch_snapshot(
            url=url,
            coin=coin,
            window_minutes=window_minutes,
            **kwargs,
        )
        if not payload:
            return None

        stats_payload, _, _ = await self._fetch_stats(url=url, **kwargs)

        hashrate = payload.get("hashrate") or {}
        network = payload.get("network") or {}
        workers = payload.get("workers") or {}
        kpi = payload.get("kpi") or {}

        live_workers = self._live_connected_workers(stats_payload, coin)
        active_workers = live_workers if live_workers is not None else int(workers.get("count") or 0)
        blocks_accepted = self._stats_coin_int(stats_payload, coin, "blocks_accepted")

        pool_hashrate_hs = self._as_float(hashrate.get("pool_hashrate_hs"))
        network_difficulty = self._as_float(network.get("network_difficulty"))
        return PoolStats(
            hashrate=(format_hashrate(pool_hashrate_hs, "H/s") if pool_hashrate_hs is not None else None),
            active_workers=active_workers,
            blocks_found=int(blocks_accepted or 0),
            network_difficulty=network_difficulty,
            additional_stats={
                "snapshot_window_minutes": window_minutes,
                "network_hash_ps": network.get("network_hash_ps"),
                "pool_share_of_network_pct": kpi.get("pool_share_of_network_pct"),
                "active_workers_source": "stats.connected_workers" if live_workers is not None else "snapshot.workers.count",
                "blocks_source": "stats.blocks_accepted",
            },
        )

    async def get_blocks(
        self,
        url: str,
        coin: str,
        limit: int = 10,
        **kwargs,
    ) -> List[PoolBlock]:
        """
        No block list endpoint yet in HMM-Local Stratum stable API.
        Return empty list for now.
        """
        return []

    async def get_dashboard_data(
        self,
        url: str,
        coin: str,
        username: Optional[str] = None,
        **kwargs,
    ) -> Optional[DashboardTileData]:
        """Map local snapshot payload into dashboard tile schema."""
        window_minutes = int(kwargs.get("snapshot_window_minutes", 15) or 15)
        payload, latency_ms, error = await self._fetch_snapshot(
            url=url,
            coin=coin,
            window_minutes=window_minutes,
            **kwargs,
        )

        stats_payload, _, _ = await self._fetch_stats(url=url, **kwargs)

        if not payload:
            return DashboardTileData(
                health_status=False,
                health_message=error or "snapshot_unavailable",
                latency_ms=latency_ms,
                currency=coin.upper(),
                supports_earnings=False,
                supports_balance=False,
            )

        quality = payload.get("quality") or {}
        network = payload.get("network") or {}
        hashrate = payload.get("hashrate") or {}
        kpi = payload.get("kpi") or {}
        rejects = payload.get("rejects") or {}

        stale = bool(quality.get("stale"))
        health_status = bool(quality.get("has_required_inputs")) and not stale

        snapshot_workers = int((payload.get("workers") or {}).get("count") or 0)
        live_workers = self._live_connected_workers(stats_payload, coin)
        active_workers = live_workers if live_workers is not None else snapshot_workers
        blocks_accepted = self._stats_coin_int(stats_payload, coin, "blocks_accepted")
        health_message = f"{active_workers} workers online"

        share_accept_count = int(kpi.get("share_accept_count") or 0)
        share_reject_count = int(kpi.get("share_reject_count") or 0)
        stale_rejects = int((rejects.get("by_reason") or {}).get("stale_share", 0))
        network_difficulty = self._as_float(network.get("network_difficulty"))
        pool_hashrate_hs = self._as_float(hashrate.get("pool_hashrate_hs"))
        pool_percentage = self._as_float(kpi.get("pool_share_of_network_pct"))
        reject_rate = self._as_float(kpi.get("share_reject_rate_pct"))

        return DashboardTileData(
            # Tile 1: Health
            health_status=health_status,
            health_message=health_message,
            latency_ms=latency_ms,

            # Tile 2: Network
            network_difficulty=network_difficulty,
            pool_hashrate=(
                format_hashrate(pool_hashrate_hs, "H/s")
                if pool_hashrate_hs is not None
                else None
            ),
            estimated_time_to_block=self._format_eta(kpi.get("expected_time_to_block_sec")),
            pool_percentage=pool_percentage,
            active_workers=active_workers,

            # Tile 3: Shares
            shares_valid=share_accept_count,
            shares_invalid=share_reject_count,
            shares_stale=stale_rejects,
            reject_rate=reject_rate,

            # Tile 4: Blocks
            blocks_found_24h=int(blocks_accepted or 0),
            currency=coin.upper(),

            # Metadata
            last_updated=datetime.utcnow(),
            supports_earnings=False,
            supports_balance=False,
        )

    @staticmethod
    def _coin_from_port(port: int) -> Optional[str]:
        mapping = {3333: "BTC", 3334: "BCH", 3335: "DGB"}
        return mapping.get(int(port))

