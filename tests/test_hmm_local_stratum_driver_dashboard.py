from __future__ import annotations

import asyncio
import runpy
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "app"
DRIVER_PATH = ROOT / "bundled_config" / "drivers" / "pools" / "hmm_local_stratum_driver.py"

if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

MOD = runpy.run_path(str(DRIVER_PATH))


def test_dashboard_health_message_uses_standard_workers_online_format() -> None:
    integration = MOD["HMMLocalStratumIntegration"]()

    payload = {
        "ok": True,
        "quality": {
            "has_required_inputs": True,
            "stale": False,
            "readiness": "ready",
            "missing_inputs": [],
        },
        "workers": {"count": 3},
        "network": {"network_difficulty": 1000000.0},
        "hashrate": {"pool_hashrate_hs": 1.2e12},
        "kpi": {
            "share_accept_count": 10,
            "share_reject_count": 1,
            "share_reject_rate_pct": 9.09,
            "block_accept_count_24h": 99,
            "expected_time_to_block_sec": 7200,
            "pool_share_of_network_pct": 1.23,
        },
        "rejects": {"by_reason": {"stale_share": 0}},
    }

    async def fake_fetch_snapshot(self, *, url: str, coin: str, window_minutes: int, **kwargs):
        return payload, 12.5, None

    async def fake_fetch_stats(self, *, url: str, **kwargs):
        return {
            "coins": {
                "DGB": {
                    "connected_workers": 2,
                    "blocks_accepted": 2,
                }
            }
        }, 8.0, None

    integration._fetch_snapshot = types.MethodType(fake_fetch_snapshot, integration)
    integration._fetch_stats = types.MethodType(fake_fetch_stats, integration)

    tile = asyncio.run(
        integration.get_dashboard_data(
            url="hmm-local-stratum",
            coin="DGB",
            snapshot_window_minutes=15,
        )
    )

    assert tile is not None
    assert tile.health_status is True
    assert tile.health_message == "2 workers online"
    assert tile.active_workers == 2
    assert tile.blocks_found_24h == 2
