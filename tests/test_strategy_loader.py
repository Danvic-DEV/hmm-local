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

from core.strategy_loader import StrategyPluginLoader  # noqa: E402
from core.strategy_plugin_base import StrategyExecutionResult, StrategyMetadata, StrategyPlugin  # noqa: E402


GOOD_PLUGIN = '''
from core.strategy_plugin_base import StrategyPlugin, StrategyMetadata, StrategyExecutionResult


class GoodStrategy(StrategyPlugin):
    strategy_id = "good"

    def get_metadata(self):
        return StrategyMetadata(strategy_id="good", display_name="Good", version="1.0")

    async def execute(self, db):
        return StrategyExecutionResult(enabled=True)
'''

BROKEN_PLUGIN = '''
raise RuntimeError("deliberately broken plugin file")
'''


def test_broken_strategy_file_does_not_block_others(tmp_path):
    """A strategy file that fails to import must not prevent other strategy
    files - or the loader itself - from loading successfully (Constitution
    Principle VII: Core Self-Preservation)."""
    config_dir = tmp_path / "config"
    strategies_dir = config_dir / "strategies"
    strategies_dir.mkdir(parents=True)

    (strategies_dir / "good_strategy.py").write_text(GOOD_PLUGIN)
    (strategies_dir / "broken_strategy.py").write_text(BROKEN_PLUGIN)

    loader = StrategyPluginLoader(config_path=str(config_dir))
    loader.load_all()  # must not raise

    assert set(loader.plugins.keys()) == {"good"}
    assert loader.get_plugin("good") is not None
    assert loader.get_plugin("broken") is None


def test_missing_strategies_directory_does_not_raise(tmp_path):
    """No /config/strategies directory at all (fresh install before the
    bundled plugin is deployed) must degrade gracefully, not error."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    loader = StrategyPluginLoader(config_path=str(config_dir))
    loader.load_all()  # must not raise

    assert loader.get_all_plugins() == []


def test_empty_strategies_directory_returns_no_plugins(tmp_path):
    config_dir = tmp_path / "config"
    (config_dir / "strategies").mkdir(parents=True)

    loader = StrategyPluginLoader(config_path=str(config_dir))
    loader.load_all()

    assert loader.get_all_plugins() == []


# ---------------------------------------------------------------------------
# T026 - a strategy plugin raising unexpectedly must not propagate out of the
# scheduler job (Constitution Principle VII, Core Self-Preservation). This
# needs core.scheduler, which has real dependencies (notably the POSIX-only
# `resource` module) - stub what's needed to import it in isolation, mirroring
# the existing pattern in tests/test_scheduler_guardrails.py.
# ---------------------------------------------------------------------------

def _stub_scheduler_dependencies():
    """Only stub what's genuinely platform-incompatible (the POSIX-only
    `resource` module) - everything else (core.config, core.database, ...)
    imports for real. Deliberately does NOT replace core.database with a
    fake: doing so previously leaked into sys.modules and broke every other
    test file in the same pytest session that needed the real model classes,
    since module stubs are process-global, not per-test-file."""
    if "resource" not in sys.modules:
        resource_mod = types.ModuleType("resource")
        resource_mod.getrusage = lambda _who: types.SimpleNamespace(ru_maxrss=0)
        resource_mod.RUSAGE_SELF = 0
        sys.modules["resource"] = resource_mod


_stub_scheduler_dependencies()

import core.scheduler as scheduler_module  # noqa: E402


class _RaisingPlugin(StrategyPlugin):
    strategy_id = "raising"

    def get_metadata(self):
        return StrategyMetadata(strategy_id="raising", display_name="Raising", version="1.0")

    async def execute(self, db):
        raise RuntimeError("deliberately broken plugin")


class _GoodPlugin(StrategyPlugin):
    strategy_id = "good"

    def __init__(self):
        self.ran = False

    def get_metadata(self):
        return StrategyMetadata(strategy_id="good", display_name="Good", version="1.0")

    async def execute(self, db):
        self.ran = True
        return StrategyExecutionResult(enabled=True)


def test_raising_plugin_does_not_stop_other_plugins_or_propagate(monkeypatch):
    import core.strategy_loader as strategy_loader_module
    import core.database as database_module

    loader = StrategyPluginLoader(config_path="/nonexistent")
    good = _GoodPlugin()
    loader.plugins = {"raising": _RaisingPlugin(), "good": good}

    # _execute_strategy_plugins does deferred `from core.X import Y` inside
    # the method, so the real modules' attributes must be patched, not
    # scheduler_module's.
    monkeypatch.setattr(strategy_loader_module, "get_strategy_loader", lambda: loader)

    class _FakeAsyncSessionCtx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(database_module, "AsyncSessionLocal", lambda: _FakeAsyncSessionCtx())

    service = scheduler_module.SchedulerService.__new__(scheduler_module.SchedulerService)

    # Must not raise, even though one plugin always does.
    asyncio.run(service._execute_strategy_plugins())

    assert good.ran is True
