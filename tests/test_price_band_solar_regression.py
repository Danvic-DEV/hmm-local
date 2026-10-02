from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import List

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from api.price_band_strategy import PriceBandStrategySettings, save_price_band_strategy_settings  # noqa: E402


class _FakeResult:
    def __init__(self, scalar=None, scalars_list=None, all_list=None):
        self._scalar = scalar
        self._scalars_list = scalars_list or []
        self._all_list = all_list if all_list is not None else []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: self._scalars_list)

    def all(self):
        return self._all_list


class _FakeDB:
    def __init__(self, results: List[_FakeResult]):
        self._results = list(results)
        self.added = []
        self.deleted = []
        self.committed = False
        self.flushed = False

    async def execute(self, _query):
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True


def test_save_settings_succeeds_normally_with_no_solar_conflict():
    """T027 - the new exclusivity check this feature adds to Price Band
    Strategy's existing save endpoint must not break normal price-band-only
    usage (no Solar Strategy involvement at all)."""
    fake_strategy = SimpleNamespace(enabled=False, champion_mode_enabled=False, updated_at=None)

    db = _FakeDB([
        _FakeResult(all_list=[]),              # reject_conflicting_solar_enrollment: no conflicts
        _FakeResult(scalar=fake_strategy),      # existing PriceBandStrategyConfig
        _FakeResult(scalars_list=[]),           # existing MinerStrategy rows to clear
    ])

    settings = PriceBandStrategySettings(enabled=True, miner_ids=[1, 2], champion_mode_enabled=False)

    result = asyncio.run(save_price_band_strategy_settings(settings, db))

    assert result["enabled"] is True
    assert result["enrolled_count"] == 2
    assert db.committed is True
    assert len(db.added) == 2  # two new MinerStrategy rows
    assert fake_strategy.enabled is True
