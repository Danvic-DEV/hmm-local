# Tasks: Solar Strategy Plugin

**Input**: Design documents from `/specs/001-solar-strategy-plugin/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/strategy-plugin-contract.md, quickstart.md (all present)

**Tests**: Included, not optional - Constitution's Development Workflow section
requires a corresponding test for "new scheduler/strategy/plugin behavior
that affects automated decisions," which this entire feature is.

**Organization**: Tasks are grouped by user story (from spec.md) to enable
independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1-US4, per spec.md)

---

## Phase 1: Setup

**Purpose**: Project scaffolding for the new plugin category, no behavior yet.

- [x] T001 Create `bundled_config/strategies/` directory; confirm `entrypoint.sh` deploys it to `/config/strategies/` on first run the same way `bundled_config/drivers/` already is
- [x] T002 [P] Add `SolarStrategyConfig` and `SolarMinerEnrollment` models to `app/core/database.py` (new tables only - see data-model.md; no changes to any existing table/column)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The plugin contract, loader, and scheduler wiring every user
story depends on. This is also where Constitution VII's failure-isolation
guarantee actually comes from - built once, here, not per-story.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [x] T003 [P] Define `StrategyPlugin`, `StrategyMetadata`, `StrategyExecutionResult` in `app/core/strategy_plugin_base.py` per `contracts/strategy-plugin-contract.md`
- [x] T004 Implement `app/core/strategy_loader.py`: glob `/config/strategies/*_strategy.py`, load each inside its own `try/except` (mirrors `app/core/pool_loader.py` exactly), register by `strategy_id` (depends on T003)
- [x] T005 Register the `"Execute strategy plugins every minute"` job in `app/core/scheduler.py`, iterating `get_strategy_loader().get_all_plugins()` with each plugin's `execute(db)` call wrapped in its own `try/except` (depends on T004)
- [x] T006 [P] Call `init_strategy_loader()` at startup in `app/main.py`, alongside the existing `init_pool_loader()`/miner loader calls (depends on T004)
- [x] T007 [P] Unit test: a strategy file that fails to import does not block other strategy files or app startup, in `tests/test_strategy_loader.py` (depends on T004)

**Checkpoint**: Foundation ready - loader, contract, and scheduler isolation exist. User story work can begin.

---

## Phase 3: User Story 1 - Capture free solar power automatically (Priority: P1) 🎯 MVP

**Goal**: An enrolled miner turns on/off and changes tuning mode automatically based on live solar surplus, sized to its own observed power draw.

**Independent Test**: Enable the feature, enroll one miner, confirm it responds correctly to real surplus changes (quickstart.md Scenario 3).

### Tests for User Story 1

- [x] T008 [P] [US1] Unit test: bin-packing/mode sizing against `MinerModePowerStats` picks the highest-draw mode that fits remaining surplus, most-efficient-miner-first, in `tests/test_solar_strategy.py`
- [x] T009 [P] [US1] Unit test: EMA surplus smoothing matches the existing `MinerModePowerStats` EMA formula/alpha, in `tests/test_solar_strategy.py`
- [x] T010 [P] [US1] Unit test: on/off requires 5 consecutive confirming cycles; a mode-only change on an already-on miner applies immediately, in `tests/test_solar_strategy.py`

### Implementation for User Story 1

- [x] T011 [US1] Implement `SolarStrategy(StrategyPlugin)` skeleton in `bundled_config/strategies/solar_strategy.py`: load `SolarStrategyConfig`, read the configured HA sensor via the existing `HomeAssistantIntegration.get_device_state_value`, apply EMA smoothing (depends on T003, T004, T009)
- [x] T012 [US1] Implement miner ranking (reuse the existing efficiency-leaderboard query) + bin-packing against `MinerModePowerStats` in `solar_strategy.py` (depends on T011, T008)
- [x] T013 [US1] Implement the 5-cycle on/off debounce and immediate mode-change application - `adapter.set_mode()`, pool switch via the configured `pool_id`, HA on/off control - in `solar_strategy.py` (depends on T012, T010)
- [x] T014 [US1] Call `log_audit(...)` for every solar-driven state change in `solar_strategy.py` (depends on T013)
- [x] T015 [US1] Implement `GET`/`POST /api/settings/solar-strategy` and `POST /api/settings/solar-strategy/execute` in `app/api/solar_strategy.py` per `contracts/strategy-plugin-contract.md` (depends on T011)
- [x] T016 [US1] Register the `solar_strategy` router in `app/main.py`
- [x] T017 [US1] Build `ui-react/src/pages/SolarStrategy.tsx`: enable toggle, HA sensor picker (filter `GET /api/integrations/homeassistant/devices` to `domain === "sensor"`), pool picker, per-miner enrollment checkboxes (depends on T015)
- [x] T018 [US1] Add the Solar Strategy route/nav entry in `ui-react/src/App.tsx`

**Checkpoint**: Solar Strategy can be enabled, configured, and enrolled miners respond to real surplus - functionally complete, but not yet safe for simultaneous use with Price Band Strategy (see US2).

---

## Phase 4: User Story 2 - Mutual exclusivity with Price Band Strategy (Priority: P1)

**Goal**: A miner can never be enrolled in both strategies at once.

**Independent Test**: Attempt to enroll an already-price-band-enrolled miner into Solar Strategy (and vice versa) and confirm it's rejected (quickstart.md Scenario 2).

### Tests for User Story 2

- [x] T019 [P] [US2] Unit test: enrolling a miner already enrolled in the other strategy is rejected by the shared exclusivity check, in `tests/test_solar_strategy.py`

### Implementation for User Story 2

- [x] T020 [US2] Implement the shared enrollment-exclusivity check in `app/core/strategy_enrollment.py` (depends on T002)
- [x] T021 [US2] Call the exclusivity check from `POST /api/settings/solar-strategy` in `app/api/solar_strategy.py` (depends on T020, T015)
- [x] T022 [US2] Call the exclusivity check from the existing `save_price_band_strategy_settings` in `app/api/price_band_strategy.py` - the one deliberate, narrow touch to existing Price Band Strategy code this feature makes (depends on T020)
- [x] T023 [US2] Surface the rejection/conflict clearly in `ui-react/src/pages/SolarStrategy.tsx` and `ui-react/src/pages/PriceBandStrategy.tsx` (depends on T021, T022)

**Checkpoint**: Both P1 stories complete - the feature is now safe to enable for real use, not just to test in isolation.

---

## Phase 5: User Story 3 - Understand why Solar Strategy did what it did (Priority: P2)

**Goal**: Every solar-driven action is traceable from logs/audit history alone.

**Independent Test**: After a real action, find a matching audit entry naming the miner, the change, and the surplus value that justified it (quickstart.md Scenario 5).

- [x] T024 [US3] Review every state-changing branch in `solar_strategy.py`'s `execute()` and confirm each calls `log_audit` with miner, before→after state, and the surplus/threshold compared (depends on T014)
- [x] T025 [P] [US3] Extend whatever surface already shows Price Band Strategy's audit history to also show `solar_strategy`-sourced entries (reuse the existing view rather than building a new one)

**Checkpoint**: An operator can answer "why did this miner turn on" without reading source code.

---

## Phase 6: User Story 4 - Safe to turn on without breaking anything else (Priority: P2)

**Goal**: Disabled-by-default is truly inert; a Solar Strategy bug can't take down the core or Price Band Strategy; adopting the feature needs no manual DB step.

**Independent Test**: quickstart.md Scenarios 1, 6, and 7.

- [x] T026 [P] [US4] Test: a deliberately-raising test strategy plugin doesn't stop Price Band Strategy's job or the scheduler, in `tests/test_strategy_loader.py` (depends on T005)
- [x] T027 [P] [US4] Test: with `SolarStrategyConfig.enabled=False`, Price Band Strategy's behavior/output is identical to its pre-feature baseline, in `tests/test_solar_strategy.py`
- [x] T028 [P] [US4] Test: against a DB fixture seeded like an existing install, app startup creates the new tables automatically and `GET /api/settings/solar-strategy` returns disabled defaults with zero manual SQL, in `tests/test_solar_strategy.py`

**Checkpoint**: All four user stories complete and independently verified.

---

## Final Phase: Polish & Cross-Cutting Concerns

- [x] T029 [P] Write `docs/STRATEGY_PLUGIN_CONTRACT.md` from `contracts/strategy-plugin-contract.md`, following the pattern `docs/ENERGY_PROVIDER_PLUGIN_CONTRACT.md` is supposed to (per the constitution's own Sync Impact Report, that doc was never actually written either - worth doing both, but at minimum this one)
- [x] T030 [P] Update `README.md` to mention Solar Strategy
- [ ] T031 Run all 7 `quickstart.md` scenarios end-to-end
  - **Pending** - needs a real deployment with a live HA sensor, a real pool, and real miners; not verifiable from a dev machine. Over to Richard once this is deployed.
- [x] T032 Run `scripts/pre_deploy_gate.sh` and confirm it's green
  - Verified two ways: locally (Python/Node newly installed this session) - every file this feature touches or added passes; 3 pre-existing files fail locally only due to a POSIX-only `resource` import unrelated to this work. And via the real CI gate (Linux, PR #3 against `001-solar-strategy-plugin`) - fully green, including those 3 files.

---

## Dependencies & Execution Order

- **Setup (Phase 1)**: No dependencies.
- **Foundational (Phase 2)**: Depends on Setup. Blocks every user story - this is where Constitution VII's isolation guarantee is actually built.
- **US1 (Phase 3)**: Depends on Foundational only. Delivers the core value, independently testable, but not yet safe to run alongside Price Band Strategy.
- **US2 (Phase 4)**: Depends on Foundational and on the `SolarMinerEnrollment`/API work T002/T015 landed in US1 (shares the same enrollment surface) - not on US1's solar decision logic itself.
- **US3 (Phase 5)**: Depends on US1 (T014's audit calls existing to review).
- **US4 (Phase 6)**: Depends on Foundational (T004/T005) and US1 (for a real plugin to test against); independent of US2/US3.
- **Polish**: Depends on all four stories.

## Parallel Example: Foundational Phase

```bash
Task: "Define StrategyPlugin/StrategyMetadata/StrategyExecutionResult in app/core/strategy_plugin_base.py"
# Then, once that's done:
Task: "Call init_strategy_loader() at startup in app/main.py"
Task: "Unit test: broken strategy file doesn't block others, in tests/test_strategy_loader.py"
```

## Implementation Strategy

### MVP vs. "safe to actually enable"

Phase 3 (US1) alone is a legitimate MVP to validate the core decision logic
in isolation (enable Solar Strategy with zero miners enrolled in Price Band
Strategy at all, so exclusivity can't yet be violated). **Phase 4 (US2)
should land before the feature is enabled on a fleet that also uses Price
Band Strategy** - shipping US1 without US2 is fine for development/testing,
not for real operator use.

### Incremental delivery

1. Setup + Foundational → loader/scheduler/contract exist, nothing runs yet.
2. US1 → Solar Strategy works in isolation. Validate with quickstart Scenarios 1 and 3.
3. US2 → safe to use alongside Price Band Strategy. Validate with Scenario 2.
4. US3 → trustworthy/debuggable. Validate with Scenario 5.
5. US4 → confidence to leave it running unattended on the one production instance. Validate with Scenarios 6 and 7.
6. Polish → docs, README, full quickstart run, CI green.
