from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure app/ is importable when tests run from repo root
APP_ROOT = Path(__file__).resolve().parents[1] / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from core.strategy_loader import StrategyPluginLoader  # noqa: E402


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
