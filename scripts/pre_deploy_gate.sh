#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_BASE_URL="${API_BASE_URL:-http://localhost:8080}"
RUN_API_SMOKE="${RUN_API_SMOKE:-0}"

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

PYTHON_BIN="python3"
if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
fi

step "Running backend tests (pytest)"
if command -v pytest >/dev/null 2>&1; then
  pytest tests
else
  "$PYTHON_BIN" -m pytest tests
fi

step "Running frontend lint"
(
  cd ui-react
  npm run lint
)

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

  printf "%s" "$runtime_json" | jq -e . >/dev/null
  printf "%s" "$pools_json" | jq -e 'type == "object"' >/dev/null

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
fi

echo
if [[ "$RUN_API_SMOKE" == "1" ]]; then
  echo "GO: pre-deploy checks passed (tests, lint, build, API smoke)"
else
  echo "GO: pre-deploy checks passed (tests, lint, build)"
  echo "Tip: set RUN_API_SMOKE=1 API_BASE_URL=http://localhost:8080 for API contract checks."
fi
