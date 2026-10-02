# Phase 0 Research: Solar Strategy Plugin

No `[NEEDS CLARIFICATION]` markers were left in the Technical Context -
this feature extends an already-understood codebase (deep exploration of
the plugin loaders, scheduler, and Price Band Strategy's internals was
done earlier the same day, including a first implementation attempt that
was built, tested, and then deliberately abandoned once it was found to
violate the project's own constitution). This document records the key
decisions and the alternatives rejected, rather than resolving unknowns.

## Decision: A genuinely new plugin category, not an extension of Price Band Strategy

**Decision**: Solar Strategy is implemented as a new `StrategyPlugin`
contract + loader, discovered the same way miner drivers, pool drivers,
and energy providers already are.

**Rationale**: Constitution Principle I (Plugin-First Architecture,
NON-NEGOTIABLE) explicitly names "electricity-cost-driven switching" as a
capability that MUST be a plugin, "never as special-cased branches inside
core orchestration code."

**Alternatives considered**: Hooking solar logic directly into
`price_band_strategy.py::execute_strategy` as a second decision pass,
OR'd with the price-driven result. This was actually built first and
reverted - it worked functionally, but hard-wired a second capability
directly into core orchestration, the exact pattern Principle I forbids.
Rejected.

## Decision: Solar Strategy runs as its own scheduler job, not a call-out from Price Band Strategy's job

**Decision**: A new APScheduler job ("Execute strategy plugins every
minute") iterates all loaded, enabled strategy plugins and calls each
independently; Price Band Strategy keeps its own existing job unchanged.

**Rationale**: Constitution Principle VII (Core Self-Preservation)
requires that a plugin failure never take down anything else. APScheduler
already isolates job failures via the `EVENT_JOB_ERROR` listener pattern
(`SchedulerService._register_job_memory_listener`) that every other
scheduled job already relies on - running Solar Strategy as its own job
gets this isolation for free, with no bespoke try/except scaffolding
needed at the call site (the gap in the first, reverted attempt, where
the solar overlay was called inline with no isolation around it).

**Alternatives considered**: Calling the Solar Strategy plugin from
inside Price Band Strategy's existing job (the first attempt's shape).
Rejected - reintroduces the exact coupling and failure-isolation gap
Principle VII exists to prevent, and works against "mutually exclusive"
being a structural property rather than a runtime-checked one.

## Decision: Brand-new tables only, never new columns on existing tables

**Decision**: All new persistent state (strategy config, enrollment,
decision log) lives in new tables, created automatically by the existing
`Base.metadata.create_all` on next startup.

**Rationale**: This is a single-operator, production-only deployment with
no dev/staging environment (explicit assumption in spec.md). The first
attempt added columns to `PriceBandStrategyConfig` and `MinerStrategy`,
which `create_all` does NOT retrofit onto an existing table - adopting it
would have required manually generating and hand-applying `ALTER TABLE`
SQL against the operator's live production database, which is what
caused the feature to be abandoned. A new table has no such problem:
`create_all` creates any table that doesn't exist yet, automatically, on
a normal restart - satisfying FR-013/SC-006 (zero manual database steps)
for free, simply by choosing new tables over altered ones.

**Alternatives considered**: The project's own
`scripts/generate_schema_reconcile_sql.py` (generate-SQL-for-manual-review
workflow). This is real, deliberate tooling for retrofitting *existing*
tables when that's unavoidable - but it is not a substitute for choosing
a design that avoids needing it in the first place when a new,
independent capability doesn't actually require touching old tables.

## Decision: Solar Strategy's pool is a reference to an existing `Pool`, not a new credential field

**Decision**: Solar Strategy configuration stores a `pool_id` foreign key
into the existing `Pool` table; the operator creates/selects that pool
through the existing pool-management flow.

**Rationale**: Constitution Principle V (Security-Sensitive Credential
Handling) governs pool credentials. Reusing the existing `Pool` model and
its already-established credential-handling path means this feature
introduces zero new credential storage/handling surface to get right or
get wrong.

**Alternatives considered**: A standalone `pool_url`/`pool_user`/
`pool_password` set of fields directly on the Solar Strategy config
table. Rejected - duplicates a security-sensitive concern the codebase
already solves once, correctly.

## Decision: Reuse existing power/efficiency data, add no new instrumentation

**Decision**: Wattage sizing per mode comes from the existing
`MinerModePowerStats` table (already populated continuously from live
telemetry); miner prioritization under constrained surplus reuses the
existing efficiency-leaderboard concept already used elsewhere for
automated miner selection.

**Rationale**: Constitution Principle VI (Conservative Dependencies) -
this data already exists, is already accurate (it's what the UI's
per-miner "ALL MODES" stats display is built from), and needed no new
telemetry collection, polling, or dependency to use.

**Alternatives considered**: A static, operator-entered watts-per-mode
table per miner. Rejected as both unnecessary (the real data already
exists and is more accurate) and extra operator burden to maintain.

## Decision: EMA-smoothed surplus reading; in-memory debounce counter

**Decision**: The raw solar-surplus sensor reading is smoothed with the
same EMA approach `MinerModePowerStats` already uses (`new = alpha*sample
+ (1-alpha)*prior`) before being used for sizing decisions. On/off
transitions require 5 consecutive one-minute cycles of consistent desired
state before acting (FR-007); mode-only changes on an already-running
miner apply immediately (FR-008). Both smoothing state and the debounce
counter are kept in memory within the plugin, not persisted.

**Rationale**: Matches an existing, proven pattern in the codebase rather
than inventing a new one. In-memory state resetting on a restart is an
accepted, low-consequence tradeoff - worst case after a restart is a
slightly slower (not incorrect) first few cycles of re-establishing
smoothing/debounce state, consistent with how the codebase's existing
`_miner_failure_counts`-style counters already behave.

**Alternatives considered**: Persisting smoothing/debounce state in the
database for restart-survival. Rejected as unnecessary complexity for a
cosmetic edge case with no correctness or safety impact.
