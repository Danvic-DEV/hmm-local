# Feature Specification: Solar Strategy Plugin

**Feature Branch**: `001-solar-strategy-plugin`

**Created**: 2026-10-01

**Status**: Draft

**Input**: User description: "Add a standalone Solar Strategy as a new, dedicated plugin category in HMM-Local, parallel to and mutually exclusive from the existing Price Band Strategy (a miner can be enrolled in Price Band Strategy or Solar Strategy, never both at once). Solar Strategy reads a live excess-solar-surplus signal from a configured Home Assistant sensor and decides which enrolled miners should run and at what tuning mode, sized to fit within that live surplus, most efficient miners first, using each miner's own historically observed power draw per mode. Solar Strategy needs its own dedicated pool configuration. Mode changes on an already-running miner apply immediately; turning a miner fully on or off only happens after the desired state has held for 5 consecutive one-minute evaluations. All state-changing decisions are logged/audited the same way Price Band Strategy's are today. Single-operator, self-hosted, production-only deployment (no dev/staging) - the feature must default to fully inactive and require no manual database intervention to adopt."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Capture free solar power automatically (Priority: P1)

As the operator, I want a subset of my miners to automatically start mining when I have excess solar generation, and stop (or throttle back) when that surplus goes away, so that I capture mining capacity that would otherwise go unused without having to watch my solar production and flip switches myself.

**Why this priority**: This is the entire point of the feature - without it, there's nothing to deliver. Everything else (enrollment control, observability) only matters in service of this working correctly.

**Independent Test**: Enable Solar Strategy, enroll one miner, and observe that it turns on within a few minutes of real solar surplus becoming available, and turns off again once that surplus is gone for a sustained period - without the operator manually touching the miner.

**Acceptance Scenarios**:

1. **Given** Solar Strategy is enabled with surplus currently available and a miner enrolled but off, **When** the surplus has been available long enough to confirm it's not a momentary blip, **Then** the miner turns on in a tuning mode sized to fit within the available surplus.
2. **Given** an enrolled miner currently running on solar surplus, **When** the surplus drops to a level that can no longer sustain it for a sustained period, **Then** the miner is turned off.
3. **Given** more enrolled miners are eligible than the current surplus can support, **When** Solar Strategy evaluates them, **Then** it runs the most efficient/profitable eligible miners first and leaves the rest off.
4. **Given** an enrolled miner already running on solar power, **When** the available surplus changes (but stays enough to keep the miner on), **Then** its tuning mode adjusts immediately to fit the new surplus, without waiting.

---

### User Story 2 - Keep Solar Strategy and Price Band Strategy from fighting over the same miner (Priority: P1)

As the operator, I want each miner to be controlled by exactly one strategy at a time, so I never end up with two automated systems disagreeing about whether a miner should be on, off, or in what mode.

**Why this priority**: This was the direct cause of abandoning the first attempt at this feature - without strict mutual exclusivity, the two strategies can issue conflicting commands to the same hardware. It's as fundamental as User Story 1.

**Independent Test**: Attempt to enroll a miner that is currently enrolled in Price Band Strategy into Solar Strategy (or vice versa) and confirm the system prevents it, requiring the operator to unenroll from one before enrolling in the other.

**Acceptance Scenarios**:

1. **Given** a miner is currently enrolled in Price Band Strategy, **When** the operator tries to also enroll it in Solar Strategy, **Then** the system blocks it (or automatically unenrolls it from Price Band Strategy first, if the operator confirms) rather than allowing both simultaneously.
2. **Given** a miner is enrolled in Solar Strategy, **When** the operator views either strategy's enrollment list, **Then** that miner appears in exactly one of the two lists, never both.

---

### User Story 3 - Understand why Solar Strategy did what it did (Priority: P2)

As the operator, I want to see, after the fact, why Solar Strategy turned a specific miner on, off, or changed its mode, so I can trust the automation and tune it instead of treating it as a black box.

**Why this priority**: Important for trust and troubleshooting, but the feature still delivers real value (User Story 1) even with minimal logging on day one - this makes it trustworthy to leave running unattended.

**Independent Test**: After Solar Strategy takes an action, find a corresponding log/audit entry that names the miner, the action taken, and the surplus value that justified it, without needing to read source code.

**Acceptance Scenarios**:

1. **Given** Solar Strategy turns a miner on, off, or changes its mode, **When** the operator reviews the audit/log history, **Then** they can see which miner, what changed, and what surplus reading drove the decision.

---

### User Story 4 - Safe to turn on without breaking anything else (Priority: P2)

As the operator, I want to be able to deploy this feature to my single production system and leave it switched off until I'm ready, confident that nothing about my existing fleet or Price Band Strategy changes in the meantime, and that a bug in Solar Strategy can't take down the rest of the system.

**Why this priority**: Directly addresses why the first attempt at this feature was abandoned - it must be safe to ship to a single, production-only deployment with no dev/staging environment to test in first.

**Independent Test**: Deploy the feature with Solar Strategy left disabled and confirm Price Band Strategy and the rest of the fleet behave identically to before the deployment; separately, simulate a failure inside Solar Strategy's decision-making and confirm Price Band Strategy and fleet monitoring continue operating normally.

**Acceptance Scenarios**:

1. **Given** Solar Strategy is installed but left disabled, **When** the system runs normally, **Then** Price Band Strategy and all other existing behavior is unchanged.
2. **Given** Solar Strategy is enabled and encounters an unexpected error while making a decision, **When** that error occurs, **Then** it is logged loudly and Solar Strategy skips that cycle, while Price Band Strategy, telemetry, and the rest of the system keep running unaffected.
3. **Given** an existing installation is updated to a version that includes this feature, **When** the update is applied, **Then** no manual database changes are required for the system to keep working.

---

### Edge Cases

- What happens when the configured solar sensor becomes unreachable, stops updating, or returns a non-numeric reading? (Expected: treated as "no surplus available" - no solar-driven action taken - rather than erroring or guessing a value.)
- What happens when solar surplus fluctuates rapidly (e.g. passing clouds) across the threshold needed to keep a miner on? (Expected: on/off state doesn't flap - see FR-007 debounce requirement; tuning-mode-only adjustments within an already-on miner are not subject to this restriction.)
- What happens when no miners are enrolled in Solar Strategy at all? (Expected: Solar Strategy does nothing, no errors.)
- What happens when the operator has not configured a dedicated Solar Strategy pool yet, but has enrolled miners and enabled the strategy? (Expected: Solar Strategy takes no action for those miners and surfaces this as a clear, visible configuration gap rather than failing silently or using the wrong pool.)
- What happens when a miner enrolled in Solar Strategy becomes briefly unreachable? (Expected: consistent with existing fleet behavior elsewhere - skip that miner for this cycle, don't let it block decisions for other enrolled miners.)
- What happens on a brand-new install with no historical per-mode power data yet for a given miner? (Expected: that miner is skipped from solar allocation until enough data exists to size it safely, rather than guessing its power draw.)

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST allow the operator to enable or disable Solar Strategy independently of Price Band Strategy.
- **FR-002**: System MUST allow the operator to configure which Home Assistant sensor represents live excess solar surplus.
- **FR-003**: System MUST allow the operator to enroll or unenroll individual miners in Solar Strategy.
- **FR-004**: System MUST prevent a miner from being enrolled in both Solar Strategy and Price Band Strategy at the same time.
- **FR-005**: System MUST, for each enrolled and eligible miner, decide whether it should run and at what tuning mode, based on current solar surplus and that miner's own historically observed power draw per mode.
- **FR-006**: System MUST prioritize the most efficient/profitable enrolled miners first when available surplus cannot support all enrolled miners simultaneously.
- **FR-007**: System MUST NOT change a miner's on/off power state until the desired state has held consistently for 5 consecutive one-minute evaluation cycles.
- **FR-008**: System MUST apply a tuning-mode change on an already-running enrolled miner immediately, without the on/off debounce delay.
- **FR-009**: System MUST allow the operator to configure a dedicated mining pool for Solar Strategy, independent of Price Band Strategy's pool configuration.
- **FR-010**: System MUST log every Solar-Strategy-driven state change (on/off, mode, pool) with enough detail - what changed, why, and what surplus value was compared against what threshold - for the operator to reconstruct the decision without reading source code.
- **FR-011**: System MUST continue operating Price Band Strategy and the rest of the fleet normally if Solar Strategy encounters an internal error; a Solar Strategy failure MUST NOT disrupt other miners, other strategies, or the core system.
- **FR-012**: System MUST default to fully disabled/inactive (zero behavior change from today) until the operator explicitly configures and enables it.
- **FR-013**: System MUST NOT require any manual or destructive database changes for an existing installation to adopt this feature - adoption MUST be purely additive.
- **FR-014**: System MUST treat an unreachable, stale, or non-numeric solar sensor reading as "no surplus available" rather than erroring or guessing a value.

### Key Entities

- **Solar Strategy Configuration**: Operator-level settings for the feature - whether it's enabled, which Home Assistant sensor supplies the surplus reading, and which pool solar-claimed miners mine against.
- **Solar Enrollment**: A per-miner record of opt-in to Solar Strategy, mutually exclusive with that miner's enrollment in Price Band Strategy.
- **Solar Decision Record**: A logged/audited entry for each automated action Solar Strategy takes, capturing which miner, what changed, and what surplus value justified it.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: After enabling Solar Strategy and enrolling at least one miner, the operator observes it respond (turn on, off, or change mode) to a sustained change in solar surplus within 5 minutes, with no manual intervention.
- **SC-002**: A miner never appears in both Price Band Strategy's and Solar Strategy's enrolled-miner lists at the same time, in any view the operator checks.
- **SC-003**: Over any 24-hour period of fluctuating solar surplus, no enrolled miner changes on/off power state more than once every 5 minutes.
- **SC-004**: For any Solar-Strategy-driven action in the last 7 days, the operator can determine from logs/audit history which miner was affected, what changed, and what surplus value drove it - without asking the developer or reading source code.
- **SC-005**: With Solar Strategy left disabled, the fleet's behavior (Price Band Strategy decisions, miner states) is indistinguishable from the system's behavior before this feature was installed.
- **SC-006**: Updating an existing installation to a version including this feature requires zero manual database steps from the operator.

## Assumptions

- The operator already has a working Home Assistant integration with a sensor reporting live solar surplus in watts (an existing, separately-built capability) - this feature only needs to read it.
- Per-miner, per-mode historical power draw is already tracked by the system from live telemetry and is accurate enough to size solar allocation without new instrumentation.
- This is a single-operator, single-deployment system with no separate staging/dev environment - the feature must be safe to adopt directly on the operator's production instance, and schema changes (if any) must be purely additive and require no manual intervention.
- "Most efficient/profitable" ranking reuses the system's existing efficiency-ranking concept (already used elsewhere for automated miner selection) rather than introducing a new ranking methodology.
- The Solar Strategy pool is a one-time operator configuration choice, not something that changes automatically based on live conditions.
- When a miner is unenrolled from Solar Strategy (or the strategy is disabled) while it happens to be running, it is simply no longer managed by Solar Strategy going forward - its current on/off state and mode are left as-is rather than being forced to a particular state, consistent with how Price Band Strategy already treats miners that are no longer enrolled.
