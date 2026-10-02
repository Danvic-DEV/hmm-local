# Phase 1 Data Model: Solar Strategy Plugin

All entities below are **new tables**. No existing table gains a new
column (see `research.md` for why - adoption must not require manual
database changes on the operator's single production instance).

## SolarStrategyConfig (table: `solar_strategy_config`)

Operator-level settings for the feature. Singleton row, same convention
`PriceBandStrategyConfig` already uses (created on first access if
missing).

| Field | Type | Notes |
|---|---|---|
| `id` | PK int | |
| `enabled` | bool, default `False` | FR-012 - must default off |
| `solar_surplus_entity_id` | string, nullable | HA entity id supplying live surplus watts (FR-002) |
| `pool_id` | int FK → `Pool.id`, nullable | Dedicated Solar Strategy pool (FR-009) - reuses the existing `Pool` model/credential path, see `research.md` |
| `created_at` / `updated_at` | datetime | |

**Validation**: `enabled=True` with `solar_surplus_entity_id` or `pool_id`
unset is a valid *stored* state, but the plugin's `execute()` must treat
it as "not actually runnable yet" and report that clearly (see edge case
in spec.md: "operator has not configured a dedicated pool yet") rather
than erroring.

## SolarMinerEnrollment (table: `solar_miner_enrollment`)

Per-miner opt-in, mirroring the shape of the existing `MinerStrategy`
table but intentionally a **separate** table, not a shared one - keeps
Solar Strategy's schema fully independent of Price Band Strategy's.

| Field | Type | Notes |
|---|---|---|
| `id` | PK int | |
| `miner_id` | int, indexed, **unique** | One row per miner (FR-003) |
| `enabled` | bool, default `True` | Allows toggling off without deleting the row |
| `created_at` | datetime | |

**Validation (cross-entity, FR-004)**: A `miner_id` MUST NOT have an
`enabled=True` row here at the same time it has `strategy_enabled=True`
in the existing `MinerStrategy` table. This can't be a database
constraint across two independently-owned tables, so it's enforced at
the API layer: a shared enrollment-exclusivity check, called by both
Solar Strategy's and Price Band Strategy's enrollment endpoints, that
rejects (or - operator's choice, see `quickstart.md` validation scenario
2 - offers to auto-unenroll from the other) an enrollment attempt that
would violate it. This is the one place this feature touches existing
Price Band Strategy code: a small, explicit, justified addition to its
*enrollment endpoint's validation*, not its decision logic.

## Solar decision logging - reuses the existing `AuditLog` mechanism, no new table

**Decision**: What spec.md's Key Entities section calls a "Solar Decision
Record" is **not** a new table. Constitution Principle IV requires
state-changing decisions to go through the existing `AuditLogger`/
`log_audit(...)` pattern (`app/core/audit.py`) - the same mechanism
Price Band Strategy already uses for band-transition and
champion-promotion logging. Adding a second, parallel logging table would
duplicate that mechanism unnecessarily (Principle VI). Every solar-driven
action calls the existing `log_audit(db, action=..., resource_type=
"solar_strategy", resource_name=..., changes={...})` with enough detail
(miner, surplus value read, threshold compared, mode/pool before→after)
to satisfy FR-010/SC-004.

## State transitions (SolarMinerEnrollment × live evaluation)

```text
not enrolled -> enrolled (enabled=True)       [operator action, blocked if price-band-enrolled]
enrolled -> not enrolled (row deleted/enabled=False) [operator action]
enrolled, off -> enrolled, on   [5 consecutive confirming cycles, FR-007]
enrolled, on -> enrolled, off   [5 consecutive confirming cycles, FR-007]
enrolled, on, mode A -> enrolled, on, mode B   [immediate, FR-008, no cycle requirement]
```
