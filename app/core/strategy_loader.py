"""
Strategy Plugin Loader

Loads switching-strategy plugins from /config/strategies/. Mirrors
app/core/pool_loader.py's discovery mechanism exactly: dynamic import of
each *_strategy.py file inside its own try/except, so one broken plugin
file can never block another plugin, or app startup, from loading
(Constitution Principle VII, Core Self-Preservation).
"""
import importlib.util
import logging
from pathlib import Path
from typing import Dict, List, Optional

from core.strategy_plugin_base import StrategyPlugin

logger = logging.getLogger(__name__)


class StrategyPluginLoader:
    """Loads strategy plugins from /config/strategies/."""

    def __init__(self, config_path: str = "/config"):
        self.config_path = Path(config_path)
        self.strategies_path = self.config_path / "strategies"

        self.plugins: Dict[str, StrategyPlugin] = {}  # strategy_id -> instance

    def load_all(self):
        """Dynamically load all strategy plugin files from /config/strategies/"""
        if not self.strategies_path.exists():
            logger.warning(f"Strategies directory not found: {self.strategies_path}")
            return

        logger.info(f"Loading strategy plugins from {self.strategies_path}")

        for file_path in self.strategies_path.glob("*_strategy.py"):
            try:
                spec = importlib.util.spec_from_file_location(file_path.stem, file_path)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    # Find StrategyPlugin subclasses
                    for attr_name in dir(module):
                        attr = getattr(module, attr_name)
                        if (
                            isinstance(attr, type)
                            and issubclass(attr, StrategyPlugin)
                            and attr is not StrategyPlugin
                        ):
                            plugin_instance = attr()
                            strategy_id = getattr(plugin_instance, "strategy_id", None)

                            if strategy_id and strategy_id != "unknown":
                                self.plugins[strategy_id] = plugin_instance
                                logger.info(f"✅ Loaded strategy plugin: {strategy_id} from {file_path.name}")
                            else:
                                logger.warning(f"⚠️  Strategy plugin in {file_path.name} missing strategy_id attribute")

            except Exception as e:
                import traceback
                logger.error(f"❌ Failed to load strategy plugin {file_path.name}: {e}")
                logger.error(f"Full traceback:\n{traceback.format_exc()}")

        logger.info(f"Loaded {len(self.plugins)} strategy plugins: {list(self.plugins.keys())}")

    def get_plugin(self, strategy_id: str) -> Optional[StrategyPlugin]:
        """Get plugin instance by strategy_id"""
        return self.plugins.get(strategy_id)

    def get_all_plugins(self) -> List[StrategyPlugin]:
        """Get all loaded strategy plugin instances"""
        return list(self.plugins.values())


# Global instance
_strategy_loader: Optional[StrategyPluginLoader] = None


def init_strategy_loader(config_path: str = "/config"):
    """Initialize the global strategy plugin loader"""
    global _strategy_loader
    _strategy_loader = StrategyPluginLoader(config_path)
    _strategy_loader.load_all()
    return _strategy_loader


def get_strategy_loader() -> StrategyPluginLoader:
    """Get the global strategy plugin loader instance"""
    global _strategy_loader
    if _strategy_loader is None:
        raise RuntimeError("Strategy loader not initialized. Call init_strategy_loader() first.")
    return _strategy_loader
