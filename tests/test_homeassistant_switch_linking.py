from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import HTTPException


APP_ROOT = Path(__file__).resolve().parents[1] / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))


class _DummyColumn:
    def __init__(self, name: str):
        self.name = name

    def __eq__(self, other):
        return ("eq", self.name, other)

    def in_(self, values):
        return ("in", self.name, tuple(values))


if "core.database" not in sys.modules:
    db_mod = types.ModuleType("core.database")

    class _HomeAssistantConfig:
        pass

    class _HomeAssistantDevice:
        id = _DummyColumn("id")
        entity_id = _DummyColumn("entity_id")
        enrolled = _DummyColumn("enrolled")

    class _MinerHASwitchLink:
        id = _DummyColumn("id")
        miner_id = _DummyColumn("miner_id")
        ha_device_id = _DummyColumn("ha_device_id")

        def __init__(self, miner_id: int, ha_device_id: int):
            self.miner_id = miner_id
            self.ha_device_id = ha_device_id

    class _Miner:
        id = _DummyColumn("id")

    async def _get_db():
        yield None

    db_mod.get_db = _get_db
    db_mod.HomeAssistantConfig = _HomeAssistantConfig
    db_mod.HomeAssistantDevice = _HomeAssistantDevice
    db_mod.MinerHASwitchLink = _MinerHASwitchLink
    db_mod.Miner = _Miner
    sys.modules["core.database"] = db_mod

if "integrations.homeassistant" not in sys.modules:
    ha_mod = types.ModuleType("integrations.homeassistant")

    class _HomeAssistantIntegration:
        def __init__(self, *_args, **_kwargs):
            pass

    ha_mod.HomeAssistantIntegration = _HomeAssistantIntegration
    sys.modules["integrations.homeassistant"] = ha_mod

import api.integrations as integrations


class _FakeQuery:
    def where(self, *_args, **_kwargs):
        return self

    def join(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def distinct(self, *_args, **_kwargs):
        return self


class _FakeDelete:
    def where(self, *_args, **_kwargs):
        return self


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeResult:
    def __init__(self, *, scalar=None, scalars=None, all_rows=None):
        self._scalar = scalar
        self._scalars = scalars or []
        self._all_rows = all_rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return _FakeScalars(self._scalars)

    def all(self):
        return self._all_rows


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.commits = 0

    async def execute(self, _query):
        if not self._results:
            raise AssertionError("No queued result for execute()")
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


@dataclass
class _Device:
    id: int
    entity_id: str
    name: str
    domain: str
    enrolled: bool = True
    never_auto_control: bool = False
    current_state: str | None = None
    capabilities: dict | None = None


@dataclass
class _MinerRow:
    id: int
    name: str


@dataclass
class _LinkRow:
    ha_device_id: int
    miner_id: int


@pytest.fixture(autouse=True)
def _patch_sqlalchemy_calls(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(integrations, "select", lambda *_args, **_kwargs: _FakeQuery())
    monkeypatch.setattr(integrations, "delete", lambda *_args, **_kwargs: _FakeDelete())


def test_get_ha_devices_returns_linked_miner_ids():
    devices = [
        _Device(id=1, entity_id="switch.rack_a", name="Rack A", domain="switch"),
        _Device(id=2, entity_id="switch.rack_b", name="Rack B", domain="switch"),
    ]
    links = [
        _LinkRow(ha_device_id=1, miner_id=101),
        _LinkRow(ha_device_id=1, miner_id=102),
    ]
    db = _FakeDB(
        [
            _FakeResult(scalars=devices),
            _FakeResult(scalars=links),
            _FakeResult(scalars=[_MinerRow(101, "Miner 101"), _MinerRow(102, "Miner 102")]),
        ]
    )

    import asyncio

    response = asyncio.run(integrations.get_ha_devices(enrolled_only=False, db=db))

    assert response["devices"][0]["linked_miner_ids"] == [101, 102]
    assert response["devices"][1]["linked_miner_ids"] == []
    assert db.commits == 0


def test_get_ha_devices_filters_and_cleans_orphan_links():
    devices = [
        _Device(id=1, entity_id="switch.rack_a", name="Rack A", domain="switch"),
    ]
    links = [
        _LinkRow(ha_device_id=1, miner_id=101),
        _LinkRow(ha_device_id=1, miner_id=999),
    ]
    db = _FakeDB(
        [
            _FakeResult(scalars=devices),
            _FakeResult(scalars=links),
            _FakeResult(scalars=[_MinerRow(101, "Miner 101")]),
            _FakeResult(),
        ]
    )

    import asyncio

    response = asyncio.run(integrations.get_ha_devices(enrolled_only=False, db=db))

    assert response["devices"][0]["linked_miner_ids"] == [101]
    assert db.commits == 1


def test_link_endpoint_replaces_links_and_deduplicates_miner_ids():
    device = _Device(id=7, entity_id="switch.shared_power", name="Shared", domain="switch")
    db = _FakeDB(
        [
            _FakeResult(scalar=device),
            _FakeResult(scalars=[]),
            _FakeResult(scalars=[_MinerRow(5, "A"), _MinerRow(6, "B")]),
            _FakeResult(all_rows=[]),
            _FakeResult(),
        ]
    )

    import asyncio

    request = integrations.DeviceLinkRequest(miner_ids=[5, 5, 6])
    response = asyncio.run(integrations.link_ha_device_to_miner(device_id=7, request=request, db=db))

    assert response["success"] is True
    assert response["message"] == "Device linked to 2 miner(s)"
    assert [(row.miner_id, row.ha_device_id) for row in db.added] == [(5, 7), (6, 7)]
    assert db.commits == 1


def test_link_endpoint_rejects_conflict_when_miner_already_linked_elsewhere():
    device = _Device(id=7, entity_id="switch.shared_power", name="Shared", domain="switch")
    conflict_device = _Device(id=9, entity_id="switch.other", name="Other", domain="switch")
    existing_link = _LinkRow(ha_device_id=9, miner_id=6)
    db = _FakeDB(
        [
            _FakeResult(scalar=device),
            _FakeResult(scalars=[]),
            _FakeResult(scalars=[_MinerRow(6, "B")]),
            _FakeResult(all_rows=[(existing_link, conflict_device)]),
        ]
    )

    import asyncio

    request = integrations.DeviceLinkRequest(miner_ids=[6])

    with pytest.raises(HTTPException) as exc:
        asyncio.run(integrations.link_ha_device_to_miner(device_id=7, request=request, db=db))

    assert exc.value.status_code == 409
    assert "miner_id=6 already linked to switch.other" in exc.value.detail
    assert db.commits == 0


def test_link_endpoint_rejects_missing_miner_ids():
    device = _Device(id=7, entity_id="switch.shared_power", name="Shared", domain="switch")
    db = _FakeDB(
        [
            _FakeResult(scalar=device),
            _FakeResult(scalars=[]),
            _FakeResult(scalars=[_MinerRow(5, "A")]),
        ]
    )

    import asyncio

    request = integrations.DeviceLinkRequest(miner_ids=[5, 999])

    with pytest.raises(HTTPException) as exc:
        asyncio.run(integrations.link_ha_device_to_miner(device_id=7, request=request, db=db))

    assert exc.value.status_code == 404
    assert "999" in exc.value.detail
    assert db.commits == 0


def test_link_endpoint_ignores_stale_miner_ids_already_linked_to_device():
    device = _Device(id=7, entity_id="switch.shared_power", name="Shared", domain="switch")
    db = _FakeDB(
        [
            _FakeResult(scalar=device),
            _FakeResult(scalars=[_LinkRow(ha_device_id=7, miner_id=999)]),
            _FakeResult(scalars=[_MinerRow(5, "A")]),
            _FakeResult(all_rows=[]),
            _FakeResult(),
        ]
    )

    import asyncio

    request = integrations.DeviceLinkRequest(miner_ids=[5, 999])
    response = asyncio.run(integrations.link_ha_device_to_miner(device_id=7, request=request, db=db))

    assert response["success"] is True
    assert response["message"] == "Device linked to 1 miner(s)"
    assert [(row.miner_id, row.ha_device_id) for row in db.added] == [(5, 7)]
    assert db.commits == 1
