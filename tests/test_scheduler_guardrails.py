from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest


# Ensure app/ is importable when tests run from repo root
APP_ROOT = Path(__file__).resolve().parents[1] / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))


# Lightweight stubs so importing `core.scheduler` does not require full app bootstrap.
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
        pass

    class _Miner:
        pass

    class _AuditLog:
        pass

    db_mod.EnergyPrice = _EnergyPrice
    db_mod.Telemetry = _Telemetry
    db_mod.Miner = _Miner
    db_mod.AuditLog = _AuditLog
    sys.modules["core.database"] = db_mod

import core.scheduler as scheduler_module
from core.scheduler import SchedulerService


class _Job:
    def __init__(self, job_id: str):
        self.id = job_id


class _FakeScheduler:
    def __init__(self, running: bool = False, job_ids: list[str] | None = None):
        self.running = running
        self._jobs = [_Job(job_id) for job_id in (job_ids or [])]
        self.shutdown_called = False
        self.raise_on_shutdown = False
        self.start_called = False
        self.remove_all_jobs_called = False
        self.add_job_calls: list[dict] = []
        self.add_listener_calls: list[dict] = []
        self.raise_on_remove = False

    def get_jobs(self):
        return self._jobs

    def shutdown(self):
        if self.raise_on_shutdown:
            raise RuntimeError("shutdown failed")
        self.shutdown_called = True

    def start(self):
        self.start_called = True
        self.running = True

    def remove_all_jobs(self):
        self.remove_all_jobs_called = True
        self._jobs = []

    def remove_job(self, job_id: str):
        if self.raise_on_remove:
            raise KeyError(job_id)
        self._jobs = [job for job in self._jobs if job.id != job_id]

    def add_job(self, func, trigger=None, **kwargs):
        job_id = kwargs.get("id")
        if job_id:
            self._jobs.append(_Job(job_id))
        self.add_job_calls.append({"func": func, "trigger": trigger, **kwargs})

    def add_listener(self, callback, mask):
        self.add_listener_calls.append({"callback": callback, "mask": mask})


def _required_job_ids() -> list[str]:
    return [
        "update_energy_prices",
        "collect_telemetry",
        "telemetry_freshness_watchdog",
        "evaluate_automation_rules",
        "reconcile_automation_rules",
        "execute_price_band_strategy",
        "reconcile_price_band_strategy",
        "monitor_database_health",
        "monitor_pool_health",
        "monitor_ha_keepalive",
    ]


def test_validate_registered_jobs_passes_when_required_jobs_exist():
    service = SchedulerService()
    service.scheduler = _FakeScheduler(job_ids=_required_job_ids())

    # Should not raise
    service._validate_registered_jobs()


def test_validate_registered_jobs_raises_with_missing_ids():
    missing = "monitor_pool_health"
    present = [job_id for job_id in _required_job_ids() if job_id != missing]

    service = SchedulerService()
    service.scheduler = _FakeScheduler(job_ids=present)

    with pytest.raises(RuntimeError) as exc:
        service._validate_registered_jobs()

    assert missing in str(exc.value)


def test_validate_registered_jobs_error_lists_missing_ids_sorted():
    service = SchedulerService()
    # Missing both monitor_* jobs on purpose; order should be deterministic.
    present = [
        job_id
        for job_id in _required_job_ids()
        if job_id not in {"monitor_database_health", "monitor_pool_health"}
    ]
    service.scheduler = _FakeScheduler(job_ids=present)

    with pytest.raises(RuntimeError) as exc:
        service._validate_registered_jobs()

    assert str(exc.value) == (
        "Scheduler missing critical jobs: monitor_database_health, monitor_pool_health"
    )


def test_start_is_idempotent_when_already_running(monkeypatch: pytest.MonkeyPatch):
    service = SchedulerService()
    fake = _FakeScheduler(running=True, job_ids=_required_job_ids())
    service.scheduler = fake

    # If `start()` tries to register, fail fast.
    monkeypatch.setattr(service, "_register_core_jobs", lambda: (_ for _ in ()).throw(AssertionError("should not register")))

    service.start()

    assert fake.start_called is False
    assert fake.remove_all_jobs_called is False


def test_shutdown_is_idempotent_when_already_stopped():
    service = SchedulerService()
    fake = _FakeScheduler(running=False)
    service.scheduler = fake

    service.shutdown()

    assert fake.shutdown_called is False


def test_shutdown_stops_listener_even_if_scheduler_already_stopped():
    service = SchedulerService()
    fake = _FakeScheduler(running=False)
    service.scheduler = fake

    class _Listener:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    listener = _Listener()
    service.nmminer_listener = listener

    service.shutdown()

    assert listener.stopped is True
    assert fake.shutdown_called is False


def test_start_clears_stale_jobs_before_registration(monkeypatch: pytest.MonkeyPatch):
    service = SchedulerService()
    fake = _FakeScheduler(running=False, job_ids=["stale_job"])
    service.scheduler = fake

    monkeypatch.setattr(service, "_register_core_jobs", lambda: None)
    monkeypatch.setattr(service, "_register_maintenance_jobs", lambda: None)
    monkeypatch.setattr(service, "_register_pool_and_ha_jobs", lambda: None)
    monkeypatch.setattr(service, "_register_cloud_jobs", lambda: None)
    monkeypatch.setattr(service, "_register_metrics_jobs", lambda: None)
    monkeypatch.setattr(service, "_register_anomaly_jobs", lambda: None)
    monkeypatch.setattr(service, "_register_strategy_jobs", lambda: None)
    monkeypatch.setattr(service, "_register_startup_jobs", lambda: None)
    monkeypatch.setattr(service, "_update_discovery_schedule", lambda: None)
    monkeypatch.setattr(service, "_validate_registered_jobs", lambda: None)

    service.start()

    assert fake.remove_all_jobs_called is True
    assert fake.start_called is True


def test_start_raises_when_required_jobs_missing(monkeypatch: pytest.MonkeyPatch):
    service = SchedulerService()
    fake = _FakeScheduler(running=False)
    service.scheduler = fake

    monkeypatch.setattr(service, "_register_core_jobs", lambda: None)
    monkeypatch.setattr(service, "_register_maintenance_jobs", lambda: None)
    monkeypatch.setattr(service, "_register_pool_and_ha_jobs", lambda: None)
    monkeypatch.setattr(service, "_register_cloud_jobs", lambda: None)
    monkeypatch.setattr(service, "_register_metrics_jobs", lambda: None)
    monkeypatch.setattr(service, "_register_anomaly_jobs", lambda: None)
    monkeypatch.setattr(service, "_register_strategy_jobs", lambda: None)
    monkeypatch.setattr(service, "_register_startup_jobs", lambda: None)
    monkeypatch.setattr(service, "_update_discovery_schedule", lambda: None)

    with pytest.raises(RuntimeError):
        service.start()

    assert fake.start_called is False


def test_stop_nmminer_listener_calls_sync_stop():
    service = SchedulerService()

    class _Listener:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    listener = _Listener()
    service.nmminer_listener = listener

    service._stop_nmminer_listener()

    assert listener.stopped is True
    assert service.nmminer_listener is None


def test_stop_nmminer_listener_calls_async_stop_without_running_loop():
    service = SchedulerService()

    class _Listener:
        def __init__(self):
            self.stopped = False

        async def stop(self):
            self.stopped = True

    listener = _Listener()
    service.nmminer_listener = listener

    service._stop_nmminer_listener()

    assert listener.stopped is True
    assert service.nmminer_listener is None


def test_stop_nmminer_listener_schedules_async_stop_with_running_loop(monkeypatch: pytest.MonkeyPatch):
    service = SchedulerService()
    state = {"completed": False, "task_called": False}

    class _Listener:
        async def stop(self):
            state["completed"] = True

    class _FakeLoop:
        def create_task(self, coro):
            state["task_called"] = True
            asyncio.run(coro)

    service.nmminer_listener = _Listener()

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _FakeLoop())

    service._stop_nmminer_listener()

    assert state["task_called"] is True
    assert state["completed"] is True
    assert service.nmminer_listener is None


def test_shutdown_continues_if_listener_stop_raises():
    service = SchedulerService()
    fake = _FakeScheduler(running=True)
    service.scheduler = fake

    class _Listener:
        def stop(self):
            raise RuntimeError("boom")

    service.nmminer_listener = _Listener()

    service.shutdown()

    assert fake.shutdown_called is True
    assert service.nmminer_listener is None


def test_shutdown_handles_scheduler_shutdown_exception():
    service = SchedulerService()
    fake = _FakeScheduler(running=True)
    fake.raise_on_shutdown = True
    service.scheduler = fake

    # Should not raise
    service.shutdown()


def test_update_discovery_schedule_readds_job():
    service = SchedulerService()
    fake = _FakeScheduler(running=False, job_ids=["auto_discover_miners"])
    service.scheduler = fake

    service._update_discovery_schedule()

    assert any(call.get("id") == "auto_discover_miners" for call in fake.add_job_calls)


def test_update_discovery_schedule_handles_missing_existing_job():
    service = SchedulerService()
    fake = _FakeScheduler(running=False)
    fake.raise_on_remove = True
    service.scheduler = fake

    service._update_discovery_schedule()

    assert any(call.get("id") == "auto_discover_miners" for call in fake.add_job_calls)


def test_update_discovery_schedule_clamps_interval_to_minimum(monkeypatch: pytest.MonkeyPatch):
    service = SchedulerService()
    fake = _FakeScheduler(running=False)
    service.scheduler = fake

    original_get = scheduler_module.app_config.get

    def _fake_get(key, default=None):
        if key == "network_discovery":
            return {"scan_interval_hours": 0}
        return original_get(key, default)

    monkeypatch.setattr(scheduler_module.app_config, "get", _fake_get)

    service._update_discovery_schedule()

    assert any(call.get("id") == "auto_discover_miners" for call in fake.add_job_calls)
    interval_trigger = fake.add_job_calls[-1]["trigger"]
    assert getattr(interval_trigger, "interval").total_seconds() == 3600


def test_config_coercion_helpers_fallback_safely():
    assert scheduler_module._as_int("bad", 5) == 5
    assert scheduler_module._as_int(None, 7) == 7
    assert scheduler_module._as_float("bad", 1.5) == 1.5
    assert scheduler_module._as_float(None, 2.5) == 2.5
    assert scheduler_module._as_str("", "x") == "x"
    assert scheduler_module._as_dict(None) == {}


def test_as_dict_preserves_input_dict_instance():
    value = {"a": 1}
    assert scheduler_module._as_dict(value) is value
