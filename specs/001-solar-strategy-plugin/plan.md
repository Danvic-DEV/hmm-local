# Implementation Plan: Solar Strategy Plugin

**Branch**: `001-solar-strategy-plugin` | **Date**: 2026-10-01 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-solar-strategy-plugin/spec.md`

**Note**: This plan was produced by manually following the `/speckit-plan` workflow
(`.claude/skills/speckit-plan/SKILL.md`) - the speckit skills aren't registered as
invokable in this session (project-scoped skills only load from the session's
primary working directory, which is `home-lab`, not `hmm-local`), so the
documented process was followed by hand against the real `.specify/` templates
and scripts.

## Summary

Add Solar Strategy as a genuinely new plugin category - a `StrategyPlugin`
contract plus a loader, mirroring the existing `MinerAdapter`/`miner_loader.py`,
`BasePoolIntegration`/`pool_loader.py`, and `EnergyPriceProvider`/
`providers/energy/loader.py` patterns - rather than hard-coding solar logic
into `app/core/price_band_strategy.py` (the mistake made and reverted earlier
today). Solar Strategy runs as its own independent APScheduler job, not a
call-out from Price Band Strategy's job, which gets failure isolation from
the scheduler's existing per-job error handling for free and makes mutual
exclusivity a structural property rather than a runtime check. All new state
lives in brand-new tables (never new columns on existing ones), so adoption
on the operator's single production instance is handled automatically by the
existing `init_db()` / `Base.metadata.create_all` fresh-table creation - no
manual SQL, closing the gap that caused the first attempt to be abandoned.

## Technical Context

**Language/Version**: Python 3.11+ (backend), TypeScript/React (UI) - matches existing stack, no new language/runtime.

**Primary Dependencies**: FastAPI, SQLAlchemy (async), APScheduler - all already in `requirements.txt`. No new dependency (Constitution VI).

**Storage**: PostgreSQL (primary) with existing SQLite fallback. New tables only, no `ALTER TABLE` on existing tables.

**Testing**: pytest, `tests/test_*.py`, run via `scripts/pre_deploy_gate.sh` (existing CI quality gate).

**Target Platform**: Linux server, Docker container - same low-power/unattended target as the rest of HMM-Local (Pi/NAS/spare box).

**Project Type**: Existing single web-service repo (FastAPI + React) - this adds a new plugin category within it, not a new app/service.

**Performance Goals**: Evaluate enrolled miners once per minute - same cadence as the existing Price Band Strategy job, proven adequate for this domain.

**Constraints**: Must run reliably on low-power unattended hardware; must not add runtime dependencies; schema changes must be purely additive (new tables only); a Solar Strategy failure must never affect Price Band Strategy, telemetry, or the core process (Constitution VII).

**Scale/Scope**: A single operator's home mining fleet - same order of magnitude as the existing Price Band Strategy already manages (not a multi-tenant or internet-scale concern).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design (see below).*

| Principle | Status | How this plan satisfies it |
|---|---|---|
| I. Plugin-First Architecture (NON-NEGOTIABLE) | PASS | New `StrategyPlugin` ABC (`app/core/strategy_plugin_base.py` or similar) + a loader (`app/core/strategy_loader.py`) that discovers `*_strategy.py` files the same way `pool_loader.py` discovers `*_driver.py` files. Solar Strategy ships as a bundled plugin file under `bundled_config/strategies/`, deployed to `/config/strategies/` by `entrypoint.sh` on first run, identical to how miner/pool drivers and energy providers are bundled today. Core orchestration (the scheduler) only knows "run every loaded, enabled strategy plugin" - it contains no solar-specific logic. |
| II. Local-First, Graceful Degradation | PASS | Unreachable/stale HA sensor → treated as no-surplus (FR-014), not an error. Unreachable miner → skipped for that cycle, doesn't block the rest (matches existing fleet-reachability handling elsewhere). |
| III. Durable State Across Restarts | PASS | All new state (config, enrollment, decision log) lives in brand-new tables, never new columns on `MinerStrategy`/`PriceBandStrategyConfig`. `Base.metadata.create_all` already creates missing tables automatically on startup - no manual migration step, satisfying FR-013/SC-006. |
| IV. Observable Decision-Making | PASS | Every solar-driven action routed through the existing `AuditLogger`/`log_audit` pattern (`app/core/audit.py`), same as Price Band Strategy's band-transition/champion logging today. |
| V. Security-Sensitive Credential Handling | PASS | Solar Strategy's "dedicated pool" is a reference (pool ID) to an existing `Pool` row, created/edited through the existing pool-management UI/credential-handling path - no new credential storage or handling is introduced by this feature. |
| VI. Conservative Dependencies, Built to Run Unattended | PASS | Zero new dependencies. Reuses `MinerModePowerStats`, the existing efficiency-leaderboard concept, `get_adapter`, and the existing scheduler. |
| VII. Core Self-Preservation | PASS | Solar Strategy executes as its **own** APScheduler job (not inline inside Price Band Strategy's job), so the scheduler's existing `EVENT_JOB_ERROR` listener / per-job isolation (already relied on for every other scheduled job) contains a Solar Strategy failure automatically - no bespoke try/except scaffolding needed to get this guarantee, and the plugin loader isolates each strategy file's import the same way `pool_loader.py` does. |

No violations requiring justification - Complexity Tracking table omitted.

## Project Structure

### Documentation (this feature)

```text
specs/001-solar-strategy-plugin/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   └── strategy-plugin-contract.md
└── tasks.md              # Phase 2 output (not created by this command)
```

### Source Code (repository root)

```text
app/
├── core/
│   ├── strategy_plugin_base.py   # NEW - StrategyPlugin ABC, StrategyMetadata, StrategyExecutionResult
│   ├── strategy_loader.py        # NEW - discovers/loads *_strategy.py from /config/strategies/
│   ├── database.py                # MODIFIED (additive only) - 3 new tables, see data-model.md
│   ├── price_band_strategy.py     # UNCHANGED - no solar-specific code added here this time
│   └── scheduler.py               # MODIFIED - register one new job: "Execute strategy plugins every minute"
├── api/
│   └── solar_strategy.py          # NEW - settings/enrollment endpoints for Solar Strategy
bundled_config/
└── strategies/
    └── solar_strategy.py          # NEW - the actual Solar Strategy plugin implementation
ui-react/src/pages/
└── SolarStrategy.tsx              # NEW - its own settings/enrollment page (not bolted onto PriceBandStrategy.tsx)
tests/
└── test_solar_strategy.py         # NEW
```

**Structure Decision**: Single existing project (FastAPI backend + React frontend), extended with a new plugin category following the project's established `app/core/<thing>_loader.py` + `bundled_config/<thing>s/` pattern. No new top-level project/service.

## Complexity Tracking

*No Constitution Check violations - table intentionally omitted.*
