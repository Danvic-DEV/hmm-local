from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1] / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))


if "core.config" not in sys.modules:
    config_mod = types.ModuleType("core.config")

    class _Config:
        @staticmethod
        def get(_key, default=None):
            return default

    config_mod.app_config = _Config()
    sys.modules["core.config"] = config_mod

if "core.cloud_push" not in sys.modules:
    cloud_mod = types.ModuleType("core.cloud_push")
    cloud_mod.init_cloud_service = lambda _cfg: None
    cloud_mod.get_cloud_service = lambda: None
    sys.modules["core.cloud_push"] = cloud_mod


if "core.database" not in sys.modules:
    db_mod = types.ModuleType("core.database")
    sys.modules["core.database"] = db_mod
else:
    db_mod = sys.modules["core.database"]


class _EnergyPrice:
    pass


class _Telemetry:
    pass


class _Miner:
    pass


class _AuditLog:
    pass


class _MinerModePowerStats:
    miner_id = object()
    mode = object()

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)
        self.resets_count = getattr(self, "resets_count", 0)


if not hasattr(db_mod, "EnergyPrice"):
    db_mod.EnergyPrice = _EnergyPrice
if not hasattr(db_mod, "Telemetry"):
    db_mod.Telemetry = _Telemetry
if not hasattr(db_mod, "Miner"):
    db_mod.Miner = _Miner
if not hasattr(db_mod, "AuditLog"):
    db_mod.AuditLog = _AuditLog
if not hasattr(db_mod, "MinerModePowerStats"):
    db_mod.MinerModePowerStats = _MinerModePowerStats


import core.scheduler as scheduler_module
from core.scheduler import SchedulerService


class _FakeQuery:
    def where(self, *_args, **_kwargs):
        return self


scheduler_module.select = lambda *_args, **_kwargs: _FakeQuery()
scheduler_module.and_ = lambda *_args: _args


@dataclass
class _FakeMiner:
    id: int
    name: str
    firmware_version: str | None


@dataclass
class _FakeTelemetry:
    power_watts: float | None
    timestamp: object
    extra_data: dict | None


class _FakeResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeDB:
    def __init__(self, execute_results):
        self._execute_results = list(execute_results)
        self.added = []
        self.execute_calls = 0

    async def execute(self, _query):
        self.execute_calls += 1
        if not self._execute_results:
            return _FakeResult()
        return self._execute_results.pop(0)

    def add(self, obj):
        self.added.append(obj)


def test_apply_running_power_sample_initializes_correctly():
    applied = SchedulerService._apply_running_power_sample(
        sample_count=0,
        avg_power_watts=None,
        ema_power_watts=None,
        min_power_watts=None,
        max_power_watts=None,
        power_sample=100.0,
    )

    assert applied["sample_count"] == 1
    assert applied["avg_power_watts"] == 100.0
    assert applied["ema_power_watts"] == 100.0
    assert applied["min_power_watts"] == 100.0
    assert applied["max_power_watts"] == 100.0


def test_apply_running_power_sample_updates_average_and_ema():
    applied = SchedulerService._apply_running_power_sample(
        sample_count=2,
        avg_power_watts=90.0,
        ema_power_watts=92.0,
        min_power_watts=80.0,
        max_power_watts=100.0,
        power_sample=120.0,
        ema_alpha=0.2,
    )

    assert applied["sample_count"] == 3
    assert round(applied["avg_power_watts"], 2) == 100.0
    assert round(applied["ema_power_watts"], 2) == 97.6
    assert applied["min_power_watts"] == 80.0
    assert applied["max_power_watts"] == 120.0


def test_update_mode_power_stats_skips_invalid_samples():
    service = SchedulerService()
    db = _FakeDB([])
    miner = _FakeMiner(id=1, name="M1", firmware_version="fw1")

    telemetry = _FakeTelemetry(power_watts=None, timestamp=object(), extra_data={"frequency": 500})
    asyncio.run(service._update_miner_mode_power_stats(db, miner, telemetry, mode="high"))

    telemetry_zero = _FakeTelemetry(power_watts=0, timestamp=object(), extra_data={"frequency": 500})
    asyncio.run(service._update_miner_mode_power_stats(db, miner, telemetry_zero, mode="high"))

    telemetry_mode = _FakeTelemetry(power_watts=90, timestamp=object(), extra_data={"frequency": 500})
    asyncio.run(service._update_miner_mode_power_stats(db, miner, telemetry_mode, mode=None))

    assert db.execute_calls == 0
    assert db.added == []


def test_update_mode_power_stats_creates_new_row():
    service = SchedulerService()
    db = _FakeDB([_FakeResult(scalar=None)])
    miner = _FakeMiner(id=7, name="M7", firmware_version="fw-a")
    telemetry = _FakeTelemetry(
        power_watts=110.0,
        timestamp=object(),
        extra_data={"frequency": 525, "voltage_mv": 1300},
    )

    asyncio.run(service._update_miner_mode_power_stats(db, miner, telemetry, mode="high"))

    assert len(db.added) == 1
    row = db.added[0]
    assert row.miner_id == 7
    assert row.mode == "high"
    assert row.sample_count == 1
    assert row.avg_power_watts == 110.0
    assert row.ema_power_watts == 110.0
    assert row.min_power_watts == 110.0
    assert row.max_power_watts == 110.0
    assert row.firmware_version == "fw-a"
    assert isinstance(row.profile_signature, str)


def test_update_mode_power_stats_resets_on_profile_drift():
    service = SchedulerService()
    existing = _MinerModePowerStats(
        miner_id=3,
        mode="eco",
        sample_count=5,
        avg_power_watts=70.0,
        ema_power_watts=72.0,
        min_power_watts=65.0,
        max_power_watts=78.0,
        firmware_version="fw-old",
        profile_signature='{"frequency":500}',
        resets_count=1,
    )
    db = _FakeDB([_FakeResult(scalar=existing)])

    miner = _FakeMiner(id=3, name="M3", firmware_version="fw-new")
    telemetry = _FakeTelemetry(
        power_watts=88.0,
        timestamp=object(),
        extra_data={"frequency": 600},
    )

    asyncio.run(service._update_miner_mode_power_stats(db, miner, telemetry, mode="eco"))

    assert existing.resets_count == 2
    assert existing.sample_count == 1
    assert existing.avg_power_watts == 88.0
    assert existing.ema_power_watts == 88.0
    assert existing.min_power_watts == 88.0
    assert existing.max_power_watts == 88.0
    assert existing.firmware_version == "fw-new"
    assert existing.profile_signature == '{"frequency":600}'
