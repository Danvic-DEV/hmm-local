# Contract: StrategyPlugin (v1)

New plugin contract, mirroring the existing `MinerAdapter`
(`app/adapters/base.py`), `BasePoolIntegration`
(`app/integrations/base_pool.py`), and `EnergyPriceProvider`
(`app/providers/energy/base.py`) pattern. Lives at
`app/core/strategy_plugin_base.py`.

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class StrategyMetadata:
    strategy_id: str        # unique, stable identifier, e.g. "solar"
    display_name: str       # e.g. "Solar Strategy"
    version: str
    description: Optional[str] = None


@dataclass
class StrategyExecutionResult:
    enabled: bool                          # was the strategy actually active this cycle
    actions: List[str] = field(default_factory=list)   # human-readable action log for this cycle
    details: Dict[str, object] = field(default_factory=dict)  # strategy-specific reporting (e.g. surplus_watts)
    error: Optional[str] = None            # set instead of raising - see Core Self-Preservation note below


class StrategyPlugin(ABC):
    """Base interface every switching-strategy plugin implements."""

    strategy_id: str = "unknown"

    @abstractmethod
    def get_metadata(self) -> StrategyMetadata:
        """Static plugin identity/version info."""

    @abstractmethod
    async def execute(self, db: AsyncSession) -> StrategyExecutionResult:
        """
        Run exactly one evaluation cycle.

        MUST NOT raise for expected/recoverable conditions (disabled,
        unconfigured, unreachable sensor, unreachable miner) - report
        them via StrategyExecutionResult.error/details instead, per
        Constitution II (Local-First, Graceful Degradation).

        For a genuinely unexpected exception, the loader/scheduler around
        this call also catches and logs it (Constitution VII) - this is a
        second layer, not a substitute for a plugin handling its own
        known failure modes gracefully.
        """
```

## Loader contract

`app/core/strategy_loader.py`, mirroring `pool_loader.py` exactly:

- Scans `/config/strategies/*_strategy.py`.
- For each file: `importlib.util.spec_from_file_location` + `exec_module`
  inside its own `try/except Exception`, logged and skipped on failure -
  one broken strategy file MUST NOT prevent others (or the rest of
  startup) from loading (Constitution VII).
- Finds `StrategyPlugin` subclasses in the module, instantiates them,
  registers by `strategy_id`.
- Exposes `get_strategy_loader().get_all_plugins() -> List[StrategyPlugin]`.

## Scheduler contract

One new job in `app/core/scheduler.py`, registered alongside the existing
"Execute Price Band Strategy every minute" job:

```python
self.scheduler.add_job(
    self._run_all_strategy_plugins,
    IntervalTrigger(minutes=1),
    id="execute_strategy_plugins",
    name="Execute strategy plugins every minute",
)
```

`_run_all_strategy_plugins` iterates `get_strategy_loader().get_all_plugins()`
and `await`s each plugin's `execute(db)` **inside the same per-plugin
try/except isolation the loader itself uses** - a second, redundant
layer on top of APScheduler's own `EVENT_JOB_ERROR` handling, so a bug in
one strategy plugin can't stop the next plugin in the same cycle, let
alone the scheduler itself.

## Bundled implementation

`bundled_config/strategies/solar_strategy.py` - the actual Solar
Strategy, implementing `StrategyPlugin`. Internally it is free to reuse
existing building blocks directly (no new contract needed for these,
they're already stable, reusable code, not external plugin surfaces):

- `MinerModePowerStats` for per-mode wattage sizing.
- The existing efficiency-leaderboard query for most-efficient-first
  ranking under a constrained surplus budget.
- `get_adapter(miner)` / `adapter.set_mode()` / HA device on-off control -
  the same adapter and Home Assistant integration calls Price Band
  Strategy already uses, just invoked from the new plugin instead of from
  core orchestration.
- `log_audit(...)` for every state-changing action (Constitution IV).

## API endpoints (new file `app/api/solar_strategy.py`)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/settings/solar-strategy` | Current config + enrollment list (mirrors `GET /price-band-strategy`'s response shape) |
| `POST` | `/api/settings/solar-strategy` | Save config (`enabled`, `solar_surplus_entity_id`, `pool_id`) + full enrolled-miner list - same whole-list-replace semantics as Price Band Strategy's existing endpoint, for consistency |
| `POST` | `/api/settings/solar-strategy/execute` | Manual trigger, mirrors `POST /price-band-strategy/execute` |

The enrollment-exclusivity check (data-model.md) lives in a small shared
helper both this endpoint and Price Band Strategy's existing
`save_price_band_strategy_settings` call - the one deliberate, narrow
touchpoint on existing Price Band Strategy code.
