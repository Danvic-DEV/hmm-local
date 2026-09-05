"""Telemetry high-water mark tracking (daily + since boot)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict

from core.config import settings


METRICS_PATH = settings.CONFIG_DIR / "telemetry_metrics.json"
logger = logging.getLogger(__name__)


@dataclass
class TelemetryMetrics:
    peak_concurrency: int = 0
    max_backlog: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "peak_concurrency": self.peak_concurrency,
            "max_backlog": self.max_backlog,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TelemetryMetrics":
        return cls(
            peak_concurrency=int(data.get("peak_concurrency", 0)),
            max_backlog=int(data.get("max_backlog", 0)),
        )


@dataclass
class TelemetryMetricsStore:
    last_24h_date: str
    last_24h: TelemetryMetrics
    since_boot: TelemetryMetrics

    def to_dict(self) -> Dict[str, Any]:
        return {
            "last_24h_date": self.last_24h_date,
            "last_24h": self.last_24h.to_dict(),
            "since_boot": self.since_boot.to_dict(),
        }


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _empty_store() -> TelemetryMetricsStore:
    return TelemetryMetricsStore(
        last_24h_date=_today_key(),
        last_24h=TelemetryMetrics(),
        since_boot=TelemetryMetrics(),
    )


def _load_store() -> TelemetryMetricsStore:
    if METRICS_PATH.exists():
        try:
            with open(METRICS_PATH, "r") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("metrics store must contain a JSON object")
            return TelemetryMetricsStore(
                last_24h_date=data.get("last_24h_date", _today_key()),
                last_24h=TelemetryMetrics.from_dict(data.get("last_24h", {})),
                since_boot=TelemetryMetrics.from_dict(data.get("since_boot", {})),
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Resetting invalid telemetry metrics store %s: %s", METRICS_PATH, exc)

    return _empty_store()


def _save_store(store: TelemetryMetricsStore) -> None:
    settings.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(METRICS_PATH, "w") as f:
        json.dump(store.to_dict(), f, indent=2)


def _roll_daily(store: TelemetryMetricsStore) -> TelemetryMetricsStore:
    today = _today_key()
    if store.last_24h_date != today:
        store.last_24h_date = today
        store.last_24h = TelemetryMetrics()
    return store


def update_concurrency_peak(current: int) -> TelemetryMetricsStore:
    store = _roll_daily(_load_store())
    store.last_24h.peak_concurrency = max(store.last_24h.peak_concurrency, current)
    store.since_boot.peak_concurrency = max(store.since_boot.peak_concurrency, current)
    _save_store(store)
    return store


def update_backlog(current: int) -> TelemetryMetricsStore:
    store = _roll_daily(_load_store())
    store.last_24h.max_backlog = max(store.last_24h.max_backlog, current)
    store.since_boot.max_backlog = max(store.since_boot.max_backlog, current)
    _save_store(store)
    return store


def get_metrics() -> TelemetryMetricsStore:
    return _roll_daily(_load_store())
