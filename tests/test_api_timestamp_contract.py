from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1] / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))


def test_to_utc_iso8601_serializes_naive_datetime_with_z_suffix() -> None:
    from api.time_utils import to_utc_iso8601

    dt = datetime(2026, 6, 9, 12, 30, 0)
    result = to_utc_iso8601(dt)

    assert result == "2026-06-09T12:30:00Z"


def test_to_utc_iso8601_normalizes_aware_datetime_to_utc() -> None:
    from api.time_utils import to_utc_iso8601

    plus_two = timezone(timedelta(hours=2))
    dt = datetime(2026, 6, 9, 14, 30, 0, tzinfo=plus_two)
    result = to_utc_iso8601(dt)

    assert result == "2026-06-09T12:30:00Z"


def test_api_modules_only_use_isoformat_in_approved_serializers() -> None:
    api_dir = APP_ROOT / "api"
    approved_files = {
        "dashboard.py",
        "time_utils.py",
    }
    isoformat_pattern = re.compile(r"(?<!from)\.isoformat\(")
    offenders: list[str] = []

    for path in sorted(api_dir.glob("*.py")):
        content = path.read_text(encoding="utf-8")
        if not isoformat_pattern.search(content):
            continue
        if path.name not in approved_files:
            offenders.append(path.name)

    assert offenders == [], (
        "Unexpected .isoformat() usage in app/api modules: "
        + ", ".join(offenders)
        + ". Use api.time_utils.to_utc_iso8601 for response timestamps."
    )