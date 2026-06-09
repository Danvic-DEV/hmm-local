#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_BASE_URL="${API_BASE_URL:-http://localhost:8080}"
RUN_API_SMOKE="${RUN_API_SMOKE:-0}"
RUN_FRONTEND_LINT="${RUN_FRONTEND_LINT:-0}"

step() {
  printf "\n[%s] %s\n" "$(date -u +%H:%M:%S)" "$1"
}

fail() {
  echo "NO-GO: $1"
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

require_cmd git
require_cmd npm

cd "$ROOT_DIR"
export PYTHONPATH="$ROOT_DIR/app${PYTHONPATH:+:$PYTHONPATH}"
# Ensure app/core/config.py takes the pytest-safe temp config path during collection.
export PYTEST_CURRENT_TEST=1

PYTHON_BIN="${PYTHON_BIN_OVERRIDE:-python3}"
if [[ -z "${PYTHON_BIN_OVERRIDE:-}" && -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
fi

step "Running backend tests (pytest)"
TEST_FILES="$(find tests -maxdepth 1 -name 'test_*.py' | sort)"
[[ -n "$TEST_FILES" ]] || fail "no test files found under tests/"

FAILED_TESTS=""
while IFS= read -r test_file; do
  [[ -n "$test_file" ]] || continue
  echo "- pytest ${test_file}"
  if ! "$PYTHON_BIN" -m pytest "$test_file"; then
    FAILED_TESTS+="${test_file}\n"
  fi
done <<EOF
$TEST_FILES
EOF

if [[ -n "$FAILED_TESTS" ]]; then
  echo "Backend test failures:"
  printf "%b" "$FAILED_TESTS" | sed 's/^/  /'
  fail "backend tests failed"
fi

if [[ "$RUN_FRONTEND_LINT" == "1" ]]; then
  step "Running frontend lint"
  (
    cd ui-react
    npm run lint
  )
else
  step "Skipping frontend lint (set RUN_FRONTEND_LINT=1 to enable strict lint gate)"
fi

step "Running frontend build"
(
  cd ui-react
  npm run build
)

if [[ "$RUN_API_SMOKE" == "1" ]]; then
  require_cmd curl
  require_cmd jq

  step "Running API smoke checks against ${API_BASE_URL}"
  runtime_json="$(curl -fsS "${API_BASE_URL}/api/health/runtime")"
  pools_json="$(curl -fsS "${API_BASE_URL}/api/dashboard/pools")"
  energy_current_json="$(curl -fsS "${API_BASE_URL}/api/dashboard/energy/current")"
  energy_next_json="$(curl -fsS "${API_BASE_URL}/api/dashboard/energy/next")"
  operations_json="$(curl -fsS "${API_BASE_URL}/api/operations/status")"
  audit_logs_json="$(curl -fsS "${API_BASE_URL}/api/audit/logs?limit=20")"
  notifications_logs_json="$(curl -fsS "${API_BASE_URL}/api/notifications/logs?limit=20")"

  printf "%s" "$runtime_json" | jq -e . >/dev/null
  printf "%s" "$pools_json" | jq -e 'type == "object"' >/dev/null
  printf "%s" "$energy_current_json" | jq -e . >/dev/null
  printf "%s" "$energy_next_json" | jq -e . >/dev/null
  printf "%s" "$operations_json" | jq -e . >/dev/null
  printf "%s" "$audit_logs_json" | jq -e 'type == "array"' >/dev/null
  printf "%s" "$notifications_logs_json" | jq -e 'type == "array"' >/dev/null

  bad_timestamps="$(printf "%s" "$pools_json" | jq -r '
    to_entries[]
    | select(.value.last_updated != null)
    | .value.last_updated
    | select(test("(Z|[+-][0-9]{2}:[0-9]{2})$") | not)
  ')"

  if [[ -n "$bad_timestamps" ]]; then
    echo "Found last_updated values without timezone offset:"
    echo "$bad_timestamps"
    fail "timestamp contract check failed"
  fi

  bad_energy_timestamps="$({
    printf "%s\n" "$energy_current_json"
    printf "%s\n" "$energy_next_json"
  } | jq -r '
    . as $row
    | ["valid_from", "valid_to"][]
    | $row[.]
    | select(. != null)
    | select(test("(Z|[+-][0-9]{2}:[0-9]{2})$") | not)
  ')"

  if [[ -n "$bad_energy_timestamps" ]]; then
    echo "Found energy timestamps without timezone offset:"
    echo "$bad_energy_timestamps"
    fail "energy timestamp contract check failed"
  fi

  bad_operations_timestamps="$(printf "%s" "$operations_json" | jq -r '
    [
      .strategy.last_action_time,
      .ha.detail.last_success,
      .ha.detail.downtime_start
    ]
    | .[]
    | select(. != null)
    | select(test("(Z|[+-][0-9]{2}:[0-9]{2})$") | not)
  ')"
  if [[ -n "$bad_operations_timestamps" ]]; then
    echo "Found operations timestamps without timezone offset:"
    echo "$bad_operations_timestamps"
    fail "operations timestamp contract check failed"
  fi

  bad_audit_timestamps="$(printf "%s" "$audit_logs_json" | jq -r '
    .[]
    | .timestamp
    | select(. != null)
    | select(test("(Z|[+-][0-9]{2}:[0-9]{2})$") | not)
  ')"
  if [[ -n "$bad_audit_timestamps" ]]; then
    echo "Found audit timestamps without timezone offset:"
    echo "$bad_audit_timestamps"
    fail "audit timestamp contract check failed"
  fi

  bad_notification_timestamps="$(printf "%s" "$notifications_logs_json" | jq -r '
    .[]
    | .timestamp
    | select(. != null)
    | select(test("(Z|[+-][0-9]{2}:[0-9]{2})$") | not)
  ')"
  if [[ -n "$bad_notification_timestamps" ]]; then
    echo "Found notification timestamps without timezone offset:"
    echo "$bad_notification_timestamps"
    fail "notification timestamp contract check failed"
  fi
fi

echo
if [[ "$RUN_API_SMOKE" == "1" ]]; then
  if [[ "$RUN_FRONTEND_LINT" == "1" ]]; then
    echo "GO: pre-deploy checks passed (tests, lint, build, API smoke)"
  else
    echo "GO: pre-deploy checks passed (tests, build, API smoke; lint skipped)"
  fi
else
  if [[ "$RUN_FRONTEND_LINT" == "1" ]]; then
    echo "GO: pre-deploy checks passed (tests, lint, build)"
  else
    echo "GO: pre-deploy checks passed (tests, build; lint skipped)"
  fi
  echo "Tip: set RUN_FRONTEND_LINT=1 for strict lint and RUN_API_SMOKE=1 API_BASE_URL=http://localhost:8080 for API contract checks."
fi
