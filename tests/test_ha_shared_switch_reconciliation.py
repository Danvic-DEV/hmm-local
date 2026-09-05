from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


# Ensure app/ is importable when tests run from repo root
APP_ROOT = Path(__file__).resolve().parents[1] / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))


class _DummyColumn:
    def __init__(self, name: str):
        self.name = name

    def __eq__(self, other):
        return ("eq", self.name, other)

    def __le__(self, other):
        return ("le", self.name, other)

    def isnot(self, other):
        return ("isnot", self.name, other)

    def in_(self, values):
        return ("in", self.name, tuple(values))


class _FakeQuery:
    def where(self, *_args, **_kwargs):
        return self

    def join(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def distinct(self):
        return self


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

    class _EnergyPrice:
        pass

    class _Telemetry:
        miner_id = _DummyColumn("miner_id")
        timestamp = _DummyColumn("timestamp")

    class _Miner:
        pass

    class _AuditLog:
        pass

    db_mod.EnergyPrice = _EnergyPrice
    db_mod.Telemetry = _Telemetry
    db_mod.Miner = _Miner
    db_mod.AuditLog = _AuditLog
    sys.modules["core.database"] = db_mod

# If another test already stubbed core.database, ensure required symbols exist.
db_mod = sys.modules["core.database"]
if not hasattr(db_mod, "EnergyPrice"):
    class _EnergyPrice:
        pass

    db_mod.EnergyPrice = _EnergyPrice
if not hasattr(db_mod, "Telemetry"):
    class _Telemetry:
        miner_id = _DummyColumn("miner_id")
        timestamp = _DummyColumn("timestamp")

    db_mod.Telemetry = _Telemetry
if not hasattr(db_mod, "Miner"):
    class _Miner:
        pass

    db_mod.Miner = _Miner
if not hasattr(db_mod, "AuditLog"):
    class _AuditLog:
        pass

    db_mod.AuditLog = _AuditLog

import core.scheduler as scheduler_module
from core.scheduler import SchedulerService


class _HomeAssistantConfig:
    enabled = _DummyColumn("enabled")

    def __init__(self, enabled: bool, base_url: str, access_token: str):
        self.enabled = enabled
        self.base_url = base_url
        self.access_token = access_token


@dataclass
class _PriceBandStrategyConfig:
    champion_mode_enabled: bool = False
    current_champion_miner_id: int | None = None


@dataclass
class _HomeAssistantDevice:
    id: int
    name: str
    entity_id: str
    current_state: str
    last_off_command_timestamp: datetime | None
    last_state_change: datetime | None = None


class _HomeAssistantDeviceModel:
    id = _DummyColumn("id")
    current_state = _DummyColumn("current_state")
    last_off_command_timestamp = _DummyColumn("last_off_command_timestamp")


class _MinerStrategyModel:
    miner_id = _DummyColumn("miner_id")
    strategy_enabled = _DummyColumn("strategy_enabled")


class _MinerHASwitchLinkModel:
    miner_id = _DummyColumn("miner_id")
    ha_device_id = _DummyColumn("ha_device_id")


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Result:
    def __init__(self, *, scalar=None, scalars=None):
        self._scalar = scalar
        self._scalars = scalars or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return _Scalars(self._scalars)


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.commits = 0

    async def execute(self, _query):
        if not self._results:
            raise AssertionError("No queued query result")
        return self._results.pop(0)

    async def commit(self):
        self.commits += 1


class _AsyncSessionLocal:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _HomeAssistantIntegration:
    def __init__(self, *_args, **_kwargs):
        pass

    async def turn_on(self, _entity_id: str):
        return True

    async def turn_off(self, _entity_id: str):
        return True


class _NotificationService:
    async def send_to_all_channels(self, **_kwargs):
        return None


def test_reconcile_shared_switch_clears_timestamp_when_no_enrolled_linked_miners(monkeypatch):
    # Patch models and session source imported at runtime inside _reconcile_ha_device_states.
    db_module = sys.modules["core.database"]
    device = _HomeAssistantDevice(
        id=11,
        name="Shared Rack Switch",
        entity_id="switch.shared_rack",
        current_state="off",
        last_off_command_timestamp=datetime.utcnow() - timedelta(minutes=10),
    )
    fake_db = _FakeDB(
        [
            # HomeAssistantConfig lookup
            _Result(scalars=[_HomeAssistantConfig(enabled=True, base_url="http://ha", access_token="token")]),
            # PriceBandStrategyConfig lookup
            _Result(scalar=_PriceBandStrategyConfig(champion_mode_enabled=False, current_champion_miner_id=None)),
            # OFF device lookup
            _Result(scalars=[device]),
            # Link lookup for this switch
            _Result(scalars=[101, 202]),
            # Strategy-enrolled lookup (none enrolled)
            _Result(scalars=[]),
        ]
    )

    db_module.AsyncSessionLocal = lambda: _AsyncSessionLocal(fake_db)
    db_module.HomeAssistantDevice = _HomeAssistantDeviceModel
    db_module.HomeAssistantConfig = _HomeAssistantConfig
    db_module.Telemetry = getattr(db_module, "Telemetry")
    db_module.PriceBandStrategyConfig = _PriceBandStrategyConfig
    db_module.MinerStrategy = _MinerStrategyModel
    db_module.MinerHASwitchLink = _MinerHASwitchLinkModel

    # Patch runtime imports used by the method.
    ha_module = types.ModuleType("integrations.homeassistant")
    ha_module.HomeAssistantIntegration = _HomeAssistantIntegration
    sys.modules["integrations.homeassistant"] = ha_module

    notifications_module = types.ModuleType("core.notifications")
    notifications_module.NotificationService = _NotificationService
    sys.modules["core.notifications"] = notifications_module

    import sqlalchemy

    monkeypatch.setattr(sqlalchemy, "select", lambda *_args, **_kwargs: _FakeQuery())

    service = SchedulerService()
    asyncio.run(service._reconcile_ha_device_states())

    # Because no linked miners are enrolled in strategy, reconciliation should skip and clear off timestamp.
    assert device.last_off_command_timestamp is None
    assert fake_db.commits == 1


def _build_base_fake_db(device, extra_results):
    """Helper: build a FakeDB with standard HA config + strategy preamble + device list."""
    return _FakeDB(
        [
            _Result(scalars=[_HomeAssistantConfig(enabled=True, base_url="http://ha", access_token="token")]),
            _Result(scalar=_PriceBandStrategyConfig(champion_mode_enabled=False, current_champion_miner_id=None)),
            _Result(scalars=[device]),
        ]
        + list(extra_results)
    )


def _apply_common_patches(db_module, fake_db, monkeypatch):
    import sqlalchemy

    db_module.AsyncSessionLocal = lambda: _AsyncSessionLocal(fake_db)
    db_module.HomeAssistantDevice = _HomeAssistantDeviceModel
    db_module.HomeAssistantConfig = _HomeAssistantConfig
    db_module.Telemetry = getattr(db_module, "Telemetry")
    db_module.PriceBandStrategyConfig = _PriceBandStrategyConfig
    db_module.MinerStrategy = _MinerStrategyModel
    db_module.MinerHASwitchLink = _MinerHASwitchLinkModel

    ha_module = types.ModuleType("integrations.homeassistant")
    ha_module.HomeAssistantIntegration = _HomeAssistantIntegration
    sys.modules["integrations.homeassistant"] = ha_module

    notifications_module = types.ModuleType("core.notifications")
    notifications_module.NotificationService = _NotificationService
    sys.modules["core.notifications"] = notifications_module

    monkeypatch.setattr(sqlalchemy, "select", lambda *_args, **_kwargs: _FakeQuery())


def test_reconcile_skips_switch_when_only_champion_linked(monkeypatch):
    """When all enrolled miners for a switch are the current champion, reconciliation is skipped."""
    db_module = sys.modules["core.database"]
    device = _HomeAssistantDevice(
        id=22,
        name="Champion Switch",
        entity_id="switch.champion",
        current_state="off",
        last_off_command_timestamp=datetime.utcnow() - timedelta(minutes=10),
    )
    champion_id = 55
    fake_db = _FakeDB(
        [
            _Result(scalars=[_HomeAssistantConfig(enabled=True, base_url="http://ha", access_token="token")]),
            # Champion mode active
            _Result(
                scalar=_PriceBandStrategyConfig(
                    champion_mode_enabled=True, current_champion_miner_id=champion_id
                )
            ),
            _Result(scalars=[device]),
            # Links: only the champion miner
            _Result(scalars=[champion_id]),
            # Strategy-enrolled: same miner is enrolled
            _Result(scalars=[champion_id]),
            # No telemetry query should be reached; extra safety result
        ]
    )

    _apply_common_patches(db_module, fake_db, monkeypatch)

    service = SchedulerService()
    asyncio.run(service._reconcile_ha_device_states())

    # Champion is the only linked enrolled miner → switch is protected; timestamp NOT changed.
    assert device.last_off_command_timestamp is not None


def test_reconcile_skips_when_no_recent_telemetry(monkeypatch):
    """When enrolled miners have no telemetry in the last 3 minutes, device is NOT reconciled."""
    db_module = sys.modules["core.database"]
    device = _HomeAssistantDevice(
        id=33,
        name="Quiet Switch",
        entity_id="switch.quiet",
        current_state="off",
        last_off_command_timestamp=datetime.utcnow() - timedelta(minutes=10),
    )
    fake_db = _FakeDB(
        [
            _Result(scalars=[_HomeAssistantConfig(enabled=True, base_url="http://ha", access_token="token")]),
            _Result(scalar=_PriceBandStrategyConfig(champion_mode_enabled=False, current_champion_miner_id=None)),
            _Result(scalars=[device]),
            # Links: miner 77
            _Result(scalars=[77]),
            # Enrolled: miner 77 enrolled
            _Result(scalars=[77]),
            # Recent telemetry: none (miner is truly off)
            _Result(scalars=[]),
        ]
    )

    _apply_common_patches(db_module, fake_db, monkeypatch)

    service = SchedulerService()
    asyncio.run(service._reconcile_ha_device_states())

    # No recent telemetry → no reconciliation action, timestamp unchanged.
    assert device.last_off_command_timestamp is not None
