#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-https://miners.danvic.co.uk}"
MAX_POOL_AGE_SECONDS="${MAX_POOL_AGE_SECONDS:-900}"

step() {
  printf "\n[%s] %s\n" "$(date -u +%H:%M:%S)" "$1"
}

fail() {
  echo "FAIL: $1"
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

require_cmd curl
require_cmd jq
require_cmd python3

step "Fetching live health endpoints from ${BASE_URL}"
runtime_json="$(curl -fsS "${BASE_URL}/api/health/runtime")"
pools_json="$(curl -fsS "${BASE_URL}/api/dashboard/pools")"
energy_current_json="$(curl -fsS "${BASE_URL}/api/dashboard/energy/current")"
energy_next_json="$(curl -fsS "${BASE_URL}/api/dashboard/energy/next")"
operations_json="$(curl -fsS "${BASE_URL}/api/operations/status")"
audit_logs_json="$(curl -fsS "${BASE_URL}/api/audit/logs?limit=20")"
notifications_logs_json="$(curl -fsS "${BASE_URL}/api/notifications/logs?limit=20")"
strategy_json="$(curl -fsS "${BASE_URL}/api/settings/price-band-strategy")"
pool_effort_json="$(curl -fsS "${BASE_URL}/api/pools/effort")"
ha_config_json="$(curl -fsS "${BASE_URL}/api/integrations/homeassistant/config")"

printf "%s" "$runtime_json" | jq -e . >/dev/null || fail "invalid JSON from /api/health/runtime"
printf "%s" "$pools_json" | jq -e 'type == "object"' >/dev/null || fail "invalid JSON from /api/dashboard/pools"
printf "%s" "$energy_current_json" | jq -e . >/dev/null || fail "invalid JSON from /api/dashboard/energy/current"
printf "%s" "$energy_next_json" | jq -e . >/dev/null || fail "invalid JSON from /api/dashboard/energy/next"
printf "%s" "$operations_json" | jq -e . >/dev/null || fail "invalid JSON from /api/operations/status"
printf "%s" "$audit_logs_json" | jq -e 'type == "array"' >/dev/null || fail "invalid JSON from /api/audit/logs"
printf "%s" "$notifications_logs_json" | jq -e 'type == "array"' >/dev/null || fail "invalid JSON from /api/notifications/logs"
printf "%s" "$strategy_json" | jq -e . >/dev/null || fail "invalid JSON from /api/settings/price-band-strategy"
printf "%s" "$pool_effort_json" | jq -e '.efforts and (.efforts | type == "array")' >/dev/null || fail "invalid JSON from /api/pools/effort"
printf "%s" "$ha_config_json" | jq -e . >/dev/null || fail "invalid JSON from /api/integrations/homeassistant/config"

step "Checking pool payload integrity"
pool_count="$(printf "%s" "$pools_json" | jq 'length')"
if [[ "$pool_count" -eq 0 ]]; then
  fail "no pools returned from /api/dashboard/pools"
fi

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

bad_strategy_timestamps="$(printf "%s" "$strategy_json" | jq -r '
  .last_action_time
  | select(. != null)
  | select(test("(Z|[+-][0-9]{2}:[0-9]{2})$") | not)
')"
if [[ -n "$bad_strategy_timestamps" ]]; then
  echo "Found strategy timestamps without timezone offset:"
  echo "$bad_strategy_timestamps"
  fail "strategy timestamp contract check failed"
fi

bad_pool_effort_timestamps="$(printf "%s" "$pool_effort_json" | jq -r '
  .efforts[]
  | [ .effort_start, .last_reset, .last_updated ]
  | .[]
  | select(. != null)
  | select(test("(Z|[+-][0-9]{2}:[0-9]{2})$") | not)
')"
if [[ -n "$bad_pool_effort_timestamps" ]]; then
  echo "Found pool effort timestamps without timezone offset:"
  echo "$bad_pool_effort_timestamps"
  fail "pool effort timestamp contract check failed"
fi

bad_ha_config_timestamps="$(printf "%s" "$ha_config_json" | jq -r '
  .last_test
  | select(. != null)
  | select(test("(Z|[+-][0-9]{2}:[0-9]{2})$") | not)
')"
if [[ -n "$bad_ha_config_timestamps" ]]; then
  echo "Found Home Assistant config timestamps without timezone offset:"
  echo "$bad_ha_config_timestamps"
  fail "homeassistant timestamp contract check failed"
fi

step "Checking pool timestamp freshness (<= ${MAX_POOL_AGE_SECONDS}s)"
export POOLS_JSON="$pools_json"
export MAX_POOL_AGE_SECONDS
python3 - <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

pools = json.loads(os.environ["POOLS_JSON"])
max_age = int(os.environ["MAX_POOL_AGE_SECONDS"])
now = datetime.now(timezone.utc)
violations = []

for pool_id, payload in pools.items():
    ts = payload.get("last_updated")
    name = payload.get("display_name", pool_id)
    if not ts:
        continue
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        violations.append(f"{name}: invalid ISO timestamp")
        continue
    if dt.tzinfo is None:
        violations.append(f"{name}: naive timestamp")
        continue
    age = (now - dt.astimezone(timezone.utc)).total_seconds()
    if age > max_age:
        violations.append(f"{name}: stale ({int(age)}s)")

if violations:
    print("\n".join(violations))
    sys.exit(1)
PY

step "Checking runtime diagnostics sanity"
printf "%s" "$runtime_json" | jq -e '.process.rss_mb != null and .process.asyncio_task_count != null' >/dev/null || fail "runtime process metrics missing"
printf "%s" "$runtime_json" | jq -e '.websocket.active_connections != null and .websocket.queue_size != null' >/dev/null || fail "runtime websocket metrics missing"

echo
echo "PASS: post-deploy live checks passed"
echo "Summary: pools=${pool_count} base_url=${BASE_URL}"
