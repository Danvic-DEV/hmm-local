from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import List, Tuple

import pytest
from fastapi import HTTPException

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from core.strategy_enrollment import (  # noqa: E402
    reject_conflicting_price_band_enrollment,
    reject_conflicting_solar_enrollment,
)


class _FakeResult:
    def __init__(self, rows: List[Tuple]):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    """Returns queued results in order, regardless of the actual query -
    enough to test the conflict-detection branching without a real DB."""

    def __init__(self, results: List[_FakeResult]):
        self._results = list(results)

    async def execute(self, _query):
        return self._results.pop(0)


def test_no_conflict_when_lists_empty():
    db = _FakeDB([])
    asyncio.run(reject_conflicting_price_band_enrollment(db, []))
    asyncio.run(reject_conflicting_solar_enrollment(db, []))  # must not raise, must not even query


def test_no_conflict_when_no_overlap():
    db = _FakeDB([_FakeResult([])])  # no MinerStrategy rows match
    asyncio.run(reject_conflicting_price_band_enrollment(db, [1, 2, 3]))  # must not raise


def test_raises_on_conflict_with_names():
    db = _FakeDB([
        _FakeResult([(5,)]),               # conflicting miner_id from MinerStrategy
        _FakeResult([("Bitaxe01",)]),      # name lookup
    ])
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(reject_conflicting_price_band_enrollment(db, [5]))

    assert exc_info.value.status_code == 409
    assert "Bitaxe01" in exc_info.value.detail
    assert "Price Band Strategy" in exc_info.value.detail


def test_reverse_direction_raises_too():
    db = _FakeDB([
        _FakeResult([(7,)]),               # conflicting miner_id from SolarMinerEnrollment
        _FakeResult([("nOrange",)]),
    ])
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(reject_conflicting_solar_enrollment(db, [7]))

    assert exc_info.value.status_code == 409
    assert "nOrange" in exc_info.value.detail
    assert "Solar Strategy" in exc_info.value.detail
