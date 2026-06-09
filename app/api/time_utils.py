"""API datetime serialization helpers."""

from datetime import timezone


def to_utc_iso8601(dt):
    """Serialize datetimes as explicit UTC ISO8601 (Z suffix)."""
    if not dt:
        return None

    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt.isoformat().replace("+00:00", "Z")
