# Quickstart: Validating Solar Strategy

Prerequisites: an existing HMM-Local install with at least one enrolled
ASIC miner with some telemetry history (so `MinerModePowerStats` has
real per-mode wattage data for it), a Home Assistant sensor reporting
live solar surplus watts, and a configured mining pool to use as Solar
Strategy's dedicated pool.

## Scenario 1 - Zero-impact when disabled (SC-005)

1. Deploy the build including this feature, with Solar Strategy left at
   its default (`enabled=False`).
2. Watch Price Band Strategy run its normal cycle a few times (logs /
   `GET /api/settings/price-band-strategy`).
3. **Expected**: identical behavior/log output to before this feature was
   deployed. No new errors, no new tables referenced in a way that
   changes existing behavior.

## Scenario 2 - Mutual exclusivity (US2, SC-002)

1. Pick a miner currently enrolled in Price Band Strategy.
2. Attempt to enroll that same miner in Solar Strategy via
   `POST /api/settings/solar-strategy`.
3. **Expected**: rejected (or the operator is prompted to confirm
   unenrolling it from Price Band Strategy first - see
   `contracts/strategy-plugin-contract.md`'s shared exclusivity check).
   Confirm via both `GET /api/settings/price-band-strategy` and
   `GET /api/settings/solar-strategy` that the miner never appears
   enrolled in both at once.

## Scenario 3 - Responds to real solar surplus (US1, SC-001)

1. Configure and enable Solar Strategy: set `solar_surplus_entity_id` to
   the real HA sensor, set `pool_id` to the dedicated pool, enroll one
   miner that is currently off and has power-draw history.
2. Wait for a period of real, sustained solar surplus.
3. **Expected**: within 5 minutes of the surplus being sustained, the
   miner turns on in a mode whose `MinerModePowerStats` wattage fits the
   observed surplus. Confirm via the miner's own status and the audit
   log entry Solar Strategy wrote for the action.
4. Wait for surplus to drop to (near) zero for a sustained period.
5. **Expected**: the miner turns off within 5 minutes, with a
   corresponding audit log entry.

## Scenario 4 - No flapping under fluctuating surplus (US1/US4, SC-003)

1. With the same miner enrolled, observe behavior over a period where
   surplus crosses the on/off threshold repeatedly within a few minutes
   (e.g. passing cloud cover).
2. **Expected**: the miner does not change on/off power state more than
   once every 5 minutes. If surplus stays high enough to keep it on but
   fluctuates within the "on" range, its tuning mode may change
   immediately and repeatedly - that's expected (FR-008), only full
   on/off is debounced.

## Scenario 5 - Decisions are traceable (US3, SC-004)

1. After Scenario 3 or 4 has produced at least one action.
2. Review the audit log (however it's surfaced to the operator - API or
   UI) for entries with `resource_type="solar_strategy"`.
3. **Expected**: each entry names the miner, what changed (on/off/mode),
   and the surplus value that justified it - enough to answer "why did
   this happen" without reading source code.

## Scenario 6 - A broken strategy plugin doesn't take anything else down (US4)

1. (Development/staging check, not something to do against a real
   production instance) Introduce a deliberate exception into a test
   copy of the Solar Strategy plugin's `execute()` method.
2. Run the scheduler.
3. **Expected**: the error is logged loudly (plugin loader / scheduler
   job-error path), Solar Strategy's cycle is skipped, and Price Band
   Strategy's own scheduled job continues running normally in the same
   process, unaffected.

## Scenario 7 - Adopting on an existing install needs no manual DB step (FR-013, SC-006)

1. Take an existing HMM-Local install (with real data) and deploy the
   build including this feature.
2. **Expected**: the three new tables (`solar_strategy_config`,
   `solar_miner_enrollment`, and the standard `AuditLog` table which
   already exists) are present after a normal restart, with zero manual
   SQL run by the operator. `GET /api/settings/solar-strategy` returns a
   default (`enabled=False`) config without error.
