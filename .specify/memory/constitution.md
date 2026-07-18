<!--
Sync Impact Report
==================
Version change: (unratified template) → 1.0.0
Rationale: Initial ratification. The prior file was the unfilled
`constitution-template.md` scaffold with no adopted principles, so this is
treated as a first adoption (MINOR/MAJOR semantics don't apply retroactively;
starting at 1.0.0).

Principles established:
- I. Plugin-First Architecture (NON-NEGOTIABLE)
- II. Local-First, Graceful Degradation
- III. Durable State Across Restarts
- IV. Observable Decision-Making
- V. Security-Sensitive Credential Handling
- VI. Conservative Dependencies, Built to Run Unattended

Sections added:
- Technology & Structure Constraints
- Development Workflow & Quality Gates
- Governance (amendment procedure, versioning policy, compliance review)

Removed sections: none (template placeholders replaced, nothing dropped).

Templates requiring updates:
- ✅ .specify/templates/plan-template.md — "Constitution Check" gate is
  generic ("[Gates determined based on constitution file]"); no edit needed,
  it will pick up these principles at plan time.
- ✅ .specify/templates/spec-template.md — no constitution-specific
  references; generic FR/SC structure remains compatible.
- ✅ .specify/templates/tasks-template.md — generic phase/task structure;
  compatible with plugin-first task organization (new plugins are new files,
  not core edits) without template changes.
- ✅ .claude/skills/speckit-*/SKILL.md — scanned for CLAUDE-only or other
  agent-specific hardcoded references; none found requiring generalization.
- ⚠ README.md / docs/*_PLUGIN_CONTRACT.md — not modified by this command;
  recommend a follow-up pass to cross-link the constitution from
  docs/ENERGY_PROVIDER_PLUGIN_CONTRACT.md and any future
  MINER/POOL_PLUGIN_CONTRACT docs, since Principle I now formally governs
  those contracts.

Deferred items / TODOs: none. All placeholders resolved from repository
inspection (app/adapters, app/integrations, app/providers/energy,
app/core/{config,database,scheduler,audit,price_band_strategy}.py,
bundled_config/, scripts/pre_deploy_gate.sh, .github/workflows,
docs/SCHEDULER_AUDIT.md, docs/ENERGY_PROVIDER_PLUGIN_CONTRACT.md, README.md).
-->

# Home Miner Manager Constitution

## Core Principles

### I. Plugin-First Architecture (NON-NEGOTIABLE)

Every capability — mode management, pool management, electricity-cost-driven
switching, energy provider integration, and any future capability — is a
plugin against a stable core API. The core (`app/core/`, `app/api/`) MUST
expose narrow, versioned contracts (e.g. `MinerAdapter` in
`app/adapters/base.py`, `BasePoolIntegration` in
`app/integrations/base_pool.py`, `EnergyPriceProvider` in
`app/providers/energy/base.py`) that plugins implement; it MUST NOT contain
logic specific to one miner brand, one pool operator, or one energy
provider. New hardware support, pool integrations, or provider integrations
MUST be added as new plugin files (`*_driver.py` under
`bundled_config/drivers/{miners,pools}/`, `*_provider.py` under
`bundled_config/providers/energy/`) discovered by the existing loaders
(`miner_loader.py`, `pool_loader.py`, `providers/energy/loader.py`), never as
special-cased branches inside core orchestration code.

Changing a plugin contract (adding/removing/renaming an abstract method,
changing its signature, or changing the semantics loaders rely on — e.g.
`miner_type`/`pool_type`/`get_metadata()`) is a breaking change. It MUST be
justified explicitly (why existing plugins cannot satisfy the need without
the change) and MUST update every bundled plugin implementing that contract
in the same change.

**Rationale**: The system's value is in supporting a growing, unpredictable
set of miners, pools, and energy tariffs without the core rotting into a
pile of vendor-specific conditionals. A stable contract is what makes that
sustainable long-term for a project run by a small team.

### II. Local-First, Graceful Degradation

This tool manages physical hardware on a home network: rigs, switches, and
pool endpoints that go offline, flake, or time out. Every code path that
talks to a rig, a pool, an external integration (Home Assistant, energy
provider APIs), or the internet MUST assume the remote end is unreachable
and MUST NOT let that unreachability crash the scheduler, corrupt state, or
block other rigs. Prefer per-rig/per-plugin try/except with logged warnings
over unhandled exceptions; prefer degraded/partial operation (as implemented
by `SchedulerService._is_degraded_mode_active` /
`_should_skip_non_critical_job` and the telemetry freshness watchdog in
`app/core/scheduler.py`) over stopping the whole service.

**Rationale**: This runs unattended in someone's home, often on a Pi or
spare box, against hardware that is inherently unreliable (Wi-Fi drops,
firmware hangs, power cuts). A single unreachable miner must never take
down monitoring or control of the rest of the fleet.

### III. Durable State Across Restarts

Historical telemetry, stats, and audit records MUST survive process
restarts, container updates, and database engine fallback (PostgreSQL →
SQLite). Schema changes are additive: `init_db()` creates tables from
current models on fresh install with no destructive migrations, and startup
code that needs to reconcile old data (e.g.
`_backfill_legacy_ha_switch_links`) MUST backfill/tolerate old shapes rather
than drop them. Config and state under `/config` MUST NOT be treated as
disposable; destructive rewrites of `config.yaml` or the database are
prohibited outside of an explicit, user-initiated action.

**Rationale**: Users rely on this tool's historical stats (efficiency
trends, band transitions, pool performance) to make decisions over weeks
and months. A platform update or database fallback silently wiping history
breaks that trust and the tool's core value proposition.

### IV. Observable Decision-Making

Any automated decision that changes what a miner does — price band
transitions, champion election/promotion, pool failover, mode switching —
MUST be logged with enough context (current values, thresholds compared,
which entity won/lost and why) that an operator reading logs or the audit
trail can reconstruct *why* the system acted, without reading the source.
Use the existing patterns: structured `logger.info`/`logger.warning` calls
at decision points (as in `price_band_strategy.py`'s band-transition and
champion-promotion logging) and `AuditLogger.log(...)` (`app/core/audit.py`)
for state-changing actions on miners, pools, and strategies. Silent
automated switching is not acceptable.

**Rationale**: Cost-based switching acts on the user's electricity bill and
hardware without a human in the loop each time. If the reasoning isn't
visible, the user cannot trust, debug, or tune the strategy.

### V. Security-Sensitive Credential Handling

Wallet addresses, pool credentials (`pool_user`, `pool_password`), database
passwords, MQTT credentials, and API keys/tokens for integrations (Home
Assistant, energy providers, cloud push) are security-sensitive. They MUST
be sourced from `/config` (config.yaml, `.env`) or environment variables —
never hard-coded, never committed (see `.gitignore` exclusions for
`config/`, `.env`, `CONTAINER_RECOVERY.txt`). They MUST NOT appear in log
statements, error messages, audit log `changes` payloads, or exception
tracebacks; redact or omit the value, logging only identifiers (miner name,
pool ID) instead. API responses that echo configuration MUST strip
credential fields before serialization.

**Rationale**: This tool holds the keys to real mining payouts and home
network devices. A leaked wallet address redirects earnings; leaked pool or
HA credentials give an attacker control of hardware and history is full of
credential-leak-via-logs incidents.

### VI. Conservative Dependencies, Built to Run Unattended

New runtime dependencies (Python packages in `requirements.txt`, npm
packages in `ui-react/package.json`) require justification beyond
convenience: prefer the standard library or an existing dependency already
in the tree before adding a new one, and avoid dependencies with heavy
transitive footprints, unstable release cadence, or exotic native build
requirements — this must build reliably in the project's slim Docker image
and keep the ~400-800MB memory footprint documented in `README.md`. Code
MUST be written for a process that runs for months without restart:
avoid unbounded in-memory growth, clean up scheduler jobs/listeners on
shutdown (see `SchedulerService.shutdown()`), and prefer bounded
caches/retention over ever-growing state.

**Rationale**: HMM targets low-power, long-uptime home hardware (Raspberry
Pi, NAS, spare laptop) run by a single operator with no dedicated ops team.
Dependency churn and resource leaks that would be a minor annoyance in a
frequently-redeployed cloud service become real reliability problems here.

## Technology & Structure Constraints

- **Backend**: Python 3.11+, FastAPI, SQLAlchemy (async) with PostgreSQL as
  the recommended engine and automatic SQLite fallback when PostgreSQL is
  unavailable (`app/core/database.py`, `app/core/config.py`). Config lives
  under `/config` (mounted volume), never baked into the image.
- **Frontend**: React + TypeScript under `ui-react/`, built with Vite,
  Tailwind, and Radix UI primitives; the built assets are served by the
  FastAPI app (`app/ui/static`).
- **Plugins ship in two places**: bundled reference implementations under
  `bundled_config/{drivers/miners,drivers/pools,providers/energy}/` deployed
  by `entrypoint.sh` into `/config/...` on first run, and user-added plugins
  live directly in the deployed `/config/...` directories. Both are
  discovered the same way by the loaders — no special-casing bundled vs.
  user-provided plugins in core code.
- **Deployment**: Docker/`docker-compose`, with a separate `hmm-local`
  (main app) and `hmm-local-updater` (platform update companion) image,
  built independently based on changed paths (`.github/workflows/docker-publish.yml`).

## Development Workflow & Quality Gates

- Backend tests live in `tests/test_*.py` (pytest) and run against the
  SQLite fallback path (`PYTEST_CURRENT_TEST` env var short-circuits
  `/config` writes in `app/core/config.py`). New scheduler/strategy/plugin
  behavior that affects automated decisions MUST have a corresponding test.
- `scripts/pre_deploy_gate.sh` (backend tests, optional frontend lint/API
  smoke) is the local/CI quality gate and MUST pass before merge; CI runs it
  as the `quality-gate` job in `.github/workflows/docker-publish.yml` for
  any change touching `app/**`, `bundled_config/**`, `ui-react/**`,
  `tests/**`, or `scripts/**`.
- Logging hygiene: no bare `except:`, no `print()`/`traceback.print_exc()`
  in place of `logger.*` calls in core/scheduler code (see
  `docs/SCHEDULER_AUDIT.md` for the standard this was hardened to);
  exceptions from unreachable hardware or external APIs are caught and
  logged at the narrowest reasonable scope, not allowed to propagate and
  kill the scheduler.
- Changes to a plugin contract (Principle I) MUST update every bundled
  implementation of that contract and note the breaking change in the
  relevant `docs/*_PLUGIN_CONTRACT.md` file (see
  `docs/ENERGY_PROVIDER_PLUGIN_CONTRACT.md` as the existing pattern for
  miner/pool/energy contracts).

## Governance

This constitution supersedes ad hoc practice for architectural and
domain-safety decisions in this repository. Amendments are made by editing
`.specify/memory/constitution.md` directly (via `/speckit.constitution` or
manual edit), and MUST include a Sync Impact Report as a leading HTML
comment describing what changed and why.

**Versioning policy** (semantic versioning of this document):
- **MAJOR**: A principle is removed or redefined in a way that is backward
  incompatible with prior guidance (e.g. relaxing the plugin-only rule).
- **MINOR**: A new principle or constraint section is added, or existing
  guidance is materially expanded.
- **PATCH**: Wording clarifications, typo fixes, non-semantic edits.

**Compliance review**: Pull requests that add a capability outside the
plugin loaders, that could log or persist a credential, that add an
automated decision without a corresponding log/audit trail, or that add a
dependency without justification, MUST be flagged in review against this
constitution before merge. Complexity or deviation from a principle MUST be
justified in the PR description; silent deviation is not acceptable. Use
`README.md` and the `docs/*_PLUGIN_CONTRACT.md` files for concrete,
up-to-date implementation guidance that operationalizes these principles.

**Version**: 1.0.0 | **Ratified**: 2026-07-18 | **Last Amended**: 2026-07-18
