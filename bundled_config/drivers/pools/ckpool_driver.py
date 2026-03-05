"""
CKPool Local Integration Plugin

Supports local CKPool-style solo pool status endpoints served over HTTP,
including:
- /pool/pool.status (line-delimited JSON objects)
- /users/<wallet> (wallet + worker stats)
"""
import json
import logging
import re
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

__version__ = "1.0.8"


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

    def _extract_network_difficulty(self, status: Dict[str, Any]) -> Optional[float]:
        """
        Extract true chain/network difficulty when explicitly provided by API.
        Do not treat CKPool share diff as network difficulty.
        """
        candidate_keys = [
            "network_difficulty",
            "networkDifficulty",
            "networkdiff",
            "n_diff",
            "chain_difficulty",
        ]

        for key in candidate_keys:
            value = status.get(key)
            if value is None:
                continue
            try:
                parsed = float(value)
                if parsed > 0:
                    return parsed
            except Exception:
                continue

        return None

    async def _extract_network_difficulty_from_log(self, api_base: str) -> Optional[float]:
        """
        Secondary source for network difficulty from ckpool.log lines like:
        'Network diff set to 949839478.1'
        """
        try:
            log_url = f"{api_base}/ckpool.log"
            async with aiohttp.ClientSession() as session:
                payload = await self._get_json_text(session, log_url)
                if not payload:
                    return None

            matches = re.findall(r"Network diff set to\s+([0-9]+(?:\.[0-9]+)?)", payload)
            if not matches:
                return None

            latest = float(matches[-1])
            return latest if latest > 0 else None
        except Exception:
            return None

    async def _fetch_ckpool_log(self, api_base: str) -> Optional[str]:
        try:
            log_url = f"{api_base}/ckpool.log"
            async with aiohttp.ClientSession() as session:
                return await self._get_json_text(session, log_url)
        except Exception:
            return None

    def _parse_share_counts_from_log(self, log_text: str, window_seconds: int = 3600) -> Dict[str, int]:
        if not log_text:
            return {"accepted": 0, "rejected": 0, "stale": 0}

        cutoff = datetime.utcnow().timestamp() - max(1, window_seconds)
        accepted = 0
        rejected = 0
        stale = 0
        pool_points: List[tuple[float, int, int]] = []

        for raw_line in log_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            timestamp_match = re.match(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\]", line)
            if not timestamp_match:
                continue

            try:
                line_ts = datetime.strptime(timestamp_match.group(1), "%Y-%m-%d %H:%M:%S.%f").timestamp()
            except ValueError:
                try:
                    line_ts = datetime.strptime(timestamp_match.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                except ValueError:
                    continue

            if line_ts < cutoff:
                continue

            if re.search(r"Accepted\s+share", line, flags=re.IGNORECASE):
                accepted += 1
                continue

            if re.search(r"Rejected\s+share|Low\s+difficulty\s+share", line, flags=re.IGNORECASE):
                rejected += 1
                continue

            if re.search(r"Stale\s+share", line, flags=re.IGNORECASE):
                stale += 1

            # Fallback source: CKPool periodic summary lines
            # Example: [ts] Pool:{"diff":...,"accepted":6155000,"rejected":52000,...}
            if "Pool:{" in line:
                try:
                    payload = line.split("Pool:", 1)[1].strip()
                    data = json.loads(payload)
                    if "accepted" not in data or "rejected" not in data:
                        continue

                    acc_total = int(data.get("accepted"))
                    rej_total = int(data.get("rejected"))
                    pool_points.append((line_ts, acc_total, rej_total))
                except Exception:
                    pass

        # If no explicit event lines were found, derive 1h counts from cumulative deltas.
        if accepted == 0 and rejected == 0 and len(pool_points) >= 2:
            pool_points.sort(key=lambda item: item[0])
            _, start_acc, start_rej = pool_points[0]
            _, end_acc, end_rej = pool_points[-1]
            accepted = max(0, end_acc - start_acc)
            rejected = max(0, end_rej - start_rej)

        return {
            "accepted": accepted,
            "rejected": rejected,
            "stale": stale,
        }

    def _has_explicit_share_event_lines(self, log_text: str) -> bool:
        if not log_text:
            return False
        return bool(re.search(r"Accepted\s+share|Rejected\s+share|Stale\s+share|Low\s+difficulty\s+share", log_text, flags=re.IGNORECASE))

    def _estimate_accepted_shares_1h_from_sps(self, status: Dict[str, Any]) -> Optional[int]:
        sps_1h = status.get("SPS1h")
        if sps_1h is None:
            return None
        try:
            sps = float(sps_1h)
            if sps <= 0:
                return 0
            return int(round(sps * 3600))
        except Exception:
            return None

    async def _resolve_network_difficulty(self, api_base: str, status: Dict[str, Any]) -> Optional[float]:
        network_diff = self._extract_network_difficulty(status)
        if network_diff is not None:
            return network_diff
        return await self._extract_network_difficulty_from_log(api_base)

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
            "shares_valid_total": int(shares_valid or 0),
            "bestshare": bestshare,
            "lastshare": lastshare,
            "hashrate_1m_hs": self._parse_compact_hashrate_to_hs(user_data.get("hashrate1m")),
            "hashrate_5m_hs": self._parse_compact_hashrate_to_hs(user_data.get("hashrate5m")),
            "hashrate_1h_hs": self._parse_compact_hashrate_to_hs(user_data.get("hashrate1hr")),
            "hashrate_1d_hs": self._parse_compact_hashrate_to_hs(user_data.get("hashrate1d")),
            "hashrate_7d_hs": self._parse_compact_hashrate_to_hs(user_data.get("hashrate7d")),
        }

    def _estimate_user_recent_shares(self, status: Dict[str, Any], metrics: Dict[str, Any]) -> Optional[int]:
        """
        Estimate user valid shares over a recent window using pool SPS and
        user/pool hashrate ratio. This avoids showing CKPool lifetime counters
        as tile shares.
        """
        windows = [
            ("SPS1h", "hashrate1hr", "hashrate_1h_hs", 3600),
            ("SPS15m", "hashrate15m", "hashrate_5m_hs", 900),
            ("SPS5m", "hashrate5m", "hashrate_5m_hs", 300),
            ("SPS1m", "hashrate1m", "hashrate_1m_hs", 60),
        ]

        for sps_key, pool_hash_key, user_hash_key, window_seconds in windows:
            sps_value = status.get(sps_key)
            if sps_value is None:
                continue

            try:
                pool_sps = float(sps_value)
            except Exception:
                continue

            if pool_sps <= 0:
                continue

            pool_hash_hs = self._parse_compact_hashrate_to_hs(status.get(pool_hash_key))
            user_hash_hs = float(metrics.get(user_hash_key) or 0)

            if pool_hash_hs <= 0 or user_hash_hs <= 0:
                continue

            ratio = user_hash_hs / pool_hash_hs
            ratio = max(0.0, min(1.0, ratio))
            estimated = int(round(pool_sps * window_seconds * ratio))
            return max(0, estimated)

        return None

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
            return await self._resolve_network_difficulty(api_base, status)
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
            network_difficulty = await self._resolve_network_difficulty(api_base, status)

            pool_hashrate_hs = self._parse_compact_hashrate_to_hs(status.get("hashrate5m") or status.get("hashrate1m"))
            accepted = int(status.get("accepted") or 0)
            rejected = int(status.get("rejected") or 0)
            total = accepted + rejected
            reject_rate = (rejected / total * 100) if total > 0 else 0.0

            return PoolStats(
                hashrate=format_hashrate(pool_hashrate_hs, "H/s"),
                active_workers=int(status.get("Workers") or 0),
                blocks_found=None,
                network_difficulty=network_difficulty,
                additional_stats={
                    "users": int(status.get("Users") or 0),
                    "idle_workers": int(status.get("Idle") or 0),
                    "disconnected_workers": int(status.get("Disconnected") or 0),
                    "pool_share_difficulty": float(status.get("diff")) if status.get("diff") is not None else None,
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
                "shares_valid_total": metrics["shares_valid_total"],
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
            log_text = await self._fetch_ckpool_log(api_base)
            share_counts = self._parse_share_counts_from_log(log_text or "")
            has_event_lines = self._has_explicit_share_event_lines(log_text or "")

            network_difficulty = self._extract_network_difficulty(status)
            if network_difficulty is None and log_text:
                matches = re.findall(r"Network diff set to\s+([0-9]+(?:\.[0-9]+)?)", log_text)
                if matches:
                    try:
                        network_difficulty = float(matches[-1])
                    except Exception:
                        network_difficulty = None

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

            shares_valid_display = None
            shares_invalid_display = None
            shares_stale_display = None
            reject_rate = None

            if has_event_lines:
                shares_valid_display = share_counts["accepted"]
                shares_invalid_display = share_counts["rejected"]
                shares_stale_display = share_counts["stale"]

                total_share_events = (
                    (shares_valid_display or 0)
                    + (shares_invalid_display or 0)
                    + (shares_stale_display or 0)
                )
                if total_share_events > 0:
                    reject_rate = round(((shares_invalid_display or 0) + (shares_stale_display or 0)) / total_share_events * 100, 2)
            else:
                # No explicit per-share event lines in log: use SPS1h accepted estimate only.
                shares_valid_display = self._estimate_accepted_shares_1h_from_sps(status)

            workers_total = int(metrics["workers_total"] or 0)
            if workers_total == 1:
                health_message = "1 worker online"
            else:
                health_message = f"{workers_total} workers online"

            last_updated = datetime.utcnow()
            if metrics.get("lastshare"):
                try:
                    last_updated = datetime.utcfromtimestamp(int(metrics["lastshare"]))
                except Exception:
                    pass

            return DashboardTileData(
                health_status=True,
                health_message=health_message,
                latency_ms=latency_ms,
                network_difficulty=network_difficulty,
                pool_hashrate=format_hashrate(user_hashrate_hs, "H/s"),
                active_workers=metrics["workers_total"],
                shares_valid=shares_valid_display,
                shares_invalid=shares_invalid_display,
                shares_stale=shares_stale_display,
                reject_rate=reject_rate,
                currency=coin.upper(),
                supports_earnings=False,
                supports_balance=False,
                last_updated=last_updated,
            )
        except Exception as e:
            logger.error(f"CKPool get_dashboard_data failed for {api_base}: {e}")
            return DashboardTileData(
                health_status=False,
                health_message=str(e),
                currency=coin.upper(),
            )
