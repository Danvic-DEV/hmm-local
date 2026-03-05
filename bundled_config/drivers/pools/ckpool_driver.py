"""
CKPool Local Integration Plugin

Supports local CKPool-style solo pool status endpoints served over HTTP,
including:
- /pool/pool.status (line-delimited JSON objects)
- /users/<wallet> (wallet + worker stats)
"""
import json
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

__version__ = "1.0.0"


class CKPoolIntegration(BasePoolIntegration):
    """Integration for local CKPool-compatible pools."""

    pool_type = "ckpool"
    display_name = "CKPool (Local)"
    driver_version = __version__
    documentation_url = "https://github.com/ctubio/ckpool"
    supports_coins = ["BTC", "BCH", "DGB", "BC2"]
    requires_api_key = False

    API_TIMEOUT = 8
    DEFAULT_API_PORT = 3001

    def get_pool_templates(self) -> List[PoolTemplate]:
        """Return a generic local CKPool template."""
        return [
            PoolTemplate(
                template_id="local_multicoin",
                display_name="CKPool Local (Multi-Coin)",
                url="127.0.0.1",
                port=3333,
                coin="DGB",
                mining_model=MiningModel.SOLO,
                region="Local",
                requires_auth=False,
                supports_shares=True,
                supports_earnings=False,
                supports_balance=False,
                description="Local CKPool endpoint (set host, coin, and wallet in pool config)",
                fee_percent=0.0,
            )
        ]

    def _normalize_api_base(self, url: str, api_base_url: Optional[str], api_port: Optional[int]) -> str:
        if api_base_url:
            return api_base_url.rstrip("/")

        host = (url or "").replace("http://", "").replace("https://", "")
        host = host.split("/")[0].split(":")[0]
        port = api_port or self.DEFAULT_API_PORT
        return f"http://{host}:{port}"

    async def _get_json_text(self, session: aiohttp.ClientSession, endpoint: str) -> Optional[str]:
        async with session.get(endpoint, timeout=aiohttp.ClientTimeout(total=self.API_TIMEOUT)) as response:
            if response.status != 200:
                return None
            return await response.text()

    def _parse_status_payload(self, payload: str) -> Dict[str, Any]:
        """
        Parse ckpool /pool/pool.status line-delimited JSON object stream.
        Merges all object keys into one dictionary.
        """
        merged: Dict[str, Any] = {}

        if not payload:
            return merged

        for line in payload.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    merged.update(obj)
                continue
            except Exception:
                pass

            # Fallback for lines containing multiple adjacent JSON objects
            parts = stripped.replace("}{", "}\n{").splitlines()
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                try:
                    obj = json.loads(part)
                    if isinstance(obj, dict):
                        merged.update(obj)
                except Exception:
                    continue

        return merged

    def _parse_compact_hashrate_to_hs(self, value: Any) -> float:
        """Convert compact hashrate strings like '930G' to H/s."""
        if value is None:
            return 0.0

        if isinstance(value, (int, float)):
            return float(value)

        text = str(value).strip().upper()
        if not text:
            return 0.0

        multiplier_map = {
            "K": 1e3,
            "M": 1e6,
            "G": 1e9,
            "T": 1e12,
            "P": 1e15,
            "E": 1e18,
        }

        suffix = text[-1]
        if suffix in multiplier_map:
            try:
                return float(text[:-1]) * multiplier_map[suffix]
            except Exception:
                return 0.0

        try:
            return float(text)
        except Exception:
            return 0.0

    async def _fetch_status(self, api_base: str) -> Dict[str, Any]:
        status_url = f"{api_base}/pool/pool.status"
        async with aiohttp.ClientSession() as session:
            payload = await self._get_json_text(session, status_url)
            if payload is None:
                return {}
            return self._parse_status_payload(payload)

    async def _fetch_user(self, api_base: str, wallet: str) -> Dict[str, Any]:
        user_url = f"{api_base}/users/{wallet}"
        async with aiohttp.ClientSession() as session:
            payload = await self._get_json_text(session, user_url)
            if payload is None:
                return {}
            try:
                data = json.loads(payload)
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}

    def _build_user_metrics(self, user_data: Dict[str, Any]) -> Dict[str, Any]:
        workers = user_data.get("worker", [])
        workers_total = int(user_data.get("workers") or len(workers) or 0)

        shares_valid = user_data.get("shares")
        if shares_valid is None:
            shares_valid = sum(int(worker.get("shares") or 0) for worker in workers)

        bestshare = user_data.get("bestshare")
        if bestshare is None and workers:
            try:
                bestshare = max(float(worker.get("bestshare") or 0) for worker in workers)
            except Exception:
                bestshare = None

        lastshare = user_data.get("lastshare")
        if lastshare is None and workers:
            try:
                lastshare = max(int(worker.get("lastshare") or 0) for worker in workers)
            except Exception:
                lastshare = None

        return {
            "workers_total": workers_total,
            "shares_valid": int(shares_valid or 0),
            "bestshare": bestshare,
            "lastshare": lastshare,
            "hashrate_1m_hs": self._parse_compact_hashrate_to_hs(user_data.get("hashrate1m")),
            "hashrate_5m_hs": self._parse_compact_hashrate_to_hs(user_data.get("hashrate5m")),
            "hashrate_1h_hs": self._parse_compact_hashrate_to_hs(user_data.get("hashrate1hr")),
            "hashrate_1d_hs": self._parse_compact_hashrate_to_hs(user_data.get("hashrate1d")),
            "hashrate_7d_hs": self._parse_compact_hashrate_to_hs(user_data.get("hashrate7d")),
        }

    async def detect(self, url: str, port: int) -> bool:
        """Detect CKPool by probing /pool/pool.status on common local API ports."""
        candidates = [port, self.DEFAULT_API_PORT]
        seen = set()

        for candidate_port in candidates:
            if candidate_port in seen:
                continue
            seen.add(candidate_port)

            api_base = self._normalize_api_base(url, None, candidate_port)
            try:
                status = await self._fetch_status(api_base)
                if status and ("hashrate1m" in status or "Users" in status or "Workers" in status):
                    return True
            except Exception:
                continue

        return False

    async def get_health(self, url: str, port: int, **kwargs) -> PoolHealthStatus:
        api_base = self._normalize_api_base(
            url,
            kwargs.get("api_base_url"),
            kwargs.get("api_port"),
        )

        try:
            start_time = datetime.utcnow()
            status = await self._fetch_status(api_base)
            latency_ms = (datetime.utcnow() - start_time).total_seconds() * 1000

            if not status:
                return PoolHealthStatus(
                    is_healthy=False,
                    latency_ms=latency_ms,
                    error_message="No status data from CKPool API",
                )

            return PoolHealthStatus(
                is_healthy=True,
                latency_ms=latency_ms,
                additional_info={
                    "users": status.get("Users", 0),
                    "workers": status.get("Workers", 0),
                    "accepted": status.get("accepted", 0),
                    "rejected": status.get("rejected", 0),
                },
            )
        except Exception as e:
            logger.error(f"CKPool health check failed for {api_base}: {e}")
            return PoolHealthStatus(is_healthy=False, error_message=str(e))

    async def get_network_difficulty(self, coin: str, **kwargs) -> Optional[float]:
        api_base = self._normalize_api_base(
            kwargs.get("url", ""),
            kwargs.get("api_base_url"),
            kwargs.get("api_port"),
        )

        try:
            status = await self._fetch_status(api_base)
            # CKPool status exposes pool share difficulty as "diff". Use when available.
            diff = status.get("diff")
            return float(diff) if diff is not None else None
        except Exception as e:
            logger.error(f"CKPool difficulty fetch failed for {api_base}: {e}")
            return None

    async def get_pool_stats(
        self,
        url: str,
        coin: str,
        username: Optional[str] = None,
        **kwargs,
    ) -> Optional[PoolStats]:
        api_base = self._normalize_api_base(url, kwargs.get("api_base_url"), kwargs.get("api_port"))

        try:
            status = await self._fetch_status(api_base)
            if not status:
                return None

            pool_hashrate_hs = self._parse_compact_hashrate_to_hs(status.get("hashrate5m") or status.get("hashrate1m"))
            accepted = int(status.get("accepted") or 0)
            rejected = int(status.get("rejected") or 0)
            total = accepted + rejected
            reject_rate = (rejected / total * 100) if total > 0 else 0.0

            return PoolStats(
                hashrate=format_hashrate(pool_hashrate_hs, "H/s"),
                active_workers=int(status.get("Workers") or 0),
                blocks_found=None,
                network_difficulty=float(status.get("diff")) if status.get("diff") is not None else None,
                additional_stats={
                    "users": int(status.get("Users") or 0),
                    "idle_workers": int(status.get("Idle") or 0),
                    "disconnected_workers": int(status.get("Disconnected") or 0),
                    "accepted": accepted,
                    "rejected": rejected,
                    "bestshare": status.get("bestshare"),
                    "sps_1m": status.get("SPS1m"),
                    "sps_5m": status.get("SPS5m"),
                    "sps_15m": status.get("SPS15m"),
                    "sps_1h": status.get("SPS1h"),
                    "hashrate_1m": status.get("hashrate1m"),
                    "hashrate_5m": status.get("hashrate5m"),
                    "hashrate_15m": status.get("hashrate15m"),
                    "hashrate_1h": status.get("hashrate1hr"),
                    "hashrate_6h": status.get("hashrate6hr"),
                    "hashrate_1d": status.get("hashrate1d"),
                    "hashrate_7d": status.get("hashrate7d"),
                    "reject_rate": round(reject_rate, 2),
                },
            )
        except Exception as e:
            logger.error(f"CKPool get_pool_stats failed for {api_base}: {e}")
            return None

    async def get_blocks(
        self,
        url: str,
        coin: str,
        hours: int = 24,
        **kwargs,
    ) -> List[PoolBlock]:
        # CKPool status endpoint does not expose full block history in discovered API shape.
        return []

    async def get_worker_stats(
        self,
        url: str,
        coin: str,
        worker_name: str,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:
        api_base = self._normalize_api_base(url, kwargs.get("api_base_url"), kwargs.get("api_port"))
        wallet = kwargs.get("wallet") or kwargs.get("username")
        if not wallet:
            return None

        try:
            data = await self._fetch_user(api_base, wallet)
            if not data:
                return None

            workers = data.get("worker", [])
            for worker in workers:
                candidate = worker.get("workername", "")
                if worker_name.lower() in candidate.lower():
                    return worker

            return None
        except Exception as e:
            logger.error(f"CKPool get_worker_stats failed for {api_base}: {e}")
            return None

    async def get_user_stats(
        self,
        url: str,
        coin: str,
        username: str,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:
        api_base = self._normalize_api_base(url, kwargs.get("api_base_url"), kwargs.get("api_port"))
        wallet = kwargs.get("wallet") or username
        if not wallet:
            return None

        try:
            user_data = await self._fetch_user(api_base, wallet)
            if not user_data:
                return None

            metrics = self._build_user_metrics(user_data)
            return {
                "wallet": wallet,
                "workers_total": metrics["workers_total"],
                "shares_valid": metrics["shares_valid"],
                "bestshare": metrics["bestshare"],
                "lastshare": metrics["lastshare"],
                "hashrate_1m": user_data.get("hashrate1m"),
                "hashrate_5m": user_data.get("hashrate5m"),
                "hashrate_1h": user_data.get("hashrate1hr"),
                "hashrate_1d": user_data.get("hashrate1d"),
                "hashrate_7d": user_data.get("hashrate7d"),
                "hashrate_1m_hs": metrics["hashrate_1m_hs"],
                "hashrate_5m_hs": metrics["hashrate_5m_hs"],
                "hashrate_1h_hs": metrics["hashrate_1h_hs"],
                "hashrate_1d_hs": metrics["hashrate_1d_hs"],
                "hashrate_7d_hs": metrics["hashrate_7d_hs"],
            }
        except Exception as e:
            logger.error(f"CKPool get_user_stats failed for {api_base}: {e}")
            return None

    def get_config_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "api_base_url": {
                    "type": "string",
                    "title": "CKPool API Base URL",
                    "description": "Example: http://10.200.204.2:3001",
                },
                "wallet": {
                    "type": "string",
                    "title": "Wallet Address",
                    "description": "Wallet used for /users/<wallet> dashboard stats",
                },
            },
        }

    async def get_dashboard_data(
        self,
        url: str,
        coin: str,
        username: Optional[str] = None,
        **kwargs,
    ) -> Optional[DashboardTileData]:
        api_base = self._normalize_api_base(url, kwargs.get("api_base_url"), kwargs.get("api_port"))
        wallet = kwargs.get("wallet") or username

        if not wallet:
            return DashboardTileData(
                health_status=False,
                health_message="CKPool dashboard requires wallet (user-level metrics)",
                currency=coin.upper(),
            )

        try:
            start_time = datetime.utcnow()
            status = await self._fetch_status(api_base)
            user_data = await self._fetch_user(api_base, wallet)
            latency_ms = (datetime.utcnow() - start_time).total_seconds() * 1000

            if not status:
                return DashboardTileData(
                    health_status=False,
                    health_message="No CKPool status data",
                    latency_ms=latency_ms,
                    currency=coin.upper(),
                )

            if not user_data:
                return DashboardTileData(
                    health_status=False,
                    health_message=f"CKPool reachable but wallet not found: {wallet}",
                    latency_ms=latency_ms,
                    currency=coin.upper(),
                )

            metrics = self._build_user_metrics(user_data)
            user_hashrate_hs = metrics["hashrate_5m_hs"] or metrics["hashrate_1m_hs"]

            health_message = f"Wallet active: {metrics['workers_total']} worker(s)"
            if metrics["bestshare"] is not None:
                health_message = f"Wallet active: {metrics['workers_total']} worker(s), bestshare {metrics['bestshare']:.2f}"

            return DashboardTileData(
                health_status=True,
                health_message=health_message,
                latency_ms=latency_ms,
                network_difficulty=float(status.get("diff")) if status.get("diff") is not None else None,
                pool_hashrate=format_hashrate(user_hashrate_hs, "H/s"),
                active_workers=metrics["workers_total"],
                shares_valid=metrics["shares_valid"],
                shares_invalid=None,
                shares_stale=None,
                reject_rate=None,
                currency=coin.upper(),
                supports_earnings=False,
                supports_balance=False,
                last_updated=datetime.utcnow(),
            )
        except Exception as e:
            logger.error(f"CKPool get_dashboard_data failed for {api_base}: {e}")
            return DashboardTileData(
                health_status=False,
                health_message=str(e),
                currency=coin.upper(),
            )
