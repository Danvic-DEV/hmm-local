from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1] / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))


class _DummyColumn:
    def __init__(self, name: str):
        self.name = name

    def __eq__(self, other):
        return ("eq", self.name, other)


class _FakeQuery:
    def where(self, *_args, **_kwargs):
        return self

    def join(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeResult:
    def __init__(self, *, scalar=None, scalars=None):
        self._scalar = scalar
        self._scalars = scalars or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return _FakeScalars(self._scalars)


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


if "core.database" not in sys.modules:
    db_mod = types.ModuleType("core.database")

    class _PriceBandStrategyConfig:
        pass

    class _MinerStrategy:
        miner_id = _DummyColumn("miner_id")
        strategy_enabled = _DummyColumn("strategy_enabled")

    class _Miner:
        id = _DummyColumn("id")

    class _Pool:
        id = _DummyColumn("id")

    class _EnergyPrice:
        pass

    class _Telemetry:
        pass

    class _PriceBandStrategyBand:
        sort_order = _DummyColumn("sort_order")

    class _HomeAssistantConfig:
        enabled = _DummyColumn("enabled")

        def __init__(self, enabled: bool, base_url: str, access_token: str):
            self.enabled = enabled
            self.base_url = base_url
            self.access_token = access_token

    class _HomeAssistantDevice:
        enrolled = _DummyColumn("enrolled")

        def __init__(self, name: str, entity_id: str, current_state: str, last_off_command_timestamp):
            self.name = name
            self.entity_id = entity_id
            self.current_state = current_state
            self.last_off_command_timestamp = last_off_command_timestamp
            self.last_state_change = None

    class _StrategyBandModeTarget:
        pass

    class _MinerHASwitchLink:
        miner_id = _DummyColumn("miner_id")
        ha_device_id = _DummyColumn("ha_device_id")

    async def _get_db():
        yield None

    db_mod.PriceBandStrategyConfig = _PriceBandStrategyConfig
    db_mod.MinerStrategy = _MinerStrategy
    db_mod.Miner = _Miner
    db_mod.Pool = _Pool
    db_mod.EnergyPrice = _EnergyPrice
    db_mod.Telemetry = _Telemetry
    db_mod.PriceBandStrategyBand = _PriceBandStrategyBand
    db_mod.HomeAssistantConfig = _HomeAssistantConfig
    db_mod.HomeAssistantDevice = _HomeAssistantDevice
    db_mod.StrategyBandModeTarget = _StrategyBandModeTarget
    db_mod.MinerHASwitchLink = _MinerHASwitchLink
    db_mod.get_db = _get_db
    sys.modules["core.database"] = db_mod

if "core.config" not in sys.modules:
    config_mod = types.ModuleType("core.config")

    class _Config:
        @staticmethod
        def get(_key, default=None):
            return default

    config_mod.app_config = _Config()
    sys.modules["core.config"] = config_mod

if "core.audit" not in sys.modules:
    audit_mod = types.ModuleType("core.audit")

    async def _log_audit(*_args, **_kwargs):
        return None

    audit_mod.log_audit = _log_audit
    sys.modules["core.audit"] = audit_mod

if "core.energy" not in sys.modules:
    energy_mod = types.ModuleType("core.energy")

    async def _get_current_energy_price(*_args, **_kwargs):
        return None

    energy_mod.get_current_energy_price = _get_current_energy_price
    sys.modules["core.energy"] = energy_mod

if "core.miner_capabilities" not in sys.modules:
    capabilities_mod = types.ModuleType("core.miner_capabilities")

    def _get_champion_lowest_mode(_miner_type: str) -> str:
        return "eco"

    capabilities_mod.get_champion_lowest_mode = _get_champion_lowest_mode
    sys.modules["core.miner_capabilities"] = capabilities_mod

if "core.price_band_bands" not in sys.modules:
    bands_mod = types.ModuleType("core.price_band_bands")

    async def _ensure_strategy_bands(*_args, **_kwargs):
        return True

    async def _get_strategy_bands(*_args, **_kwargs):
        return []

    def _get_band_for_price(*_args, **_kwargs):
        return None

    bands_mod.ensure_strategy_bands = _ensure_strategy_bands
    bands_mod.get_strategy_bands = _get_strategy_bands
    bands_mod.get_band_for_price = _get_band_for_price
    sys.modules["core.price_band_bands"] = bands_mod

if "integrations.homeassistant" not in sys.modules:
    ha_mod = types.ModuleType("integrations.homeassistant")

    class _HomeAssistantIntegration:
        def __init__(self, *_args, **_kwargs):
            self.turn_on_calls = 0
            self.turn_off_calls = 0

        async def get_device_state(self, _entity_id: str):
            return None

        async def turn_on(self, _entity_id: str):
            self.turn_on_calls += 1
            return True

        async def turn_off(self, _entity_id: str):
            self.turn_off_calls += 1
            return True

    ha_mod.HomeAssistantIntegration = _HomeAssistantIntegration
    sys.modules["integrations.homeassistant"] = ha_mod

import core.price_band_strategy as price_band_strategy_module
from core.price_band_strategy import PriceBandStrategy


@dataclass
class _Miner:
    id: int
    name: str


@dataclass
class _Band:
    id: int
    sort_order: int
    target_pool_id: int | None


@dataclass
class _Strategy:
    id: int = 1
    enabled: bool = True
    current_price_band: str = "OFF"
    current_band_sort_order: int = 1
    champion_mode_enabled: bool = False
    current_champion_miner_id: int | None = None
    state_data: dict | None = None


@dataclass
class _State:
    state: str
    last_updated: datetime


def test_control_ha_device_clears_stale_off_timestamp_when_already_on(monkeypatch):
    device = types.SimpleNamespace(
        name="Rack Switch",
        entity_id="switch.rack",
        current_state="on",
        last_off_command_timestamp=datetime.utcnow() - timedelta(minutes=30),
        last_state_change=None,
    )
    db = _FakeDB([_FakeResult(scalar=price_band_strategy_module.HomeAssistantConfig(True, "http://ha", "token"))])

    async def _get_enrolled_ha_device(_db, _miner_id):
        return device

    class _Integration:
        def __init__(self, *_args, **_kwargs):
            self.turn_on_calls = 0

        async def get_device_state(self, _entity_id):
            return _State(state="on", last_updated=datetime.utcnow())

        async def turn_on(self, _entity_id):
            self.turn_on_calls += 1
            return True

        async def turn_off(self, _entity_id):
            raise AssertionError("turn_off should not be called")

    monkeypatch.setattr(PriceBandStrategy, "_get_enrolled_ha_device", staticmethod(_get_enrolled_ha_device))
    monkeypatch.setattr(sys.modules["integrations.homeassistant"], "HomeAssistantIntegration", _Integration)
    monkeypatch.setattr(price_band_strategy_module, "select", lambda *_args, **_kwargs: _FakeQuery())

    result = asyncio.run(PriceBandStrategy.control_ha_device_for_miner(db, _Miner(id=1, name="Miner 1"), turn_on=True))

    assert result is True
    assert device.last_off_command_timestamp is None
    assert db.commits == 2


def test_off_reconciliation_persists_seeded_off_timestamp(monkeypatch):
    device = types.SimpleNamespace(
        name="Rack Switch",
        entity_id="switch.rack",
        current_state="off",
        last_off_command_timestamp=None,
        last_state_change=None,
    )
    strategy = _Strategy()
    band = _Band(id=1, sort_order=1, target_pool_id=None)
    db = _FakeDB([
        _FakeResult(scalar=strategy),
        _FakeResult(scalar=price_band_strategy_module.HomeAssistantConfig(True, "http://ha", "token")),
    ])

    async def _ensure_strategy_bands(*_args, **_kwargs):
        return True

    async def _get_strategy_bands(*_args, **_kwargs):
        return [band]

    async def _get_enrolled_miners(*_args, **_kwargs):
        return [_Miner(id=1, name="Miner 1")]

    async def _get_enrolled_ha_device(_db, _miner_id):
        return device

    class _Integration:
        def __init__(self, *_args, **_kwargs):
            pass

        async def get_device_state(self, _entity_id):
            return _State(state="off", last_updated=datetime.utcnow())

        async def turn_off(self, _entity_id):
            raise AssertionError("turn_off should not be called")

        async def turn_on(self, _entity_id):
            raise AssertionError("turn_on should not be called")

    bands_module = sys.modules["core.price_band_bands"]
    monkeypatch.setattr(bands_module, "ensure_strategy_bands", _ensure_strategy_bands)
    monkeypatch.setattr(bands_module, "get_strategy_bands", _get_strategy_bands)
    monkeypatch.setattr(PriceBandStrategy, "get_enrolled_miners", staticmethod(_get_enrolled_miners))
    async def _load_band_mode_targets(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(PriceBandStrategy, "_load_band_mode_targets", staticmethod(_load_band_mode_targets))
    monkeypatch.setattr(PriceBandStrategy, "_get_enrolled_ha_device", staticmethod(_get_enrolled_ha_device))
    monkeypatch.setattr(sys.modules["integrations.homeassistant"], "HomeAssistantIntegration", _Integration)
    monkeypatch.setattr(price_band_strategy_module, "select", lambda *_args, **_kwargs: _FakeQuery())

    result = asyncio.run(PriceBandStrategy.reconcile_strategy(db))

    assert result["reconciled"] is True
    assert result["band"] == "OFF"
    assert device.last_off_command_timestamp is not None
    assert db.commits == 1