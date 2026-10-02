"""Switching-strategy plugin contract (v1).

Mirrors the existing MinerAdapter (app/adapters/base.py), BasePoolIntegration
(app/integrations/base_pool.py), and EnergyPriceProvider
(app/providers/energy/base.py) pattern: a narrow abstract interface,
implemented by drop-in plugin files discovered by a loader
(app/core/strategy_loader.py), never hard-coded into core orchestration.
See docs/STRATEGY_PLUGIN_CONTRACT.md for the full contract writeup.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class StrategyMetadata:
    """Static plugin identity/version info."""

    strategy_id: str
    display_name: str
    version: str
    description: Optional[str] = None


@dataclass
class StrategyExecutionResult:
    """Outcome of one evaluation cycle."""

    enabled: bool
    actions: List[str] = field(default_factory=list)
    details: Dict[str, object] = field(default_factory=dict)
    error: Optional[str] = None


class StrategyPlugin(ABC):
    """Base interface every switching-strategy plugin implements."""

    strategy_id: str = "unknown"

    @abstractmethod
    def get_metadata(self) -> StrategyMetadata:
        """Return static plugin identity/version info."""

    @abstractmethod
    async def execute(self, db: AsyncSession) -> StrategyExecutionResult:
        """Run exactly one evaluation cycle.

        MUST NOT raise for expected/recoverable conditions (disabled,
        unconfigured, unreachable sensor, unreachable miner) - report them
        via StrategyExecutionResult.error/details instead, per Constitution
        Principle II (Local-First, Graceful Degradation). The loader/
        scheduler around this call also catches and logs genuinely
        unexpected exceptions as a second layer (Principle VII) - that is
        not a substitute for handling known failure modes gracefully here.
        """
