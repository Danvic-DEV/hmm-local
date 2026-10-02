# Strategy Plugin Contract (v1)

Date: 2026-10-02

## Goal

Establish switching-strategy plugins as a real plugin category, mirroring
the miner/pool/energy-provider model. Solar Strategy is the first and
reference implementation; Price Band Strategy predates this contract and
still lives in core orchestration (`app/core/price_band_strategy.py`) -
not retrofitted as part of this change.

This separates:

- core scheduling/orchestration (generic - no knowledge of "solar" or any
  specific strategy)
- strategy-specific decision logic
- a normalized execution-result contract

## Plugin Location

- Bundled strategies: `bundled_config/strategies/`
- Deployed strategies: `/config/strategies/`
- File naming convention: `*_strategy.py`

## Core Contract

Defined in [app/core/strategy_plugin_base.py](../app/core/strategy_plugin_base.py):

- `StrategyPlugin`
- `StrategyMetadata`
- `StrategyExecutionResult`

Required methods:

- `get_metadata()`
- `execute(db)` - one evaluation cycle. MUST NOT raise for expected/
  recoverable conditions (disabled, unconfigured, unreachable sensor,
  unreachable miner) - report via `StrategyExecutionResult.error`/
  `details` instead (Constitution Principle II). A genuinely unexpected
  exception is still caught by the scheduler job as a second layer
  (Principle VII) - that is not a substitute for handling known failure
  modes gracefully inside `execute()`.

## Loader

Defined in [app/core/strategy_loader.py](../app/core/strategy_loader.py):

- `StrategyPluginLoader`
- `init_strategy_loader()`
- `get_strategy_loader()`

Each `*_strategy.py` file is imported inside its own `try/except Exception`
- one broken plugin file cannot block another plugin, or app startup, from
loading (Constitution Principle VII).

## Scheduler Integration

`app/core/scheduler.py` registers one job, generic over all loaded plugins:

- Job id `execute_strategy_plugins`, every 1 minute.
- `SchedulerService._execute_strategy_plugins()` iterates
  `get_strategy_loader().get_all_plugins()` and calls each plugin's
  `execute(db)` inside its own `try/except` - a bug in one plugin cannot
  stop another plugin in the same cycle, on top of APScheduler's own
  `EVENT_JOB_ERROR` isolation for the job as a whole.

## Reference Plugin: Solar Strategy

`solar_strategy.py` in [bundled_config/strategies](../bundled_config/strategies)

Strategy ID: `solar`

Bin-packs enrolled miners against live solar surplus (an EMA-smoothed
Home Assistant sensor reading), sized to each miner's own observed
per-mode power draw (`MinerModePowerStats`) - most efficient miner first.
On/off power transitions require 5 consecutive confirming 1-minute
cycles; tuning-mode changes on an already-on miner apply immediately.
Mutually exclusive with Price Band Strategy enrollment per miner
(`app/core/strategy_enrollment.py`).

## Runtime Deployment

- [entrypoint.sh](../entrypoint.sh) deploys bundled strategy files to
  `/config/strategies/` when empty, same pattern as drivers/providers.

## Runtime Integration (Implemented)

- Startup initialization in [app/main.py](../app/main.py):
  `init_strategy_loader("/config")`.
- Scheduler job registration in
  [app/core/scheduler.py](../app/core/scheduler.py) (`_register_strategy_jobs`).

## Management API (Implemented)

Endpoints in [app/api/solar_strategy.py](../app/api/solar_strategy.py):

- `GET /api/settings/solar-strategy`
- `POST /api/settings/solar-strategy`
- `POST /api/settings/solar-strategy/execute`

Mirrors Price Band Strategy's existing endpoint shape for operator
familiarity. State-changing actions are logged via the existing
`AuditLogger`/`log_audit(...)` pattern (`app/core/audit.py`), same as
Price Band Strategy - no separate logging mechanism was introduced.

## Schema

Two new tables only - `solar_strategy_config`, `solar_miner_enrollment`
(`app/core/database.py`). No existing table gained a column. Adopting
this feature on an existing install needs zero manual SQL:
`Base.metadata.create_all()` (what `init_db()` already calls on every
startup) creates missing tables automatically.

## Design History

See [specs/001-solar-strategy-plugin/](../specs/001-solar-strategy-plugin/)
for the full spec/plan/research/tasks produced via the project's
spec-driven workflow, including the first (reverted) attempt at this
feature and why it was abandoned - it hard-wired solar logic into
`price_band_strategy.py` directly and required manual database migration
on the operator's single production instance. Both mistakes are what
this contract and the new-tables-only schema approach exist to prevent
from happening again, for this or any future strategy plugin.

## Next Integration Slice

1. Consider retrofitting Price Band Strategy itself onto this contract,
   so "electricity-cost-driven switching" (Constitution Principle I's own
   example) is actually a plugin, not just Solar Strategy.
2. Surplus/debounce state is in-memory only (resets on restart) - fine
   today, but worth revisiting if a future strategy plugin needs stronger
   restart-survival guarantees.
