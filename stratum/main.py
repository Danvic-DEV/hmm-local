"""HMM-Local Stratum Gateway."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import struct
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    desc,
    delete,
    insert,
    select,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("hmm_local_stratum")

CONFIG_PATH = os.getenv("STRATUM_CONFIG_PATH", "/config/stratum_gateway.json")
RPC_TIMEOUT_SECONDS = float(os.getenv("RPC_TIMEOUT_SECONDS", "8"))
DGB_TEMPLATE_POLL_SECONDS = float(os.getenv("DGB_TEMPLATE_POLL_SECONDS", "3"))
DGB_EXTRANONCE1_SIZE = 4
DGB_EXTRANONCE2_SIZE = 4
DGB_STATIC_DIFFICULTY = float(os.getenv("DGB_STATIC_DIFFICULTY", "4096"))
_CONFIG_DGB_PAYOUT_ADDRESS = os.getenv("DGB_PAYOUT_ADDRESS", "").strip()
PROPOSAL_GUARD_REQUIRED_CONSECUTIVE_PASSES = 1_000_000
STRATUM_DEBUG_SHARES = os.getenv("STRATUM_DEBUG_SHARES", "true").strip().lower() == "true"
STRATUM_COMPAT_ACCEPT_VARIANTS = (
    os.getenv("STRATUM_COMPAT_ACCEPT_VARIANTS", "true").strip().lower() == "true"
)
STRATUM_VERBOSE_VARIANT_LOG = (
    os.getenv("STRATUM_VERBOSE_VARIANT_LOG", "true").strip().lower() == "true"
)
LOW_DIFF_CATASTROPHIC_RATIO = max(
    0.0,
    float(os.getenv("LOW_DIFF_CATASTROPHIC_RATIO", "0.000001")),
)
STALE_JOB_GRACE_SECONDS = int(os.getenv("STALE_JOB_GRACE_SECONDS", "30"))
STALE_JOB_GRACE_COUNT = int(os.getenv("STALE_JOB_GRACE_COUNT", "4"))
HMM_STRATUM_TRACE_DEBUG = os.getenv("HMM_STRATUM_TRACE_DEBUG", "0").strip() == "1"
SHARE_TRACE_GLOBAL_MAXLEN = 5000
SHARE_TRACE_PER_WORKER_MAXLEN = 200
try:
    STRATUM_VERSION_ROLLING_SERVER_MASK = (
        int(os.getenv("STRATUM_VERSION_ROLLING_SERVER_MASK", "0x1fffe000"), 0) & 0xFFFFFFFF
    )
except ValueError:
    STRATUM_VERSION_ROLLING_SERVER_MASK = 0x1FFFE000

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
STRATUM_DB_ENABLED = os.getenv("STRATUM_DB_ENABLED", "true").strip().lower() == "true"
STRATUM_DB_MAX_WRITE_RETRIES = max(0, int(os.getenv("STRATUM_DB_MAX_WRITE_RETRIES", "3")))
STRATUM_DB_BASE_RETRY_SECONDS = max(0.1, float(os.getenv("STRATUM_DB_BASE_RETRY_SECONDS", "0.5")))
STRATUM_DB_MAINTENANCE_SECONDS = max(15.0, float(os.getenv("STRATUM_DB_MAINTENANCE_SECONDS", "60")))
STRATUM_DB_SHARE_RETENTION_DAYS = max(1, int(os.getenv("STRATUM_DB_SHARE_RETENTION_DAYS", "14")))
STRATUM_DB_BLOCK_ATTEMPT_RETENTION_DAYS = max(
    1, int(os.getenv("STRATUM_DB_BLOCK_ATTEMPT_RETENTION_DAYS", "30"))
)
STRATUM_DB_ROLLUP_RETENTION_DAYS = max(1, int(os.getenv("STRATUM_DB_ROLLUP_RETENTION_DAYS", "90")))
STRATUM_DB_ROLLUP_LOOKBACK_MINUTES = max(5, int(os.getenv("STRATUM_DB_ROLLUP_LOOKBACK_MINUTES", "180")))
STRATUM_DB_HASHRATE_RETENTION_DAYS = max(1, int(os.getenv("STRATUM_DB_HASHRATE_RETENTION_DAYS", "30")))
STRATUM_DB_NETWORK_RETENTION_DAYS = max(1, int(os.getenv("STRATUM_DB_NETWORK_RETENTION_DAYS", "30")))
STRATUM_DB_KPI_RETENTION_DAYS = max(1, int(os.getenv("STRATUM_DB_KPI_RETENTION_DAYS", "30")))
STRATUM_DB_SPOOL_PATH = os.getenv("STRATUM_DB_SPOOL_PATH", "/config/logs/stratum_db_spool.jsonl")
STRATUM_DB_WORKER_EVENT_RETENTION_DAYS = max(
    1, int(os.getenv("STRATUM_DB_WORKER_EVENT_RETENTION_DAYS", "30"))
)

# Vardiff controller (target steady accepted share cadence).
VARDIFF_ENABLED = True
VARDIFF_TARGET_MIN_SHARES_PER_MIN = 2.0
VARDIFF_TARGET_MAX_SHARES_PER_MIN = 3.0
VARDIFF_TARGET_MID_SHARES_PER_MIN = (
    VARDIFF_TARGET_MIN_SHARES_PER_MIN + VARDIFF_TARGET_MAX_SHARES_PER_MIN
) / 2.0
VARDIFF_WINDOW_SECONDS = 180
VARDIFF_RETARGET_INTERVAL_SECONDS = max(
    10, int(os.getenv("VARDIFF_RETARGET_INTERVAL_SECONDS", "30"))
)
VARDIFF_STEP_UP_MAX_FACTOR = max(1.0, float(os.getenv("VARDIFF_STEP_UP_MAX_FACTOR", "1.6")))
VARDIFF_STEP_UP_BURST_FACTOR = max(
    VARDIFF_STEP_UP_MAX_FACTOR,
    float(os.getenv("VARDIFF_STEP_UP_BURST_FACTOR", "2.0")),
)
VARDIFF_BURST_THRESHOLD_MULTIPLIER = max(
    1.0,
    float(os.getenv("VARDIFF_BURST_THRESHOLD_MULTIPLIER", "2.0")),
)
VARDIFF_STEP_DOWN_MIN_FACTOR = min(
    1.0,
    max(0.1, float(os.getenv("VARDIFF_STEP_DOWN_MIN_FACTOR", "0.7"))),
)
VARDIFF_MIN_DIFFICULTY = 128.0
VARDIFF_MAX_DIFFICULTY = 65536.0
VARDIFF_MIN_ACCEPTED_SHARES_BEFORE_RETARGET = max(
    1,
    int(os.getenv("VARDIFF_MIN_ACCEPTED_SHARES_BEFORE_RETARGET", "4")),
)
VARDIFF_MIN_WARMUP_SECONDS = max(0, int(os.getenv("VARDIFF_MIN_WARMUP_SECONDS", "45")))

# Bitcoin diff1 target (big-endian human form)
DIFF1_TARGET_HEX = "00000000ffff0000000000000000000000000000000000000000000000000000"
DIFF1_TARGET_INT = int.from_bytes(bytes.fromhex(DIFF1_TARGET_HEX), "big")

# Hard safety assertions to prevent broken target constants.
assert DIFF1_TARGET_INT.bit_length() > 200
assert ("0x" + f"{DIFF1_TARGET_INT:064x}").startswith("0xffff0000") is False
assert DIFF1_TARGET_INT == int("00000000ffff0000000000000000000000000000000000000000000000000000", 16)

# Keep legacy name used by older code paths.
TARGET_1 = DIFF1_TARGET_INT


@dataclass
class CoinConfig:
    coin: str
    algo: str
    stratum_port: int
    rpc_url: str
    rpc_user: str
    rpc_password: str


@dataclass
class CoinRuntimeStats:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    connected_workers: int = 0
    total_connections: int = 0
    shares_submitted: int = 0
    shares_accepted: int = 0
    shares_rejected: int = 0
    last_share_at: str | None = None
    current_job_id: str | None = None
    chain_height: int | None = None
    template_height: int | None = None
    last_template_at: str | None = None
    rpc_last_ok_at: str | None = None
    rpc_last_error: str | None = None
    share_reject_reasons: dict[str, int] = field(default_factory=dict)
    duplicate_shares_acknowledged: int = 0
    catastrophic_low_diff_rejects: int = 0
    last_catastrophic_low_diff_at: str | None = None
    last_catastrophic_low_diff_worker: str | None = None
    block_candidates: int = 0
    blocks_accepted: int = 0
    blocks_rejected: int = 0
    last_block_submit_result: str | None = None
    best_share_difficulty: float | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "connected_workers": self.connected_workers,
            "total_connections": self.total_connections,
            "shares_submitted": self.shares_submitted,
            "shares_accepted": self.shares_accepted,
            "shares_rejected": self.shares_rejected,
            "last_share_at": self.last_share_at,
            "current_job_id": self.current_job_id,
            "chain_height": self.chain_height,
            "template_height": self.template_height,
            "last_template_at": self.last_template_at,
            "rpc_last_ok_at": self.rpc_last_ok_at,
            "rpc_last_error": self.rpc_last_error,
            "share_reject_reasons": self.share_reject_reasons,
            "duplicate_shares_acknowledged": self.duplicate_shares_acknowledged,
            "catastrophic_low_diff_rejects": self.catastrophic_low_diff_rejects,
            "last_catastrophic_low_diff_at": self.last_catastrophic_low_diff_at,
            "last_catastrophic_low_diff_worker": self.last_catastrophic_low_diff_worker,
            "block_candidates": self.block_candidates,
            "blocks_accepted": self.blocks_accepted,
            "blocks_rejected": self.blocks_rejected,
            "last_block_submit_result": self.last_block_submit_result,
            "best_share_difficulty": self.best_share_difficulty,
        }


@dataclass
class ProposalGuardState:
    required_consecutive_passes: int
    total_checks: int = 0
    total_passes: int = 0
    total_failures: int = 0
    consecutive_passes: int = 0
    submit_enabled: bool = False
    last_check_at: str | None = None
    last_result: str | None = None
    last_failure_reason: str | None = None
    last_template_height: int | None = None

    def mark_pass(self, template_height: int | None) -> None:
        self.total_checks += 1
        self.total_passes += 1
        self.consecutive_passes += 1
        self.last_check_at = datetime.now(timezone.utc).isoformat()
        self.last_result = "pass"
        self.last_template_height = template_height
        if self.consecutive_passes >= self.required_consecutive_passes:
            self.submit_enabled = True
            self.last_failure_reason = None

    def mark_fail(self, reason: str, template_height: int | None) -> None:
        self.total_checks += 1
        self.total_failures += 1
        self.consecutive_passes = 0
        self.submit_enabled = False
        self.last_check_at = datetime.now(timezone.utc).isoformat()
        self.last_result = "fail"
        self.last_failure_reason = reason
        self.last_template_height = template_height

    def snapshot(self) -> dict[str, Any]:
        return {
            "required_consecutive_passes": self.required_consecutive_passes,
            "total_checks": self.total_checks,
            "total_passes": self.total_passes,
            "total_failures": self.total_failures,
            "consecutive_passes": self.consecutive_passes,
            "submit_enabled": self.submit_enabled,
            "last_check_at": self.last_check_at,
            "last_result": self.last_result,
            "last_failure_reason": self.last_failure_reason,
            "last_template_height": self.last_template_height,
            "remaining_passes_to_enable": max(
                self.required_consecutive_passes - self.consecutive_passes,
                0,
            ),
        }


@dataclass
class ClientSession:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    peer: str | None = None
    subscribed: bool = False
    authorized: bool = False
    worker_name: str | None = None
    extranonce1: str | None = None
    difficulty: float = DGB_STATIC_DIFFICULTY
    version_mask: int = 0
    accepted_share_times: deque[float] = field(default_factory=deque)
    last_vardiff_adjust_at: float = 0.0
    first_accepted_share_at: float = 0.0


@dataclass
class ActiveJob:
    job_id: str
    prevhash: str
    prevhash_be: str
    coinb1: str
    coinb2: str
    coinb1_txid: str
    coinb2_txid: str
    merkle_branch: list[str]
    version: str
    nbits: str
    ntime: str
    clean_jobs: bool
    template_height: int | None = None
    target_1: int = TARGET_1
    tx_datas: list[str] = field(default_factory=list)

    def notify_params(self) -> list[Any]:
        return [
            self.job_id,
            self.prevhash,
            self.coinb1,
            self.coinb2,
            self.merkle_branch,
            self.version,
            self.nbits,
            self.ntime,
            self.clean_jobs,
        ]


@dataclass
class ShareTrace:
    cid: str
    ts: str
    coin: str
    worker: str
    job_id: str
    ex1: str
    ex2: str
    ntime: str
    nonce: str
    base_version: str | None = None
    submitted_version: str | None = None
    version_mask: str | None = None
    final_version: str | None = None
    prevhash: str | None = None
    merkle_root: str | None = None
    nbits: str | None = None
    ntime_job: str | None = None
    header_hex: str | None = None
    hash_hex: str | None = None
    hash_int_big: int | None = None
    hash_int_little: int | None = None
    assigned_diff: float | None = None
    share_target_int: int | None = None
    share_target_hex: str | None = None
    computed_diff: float | None = None
    meets_target: bool | None = None
    meets_network: bool | None = None
    reject_reason: str | None = None
    stale: bool = False
    server_response_time_ms: float | None = None
    stored: bool = False

    def to_summary(self) -> dict[str, Any]:
        return {
            "cid": self.cid,
            "ts": self.ts,
            "coin": self.coin,
            "worker": self.worker,
            "job_id": self.job_id,
            "ex1": self.ex1,
            "ex2": self.ex2,
            "ntime": self.ntime,
            "nonce": self.nonce,
            "base_version": self.base_version,
            "submitted_version": self.submitted_version,
            "version_mask": self.version_mask,
            "final_version": self.final_version,
            "hash_hex": self.hash_hex,
            "assigned_diff": self.assigned_diff,
            "computed_diff": self.computed_diff,
            "meets_target": self.meets_target,
            "meets_network": self.meets_network,
            "reject_reason": self.reject_reason,
            "stale": self.stale,
            "server_response_time_ms": self.server_response_time_ms,
        }


class StratumDataStore:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.enabled = bool(database_url) and STRATUM_DB_ENABLED
        self.engine = None
        self.metadata = MetaData()
        self.queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=20000)
        self.worker_task: asyncio.Task | None = None
        self.maintenance_task: asyncio.Task | None = None
        self.max_batch_size = 200
        self.max_write_retries = STRATUM_DB_MAX_WRITE_RETRIES
        self.base_retry_seconds = STRATUM_DB_BASE_RETRY_SECONDS
        self.maintenance_interval_seconds = STRATUM_DB_MAINTENANCE_SECONDS
        self.share_retention_days = STRATUM_DB_SHARE_RETENTION_DAYS
        self.block_attempt_retention_days = STRATUM_DB_BLOCK_ATTEMPT_RETENTION_DAYS
        self.rollup_retention_days = STRATUM_DB_ROLLUP_RETENTION_DAYS
        self.rollup_lookback_minutes = STRATUM_DB_ROLLUP_LOOKBACK_MINUTES
        self.hashrate_retention_days = STRATUM_DB_HASHRATE_RETENTION_DAYS
        self.network_retention_days = STRATUM_DB_NETWORK_RETENTION_DAYS
        self.kpi_retention_days = STRATUM_DB_KPI_RETENTION_DAYS
        self.worker_event_retention_days = STRATUM_DB_WORKER_EVENT_RETENTION_DAYS
        self.spool_path = Path(STRATUM_DB_SPOOL_PATH)
        self.total_enqueued = 0
        self.total_dropped = 0
        self.total_write_batches_ok = 0
        self.total_write_batches_failed = 0
        self.total_rows_written = 0
        self.total_retries = 0
        self.last_write_error: str | None = None
        self.last_write_ok_at: str | None = None
        self.last_write_latency_ms: float | None = None
        self.consecutive_write_failures = 0
        self.max_queue_depth_seen = 0
        self.total_spooled_rows = 0
        self.total_replayed_rows = 0
        self.db_pool_in_use_peak_since_boot = 0
        self.db_pool_wait_count_since_boot = 0
        self.db_pool_wait_seconds_sum_since_boot = 0.0
        self.db_active_queries_peak_since_boot = 0
        self.db_slow_queries_peak_since_boot = 0

        self.schema_meta = Table(
            "stratum_schema_meta",
            self.metadata,
            Column("key", String(64), primary_key=True),
            Column("value", String(255), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
        )
        self.share_metrics = Table(
            "stratum_share_metrics",
            self.metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("ts", DateTime(timezone=True), nullable=False),
            Column("coin", String(16), nullable=False),
            Column("worker", String(255), nullable=False),
            Column("job_id", String(64), nullable=False),
            Column("assigned_diff", Float, nullable=False),
            Column("computed_diff", Float, nullable=False),
            Column("accepted", Boolean, nullable=False),
            Column("reject_reason", String(128), nullable=True),
        )
        self.block_attempts = Table(
            "stratum_block_attempts",
            self.metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("ts", DateTime(timezone=True), nullable=False),
            Column("coin", String(16), nullable=False),
            Column("worker", String(255), nullable=True),
            Column("job_id", String(64), nullable=False),
            Column("template_height", Integer, nullable=True),
            Column("block_hash", String(128), nullable=False),
            Column("accepted_by_node", Boolean, nullable=False),
            Column("submit_result_raw", Text, nullable=True),
            Column("reject_reason", String(128), nullable=True),
            Column("reject_category", String(64), nullable=True),
            Column("rpc_error", Text, nullable=True),
            Column("latency_ms", Float, nullable=True),
            Column("extra", JSON, nullable=True),
        )
        self.worker_events = Table(
            "stratum_worker_events",
            self.metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("ts", DateTime(timezone=True), nullable=False),
            Column("session_id", String(64), nullable=True),
            Column("coin", String(16), nullable=False),
            Column("worker", String(255), nullable=False),
            Column("event", String(64), nullable=False),
            Column("job_id", String(64), nullable=True),
            Column("peer", String(128), nullable=True),
            Column("difficulty", Float, nullable=True),
            Column("details", JSON, nullable=True),
        )
        self.share_rollups_1m = Table(
            "stratum_share_rollups_1m",
            self.metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("bucket_ts", DateTime(timezone=True), nullable=False),
            Column("coin", String(16), nullable=False),
            Column("worker", String(255), nullable=False),
            Column("accepted_count", Integer, nullable=False),
            Column("rejected_count", Integer, nullable=False),
            Column("low_diff_reject_count", Integer, nullable=False),
            Column("duplicate_reject_count", Integer, nullable=False),
            Column("avg_assigned_diff", Float, nullable=False),
            Column("avg_computed_diff", Float, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
        )
        self.hashrate_snapshots = Table(
            "stratum_hashrate_snapshots",
            self.metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("ts", DateTime(timezone=True), nullable=False),
            Column("coin", String(16), nullable=False),
            Column("worker", String(255), nullable=False),
            Column("window_minutes", Integer, nullable=False),
            Column("accepted_shares", Integer, nullable=False),
            Column("accepted_diff_sum", Float, nullable=False),
            Column("est_hashrate_hs", Float, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
        )
        self.network_snapshots = Table(
            "stratum_network_snapshots",
            self.metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("ts", DateTime(timezone=True), nullable=False),
            Column("coin", String(16), nullable=False),
            Column("chain_height", Integer, nullable=True),
            Column("template_height", Integer, nullable=True),
            Column("job_id", String(64), nullable=True),
            Column("bits", String(32), nullable=True),
            Column("network_target", String(80), nullable=True),
            Column("network_difficulty", Float, nullable=True),
            Column("network_hash_ps", Float, nullable=True),
            Column("template_previous_blockhash", String(128), nullable=True),
            Column("template_curtime", Integer, nullable=True),
            Column("template_changed", Boolean, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
        )
        self.kpi_snapshots = Table(
            "stratum_kpi_snapshots",
            self.metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("ts", DateTime(timezone=True), nullable=False),
            Column("coin", String(16), nullable=False),
            Column("window_minutes", Integer, nullable=False),
            Column("pool_hashrate_hs", Float, nullable=True),
            Column("network_hash_ps", Float, nullable=True),
            Column("network_difficulty", Float, nullable=True),
            Column("share_accept_count", Integer, nullable=False),
            Column("share_reject_count", Integer, nullable=False),
            Column("share_reject_rate_pct", Float, nullable=True),
            Column("block_accept_count_24h", Integer, nullable=False),
            Column("block_reject_count_24h", Integer, nullable=False),
            Column("block_accept_rate_pct_24h", Float, nullable=True),
            Column("expected_time_to_block_sec", Float, nullable=True),
            Column("pool_share_of_network_pct", Float, nullable=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
        )

    async def start(self) -> None:
        if not self.enabled:
            logger.info("stratum datastore disabled")
            return
        try:
            self.engine = create_engine(self.database_url, future=True, pool_pre_ping=True)
            await asyncio.to_thread(self._init_schema_sync)
            self.worker_task = asyncio.create_task(self._writer_loop())
            self.maintenance_task = asyncio.create_task(self._maintenance_loop())
            logger.info("stratum datastore enabled")
        except Exception as exc:
            self.enabled = False
            logger.error("stratum datastore init failed: %s", exc)

    async def stop(self) -> None:
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
            self.worker_task = None
        if self.maintenance_task:
            self.maintenance_task.cancel()
            try:
                await self.maintenance_task
            except asyncio.CancelledError:
                pass
            self.maintenance_task = None
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None

    async def enqueue_share_metric(self, row: dict[str, Any]) -> None:
        if not self.enabled:
            return
        await self._enqueue("share", row)

    async def enqueue_block_attempt(self, row: dict[str, Any]) -> None:
        if not self.enabled:
            return
        await self._enqueue("block", row)

    async def enqueue_worker_event(self, row: dict[str, Any]) -> None:
        if not self.enabled:
            return
        await self._enqueue("worker_event", row)

    async def enqueue_network_snapshot(self, row: dict[str, Any]) -> None:
        if not self.enabled:
            return
        await self._enqueue("network_snapshot", row)

    async def reconcile_worker_identity(self, session_id: str, worker: str) -> int:
        if not self.enabled or self.engine is None:
            return 0
        return await asyncio.to_thread(self._reconcile_worker_identity_sync, session_id, worker)

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "queue_depth": self.queue.qsize(),
            "max_queue_depth_seen": self.max_queue_depth_seen,
            "total_enqueued": self.total_enqueued,
            "total_dropped": self.total_dropped,
            "total_write_batches_ok": self.total_write_batches_ok,
            "total_write_batches_failed": self.total_write_batches_failed,
            "total_rows_written": self.total_rows_written,
            "total_retries": self.total_retries,
            "consecutive_write_failures": self.consecutive_write_failures,
            "last_write_ok_at": self.last_write_ok_at,
            "last_write_latency_ms": self.last_write_latency_ms,
            "last_write_error": self.last_write_error,
            "total_spooled_rows": self.total_spooled_rows,
            "total_replayed_rows": self.total_replayed_rows,
            "hashrate_retention_days": self.hashrate_retention_days,
            "network_retention_days": self.network_retention_days,
            "kpi_retention_days": self.kpi_retention_days,
            "spool_path": str(self.spool_path),
        }

    async def get_database_health_snapshot(self) -> dict[str, Any]:
        """Return database health payload for external operational dashboards."""
        return await asyncio.to_thread(self.get_database_health_snapshot_sync)

    def get_database_health_snapshot_sync(self) -> dict[str, Any]:
        """Synchronous DB health snapshot (safe for use via asyncio.to_thread)."""
        now = datetime.now(timezone.utc)
        database_type = "postgresql" if "postgresql" in self.database_url.lower() else "unknown"

        pool_size = None
        pool_checked_out = None
        pool_overflow = None
        max_size_configured = None
        max_overflow_configured = None
        max_capacity_configured = None
        utilization_percent = None

        active_connections = None
        database_size_mb = None
        long_running_queries = None

        if not self.enabled:
            return {
                "status": "warning",
                "database_type": "disabled",
                "pool": {
                    "size": 0,
                    "checked_out": 0,
                    "overflow": 0,
                    "total_capacity": 0,
                    "max_size_configured": 0,
                    "max_overflow_configured": 0,
                    "max_capacity_configured": 0,
                    "utilization_percent": 0.0,
                },
                "postgresql": {
                    "active_connections": 0,
                    "database_size_mb": 0.0,
                    "long_running_queries": 0,
                },
                "high_water_marks": {
                    "last_24h": {
                        "db_pool_in_use_peak": self.db_pool_in_use_peak_since_boot,
                        "db_pool_wait_count": self.db_pool_wait_count_since_boot,
                        "db_pool_wait_seconds_sum": self.db_pool_wait_seconds_sum_since_boot,
                        "active_queries_peak": self.db_active_queries_peak_since_boot,
                        "slow_queries_peak": self.db_slow_queries_peak_since_boot,
                    },
                    "since_boot": {
                        "db_pool_in_use_peak": self.db_pool_in_use_peak_since_boot,
                        "db_pool_wait_count": self.db_pool_wait_count_since_boot,
                        "db_pool_wait_seconds_sum": self.db_pool_wait_seconds_sum_since_boot,
                        "active_queries_peak": self.db_active_queries_peak_since_boot,
                        "slow_queries_peak": self.db_slow_queries_peak_since_boot,
                    },
                    "last_24h_date": now.date().isoformat(),
                },
            }

        if self.engine is not None:
            pool = getattr(self.engine, "pool", None)
            if pool is not None:
                try:
                    pool_size = int(pool.size())
                    pool_checked_out = int(pool.checkedout())
                    pool_overflow = int(pool.overflow())
                    max_size_configured = int(getattr(pool, "_pool", None).maxsize) if getattr(pool, "_pool", None) else pool_size
                    max_overflow_configured = int(getattr(pool, "_max_overflow", 0))
                    max_capacity_configured = (
                        max_size_configured + max_overflow_configured
                        if max_overflow_configured >= 0
                        else max_size_configured
                    )
                    if max_capacity_configured > 0:
                        utilization_percent = (pool_checked_out / max_capacity_configured) * 100.0
                    self.db_pool_in_use_peak_since_boot = max(self.db_pool_in_use_peak_since_boot, pool_checked_out)
                except Exception as exc:
                    logger.debug("Failed to compute DB pool health: %s", exc)

            if "postgresql" in self.database_url.lower():
                try:
                    with self.engine.connect() as conn:
                        active_connections = int(
                            conn.exec_driver_sql(
                                "SELECT COUNT(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'active'"
                            ).scalar()
                            or 0
                        )
                        long_running_queries = int(
                            conn.exec_driver_sql(
                                "SELECT COUNT(*) FROM pg_stat_activity WHERE datname = current_database() "
                                "AND state <> 'idle' AND now() - query_start > interval '1 minute'"
                            ).scalar()
                            or 0
                        )
                        database_size_bytes = conn.exec_driver_sql(
                            "SELECT pg_database_size(current_database())"
                        ).scalar()
                        if database_size_bytes is not None:
                            database_size_mb = float(database_size_bytes) / (1024.0 * 1024.0)

                    self.db_active_queries_peak_since_boot = max(
                        self.db_active_queries_peak_since_boot,
                        active_connections,
                    )
                    self.db_slow_queries_peak_since_boot = max(
                        self.db_slow_queries_peak_since_boot,
                        long_running_queries,
                    )
                except Exception as exc:
                    logger.debug("Failed to query PostgreSQL health metrics: %s", exc)

        status = "healthy"
        if self.consecutive_write_failures >= 3:
            status = "critical"
        elif self.consecutive_write_failures > 0 or self.total_write_batches_failed > 0:
            status = "warning"

        return {
            "status": status,
            "database_type": database_type,
            "pool": {
                "size": pool_size,
                "checked_out": pool_checked_out,
                "overflow": pool_overflow,
                "total_capacity": max_capacity_configured,
                "max_size_configured": max_size_configured,
                "max_overflow_configured": max_overflow_configured,
                "max_capacity_configured": max_capacity_configured,
                "utilization_percent": utilization_percent,
            },
            "postgresql": {
                "active_connections": active_connections,
                "database_size_mb": database_size_mb,
                "long_running_queries": long_running_queries,
            },
            "high_water_marks": {
                "last_24h": {
                    "db_pool_in_use_peak": self.db_pool_in_use_peak_since_boot,
                    "db_pool_wait_count": self.db_pool_wait_count_since_boot,
                    "db_pool_wait_seconds_sum": self.db_pool_wait_seconds_sum_since_boot,
                    "active_queries_peak": self.db_active_queries_peak_since_boot,
                    "slow_queries_peak": self.db_slow_queries_peak_since_boot,
                },
                "since_boot": {
                    "db_pool_in_use_peak": self.db_pool_in_use_peak_since_boot,
                    "db_pool_wait_count": self.db_pool_wait_count_since_boot,
                    "db_pool_wait_seconds_sum": self.db_pool_wait_seconds_sum_since_boot,
                    "active_queries_peak": self.db_active_queries_peak_since_boot,
                    "slow_queries_peak": self.db_slow_queries_peak_since_boot,
                },
                "last_24h_date": now.date().isoformat(),
            },
        }

    async def _enqueue(self, kind: str, row: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait((kind, row))
            self.total_enqueued += 1
            qsize = self.queue.qsize()
            if qsize > self.max_queue_depth_seen:
                self.max_queue_depth_seen = qsize
        except asyncio.QueueFull:
            self.total_dropped += 1
            logger.warning("stratum datastore queue full; dropping %s row", kind)

    async def _writer_loop(self) -> None:
        assert self.engine is not None
        while True:
            kind, row = await self.queue.get()
            batch: list[tuple[str, dict[str, Any]]] = [(kind, row)]
            try:
                while len(batch) < self.max_batch_size:
                    batch.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                pass

            await self._write_batch_with_retry(batch)

    async def _write_batch_with_retry(self, batch: list[tuple[str, dict[str, Any]]]) -> None:
        attempts = self.max_write_retries + 1
        last_exc: Exception | None = None
        started = time.perf_counter()

        for attempt in range(1, attempts + 1):
            try:
                await asyncio.to_thread(self._write_batch_sync, batch)
                self.total_write_batches_ok += 1
                self.total_rows_written += len(batch)
                self.last_write_error = None
                self.last_write_ok_at = datetime.now(timezone.utc).isoformat()
                self.last_write_latency_ms = (time.perf_counter() - started) * 1000.0
                self.consecutive_write_failures = 0
                return
            except Exception as exc:
                last_exc = exc
                self.total_write_batches_failed += 1
                self.consecutive_write_failures += 1
                self.last_write_error = str(exc)

                if attempt < attempts:
                    self.total_retries += 1
                    delay = self.base_retry_seconds * (2 ** (attempt - 1))
                    logger.warning(
                        "stratum datastore write retry %s/%s in %.2fs: %s",
                        attempt,
                        attempts - 1,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)

        logger.error("stratum datastore write failed after retries: %s", last_exc)
        await asyncio.to_thread(self._spool_failed_batch_sync, batch, str(last_exc) if last_exc else "unknown")

    def _write_batch_sync(self, batch: list[tuple[str, dict[str, Any]]]) -> None:
        if self.engine is None:
            raise RuntimeError("datastore engine not initialized")

        share_rows = [r for k, r in batch if k == "share"]
        block_rows = [r for k, r in batch if k == "block"]
        worker_event_rows = [r for k, r in batch if k == "worker_event"]
        network_snapshot_rows = [r for k, r in batch if k == "network_snapshot"]
        if not share_rows and not block_rows and not worker_event_rows and not network_snapshot_rows:
            return

        with self.engine.begin() as conn:
            if share_rows:
                conn.execute(self.share_metrics.insert(), share_rows)
            if block_rows:
                conn.execute(self.block_attempts.insert(), block_rows)
            if worker_event_rows:
                conn.execute(self.worker_events.insert(), worker_event_rows)
            if network_snapshot_rows:
                conn.execute(self.network_snapshots.insert(), network_snapshot_rows)

    def _spool_failed_batch_sync(self, batch: list[tuple[str, dict[str, Any]]], error: str) -> None:
        self.spool_path.parent.mkdir(parents=True, exist_ok=True)
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.spool_path.open("a", encoding="utf-8") as f:
            for kind, row in batch:
                payload = {
                    "spooled_at": now_iso,
                    "kind": kind,
                    "error": error,
                    "row": self._json_safe_row(row),
                }
                f.write(json.dumps(payload) + "\n")
        self.total_spooled_rows += len(batch)

    @staticmethod
    def _json_safe_row(row: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, datetime):
                out[key] = value.isoformat()
            else:
                out[key] = value
        return out

    @staticmethod
    def _row_from_json_safe(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        for key in ("ts", "created_at", "bucket_ts"):
            value = out.get(key)
            if isinstance(value, str):
                try:
                    out[key] = datetime.fromisoformat(value)
                except ValueError:
                    pass
        return out

    def _init_schema_sync(self) -> None:
        if self.engine is None:
            raise RuntimeError("datastore engine not initialized")
        with self.engine.begin() as conn:
            self.metadata.create_all(conn)
            # Backfill schema for existing DBs created before reject_category existed.
            try:
                conn.exec_driver_sql(
                    "ALTER TABLE stratum_block_attempts ADD COLUMN IF NOT EXISTS reject_category VARCHAR(64)"
                )
            except Exception:
                pass
            try:
                conn.exec_driver_sql(
                    "ALTER TABLE stratum_worker_events ADD COLUMN IF NOT EXISTS session_id VARCHAR(64)"
                )
            except Exception:
                pass
            conn.execute(delete(self.schema_meta).where(self.schema_meta.c.key == "schema_version"))
            conn.execute(
                insert(self.schema_meta)
                .values(
                    key="schema_version",
                    value="5",
                    updated_at=datetime.now(timezone.utc),
                )
            )
        self._ensure_indexes_sync()

    def _ensure_indexes_sync(self) -> None:
        if self.engine is None:
            raise RuntimeError("datastore engine not initialized")
        stmts = [
            "CREATE INDEX IF NOT EXISTS idx_stratum_share_metrics_ts ON stratum_share_metrics (ts)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_share_metrics_coin ON stratum_share_metrics (coin)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_share_metrics_worker ON stratum_share_metrics (worker)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_share_metrics_job_id ON stratum_share_metrics (job_id)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_share_metrics_accepted ON stratum_share_metrics (accepted)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_share_metrics_coin_worker_ts ON stratum_share_metrics (coin, worker, ts)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_block_attempts_ts ON stratum_block_attempts (ts)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_block_attempts_coin ON stratum_block_attempts (coin)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_block_attempts_worker ON stratum_block_attempts (worker)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_block_attempts_template_height ON stratum_block_attempts (template_height)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_block_attempts_accepted ON stratum_block_attempts (accepted_by_node)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_worker_events_ts ON stratum_worker_events (ts)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_worker_events_session_id ON stratum_worker_events (session_id)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_worker_events_coin ON stratum_worker_events (coin)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_worker_events_worker ON stratum_worker_events (worker)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_worker_events_event ON stratum_worker_events (event)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_worker_events_coin_worker_ts ON stratum_worker_events (coin, worker, ts)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_rollups_bucket_coin_worker ON stratum_share_rollups_1m (bucket_ts, coin, worker)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_hashrate_ts ON stratum_hashrate_snapshots (ts)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_hashrate_coin_worker_window ON stratum_hashrate_snapshots (coin, worker, window_minutes, ts)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_network_ts ON stratum_network_snapshots (ts)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_network_coin_ts ON stratum_network_snapshots (coin, ts)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_network_template_height ON stratum_network_snapshots (template_height)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_kpi_ts ON stratum_kpi_snapshots (ts)",
            "CREATE INDEX IF NOT EXISTS idx_stratum_kpi_coin_window_ts ON stratum_kpi_snapshots (coin, window_minutes, ts)",
        ]
        with self.engine.begin() as conn:
            for stmt in stmts:
                conn.exec_driver_sql(stmt)

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._run_maintenance_sync)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("stratum datastore maintenance failed: %s", exc)
            await asyncio.sleep(self.maintenance_interval_seconds)

    def _run_maintenance_sync(self) -> None:
        if self.engine is None:
            return
        self._cleanup_retention_sync()
        self._refresh_hashrate_snapshots_sync()
        self._refresh_kpi_snapshots_sync()
        self._refresh_rollups_sync()
        self._replay_spooled_rows_sync(max_rows=1000)

    def _cleanup_retention_sync(self) -> None:
        if self.engine is None:
            return
        now = datetime.now(timezone.utc)
        share_cutoff = now - timedelta(days=self.share_retention_days)
        block_cutoff = now - timedelta(days=self.block_attempt_retention_days)
        worker_event_cutoff = now - timedelta(days=self.worker_event_retention_days)
        rollup_cutoff = now - timedelta(days=self.rollup_retention_days)
        hashrate_cutoff = now - timedelta(days=self.hashrate_retention_days)
        network_cutoff = now - timedelta(days=self.network_retention_days)
        kpi_cutoff = now - timedelta(days=self.kpi_retention_days)
        with self.engine.begin() as conn:
            conn.execute(delete(self.share_metrics).where(self.share_metrics.c.ts < share_cutoff))
            conn.execute(delete(self.block_attempts).where(self.block_attempts.c.ts < block_cutoff))
            conn.execute(delete(self.worker_events).where(self.worker_events.c.ts < worker_event_cutoff))
            conn.execute(delete(self.share_rollups_1m).where(self.share_rollups_1m.c.bucket_ts < rollup_cutoff))
            conn.execute(delete(self.hashrate_snapshots).where(self.hashrate_snapshots.c.ts < hashrate_cutoff))
            conn.execute(delete(self.network_snapshots).where(self.network_snapshots.c.ts < network_cutoff))
            conn.execute(delete(self.kpi_snapshots).where(self.kpi_snapshots.c.ts < kpi_cutoff))

    def _refresh_hashrate_snapshots_sync(self) -> None:
        if self.engine is None:
            return

        windows = [1, 5, 15]
        now = datetime.now(timezone.utc)
        max_window = max(windows)
        lookback_start = now - timedelta(minutes=max_window)

        with self.engine.begin() as conn:
            rows = conn.execute(
                select(self.share_metrics)
                .where(self.share_metrics.c.ts >= lookback_start)
                .where(self.share_metrics.c.accepted == True)
            ).mappings().all()

            # Replace current snapshot minute for deterministic reads.
            minute_ts = now.replace(second=0, microsecond=0)
            conn.execute(delete(self.hashrate_snapshots).where(self.hashrate_snapshots.c.ts == minute_ts))

            snapshots: list[dict[str, Any]] = []
            for window in windows:
                cutoff = now - timedelta(minutes=window)
                per_worker: dict[tuple[str, str], dict[str, float]] = {}
                for row in rows:
                    ts = row.get("ts")
                    if not isinstance(ts, datetime) or ts < cutoff:
                        continue
                    coin = str(row.get("coin") or "")
                    worker = str(row.get("worker") or "unknown")
                    key = (coin, worker)
                    agg = per_worker.get(key)
                    if agg is None:
                        agg = {"shares": 0.0, "diff_sum": 0.0}
                        per_worker[key] = agg
                    agg["shares"] += 1.0
                    # Use assigned difficulty for stable hashrate estimation.
                    # Computed difficulty can spike heavily on lucky shares and
                    # can overstate short-window hashrate/KPI percentages.
                    agg["diff_sum"] += float(row.get("assigned_diff") or 0.0)

                # worker snapshots + per-coin pool aggregate
                per_coin_pool: dict[str, dict[str, float]] = {}
                for (coin, worker), agg in per_worker.items():
                    window_seconds = float(window) * 60.0
                    est_hashrate_hs = (agg["diff_sum"] * (2 ** 32)) / max(window_seconds, 1.0)
                    snapshots.append(
                        {
                            "ts": minute_ts,
                            "coin": coin,
                            "worker": worker,
                            "window_minutes": int(window),
                            "accepted_shares": int(agg["shares"]),
                            "accepted_diff_sum": float(agg["diff_sum"]),
                            "est_hashrate_hs": float(est_hashrate_hs),
                            "created_at": now,
                        }
                    )
                    coin_pool = per_coin_pool.get(coin)
                    if coin_pool is None:
                        coin_pool = {"shares": 0.0, "diff_sum": 0.0}
                        per_coin_pool[coin] = coin_pool
                    coin_pool["shares"] += agg["shares"]
                    coin_pool["diff_sum"] += agg["diff_sum"]

                for coin, agg in per_coin_pool.items():
                    window_seconds = float(window) * 60.0
                    est_hashrate_hs = (agg["diff_sum"] * (2 ** 32)) / max(window_seconds, 1.0)
                    snapshots.append(
                        {
                            "ts": minute_ts,
                            "coin": coin,
                            "worker": "__pool__",
                            "window_minutes": int(window),
                            "accepted_shares": int(agg["shares"]),
                            "accepted_diff_sum": float(agg["diff_sum"]),
                            "est_hashrate_hs": float(est_hashrate_hs),
                            "created_at": now,
                        }
                    )

            if snapshots:
                conn.execute(self.hashrate_snapshots.insert(), snapshots)

    def _refresh_kpi_snapshots_sync(self) -> None:
        if self.engine is None:
            return

        now = datetime.now(timezone.utc)
        minute_ts = now.replace(second=0, microsecond=0)
        window_minutes = 15
        share_cutoff = now - timedelta(minutes=window_minutes)
        block_cutoff = now - timedelta(hours=24)

        with self.engine.begin() as conn:
            conn.execute(delete(self.kpi_snapshots).where(self.kpi_snapshots.c.ts == minute_ts))

            share_rows = conn.execute(
                select(self.share_metrics).where(self.share_metrics.c.ts >= share_cutoff)
            ).mappings().all()
            block_rows = conn.execute(
                select(self.block_attempts).where(self.block_attempts.c.ts >= block_cutoff)
            ).mappings().all()
            hashrate_rows = conn.execute(
                select(self.hashrate_snapshots)
                .where(self.hashrate_snapshots.c.worker == "__pool__")
                .where(self.hashrate_snapshots.c.window_minutes == window_minutes)
                .where(self.hashrate_snapshots.c.ts >= now - timedelta(hours=1))
            ).mappings().all()
            network_rows = conn.execute(
                select(self.network_snapshots).where(self.network_snapshots.c.ts >= now - timedelta(hours=2))
            ).mappings().all()

            coins: set[str] = set()
            for row in share_rows:
                coins.add(str(row.get("coin") or ""))
            for row in network_rows:
                coins.add(str(row.get("coin") or ""))
            for row in hashrate_rows:
                coins.add(str(row.get("coin") or ""))
            coins.discard("")

            latest_hashrate_by_coin: dict[str, dict[str, Any]] = {}
            for row in hashrate_rows:
                coin = str(row.get("coin") or "")
                if not coin:
                    continue
                ts = row.get("ts")
                current = latest_hashrate_by_coin.get(coin)
                if current is None or (isinstance(ts, datetime) and ts > current.get("ts", datetime.min.replace(tzinfo=timezone.utc))):
                    latest_hashrate_by_coin[coin] = dict(row)

            latest_network_by_coin: dict[str, dict[str, Any]] = {}
            for row in network_rows:
                coin = str(row.get("coin") or "")
                if not coin:
                    continue
                ts = row.get("ts")
                current = latest_network_by_coin.get(coin)
                if current is None or (isinstance(ts, datetime) and ts > current.get("ts", datetime.min.replace(tzinfo=timezone.utc))):
                    latest_network_by_coin[coin] = dict(row)

            insert_rows: list[dict[str, Any]] = []
            for coin in sorted(coins):
                share_accept = 0
                share_reject = 0
                for row in share_rows:
                    if str(row.get("coin") or "") != coin:
                        continue
                    if bool(row.get("accepted")):
                        share_accept += 1
                    else:
                        share_reject += 1

                total_shares = share_accept + share_reject
                share_reject_rate_pct = (
                    (float(share_reject) * 100.0 / float(total_shares)) if total_shares > 0 else None
                )

                block_accept = 0
                block_reject = 0
                for row in block_rows:
                    if str(row.get("coin") or "") != coin:
                        continue
                    if bool(row.get("accepted_by_node")):
                        block_accept += 1
                    else:
                        block_reject += 1

                total_blocks = block_accept + block_reject
                block_accept_rate_pct_24h = (
                    (float(block_accept) * 100.0 / float(total_blocks)) if total_blocks > 0 else None
                )

                pool_hashrate_hs = None
                network_hash_ps = None
                network_difficulty = None
                expected_time_to_block_sec = None
                pool_share_of_network_pct = None

                hr_row = latest_hashrate_by_coin.get(coin)
                if hr_row is not None:
                    try:
                        pool_hashrate_hs = float(hr_row.get("est_hashrate_hs"))
                    except (TypeError, ValueError):
                        pool_hashrate_hs = None

                net_row = latest_network_by_coin.get(coin)
                if net_row is not None:
                    try:
                        network_hash_ps = float(net_row.get("network_hash_ps"))
                    except (TypeError, ValueError):
                        network_hash_ps = None
                    try:
                        network_difficulty = float(net_row.get("network_difficulty"))
                    except (TypeError, ValueError):
                        network_difficulty = None

                if pool_hashrate_hs and pool_hashrate_hs > 0 and network_difficulty and network_difficulty > 0:
                    expected_time_to_block_sec = float(network_difficulty) * float(2 ** 32) / float(pool_hashrate_hs)
                if pool_hashrate_hs and pool_hashrate_hs > 0 and network_hash_ps and network_hash_ps > 0:
                    raw_share_pct = float(pool_hashrate_hs) * 100.0 / float(network_hash_ps)
                    pool_share_of_network_pct = max(0.0, min(raw_share_pct, 100.0))

                insert_rows.append(
                    {
                        "ts": minute_ts,
                        "coin": coin,
                        "window_minutes": window_minutes,
                        "pool_hashrate_hs": pool_hashrate_hs,
                        "network_hash_ps": network_hash_ps,
                        "network_difficulty": network_difficulty,
                        "share_accept_count": int(share_accept),
                        "share_reject_count": int(share_reject),
                        "share_reject_rate_pct": share_reject_rate_pct,
                        "block_accept_count_24h": int(block_accept),
                        "block_reject_count_24h": int(block_reject),
                        "block_accept_rate_pct_24h": block_accept_rate_pct_24h,
                        "expected_time_to_block_sec": expected_time_to_block_sec,
                        "pool_share_of_network_pct": pool_share_of_network_pct,
                        "created_at": now,
                    }
                )

            if insert_rows:
                conn.execute(self.kpi_snapshots.insert(), insert_rows)

    def _refresh_rollups_sync(self) -> None:
        if self.engine is None:
            return

        lookback_start = datetime.now(timezone.utc) - timedelta(minutes=self.rollup_lookback_minutes)
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(self.share_metrics).where(self.share_metrics.c.ts >= lookback_start)
            ).mappings().all()

            buckets: dict[tuple[datetime, str, str], dict[str, Any]] = {}
            for row in rows:
                ts = row.get("ts")
                if not isinstance(ts, datetime):
                    continue
                bucket = ts.replace(second=0, microsecond=0)
                coin = str(row.get("coin") or "")
                worker = str(row.get("worker") or "")
                key = (bucket, coin, worker)
                agg = buckets.get(key)
                if agg is None:
                    agg = {
                        "bucket_ts": bucket,
                        "coin": coin,
                        "worker": worker,
                        "accepted_count": 0,
                        "rejected_count": 0,
                        "low_diff_reject_count": 0,
                        "duplicate_reject_count": 0,
                        "sum_assigned_diff": 0.0,
                        "sum_computed_diff": 0.0,
                        "count": 0,
                    }
                    buckets[key] = agg

                accepted = bool(row.get("accepted"))
                reason = str(row.get("reject_reason") or "")
                if accepted:
                    agg["accepted_count"] += 1
                else:
                    agg["rejected_count"] += 1
                    if reason == "low_difficulty_share":
                        agg["low_diff_reject_count"] += 1
                    elif reason == "duplicate_share":
                        agg["duplicate_reject_count"] += 1
                agg["sum_assigned_diff"] += float(row.get("assigned_diff") or 0.0)
                agg["sum_computed_diff"] += float(row.get("computed_diff") or 0.0)
                agg["count"] += 1

            conn.execute(delete(self.share_rollups_1m).where(self.share_rollups_1m.c.bucket_ts >= lookback_start))

            insert_rows: list[dict[str, Any]] = []
            now = datetime.now(timezone.utc)
            for agg in buckets.values():
                count = max(1, int(agg["count"]))
                insert_rows.append(
                    {
                        "bucket_ts": agg["bucket_ts"],
                        "coin": agg["coin"],
                        "worker": agg["worker"],
                        "accepted_count": int(agg["accepted_count"]),
                        "rejected_count": int(agg["rejected_count"]),
                        "low_diff_reject_count": int(agg["low_diff_reject_count"]),
                        "duplicate_reject_count": int(agg["duplicate_reject_count"]),
                        "avg_assigned_diff": float(agg["sum_assigned_diff"]) / float(count),
                        "avg_computed_diff": float(agg["sum_computed_diff"]) / float(count),
                        "created_at": now,
                    }
                )

            if insert_rows:
                conn.execute(self.share_rollups_1m.insert(), insert_rows)

    def _replay_spooled_rows_sync(self, max_rows: int) -> None:
        if self.engine is None or not self.spool_path.exists():
            return

        with self.spool_path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        if not lines:
            return

        replay_lines = lines[:max_rows]
        remaining_lines = lines[max_rows:]

        replay_batch: list[tuple[str, dict[str, Any]]] = []
        for line in replay_lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                kind = str(payload.get("kind") or "")
                row_raw = payload.get("row") or {}
                if kind not in {"share", "block", "worker_event", "network_snapshot"} or not isinstance(row_raw, dict):
                    continue
                replay_batch.append((kind, self._row_from_json_safe(row_raw)))
            except Exception:
                continue

        if replay_batch:
            self._write_batch_sync(replay_batch)
            self.total_replayed_rows += len(replay_batch)

        if remaining_lines:
            with self.spool_path.open("w", encoding="utf-8") as f:
                f.writelines(remaining_lines)
        else:
            self.spool_path.unlink(missing_ok=True)

    def _reconcile_worker_identity_sync(self, session_id: str, worker: str) -> int:
        if self.engine is None:
            return 0
        with self.engine.begin() as conn:
            result = conn.execute(
                self.worker_events.update()
                .where(self.worker_events.c.session_id == session_id)
                .where(self.worker_events.c.worker == "unknown")
                .values(worker=worker)
            )
            return int(result.rowcount or 0)


class RpcClient:
    def __init__(self, config: CoinConfig):
        self.config = config

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        payload = {
            "jsonrpc": "1.0",
            "id": f"{self.config.coin}-{uuid.uuid4().hex[:8]}",
            "method": method,
            "params": params or [],
        }
        async with httpx.AsyncClient(timeout=RPC_TIMEOUT_SECONDS) as client:
            request_kwargs: dict[str, Any] = {"json": payload}
            if self.config.rpc_user:
                request_kwargs["auth"] = (self.config.rpc_user, self.config.rpc_password)

            resp = await client.post(self.config.rpc_url, **request_kwargs)
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                raise RuntimeError(str(data.get("error")))
            return data.get("result")


class StratumServer:
    """Minimal Stratum v1 server with notify broadcast support."""

    def __init__(
        self,
        config: CoinConfig,
        bind_host: str = "0.0.0.0",
        rpc_client: RpcClient | None = None,
        data_store: StratumDataStore | None = None,
    ):
        self.config = config
        self.bind_host = bind_host
        self.rpc_client = rpc_client
        self.data_store = data_store
        self.server: asyncio.AbstractServer | None = None
        self.stats = CoinRuntimeStats()
        self._sub_counter = 0
        self._extranonce_counter = 0
        self._clients: set[asyncio.StreamWriter] = set()
        self._sessions: dict[asyncio.StreamWriter, ClientSession] = {}
        self._active_job: ActiveJob | None = None
        self._submitted_share_keys: set[str] = set()
        self._last_share_debug: dict[str, Any] | None = None
        self._recent_jobs_per_worker: dict[str, deque[tuple[str, float, float]]] = {}
        self._recent_job_objects: dict[str, tuple[ActiveJob, float]] = {}
        self._share_traces_global: deque[ShareTrace] = deque(maxlen=SHARE_TRACE_GLOBAL_MAXLEN)
        self._share_traces_by_worker: dict[str, deque[ShareTrace]] = {}
        self._share_traces_inflight: dict[str, ShareTrace] = {}

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self._handle_client,
            host=self.bind_host,
            port=self.config.stratum_port,
        )
        logger.info(
            "Started %s stratum listener on %s:%s (algo=%s)",
            self.config.coin,
            self.bind_host,
            self.config.stratum_port,
            self.config.algo,
        )

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            logger.info("Stopped %s stratum listener", self.config.coin)

    async def set_job(self, job: ActiveJob) -> None:
        self._active_job = job
        self._submitted_share_keys.clear()
        self._remember_job_object(job)
        self.stats.current_job_id = job.job_id
        self.stats.template_height = job.template_height
        self.stats.last_template_at = datetime.now(timezone.utc).isoformat()
        self._add_job_to_known_workers(job.job_id)
        self._log_kv(
            "template_create",
            coin=self.config.coin,
            job_id=job.job_id,
            height=job.template_height,
            version=job.version,
            nbits=job.nbits,
            ntime=job.ntime,
        )
        await self.broadcast_notify(job.notify_params())

    async def broadcast_notify(self, notify_params: list[Any]) -> None:
        payload = {
            "id": None,
            "method": "mining.notify",
            "params": notify_params,
        }

        disconnected: list[asyncio.StreamWriter] = []
        for writer in self._clients:
            try:
                await self._write_json(writer, payload)
            except Exception:
                disconnected.append(writer)

        for writer in disconnected:
            await self._disconnect_client(writer)

        job_id = str(notify_params[0]) if notify_params else ""
        for writer in self._clients:
            session = self._sessions.get(writer)
            self._log_kv(
                "notify_tx",
                coin=self.config.coin,
                worker=(session.worker_name if session and session.worker_name else "unknown"),
                job_id=job_id,
            )

    async def _emit_worker_event(
        self,
        *,
        worker: str,
        event: str,
        session_id: str | None = None,
        job_id: str | None = None,
        peer: str | None = None,
        difficulty: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self.data_store is None:
            return
        await self.data_store.enqueue_worker_event(
            {
                "ts": datetime.now(timezone.utc),
                "session_id": session_id,
                "coin": self.config.coin,
                "worker": worker or "unknown",
                "event": event,
                "job_id": job_id,
                "peer": peer,
                "difficulty": difficulty,
                "details": details or {},
            }
        )

    async def _disconnect_client(self, writer: asyncio.StreamWriter, *, peer: str | None = None) -> None:
        if writer is None:
            return

        self._clients.discard(writer)
        session_final = self._sessions.pop(writer, None)

        if session_final is not None:
            self.stats.connected_workers = max(0, self.stats.connected_workers - 1)

        if peer is None:
            peer = self._peer_for_writer(writer)

        try:
            if not writer.is_closing():
                writer.close()
            await writer.wait_closed()
        except ConnectionResetError:
            pass
        except Exception:
            pass

        if session_final is None:
            return

        logger.info("%s client disconnected: %s", self.config.coin, peer)
        await self._emit_worker_event(
            worker=(session_final.worker_name if session_final and session_final.worker_name else "unknown"),
            event="client_disconnected",
            session_id=(session_final.session_id if session_final else None),
            peer=str(peer),
            difficulty=(session_final.difficulty if session_final else None),
            details={"connected_workers": self.stats.connected_workers},
        )

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        self._clients.add(writer)
        session_obj = ClientSession(peer=str(peer))
        self._sessions[writer] = session_obj
        self.stats.connected_workers += 1
        self.stats.total_connections += 1
        logger.info("%s client connected: %s", self.config.coin, peer)
        await self._emit_worker_event(
            worker="unknown",
            event="client_connected",
            session_id=session_obj.session_id,
            peer=str(peer),
            details={"connected_workers": self.stats.connected_workers},
        )

        try:
            while not reader.at_eof():
                raw = await reader.readline()
                if not raw:
                    break

                message = raw.decode("utf-8", errors="ignore").strip()
                if not message:
                    continue

                try:
                    req = json.loads(message)
                except json.JSONDecodeError:
                    await self._write_json(writer, self._error(None, -32700, "Parse error"))
                    continue

                session = self._sessions.get(writer, ClientSession())
                response = await self._handle_request(req, writer, session)
                if response is not None:
                    await self._write_json(writer, response)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("%s client handler error (%s): %s", self.config.coin, peer, exc)
        finally:
            await self._disconnect_client(writer, peer=str(peer))

    async def _handle_request(
        self,
        req: dict[str, Any],
        writer: asyncio.StreamWriter,
        session: ClientSession,
    ) -> dict[str, Any] | None:
        req_id = req.get("id")
        method = req.get("method")

        if method == "mining.subscribe":
            session.subscribed = True
            self._extranonce_counter += 1
            session.extranonce1 = f"{self._extranonce_counter & 0xFFFFFFFF:08x}"
            self._sub_counter += 1
            sub_id = f"{self.config.coin.lower()}-sub-{self._sub_counter}"
            await self._emit_worker_event(
                worker=(session.worker_name or "unknown"),
                event="subscribed",
                session_id=session.session_id,
                peer=self._peer_for_writer(writer),
                details={
                    "subscription_id": sub_id,
                    "extranonce1": session.extranonce1,
                    "extranonce2_size": DGB_EXTRANONCE2_SIZE,
                },
            )
            return {
                "id": req_id,
                "result": [
                    [["mining.notify", sub_id], ["mining.set_difficulty", sub_id]],
                    session.extranonce1,
                    DGB_EXTRANONCE2_SIZE,
                ],
                "error": None,
            }

        if method == "mining.authorize":
            params = req.get("params") or []
            if params:
                session.worker_name = str(params[0])
            session.authorized = True
            session.difficulty = DGB_STATIC_DIFFICULTY
            self._log_assigned_difficulty(session.difficulty)
            await self._emit_worker_event(
                worker=(session.worker_name or "unknown"),
                event="authorized",
                session_id=session.session_id,
                peer=self._peer_for_writer(writer),
                difficulty=float(session.difficulty),
            )
            if self.data_store is not None:
                updated = await self.data_store.reconcile_worker_identity(
                    session_id=session.session_id,
                    worker=(session.worker_name or "unknown"),
                )
                if updated:
                    self._log_kv(
                        "worker_identity_reconciled",
                        coin=self.config.coin,
                        worker=(session.worker_name or "unknown"),
                        session_id=session.session_id,
                        rows=updated,
                    )

            # Static baseline difficulty for initial phase.
            await self._write_json(
                writer,
                {"id": None, "method": "mining.set_difficulty", "params": [session.difficulty]},
            )

            # If we already have a live job, push it immediately.
            if self._active_job:
                if session.worker_name:
                    self._track_worker_job(
                        session.worker_name,
                        self._active_job.job_id,
                        session.difficulty,
                    )
                await self._write_json(
                    writer,
                    {"id": None, "method": "mining.notify", "params": self._active_job.notify_params()},
                )

            return {"id": req_id, "result": True, "error": None}

        if method == "mining.extranonce.subscribe":
            return {"id": req_id, "result": True, "error": None}

        if method == "mining.configure":
            params = req.get("params") or []
            requested_extensions = params[0] if len(params) > 0 and isinstance(params[0], list) else []
            ext_options = params[1] if len(params) > 1 and isinstance(params[1], dict) else {}

            result: dict[str, Any] = {}
            if "version-rolling" in requested_extensions:
                mask_hex = str(ext_options.get("version-rolling.mask") or "0").strip().lower()
                try:
                    if mask_hex.startswith("0x"):
                        miner_mask = int(mask_hex, 16) & 0xFFFFFFFF
                    elif any(ch in "abcdef" for ch in mask_hex):
                        miner_mask = int(mask_hex, 16) & 0xFFFFFFFF
                    else:
                        miner_mask = int(mask_hex, 10) & 0xFFFFFFFF
                except ValueError:
                    miner_mask = 0

                negotiated_mask = miner_mask & STRATUM_VERSION_ROLLING_SERVER_MASK

                session.version_mask = negotiated_mask
                result["version-rolling"] = True
                result["version-rolling.mask"] = f"{negotiated_mask:08x}"
                logger.info(
                    "%s negotiated version rolling: worker=%s miner_mask=%08x server_mask=%08x negotiated_mask=%08x",
                    self.config.coin,
                    session.worker_name or "unknown",
                    miner_mask,
                    STRATUM_VERSION_ROLLING_SERVER_MASK,
                    session.version_mask,
                )
                await self._emit_worker_event(
                    worker=(session.worker_name or "unknown"),
                    event="version_rolling_configured",
                    session_id=session.session_id,
                    peer=self._peer_for_writer(writer),
                    details={
                        "miner_mask": f"{miner_mask:08x}",
                        "server_mask": f"{STRATUM_VERSION_ROLLING_SERVER_MASK:08x}",
                        "negotiated_mask": f"{session.version_mask:08x}",
                    },
                )

            return {"id": req_id, "result": result, "error": None}

        if method == "mining.submit":
            start_perf = time.perf_counter()
            self.stats.shares_submitted += 1
            self.stats.last_share_at = datetime.now(timezone.utc).isoformat()

            if not session.subscribed:
                return self._reject_share(req_id, "not_subscribed")
            if not session.authorized:
                return self._reject_share(req_id, "not_authorized")

            params = req.get("params")
            if not isinstance(params, list) or len(params) < 5:
                return self._reject_share(req_id, "invalid_params")

            worker_name, job_id, extranonce2, ntime, nonce = params[:5]
            submitted_version: str | None = None
            if len(params) >= 6:
                submitted_version = str(params[5]).lower()
            extranonce2 = self._normalize_extranonce2(str(extranonce2))
            ntime = str(ntime).lower()
            nonce = str(nonce).lower()

            worker_name_str = str(worker_name)
            submitted_job_id = str(job_id)
            ex1_for_cid = session.extranonce1 or ""
            cid = self._build_share_cid(
                worker=worker_name_str,
                job_id=submitted_job_id,
                extranonce1=ex1_for_cid,
                extranonce2=str(extranonce2),
                ntime=str(ntime),
                nonce=str(nonce),
            )
            trace = ShareTrace(
                cid=cid,
                ts=datetime.now(timezone.utc).isoformat(),
                coin=self.config.coin,
                worker=worker_name_str,
                job_id=submitted_job_id,
                ex1=ex1_for_cid,
                ex2=str(extranonce2),
                ntime=str(ntime),
                nonce=str(nonce),
                submitted_version=submitted_version,
                assigned_diff=float(session.difficulty),
            )
            self._share_traces_inflight[cid] = trace
            self._log_share_rx(trace)

            current_job_id = self.stats.current_job_id
            if current_job_id:
                self._track_worker_job(worker_name_str, str(current_job_id), session.difficulty)
            if not current_job_id or (
                submitted_job_id != str(current_job_id)
                and not self._is_recent_job_for_worker(worker_name_str, submitted_job_id)
            ):
                trace.stale = True
                await self._finalize_trace(trace, result="REJECT", reason="stale_job", start_perf=start_perf)
                return self._reject_share(
                    req_id,
                    "stale_job",
                    {
                        "worker": worker_name_str,
                        "job_id": submitted_job_id,
                        "current_job_id": str(current_job_id),
                    },
                )

            if len(str(extranonce2)) != DGB_EXTRANONCE2_SIZE * 2:
                await self._finalize_trace(trace, result="REJECT", reason="bad_extranonce2_size", start_perf=start_perf)
                return self._reject_share(
                    req_id,
                    "bad_extranonce2_size",
                    {
                        "worker": str(worker_name),
                        "extranonce2": str(extranonce2),
                        "expected_hex_len": DGB_EXTRANONCE2_SIZE * 2,
                        "actual_hex_len": len(str(extranonce2)),
                    },
                )

            if not self._is_hex_len(str(extranonce2), DGB_EXTRANONCE2_SIZE):
                await self._finalize_trace(trace, result="REJECT", reason="invalid_extranonce2", start_perf=start_perf)
                return self._reject_share(
                    req_id,
                    "invalid_extranonce2",
                    {
                        "worker": str(worker_name),
                        "extranonce2": str(extranonce2),
                    },
                )

            if not self._is_hex_len(str(ntime), 4):
                await self._finalize_trace(trace, result="REJECT", reason="invalid_ntime", start_perf=start_perf)
                return self._reject_share(req_id, "invalid_ntime")

            if not self._is_hex_len(str(nonce), 4):
                await self._finalize_trace(trace, result="REJECT", reason="invalid_nonce", start_perf=start_perf)
                return self._reject_share(req_id, "invalid_nonce")

            if submitted_version is not None and not self._is_hex_len(submitted_version, 4):
                await self._finalize_trace(trace, result="REJECT", reason="invalid_version", start_perf=start_perf)
                return self._reject_share(
                    req_id,
                    "invalid_version",
                    {
                        "worker": str(worker_name),
                        "version": submitted_version,
                    },
                )

            version_key = submitted_version or ""
            share_key = f"{job_id}:{extranonce2}:{ntime}:{nonce}:{version_key}"
            if share_key in self._submitted_share_keys:
                self.stats.duplicate_shares_acknowledged += 1
                await self._finalize_trace(
                    trace,
                    result="ACCEPT",
                    reason="duplicate_share",
                    start_perf=start_perf,
                )
                return {"id": req_id, "result": True, "error": None}

            job = self._resolve_job_for_submit(submitted_job_id)
            if not job:
                await self._finalize_trace(trace, result="REJECT", reason="no_matching_job", start_perf=start_perf)
                return self._reject_share(
                    req_id,
                    "no_matching_job",
                    {
                        "worker": worker_name_str,
                        "submitted_job_id": submitted_job_id,
                        "current_job_id": str(self.stats.current_job_id),
                    },
                )

            trace.base_version = str(job.version).lower()
            trace.version_mask = f"{session.version_mask:08x}"
            trace.prevhash = str(job.prevhash)
            trace.nbits = str(job.nbits)
            trace.ntime_job = str(job.ntime)

            effective_submit_diff = self._resolve_submit_difficulty(
                worker_name=worker_name_str,
                submitted_job_id=submitted_job_id,
                fallback_difficulty=session.difficulty,
            )
            trace.assigned_diff = float(effective_submit_diff)

            if submitted_version is not None and session.version_mask == 0:
                if submitted_version != str(job.version).lower():
                    await self._finalize_trace(trace, result="REJECT", reason="invalid_version", start_perf=start_perf)
                    return self._reject_share(
                        req_id,
                        "invalid_version",
                        {
                            "worker": worker_name_str,
                            "job_id": submitted_job_id,
                            "base_version": str(job.version).lower(),
                            "submitted_version": submitted_version,
                            "version_mask": f"{session.version_mask:08x}",
                        },
                    )

            try:
                share_result = self._evaluate_share(
                    job=job,
                    session=session,
                    extranonce2=str(extranonce2),
                    ntime=str(ntime),
                    nonce=str(nonce),
                    submitted_version=submitted_version,
                    version_mask=session.version_mask,
                    assigned_difficulty=effective_submit_diff,
                )
            except ValueError as exc:
                msg = str(exc)
                self._capture_share_debug(
                    reason="invalid_ntime_window" if "ntime" in msg else "invalid_share",
                    worker_name=str(worker_name),
                    job_id=str(job_id),
                    extranonce2=str(extranonce2),
                    ntime=str(ntime),
                    nonce=str(nonce),
                    session=session,
                    job=job,
                    submitted_version=submitted_version,
                    version_mask=session.version_mask,
                    extra={"error": msg},
                )
                if "ntime" in msg:
                    await self._finalize_trace(trace, result="REJECT", reason="invalid_ntime_window", start_perf=start_perf)
                    return self._reject_share(req_id, "invalid_ntime_window")
                if "version" in msg:
                    await self._finalize_trace(trace, result="REJECT", reason="invalid_version", start_perf=start_perf)
                    return self._reject_share(req_id, "invalid_version")
                await self._finalize_trace(trace, result="REJECT", reason="invalid_share", start_perf=start_perf)
                return self._reject_share(req_id, "invalid_share")
            except Exception as exc:
                self._capture_share_debug(
                    reason="share_eval_failed",
                    worker_name=str(worker_name),
                    job_id=str(job_id),
                    extranonce2=str(extranonce2),
                    ntime=str(ntime),
                    nonce=str(nonce),
                    session=session,
                    job=job,
                    submitted_version=submitted_version,
                    version_mask=session.version_mask,
                    extra={"error": str(exc)},
                )
                logger.warning("%s share evaluation failed: %s", self.config.coin, exc)
                await self._finalize_trace(trace, result="REJECT", reason="share_eval_failed", start_perf=start_perf)
                return self._reject_share(req_id, "share_eval_failed")

            trace.final_version = str(share_result.get("effective_version_hex") or "")
            trace.merkle_root = str(share_result.get("merkle_root_hex") or "")
            trace.header_hex = str(share_result.get("header_hex") or "")
            trace.hash_hex = str(share_result.get("hash_hex_be") or "")
            hash_int_big_raw = share_result.get("hash_int_big")
            hash_int_little_raw = share_result.get("hash_int_little")
            share_target_raw = share_result.get("share_target")
            share_diff_raw = share_result.get("share_difficulty")

            trace.hash_int_big = int(hash_int_big_raw) if hash_int_big_raw is not None else None
            trace.hash_int_little = int(hash_int_little_raw) if hash_int_little_raw is not None else None
            trace.share_target_int = int(share_target_raw) if share_target_raw is not None else None
            trace.share_target_hex = (
                f"{int(share_target_raw):064x}" if share_target_raw is not None else None
            )
            trace.computed_diff = float(share_diff_raw) if share_diff_raw is not None else None
            trace.meets_target = bool(share_result.get("meets_share_target"))
            trace.meets_network = bool(share_result.get("meets_network_target"))

            self._log_share_eval(trace)
            self._log_share_debug_block(trace, network_target=share_result.get("network_target"))
            self._store_share_trace(trace)

            # Capture full evaluation for both accepted and rejected shares.
            self._capture_share_debug(
                reason="share_evaluated",
                worker_name=worker_name_str,
                job_id=submitted_job_id,
                extranonce2=str(extranonce2),
                ntime=str(ntime),
                nonce=str(nonce),
                session=session,
                job=job,
                submitted_version=submitted_version,
                version_mask=session.version_mask,
                share_result=share_result,
            )

            if STRATUM_VERBOSE_VARIANT_LOG:
                logger.info(
                    "%s variant_audit worker=%s job=%s selected=%s matched=%s meets_share=%s meets_network=%s "
                    "selected_diff=%.12f canonical_diff=%.12f best_share_variant=%s best_share_diff=%.12f "
                    "best_network_variant=%s best_network_diff=%.12f assigned_diff=%.12f nbits=%s",
                    self.config.coin,
                    worker_name_str,
                    submitted_job_id,
                    str(share_result.get("selected_variant") or "-"),
                    str(share_result.get("matched_variant") or "-"),
                    bool(share_result.get("meets_share_target")),
                    bool(share_result.get("meets_network_target")),
                    float(share_result.get("share_difficulty") or 0.0),
                    float(share_result.get("canonical_difficulty") or 0.0),
                    str(share_result.get("best_share_variant") or "-"),
                    float(share_result.get("best_share_variant_difficulty") or 0.0),
                    str(share_result.get("best_network_variant") or "-"),
                    float(share_result.get("best_network_variant_difficulty") or 0.0),
                    float(session.difficulty),
                    str(job.nbits),
                )

            if not share_result["meets_share_target"]:
                assigned_diff_value = float(session.difficulty or 0.0)
                computed_diff_value = float(share_result.get("share_difficulty") or 0.0)
                catastrophic_low_diff = (
                    assigned_diff_value > 0.0
                    and computed_diff_value <= (assigned_diff_value * LOW_DIFF_CATASTROPHIC_RATIO)
                )

                if catastrophic_low_diff:
                    self.stats.catastrophic_low_diff_rejects += 1
                    self.stats.last_catastrophic_low_diff_at = datetime.now(timezone.utc).isoformat()
                    self.stats.last_catastrophic_low_diff_worker = worker_name_str

                    variant_diagnostics = share_result.get("variant_diagnostics") or []
                    top_variants = sorted(
                        variant_diagnostics,
                        key=lambda row: float(row.get("difficulty") or 0.0),
                        reverse=True,
                    )[:16]
                    logger.error(
                        "%s catastrophic_low_diff worker=%s job=%s assigned_diff=%.12f computed_diff=%.12e ratio=%.12e "
                        "selected=%s best_share_variant=%s best_network_variant=%s submitted_version=%s final_version=%s top_variants=%s",
                        self.config.coin,
                        worker_name_str,
                        submitted_job_id,
                        assigned_diff_value,
                        computed_diff_value,
                        (computed_diff_value / assigned_diff_value) if assigned_diff_value > 0 else 0.0,
                        str(share_result.get("selected_variant") or "-"),
                        str(share_result.get("best_share_variant") or "-"),
                        str(share_result.get("best_network_variant") or "-"),
                        str(share_result.get("submitted_version_hex") or "-"),
                        str(share_result.get("effective_version_hex") or "-"),
                        json.dumps(top_variants, default=str),
                    )

                self._capture_share_debug(
                    reason="low_difficulty_share",
                    worker_name=worker_name_str,
                    job_id=submitted_job_id,
                    extranonce2=str(extranonce2),
                    ntime=str(ntime),
                    nonce=str(nonce),
                    session=session,
                    job=job,
                    submitted_version=submitted_version,
                    version_mask=session.version_mask,
                    share_result=share_result,
                    extra={
                        "catastrophic_low_diff": catastrophic_low_diff,
                        "assigned_diff": assigned_diff_value,
                        "computed_diff": computed_diff_value,
                        "low_diff_ratio": (
                            (computed_diff_value / assigned_diff_value) if assigned_diff_value > 0.0 else None
                        ),
                        "catastrophic_threshold_ratio": LOW_DIFF_CATASTROPHIC_RATIO,
                    },
                )
                if STRATUM_DEBUG_SHARES:
                    alt_variants = share_result.get("alt_difficulty_variants") or {}
                    alt_variants_text = ", ".join(
                        f"{k}={float(v):.6e}" for k, v in alt_variants.items()
                    )
                    logger.warning(
                        "%s low difficulty share: worker=%s job_id=%s ex2=%s ntime=%s nonce=%s "
                        "share_diff=%.6e target_diff=%.6f hash=%s hash_int=%s share_target=%s "
                        "hash_int_big=%s hash_int_little=%s meets_big=%s meets_little=%s computed_diff_big=%.6f alt=[%s]",
                        self.config.coin,
                        str(worker_name),
                        str(job_id),
                        str(extranonce2),
                        str(ntime),
                        str(nonce),
                        float(share_result["share_difficulty"]),
                        float(session.difficulty),
                        str(share_result["block_hash"]),
                        str(share_result["hash_int"]),
                        str(share_result["share_target"]),
                        str(share_result.get("hash_int_big")),
                        str(share_result.get("hash_int_little")),
                        bool(share_result.get("meets_big")),
                        bool(share_result.get("meets_little")),
                        float(share_result.get("computed_diff_big") or 0.0),
                        alt_variants_text,
                    )
                    logger.warning(
                        "%s reject-trace: job_id=%s ex1=%s ex2=%s ntime=%s nonce=%s base_version=%s "
                        "submitted_version=%s version_mask=%s final_version=%s prevhash=%s "
                        "coinbase_sha256d=%s merkle_root=%s header_hex_prefix=%s hash_hex=%s "
                        "assigned_diff=%s computed_diff=%s hash_int_big=%s hash_int_little=%s "
                        "diff1_target_hex=%s share_target_hex=%s hash_le_hex=%s share_target_int=%s meets_target=%s",
                        self.config.coin,
                        submitted_job_id,
                        str(session.extranonce1),
                        str(extranonce2),
                        str(ntime),
                        str(nonce),
                        str(share_result.get("base_version_hex")),
                        str(share_result.get("submitted_version_hex")),
                        str(share_result.get("version_mask_hex")),
                        str(share_result.get("effective_version_hex")),
                        str(share_result.get("prevhash_hex")),
                        str(share_result.get("coinbase_hash_hex")),
                        str(share_result.get("merkle_root_hex")),
                        str(share_result.get("header_hex") or "")[:152],
                        str(share_result.get("hash_hex_be")),
                        float(session.difficulty),
                        float(share_result.get("share_difficulty") or 0.0),
                        int(share_result.get("hash_int_big") or 0),
                        int(share_result.get("hash_int_little") or 0),
                        DIFF1_TARGET_HEX,
                        int(share_result.get("share_target") or 0).to_bytes(32, "big").hex(),
                        str(share_result.get("hash_le_hex") or ""),
                        int(share_result.get("share_target") or 0),
                        bool(share_result.get("meets_share_target")),
                    )
                await self._finalize_trace(
                    trace,
                    result="REJECT",
                    reason="low_difficulty_share",
                    start_perf=start_perf,
                )
                return self._reject_share(req_id, "low_difficulty_share")

            self._submitted_share_keys.add(share_key)

            self.stats.shares_accepted += 1
            matched_variant = share_result.get("matched_variant")
            if matched_variant:
                logger.info(
                    "%s accepted share via compatibility variant: %s (worker=%s job_id=%s)",
                    self.config.coin,
                    matched_variant,
                    str(worker_name),
                    str(job_id),
                )
            share_diff = float(share_result["share_difficulty"])
            if self.stats.best_share_difficulty is None or share_diff > self.stats.best_share_difficulty:
                self.stats.best_share_difficulty = share_diff

            await self._maybe_retarget_vardiff(writer, session)

            if share_result["meets_network_target"] and self.rpc_client:
                if self.config.coin == "DGB" and not _DGB_PROPOSAL_GUARD.submit_enabled:
                    logger.warning(
                        "%s submit blocked by proposal guard (consecutive_passes=%s required=%s last_failure=%s)",
                        self.config.coin,
                        _DGB_PROPOSAL_GUARD.consecutive_passes,
                        _DGB_PROPOSAL_GUARD.required_consecutive_passes,
                        _DGB_PROPOSAL_GUARD.last_failure_reason,
                    )
                    if self.data_store is not None:
                        await self.data_store.enqueue_block_attempt(
                            {
                                "ts": datetime.now(timezone.utc),
                                "coin": self.config.coin,
                                "worker": worker_name_str,
                                "job_id": job.job_id,
                                "template_height": job.template_height,
                                "block_hash": str(share_result["block_hash"]),
                                "accepted_by_node": False,
                                "submit_result_raw": "guard_blocked",
                                "reject_reason": "guard_blocked",
                                "reject_category": "other_reject",
                                "rpc_error": None,
                                "latency_ms": None,
                                "extra": {
                                    "guard_snapshot": _DGB_PROPOSAL_GUARD.snapshot(),
                                    "matched_variant": share_result.get("matched_variant"),
                                },
                            }
                        )
                    await self._finalize_trace(trace, result="ACCEPT", reason=None, start_perf=start_perf)
                    return {"id": req_id, "result": True, "error": None}

                self.stats.block_candidates += 1
                block_hex = share_result["block_hex"]
                logger.info(
                    "%s candidate submit path=segwit_aware_template_v2 worker=%s job=%s variant=%s hash=%s",
                    self.config.coin,
                    worker_name_str,
                    job.job_id,
                    str(share_result.get("selected_variant") or "canonical"),
                    str(share_result.get("block_hash") or "-"),
                )
                proposal_result: Any = None
                proposal_checked = False
                try:
                    proposal_result = await self.rpc_client.call(
                        "getblocktemplate",
                        [{"mode": "proposal", "data": block_hex}],
                    )
                    proposal_checked = True
                    if proposal_result not in (None, "", "null"):
                        self.stats.blocks_rejected += 1
                        self.stats.last_block_submit_result = f"proposal_reject: {proposal_result}"
                        logger.warning(
                            "%s block candidate rejected in proposal validation: %s",
                            self.config.coin,
                            proposal_result,
                        )
                        if self.data_store is not None:
                            reject_category = _normalize_block_reject_category(proposal_result)
                            await self.data_store.enqueue_block_attempt(
                                {
                                    "ts": datetime.now(timezone.utc),
                                    "coin": self.config.coin,
                                    "worker": worker_name_str,
                                    "job_id": job.job_id,
                                    "template_height": job.template_height,
                                    "block_hash": str(share_result["block_hash"]),
                                    "accepted_by_node": False,
                                    "submit_result_raw": str(proposal_result),
                                    "reject_reason": str(proposal_result),
                                    "reject_category": reject_category,
                                    "rpc_error": None,
                                    "latency_ms": None,
                                    "extra": {
                                        "node_submit_result": proposal_result,
                                        "matched_variant": share_result.get("matched_variant"),
                                        "proposal_checked": True,
                                        "proposal_only": True,
                                    },
                                }
                            )
                        await self._finalize_trace(trace, result="ACCEPT", reason=None, start_perf=start_perf)
                        return {"id": req_id, "result": True, "error": None}
                except Exception as proposal_exc:
                    logger.warning(
                        "%s proposal validation unavailable/failed; continuing to submitblock: %s",
                        self.config.coin,
                        proposal_exc,
                    )

                submit_started = time.perf_counter()
                try:
                    submit_result = await self.rpc_client.call("submitblock", [block_hex])
                    submit_latency_ms = (time.perf_counter() - submit_started) * 1000.0
                    if submit_result in (None, "", "null"):
                        self.stats.blocks_accepted += 1
                        self.stats.last_block_submit_result = "accepted"
                        logger.info(
                            "%s block candidate accepted (job=%s hash=%s)",
                            self.config.coin,
                            job.job_id,
                            share_result["block_hash"],
                        )
                        if self.data_store is not None:
                            reject_category = _normalize_block_reject_category(submit_result)
                            await self.data_store.enqueue_block_attempt(
                                {
                                    "ts": datetime.now(timezone.utc),
                                    "coin": self.config.coin,
                                    "worker": worker_name_str,
                                    "job_id": job.job_id,
                                    "template_height": job.template_height,
                                    "block_hash": str(share_result["block_hash"]),
                                    "accepted_by_node": True,
                                    "submit_result_raw": str(submit_result),
                                    "reject_reason": None,
                                    "reject_category": reject_category,
                                    "rpc_error": None,
                                    "latency_ms": submit_latency_ms,
                                    "extra": {
                                        "node_submit_result": submit_result,
                                        "matched_variant": share_result.get("matched_variant"),
                                        "proposal_checked": proposal_checked,
                                        "proposal_result": proposal_result,
                                    },
                                }
                            )
                    else:
                        self.stats.blocks_rejected += 1
                        self.stats.last_block_submit_result = str(submit_result)
                        logger.warning(
                            "%s block candidate rejected by node: %s",
                            self.config.coin,
                            submit_result,
                        )
                        if self.data_store is not None:
                            reject_category = _normalize_block_reject_category(submit_result)
                            await self.data_store.enqueue_block_attempt(
                                {
                                    "ts": datetime.now(timezone.utc),
                                    "coin": self.config.coin,
                                    "worker": worker_name_str,
                                    "job_id": job.job_id,
                                    "template_height": job.template_height,
                                    "block_hash": str(share_result["block_hash"]),
                                    "accepted_by_node": False,
                                    "submit_result_raw": str(submit_result),
                                    "reject_reason": str(submit_result),
                                    "reject_category": reject_category,
                                    "rpc_error": None,
                                    "latency_ms": submit_latency_ms,
                                    "extra": {
                                        "node_submit_result": submit_result,
                                        "matched_variant": share_result.get("matched_variant"),
                                        "proposal_checked": proposal_checked,
                                        "proposal_result": proposal_result,
                                    },
                                }
                            )
                except Exception as exc:
                    self.stats.blocks_rejected += 1
                    self.stats.last_block_submit_result = f"submit_error: {exc}"
                    logger.error("%s submitblock failed: %s", self.config.coin, exc)
                    if self.data_store is not None:
                        reject_category = _normalize_block_reject_category(None, rpc_error=str(exc))
                        await self.data_store.enqueue_block_attempt(
                            {
                                "ts": datetime.now(timezone.utc),
                                "coin": self.config.coin,
                                "worker": worker_name_str,
                                "job_id": job.job_id,
                                "template_height": job.template_height,
                                "block_hash": str(share_result["block_hash"]),
                                "accepted_by_node": False,
                                "submit_result_raw": None,
                                "reject_reason": "submit_error",
                                "reject_category": reject_category,
                                "rpc_error": str(exc),
                                "latency_ms": (time.perf_counter() - submit_started) * 1000.0,
                                "extra": {
                                    "matched_variant": share_result.get("matched_variant"),
                                    "proposal_checked": proposal_checked,
                                    "proposal_result": proposal_result,
                                },
                            }
                        )

            await self._finalize_trace(trace, result="ACCEPT", reason=None, start_perf=start_perf)
            return {"id": req_id, "result": True, "error": None}

        return self._error(req_id, -32601, f"Method not found: {method}")

    def _log_assigned_difficulty(self, diff: float) -> None:
        share_target_int = target_from_difficulty(diff)
        diff_check = DIFF1_TARGET_INT / max(share_target_int, 1)
        logger.info(
            "%s difficulty sanity: assigned_diff=%s share_target=%064x diff_check≈%.12f",
            self.config.coin,
            diff,
            share_target_int,
            diff_check,
        )

    async def _maybe_retarget_vardiff(
        self,
        writer: asyncio.StreamWriter,
        session: ClientSession,
    ) -> None:
        if not VARDIFF_ENABLED:
            return

        now = time.time()
        if session.first_accepted_share_at == 0.0:
            session.first_accepted_share_at = now
        session.accepted_share_times.append(now)

        window_start = now - VARDIFF_WINDOW_SECONDS
        while session.accepted_share_times and session.accepted_share_times[0] < window_start:
            session.accepted_share_times.popleft()

        if now - session.last_vardiff_adjust_at < VARDIFF_RETARGET_INTERVAL_SECONDS:
            return

        if len(session.accepted_share_times) < VARDIFF_MIN_ACCEPTED_SHARES_BEFORE_RETARGET:
            return

        if (now - session.first_accepted_share_at) < VARDIFF_MIN_WARMUP_SECONDS:
            return

        observed_rate = (
            len(session.accepted_share_times) / max(VARDIFF_WINDOW_SECONDS / 60.0, 1e-9)
        )

        if VARDIFF_TARGET_MIN_SHARES_PER_MIN <= observed_rate <= VARDIFF_TARGET_MAX_SHARES_PER_MIN:
            session.last_vardiff_adjust_at = now
            return

        current_diff = float(session.difficulty)
        raw_factor = observed_rate / max(VARDIFF_TARGET_MID_SHARES_PER_MIN, 1e-9)
        step_up_cap = VARDIFF_STEP_UP_MAX_FACTOR
        if observed_rate >= (VARDIFF_TARGET_MAX_SHARES_PER_MIN * VARDIFF_BURST_THRESHOLD_MULTIPLIER):
            step_up_cap = VARDIFF_STEP_UP_BURST_FACTOR
        factor = min(step_up_cap, max(VARDIFF_STEP_DOWN_MIN_FACTOR, raw_factor))

        new_diff = current_diff * factor
        new_diff = max(VARDIFF_MIN_DIFFICULTY, min(VARDIFF_MAX_DIFFICULTY, new_diff))

        # Avoid tiny churn.
        if abs(new_diff - current_diff) / max(current_diff, 1e-9) < 0.05:
            session.last_vardiff_adjust_at = now
            return

        session.difficulty = float(new_diff)
        session.last_vardiff_adjust_at = now

        self._log_kv(
            "vardiff_adjust",
            coin=self.config.coin,
            worker=(session.worker_name or "unknown"),
            old_diff=f"{current_diff:.3f}",
            new_diff=f"{session.difficulty:.3f}",
            observed_spm=f"{observed_rate:.3f}",
            target_min=VARDIFF_TARGET_MIN_SHARES_PER_MIN,
            target_max=VARDIFF_TARGET_MAX_SHARES_PER_MIN,
        )
        self._log_assigned_difficulty(session.difficulty)
        await self._emit_worker_event(
            worker=(session.worker_name or "unknown"),
            event="vardiff_adjust",
            session_id=session.session_id,
            job_id=(self._active_job.job_id if self._active_job else None),
            peer=self._peer_for_writer(writer),
            difficulty=float(session.difficulty),
            details={
                "old_diff": float(current_diff),
                "new_diff": float(session.difficulty),
                "observed_spm": float(observed_rate),
                "target_min": float(VARDIFF_TARGET_MIN_SHARES_PER_MIN),
                "target_max": float(VARDIFF_TARGET_MAX_SHARES_PER_MIN),
            },
        )

        # Stratum clients commonly apply new diff on next job; push notify too.
        await self._write_json(
            writer,
            {"id": None, "method": "mining.set_difficulty", "params": [session.difficulty]},
        )
        if self._active_job:
            if session.worker_name:
                self._track_worker_job(session.worker_name, self._active_job.job_id, session.difficulty)
            await self._write_json(
                writer,
                {"id": None, "method": "mining.notify", "params": self._active_job.notify_params()},
            )

    def _cleanup_recent_jobs(self, worker_name: str) -> None:
        now = time.time()
        q = self._recent_jobs_per_worker.get(worker_name)
        if not q:
            return
        while q and (now - q[0][1]) > STALE_JOB_GRACE_SECONDS:
            q.popleft()
        if not q:
            self._recent_jobs_per_worker.pop(worker_name, None)

    def _track_worker_job(self, worker_name: str, job_id: str, difficulty: float) -> None:
        self._cleanup_recent_jobs(worker_name)
        q = self._recent_jobs_per_worker.setdefault(worker_name, deque(maxlen=STALE_JOB_GRACE_COUNT))
        now = time.time()
        if q and q[-1][0] == job_id:
            q[-1] = (job_id, now, float(difficulty))
        else:
            q.append((job_id, now, float(difficulty)))

    def _add_job_to_known_workers(self, job_id: str) -> None:
        for session in self._sessions.values():
            if session.worker_name:
                self._track_worker_job(session.worker_name, job_id, session.difficulty)

    def _cleanup_recent_job_objects(self) -> None:
        now = time.time()
        stale_after = max(float(STALE_JOB_GRACE_SECONDS) * 3.0, 30.0)
        stale_ids = [
            jid
            for jid, (_job, ts) in self._recent_job_objects.items()
            if (now - ts) > stale_after
        ]
        for jid in stale_ids:
            self._recent_job_objects.pop(jid, None)

    def _remember_job_object(self, job: ActiveJob) -> None:
        self._cleanup_recent_job_objects()
        self._recent_job_objects[str(job.job_id)] = (job, time.time())

    def _resolve_job_for_submit(self, submitted_job_id: str) -> ActiveJob | None:
        submitted_job_id = str(submitted_job_id)
        if self._active_job and str(self._active_job.job_id) == submitted_job_id:
            self._remember_job_object(self._active_job)
            return self._active_job

        self._cleanup_recent_job_objects()
        found = self._recent_job_objects.get(submitted_job_id)
        if not found:
            return None

        job, _ts = found
        self._recent_job_objects[submitted_job_id] = (job, time.time())
        return job

    def _is_recent_job_for_worker(self, worker_name: str, job_id: str) -> bool:
        self._cleanup_recent_jobs(worker_name)
        q = self._recent_jobs_per_worker.get(worker_name)
        if not q:
            return False
        return any(existing_job_id == job_id for existing_job_id, _, _ in q)

    def _resolve_submit_difficulty(
        self,
        worker_name: str,
        submitted_job_id: str,
        fallback_difficulty: float,
    ) -> float:
        self._cleanup_recent_jobs(worker_name)
        q = self._recent_jobs_per_worker.get(worker_name)
        if not q:
            return float(fallback_difficulty)

        for existing_job_id, _ts, difficulty in reversed(q):
            if existing_job_id == submitted_job_id:
                return float(difficulty)

        return float(fallback_difficulty)

    def _build_share_cid(
        self,
        *,
        worker: str,
        job_id: str,
        extranonce1: str,
        extranonce2: str,
        ntime: str,
        nonce: str,
    ) -> str:
        return (
            f"{self.config.coin}:{worker}:{job_id}:{extranonce1}:{extranonce2}:{ntime}:{nonce}"
        )

    @staticmethod
    def _peer_for_writer(writer: Any) -> str | None:
        get_extra_info = getattr(writer, "get_extra_info", None)
        if callable(get_extra_info):
            try:
                peer = get_extra_info("peername")
            except Exception:
                peer = None
            if peer is not None:
                return str(peer)
        return None

    def _worker_trace_deque(self, worker: str) -> deque[ShareTrace]:
        traces = self._share_traces_by_worker.get(worker)
        if traces is None:
            traces = deque(maxlen=SHARE_TRACE_PER_WORKER_MAXLEN)
            self._share_traces_by_worker[worker] = traces
        return traces

    def _store_share_trace(self, trace: ShareTrace) -> None:
        if trace.stored:
            return
        self._share_traces_global.append(trace)
        self._worker_trace_deque(trace.worker).append(trace)
        trace.stored = True

    @staticmethod
    def _kv_str(value: Any) -> str:
        if value is None:
            return "-"
        s = str(value)
        return s.replace(" ", "_")

    def _log_kv(self, event: str, **fields: Any) -> None:
        parts = [event]
        for key, value in fields.items():
            parts.append(f"{key}={self._kv_str(value)}")
        logger.info(" ".join(parts))

    def _log_share_rx(self, trace: ShareTrace) -> None:
        self._log_kv(
            "share_rx",
            cid=trace.cid,
            coin=trace.coin,
            worker=trace.worker,
            job_id=trace.job_id,
            ex1=trace.ex1,
            ex2=trace.ex2,
            ntime=trace.ntime,
            nonce=trace.nonce,
            v_sub=trace.submitted_version,
            diff_assigned=trace.assigned_diff,
        )

    def _log_share_eval(self, trace: ShareTrace) -> None:
        self._log_kv(
            "share_eval",
            cid=trace.cid,
            final_version=trace.final_version,
            prevhash=trace.prevhash,
            merkle=trace.merkle_root,
            hash=trace.hash_hex,
            hash_int_big=trace.hash_int_big,
            hash_int_little=trace.hash_int_little,
            target=trace.share_target_int,
            diff_calc=trace.computed_diff,
            meets_target=trace.meets_target,
            meets_network=trace.meets_network,
        )

    def _log_share_result(self, trace: ShareTrace, result: str) -> None:
        self._log_kv(
            "share_result",
            cid=trace.cid,
            result=result,
            reason=trace.reject_reason,
            diff_calc=trace.computed_diff,
            target_diff=trace.assigned_diff,
            rtt_ms=(f"{trace.server_response_time_ms:.3f}" if trace.server_response_time_ms is not None else None),
        )

    def _log_share_debug_block(self, trace: ShareTrace, network_target: int | None = None) -> None:
        if not HMM_STRATUM_TRACE_DEBUG:
            return
        logger.info(
            "share_debug cid=%s\n"
            "  version(base/sub/mask/final)=%s/%s/%s/%s\n"
            "  prevhash=%s\n"
            "  merkle_root=%s\n"
            "  ntime=%s nbits=%s nonce=%s\n"
            "  header_hex=%s\n"
            "  share_target_hex=%s\n"
            "  network_target_hex=%s",
            trace.cid,
            trace.base_version,
            trace.submitted_version,
            trace.version_mask,
            trace.final_version,
            trace.prevhash,
            trace.merkle_root,
            trace.ntime,
            trace.nbits,
            trace.nonce,
            trace.header_hex,
            trace.share_target_hex,
            (f"{network_target:064x}" if network_target is not None else "-"),
        )

    async def _finalize_trace(
        self,
        trace: ShareTrace,
        *,
        result: str,
        reason: str | None,
        start_perf: float,
    ) -> None:
        trace.reject_reason = reason
        trace.server_response_time_ms = (time.perf_counter() - start_perf) * 1000.0
        self._store_share_trace(trace)
        self._log_share_result(trace, result)
        if self.data_store is not None:
            await self.data_store.enqueue_share_metric(
                {
                    "ts": datetime.now(timezone.utc),
                    "coin": self.config.coin,
                    "worker": trace.worker,
                    "job_id": trace.job_id,
                    "assigned_diff": float(trace.assigned_diff or 0.0),
                    "computed_diff": float(trace.computed_diff or 0.0),
                    "accepted": result == "ACCEPT",
                    "reject_reason": reason,
                }
            )
        self._share_traces_inflight.pop(trace.cid, None)

    def get_last_shares(self, worker: str | None = None, n: int = 50) -> list[dict[str, Any]]:
        n = max(1, min(n, 5000))
        if worker:
            traces = list(self._share_traces_by_worker.get(worker, deque()))
            return [t.to_summary() for t in traces[-n:]]
        traces = list(self._share_traces_global)
        return [t.to_summary() for t in traces[-n:]]

    @staticmethod
    def _is_hex_len(value: str, byte_len: int) -> bool:
        if len(value) != byte_len * 2:
            return False
        try:
            int(value, 16)
            return True
        except ValueError:
            return False

    @staticmethod
    def _normalize_extranonce2(value: str) -> str:
        raw = (value or "").strip().lower()
        if raw.startswith("0x"):
            raw = raw[2:]

        # Compatibility: accept shorter miner values by left-padding to configured size.
        expected_hex_len = DGB_EXTRANONCE2_SIZE * 2
        if len(raw) < expected_hex_len:
            raw = raw.rjust(expected_hex_len, "0")
        return raw

    def _reject_share(
        self,
        req_id: Any,
        reason: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.stats.shares_rejected += 1
        self.stats.share_reject_reasons[reason] = self.stats.share_reject_reasons.get(reason, 0) + 1
        if context:
            logger.warning("%s share rejected: %s (%s)", self.config.coin, reason, context)
        return {
            "id": req_id,
            "result": False,
            "error": [20, f"Share rejected: {reason}", None],
        }

    @staticmethod
    def _int_to_hex256(value: int | None) -> str | None:
        if value is None:
            return None
        return f"{int(value) & ((1 << 256) - 1):064x}"

    def _capture_share_debug(
        self,
        *,
        reason: str,
        worker_name: str,
        job_id: str,
        extranonce2: str,
        ntime: str,
        nonce: str,
        submitted_version: str | None = None,
        version_mask: int = 0,
        session: ClientSession,
        job: ActiveJob,
        share_result: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "coin": self.config.coin,
            "reason": reason,
            "submit": {
                "worker": worker_name,
                "job_id": job_id,
                "extranonce1": session.extranonce1,
                "extranonce2": extranonce2,
                "ntime": ntime,
                "nonce": nonce,
                "submitted_version": submitted_version,
                "version_mask": f"{version_mask & 0xFFFFFFFF:08x}",
                "assigned_difficulty": session.difficulty,
            },
            "job": {
                "current_job_id": self.stats.current_job_id,
                "version": job.version,
                "prevhash": job.prevhash,
                "prevhash_be": job.prevhash_be,
                "nbits": job.nbits,
                "ntime": job.ntime,
                "coinb1": job.coinb1,
                "coinb2": job.coinb2,
                "merkle_branch": list(job.merkle_branch),
                "target_1": job.target_1,
                "target_1_hex": self._int_to_hex256(job.target_1),
            },
        }

        if share_result:
            payload["evaluation"] = {
                "matched_variant": share_result.get("matched_variant"),
                "selected_variant": share_result.get("selected_variant"),
                "canonical_difficulty": share_result.get("canonical_difficulty"),
                "best_share_variant": share_result.get("best_share_variant"),
                "best_share_variant_difficulty": share_result.get("best_share_variant_difficulty"),
                "best_network_variant": share_result.get("best_network_variant"),
                "best_network_variant_difficulty": share_result.get("best_network_variant_difficulty"),
                "hash_int": share_result.get("hash_int"),
                "hash_hex": share_result.get("block_hash"),
                "share_target": share_result.get("share_target"),
                "share_target_hex": self._int_to_hex256(share_result.get("share_target")),
                "network_target": share_result.get("network_target"),
                "network_target_hex": self._int_to_hex256(share_result.get("network_target")),
                "share_difficulty": share_result.get("share_difficulty"),
                "meets_share_target": share_result.get("meets_share_target"),
                "meets_network_target": share_result.get("meets_network_target"),
                "effective_version_hex": share_result.get("effective_version_hex"),
                "alt_difficulty_variants": share_result.get("alt_difficulty_variants", {}),
                "variant_diagnostics": share_result.get("variant_diagnostics", []),
                "block_hex_prefix": (share_result.get("block_hex") or "")[:200],
            }

        if extra:
            payload["extra"] = extra

        self._last_share_debug = payload

    def _evaluate_share(
        self,
        *,
        job: ActiveJob,
        session: ClientSession,
        extranonce2: str,
        ntime: str,
        nonce: str,
        submitted_version: str | None = None,
        version_mask: int = 0,
        assigned_difficulty: float | None = None,
    ) -> dict[str, Any]:
        if not session.extranonce1:
            raise ValueError("session extranonce1 missing")

        ntime_int = int(ntime, 16)
        job_ntime_int = int(job.ntime, 16)
        if ntime_int < (job_ntime_int - 600) or ntime_int > (job_ntime_int + 7200):
            raise ValueError("ntime out of acceptable range")

        job_version_int = int(job.version, 16) & 0xFFFFFFFF
        submitted_version_int = int(submitted_version, 16) & 0xFFFFFFFF if submitted_version else None
        mask_int = version_mask & 0xFFFFFFFF

        final_version_candidates: dict[str, int] = {}
        if submitted_version_int is None:
            final_version_candidates["canonical"] = job_version_int
        elif mask_int != 0:
            final_version_candidates["canonical"] = (
                (job_version_int & (~mask_int & 0xFFFFFFFF))
                | (submitted_version_int & mask_int)
            ) & 0xFFFFFFFF

            # Compatibility path: some miners send submit version value with
            # opposite byte order.
            submitted_version_swapped = struct.unpack(">I", struct.pack("<I", submitted_version_int)
            )[0]
            final_version_candidates["submit_version_bswap32"] = (
                (job_version_int & (~mask_int & 0xFFFFFFFF))
                | (submitted_version_swapped & mask_int)
            ) & 0xFFFFFFFF
        else:
            if submitted_version_int != job_version_int:
                raise ValueError("invalid_version")
            final_version_candidates["canonical"] = job_version_int

        coinbase_bytes = build_coinbase(job.coinb1, session.extranonce1, extranonce2, job.coinb2)
        coinbase_txid_bytes = build_coinbase(
            job.coinb1_txid,
            session.extranonce1,
            extranonce2,
            job.coinb2_txid,
        )
        merkle_root_bytes = build_merkle_root(coinbase_txid_bytes, job.merkle_branch)
        effective_difficulty = float(assigned_difficulty if assigned_difficulty is not None else session.difficulty)
        share_target = target_from_difficulty(max(effective_difficulty, 0.000001))
        network_target = _target_from_nbits(job.nbits)

        ntime_bytes_le = struct.pack("<I", ntime_int)
        ntime_bytes_raw = bytes.fromhex(ntime)
        nbits_bytes_le = struct.pack("<I", int(job.nbits, 16))
        nbits_bytes_raw = bytes.fromhex(job.nbits)
        nonce_bytes_le = struct.pack("<I", int(nonce, 16))
        nonce_bytes_raw = bytes.fromhex(nonce)

        prevhash_from_be_reversed = bytes.fromhex(job.prevhash_be)[::-1]
        prevhash_notify_direct = bytes.fromhex(job.prevhash)
        merkle_display = merkle_root_bytes
        merkle_internal = merkle_root_bytes[::-1]

        def _assemble_header(
            version_int: int,
            prevhash_bytes: bytes,
            merkle_bytes: bytes,
            *,
            version_raw: bool,
            ntime_raw: bool,
            nbits_raw: bool,
            nonce_raw: bool,
        ) -> bytes:
            version_bytes = (
                bytes.fromhex(f"{version_int & 0xFFFFFFFF:08x}")
                if version_raw
                else struct.pack("<I", version_int)
            )
            h = (
                version_bytes
                + prevhash_bytes
                + merkle_bytes
                + (ntime_bytes_raw if ntime_raw else ntime_bytes_le)
                + (nbits_bytes_raw if nbits_raw else nbits_bytes_le)
                + (nonce_bytes_raw if nonce_raw else nonce_bytes_le)
            )
            if len(h) != 80:
                raise ValueError("invalid header length")
            return h

        variants: dict[str, tuple[bytes, bytes, int, int, float, bool, bool]] = {}

        candidate_headers: dict[str, bytes] = {}
        for version_name, version_int in final_version_candidates.items():
            for version_raw in (False, True):
                for ntime_raw in (False, True):
                    for nbits_raw in (False, True):
                        for nonce_raw in (False, True):
                            mode_suffix = (
                                f"ver={'raw' if version_raw else 'le'}"
                                f";ntime={'raw' if ntime_raw else 'le'}"
                                f";nbits={'raw' if nbits_raw else 'le'}"
                                f";nonce={'raw' if nonce_raw else 'le'}"
                            )
                            prefix = f"{version_name}:{mode_suffix}"
                            candidate_headers[f"{prefix}"] = _assemble_header(
                                version_int,
                                prevhash_from_be_reversed,
                                merkle_internal,
                                version_raw=version_raw,
                                ntime_raw=ntime_raw,
                                nbits_raw=nbits_raw,
                                nonce_raw=nonce_raw,
                            )
                            candidate_headers[f"{prefix}:prevhash_notify_direct"] = _assemble_header(
                                version_int,
                                prevhash_notify_direct,
                                merkle_internal,
                                version_raw=version_raw,
                                ntime_raw=ntime_raw,
                                nbits_raw=nbits_raw,
                                nonce_raw=nonce_raw,
                            )
                            candidate_headers[f"{prefix}:merkle_direct"] = _assemble_header(
                                version_int,
                                prevhash_from_be_reversed,
                                merkle_display,
                                version_raw=version_raw,
                                ntime_raw=ntime_raw,
                                nbits_raw=nbits_raw,
                                nonce_raw=nonce_raw,
                            )
                            candidate_headers[
                                f"{prefix}:prevhash_notify_direct_merkle_direct"
                            ] = _assemble_header(
                                version_int,
                                prevhash_notify_direct,
                                merkle_display,
                                version_raw=version_raw,
                                ntime_raw=ntime_raw,
                                nbits_raw=nbits_raw,
                                nonce_raw=nonce_raw,
                            )

        for name, candidate_header in candidate_headers.items():
            hbin, hbig = hash_header(candidate_header)
            hlittle = int.from_bytes(hbin, "little")
            sdiff = difficulty_from_hash(hbin)
            meets_share_variant = meets_share(hbin, share_target)
            meets_network = hlittle <= network_target
            variants[name] = (
                candidate_header,
                hbin,
                hbig,
                hlittle,
                sdiff,
                meets_share_variant,
                meets_network,
            )

        variant_diagnostics = [
            {
                "name": name,
                "difficulty": float(row[4]),
                "meets_share_target": bool(row[5]),
                "meets_network_target": bool(row[6]),
            }
            for name, row in variants.items()
        ]

        selected_name = "canonical:ver=le;ntime=le;nbits=le;nonce=le"
        pass_variants = [name for name, row in variants.items() if row[5]]
        network_pass_variants = [name for name in pass_variants if variants[name][6]]
        canonical_row = variants.get("canonical:ver=le;ntime=le;nbits=le;nonce=le")
        canonical_difficulty = float(canonical_row[4]) if canonical_row else 0.0
        best_share_variant = max(pass_variants, key=lambda n: variants[n][4]) if pass_variants else None
        best_share_variant_difficulty = (
            float(variants[best_share_variant][4]) if best_share_variant else 0.0
        )
        best_network_variant = (
            max(network_pass_variants, key=lambda n: variants[n][4]) if network_pass_variants else None
        )
        best_network_variant_difficulty = (
            float(variants[best_network_variant][4]) if best_network_variant else 0.0
        )

        # Never miss a potential block candidate when at least one share-valid
        # compatibility variant also meets the network target.
        if network_pass_variants:
            selected_name = max(network_pass_variants, key=lambda n: variants[n][4])
        elif not variants[selected_name][5] and STRATUM_COMPAT_ACCEPT_VARIANTS and pass_variants:
            selected_name = max(pass_variants, key=lambda n: variants[n][4])

        header_bytes, header_hash_bin, hash_int_big, hash_int_little, share_difficulty, meets_share_target, meets_network_target = variants[selected_name]
        meets_little = hash_int_little <= share_target
        meets_big = hash_int_big <= share_target

        alt_difficulty_variants = {
            name: float(row[4]) for name, row in variants.items() if name != selected_name
        }
        matched_variant = selected_name if selected_name != "canonical" else None
        selected_version_label = selected_name.split(":", 1)[0]
        final_version_int = final_version_candidates.get(selected_version_label, final_version_candidates["canonical"])

        tx_count = _encode_varint(1 + len(job.tx_datas))
        block_hex = header_bytes.hex() + tx_count.hex() + coinbase_bytes.hex() + "".join(job.tx_datas)

        return {
            "hash_int": hash_int_little,
            "block_hash": header_hash_bin.hex(),
            "hash_hex_be": header_hash_bin.hex(),
            "hash_int_big": hash_int_big,
            "hash_int_little": hash_int_little,
            "hash_le_hex": header_hash_bin[::-1].hex(),
            "meets_share_target": meets_share_target,
            "meets_network_target": meets_network_target,
            "meets_big": meets_big,
            "meets_little": meets_little,
            "share_target": share_target,
            "network_target": network_target,
            "block_hex": block_hex,
            "header_hex": header_bytes.hex(),
            "coinbase_hex": coinbase_bytes.hex(),
            "coinbase_hash_hex": sha256d(coinbase_bytes).hex(),
            "coinbase_txid_hash_hex": sha256d(coinbase_txid_bytes).hex(),
            "merkle_root_hex": merkle_root_bytes.hex(),
            "share_difficulty": share_difficulty,
            "computed_diff_big": share_difficulty,
            "alt_difficulty_variants": alt_difficulty_variants,
            "matched_variant": matched_variant,
            "selected_variant": selected_name,
            "canonical_difficulty": canonical_difficulty,
            "best_share_variant": best_share_variant,
            "best_share_variant_difficulty": best_share_variant_difficulty,
            "best_network_variant": best_network_variant,
            "best_network_variant_difficulty": best_network_variant_difficulty,
            "variant_diagnostics": variant_diagnostics,
            "effective_version_hex": f"{final_version_int:08x}",
            "base_version_hex": f"{job_version_int:08x}",
            "submitted_version_hex": (
                f"{submitted_version_int:08x}" if submitted_version_int is not None else None
            ),
            "version_mask_hex": f"{mask_int:08x}",
            "prevhash_hex": job.prevhash_be,
            "meets_target": meets_share_target,
            "assigned_difficulty": effective_difficulty,
        }

    async def _write_json(self, writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
        if writer is None:
            return
        writer.write((json.dumps(payload) + "\n").encode("utf-8"))
        await writer.drain()

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "id": req_id,
            "result": None,
            "error": [code, message, None],
        }


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _load_coin_configs() -> dict[str, CoinConfig]:
    dgb_algo = os.getenv("DGB_ALGO", "sha256d").strip().lower()
    if dgb_algo != "sha256d":
        logger.warning("DGB_ALGO=%s is unsupported; forcing sha256d", dgb_algo)
        dgb_algo = "sha256d"

    return {
        "BTC": CoinConfig(
            coin="BTC",
            algo="sha256d",
            stratum_port=_env_int("BTC_STRATUM_PORT", 3333),
            rpc_url=os.getenv("BTC_RPC_URL", "http://host.docker.internal:8332"),
            rpc_user=os.getenv("BTC_RPC_USER", ""),
            rpc_password=os.getenv("BTC_RPC_PASSWORD", ""),
        ),
        "BCH": CoinConfig(
            coin="BCH",
            algo="sha256d",
            stratum_port=_env_int("BCH_STRATUM_PORT", 3334),
            rpc_url=os.getenv("BCH_RPC_URL", "http://host.docker.internal:8333"),
            rpc_user=os.getenv("BCH_RPC_USER", ""),
            rpc_password=os.getenv("BCH_RPC_PASSWORD", ""),
        ),
        "DGB": CoinConfig(
            coin="DGB",
            algo=dgb_algo,
            stratum_port=_env_int("DGB_STRATUM_PORT", 3335),
            rpc_url=os.getenv("DGB_RPC_URL", "http://host.docker.internal:14022"),
            rpc_user=os.getenv("DGB_RPC_USER", ""),
            rpc_password=os.getenv("DGB_RPC_PASSWORD", ""),
        ),
    }


def _serialize_config(configs: dict[str, CoinConfig]) -> dict[str, Any]:
    payload = {
        coin: {
            "rpc_url": cfg.rpc_url,
            "rpc_user": cfg.rpc_user,
            "rpc_password": cfg.rpc_password,
            "stratum_port": cfg.stratum_port,
            "algo": cfg.algo,
        }
        for coin, cfg in configs.items()
    }
    payload["dgb_payout_address"] = _CONFIG_DGB_PAYOUT_ADDRESS
    return payload


def _load_overrides_from_disk(configs: dict[str, CoinConfig]) -> dict[str, CoinConfig]:
    global _CONFIG_DGB_PAYOUT_ADDRESS
    if not os.path.exists(CONFIG_PATH):
        return configs

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)

        for coin in ("BTC", "BCH", "DGB"):
            if coin not in payload:
                continue
            row = payload[coin] or {}
            cfg = configs[coin]
            cfg.rpc_url = str(row.get("rpc_url") or cfg.rpc_url)
            cfg.rpc_user = str(row.get("rpc_user") or cfg.rpc_user)
            cfg.rpc_password = str(row.get("rpc_password") or cfg.rpc_password)
            if row.get("stratum_port"):
                try:
                    cfg.stratum_port = int(row["stratum_port"])
                except (TypeError, ValueError):
                    logger.warning("Invalid %s stratum_port override: %s", coin, row.get("stratum_port"))

        payout_address = str(
            payload.get("dgb_payout_address")
            or ((payload.get("DGB_SETTINGS") or {}).get("payout_address") if isinstance(payload.get("DGB_SETTINGS"), dict) else "")
            or _CONFIG_DGB_PAYOUT_ADDRESS
            or ""
        ).strip()
        if payout_address:
            try:
                _scriptpubkey_from_dgb_address(payout_address)
                _CONFIG_DGB_PAYOUT_ADDRESS = payout_address
            except Exception:
                logger.warning("Ignoring invalid configured dgb_payout_address in %s", CONFIG_PATH)

        configs["DGB"].algo = "sha256d"
        return configs
    except Exception as exc:
        logger.error("Failed loading stratum config overrides (%s): %s", CONFIG_PATH, exc)
        return configs


def _save_overrides_to_disk(configs: dict[str, CoinConfig]) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(_serialize_config(configs), f, indent=2)


def _swap_endian_words_32(hex_data: str) -> str:
    """Reverse 4-byte word order across a 32-byte hash (Stratum prevhash convention)."""
    raw = bytes.fromhex(hex_data)
    if len(raw) != 32:
        raise ValueError("expected 32-byte hash")
    words = [raw[i : i + 4] for i in range(0, 32, 4)]
    return b"".join(reversed(words)).hex()


def sha256d(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def build_coinbase(
    coinb1_hex: str,
    extranonce1_hex: str,
    extranonce2_hex: str,
    coinb2_hex: str,
) -> bytes:
    # IMPORTANT: extranonce2 is appended as raw bytes exactly as provided.
    coinbase = (
        bytes.fromhex(coinb1_hex)
        + bytes.fromhex(extranonce1_hex)
        + bytes.fromhex(extranonce2_hex)
        + bytes.fromhex(coinb2_hex)
    )
    assert isinstance(coinbase, bytes)
    assert len(coinbase) > 0
    return coinbase


def build_merkle_root(coinbase_bytes: bytes, merkle_branch_hex_list: list[str]) -> bytes:
    assert isinstance(coinbase_bytes, bytes)
    assert len(coinbase_bytes) > 0

    # Internal merkle math uses little-endian hash bytes.
    root = sha256d(coinbase_bytes)
    for branch_hex in merkle_branch_hex_list:
        branch = bytes.fromhex(branch_hex)
        if len(branch) != 32:
            raise ValueError("invalid merkle branch hash length")
        # Notify merkle branches are already provided in the same little-endian
        # byte convention used by the internal merkle accumulator.
        root = sha256d(root + branch)

    assert len(root) == 32
    # Return display-order bytes for logging/callers; header builder will
    # convert back to little-endian placement.
    return root[::-1]


def build_header(
    final_version_int: int,
    prevhash_hex: str,
    merkle_root_bytes: bytes,
    ntime_hex: str,
    nbits_hex: str,
    nonce_hex: str,
) -> bytes:
    assert 0 <= final_version_int <= 0xFFFFFFFF
    assert len(merkle_root_bytes) == 32

    prevhash_be_bytes = bytes.fromhex(prevhash_hex)
    assert len(prevhash_be_bytes) == 32
    prevhash_le_bytes = prevhash_be_bytes[::-1]
    merkle_root_le_bytes = merkle_root_bytes[::-1]

    ntime_int = int(ntime_hex, 16)
    nbits_int = int(nbits_hex, 16)
    nonce_int = int(nonce_hex, 16)

    header = (
        struct.pack("<I", final_version_int)
        + prevhash_le_bytes
        + merkle_root_le_bytes
        + struct.pack("<I", ntime_int)
        + struct.pack("<I", nbits_int)
        + struct.pack("<I", nonce_int)
    )
    assert len(header) == 80
    return header


def hash_header(header_bytes: bytes) -> tuple[bytes, int]:
    assert len(header_bytes) == 80
    hash_bytes = sha256d(header_bytes)
    hash_int_big = int.from_bytes(hash_bytes, "big")
    return hash_bytes, hash_int_big


def validate_share(hash_int_big: int, share_target_int: int) -> bool:
    return hash_int_big <= share_target_int


def _sha256d(payload: bytes) -> bytes:
    return sha256d(payload)


def _target_from_nbits(nbits_hex: str) -> int:
    compact = int(nbits_hex, 16)
    exponent = compact >> 24
    mantissa = compact & 0x007FFFFF

    if exponent <= 3:
        target = mantissa >> (8 * (3 - exponent))
    else:
        target = mantissa << (8 * (exponent - 3))

    return max(0, min(target, (1 << 256) - 1))


def _difficulty_to_target(difficulty: float, target_1: int = TARGET_1) -> int:
    # Keep legacy helper name for compatibility; use canonical diff1 target math.
    return target_from_difficulty(difficulty)


def target_from_difficulty(diff: float) -> int:
    """
    Return share target as LITTLE-ENDIAN integer.
    """
    if diff <= 0:
        raise ValueError("diff must be > 0")

    # integer diff fast path
    d_int = int(round(diff))
    if abs(diff - d_int) <= 1e-9:
        return DIFF1_TARGET_INT // d_int

    # fractional diff safe path
    getcontext().prec = 80
    return int(Decimal(DIFF1_TARGET_INT) / Decimal(str(diff)))


def meets_share(hash256_bytes: bytes, share_target_int: int) -> bool:
    """
    Compare SHA256d digest using little-endian integer (Bitcoin PoW convention).
    """
    h = int.from_bytes(hash256_bytes, "little")
    return h <= share_target_int


def difficulty_from_hash(hash256_bytes: bytes) -> float:
    """
    Compute actual share difficulty from hash (little-endian).
    """
    getcontext().prec = 80
    h = int.from_bytes(hash256_bytes, "little")
    if h == 0:
        return float("inf")
    return float(Decimal(DIFF1_TARGET_INT) / Decimal(h))


def _encode_varint(value: int) -> bytes:
    if value < 0xFD:
        return bytes([value])
    if value <= 0xFFFF:
        return b"\xfd" + value.to_bytes(2, "little")
    if value <= 0xFFFFFFFF:
        return b"\xfe" + value.to_bytes(4, "little")
    return b"\xff" + value.to_bytes(8, "little")


def _normalize_block_reject_category(submit_result_raw: Any, rpc_error: str | None = None) -> str:
    if rpc_error:
        return "rpc_error"

    raw = str(submit_result_raw or "").strip().lower()
    if raw in {"", "none", "null"}:
        return "accepted"
    if "duplicate" in raw or raw in {"duplicate", "duplicate-invalid", "duplicate-inconclusive"}:
        return "duplicate"
    if "stale" in raw or "old" in raw:
        return "stale"
    if "invalid" in raw:
        return "invalid"
    if "time-too-new" in raw or "time too new" in raw:
        return "time_too_new"
    if "time-too-old" in raw or "time too old" in raw:
        return "time_too_old"
    if "high-hash" in raw:
        return "high_hash"
    if "bad-" in raw:
        return "bad_block"
    return "other_reject"


def _scriptnum_encode(value: int) -> bytes:
    if value == 0:
        return b""
    result = bytearray()
    neg = value < 0
    value = abs(value)
    while value:
        result.append(value & 0xFF)
        value >>= 8
    if result[-1] & 0x80:
        result.append(0x80 if neg else 0x00)
    elif neg:
        result[-1] |= 0x80
    return bytes(result)


def _push_data(data: bytes) -> bytes:
    if len(data) < 0x4C:
        return bytes([len(data)]) + data
    raise ValueError("pushdata too long for scaffold")


_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CHARSET_REV = {c: i for i, c in enumerate(_BECH32_CHARSET)}


def _bech32_polymod(values: list[int]) -> int:
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            if (top >> i) & 1:
                chk ^= generator[i]
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_verify_checksum(hrp: str, data: list[int]) -> bool:
    return _bech32_polymod(_bech32_hrp_expand(hrp) + data) == 1


def _bech32_decode(bech: str) -> tuple[str, list[int]]:
    value = (bech or "").strip()
    if not value:
        raise ValueError("empty bech32 string")
    if value.lower() != value and value.upper() != value:
        raise ValueError("mixed case bech32 string")

    value = value.lower()
    pos = value.rfind("1")
    if pos < 1 or pos + 7 > len(value):
        raise ValueError("invalid bech32 separator/length")

    hrp = value[:pos]
    data_part = value[pos + 1 :]
    data = []
    for char in data_part:
        if char not in _BECH32_CHARSET_REV:
            raise ValueError("invalid bech32 character")
        data.append(_BECH32_CHARSET_REV[char])

    if not _bech32_verify_checksum(hrp, data):
        raise ValueError("invalid bech32 checksum")

    return hrp, data[:-6]


def _convertbits(data: list[int], from_bits: int, to_bits: int, pad: bool) -> bytes:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << to_bits) - 1
    max_acc = (1 << (from_bits + to_bits - 1)) - 1
    for value in data:
        if value < 0 or (value >> from_bits):
            raise ValueError("invalid convertbits input")
        acc = ((acc << from_bits) | value) & max_acc
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            ret.append((acc >> bits) & maxv)

    if pad:
        if bits:
            ret.append((acc << (to_bits - bits)) & maxv)
    elif bits >= from_bits or ((acc << (to_bits - bits)) & maxv):
        raise ValueError("invalid padding in convertbits")

    return bytes(ret)


def _base58check_decode(value: str) -> tuple[int, bytes]:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    num = 0
    for char in value:
        idx = alphabet.find(char)
        if idx < 0:
            raise ValueError("invalid base58 character")
        num = num * 58 + idx

    full = num.to_bytes((num.bit_length() + 7) // 8, "big") if num > 0 else b""
    leading_ones = len(value) - len(value.lstrip("1"))
    decoded = (b"\x00" * leading_ones) + full
    if len(decoded) < 5:
        raise ValueError("invalid base58 payload length")

    payload, checksum = decoded[:-4], decoded[-4:]
    expected = sha256d(payload)[:4]
    if checksum != expected:
        raise ValueError("invalid base58 checksum")
    version = payload[0]
    body = payload[1:]
    return version, body


def _scriptpubkey_from_dgb_address(address: str) -> bytes:
    addr = (address or "").strip()
    lower = addr.lower()

    if lower.startswith("dgb1"):
        hrp, data = _bech32_decode(lower)
        if hrp != "dgb":
            raise ValueError("unexpected bech32 hrp")
        if not data:
            raise ValueError("missing witness program")

        witness_version = data[0]
        witness_program = _convertbits(data[1:], 5, 8, False)
        if witness_version != 0:
            raise ValueError("unsupported witness version")
        if len(witness_program) not in (20, 32):
            raise ValueError("unexpected witness program length")

        return bytes([0x00, len(witness_program)]) + witness_program

    version, payload = _base58check_decode(addr)
    if len(payload) != 20:
        raise ValueError("unexpected base58 payload length")

    # DigiByte mainnet commonly uses:
    # - P2PKH version 0x1e (D...)
    # - P2SH  version 0x3f
    # Also allow BTC-style versions for compatibility in custom deployments.
    if version in (0x1E, 0x00):
        return b"\x76\xa9\x14" + payload + b"\x88\xac"
    if version in (0x3F, 0x05):
        return b"\xa9\x14" + payload + b"\x87"

    raise ValueError(f"unsupported address version: {version}")


def _resolve_dgb_coinbase_script_pubkey_hex(payout_address: str | None = None) -> str:
    # Strict mode by request:
    # Use ONLY runtime worker address prefix (before '.') as payout destination.
    # No static env/script fallback paths are permitted.
    address = (payout_address or "").strip()
    if not address:
        raise RuntimeError(
            "No runtime DGB payout address resolved from connected worker names"
        )

    script = _scriptpubkey_from_dgb_address(address)
    logger.info("DGB coinbase payout configured from runtime worker address %s", address)
    return script.hex()


def _build_dgb_coinbase_parts(
    tpl: dict[str, Any],
    payout_address: str | None = None,
) -> tuple[str, str, str, str]:
    coinbase_value = int(tpl.get("coinbasevalue", 0))
    height = int(tpl.get("height", 0))
    script_pubkey_hex = _resolve_dgb_coinbase_script_pubkey_hex(payout_address=payout_address)
    script_pubkey = bytes.fromhex(script_pubkey_hex)
    witness_commitment_hex = str(tpl.get("default_witness_commitment") or "").strip()
    is_segwit_template = bool(witness_commitment_hex)

    witness_commitment_script = b""
    if is_segwit_template:
        if len(witness_commitment_hex) % 2 != 0:
            raise ValueError("invalid default_witness_commitment hex length")
        witness_commitment_script = bytes.fromhex(witness_commitment_hex)
        if not witness_commitment_script:
            raise ValueError("empty default_witness_commitment")

    height_script = _push_data(_scriptnum_encode(height))
    tag_script = _push_data(b"HMM-DGB")
    witness_reserved_value = b"\x00" * 32

    script_prefix = height_script + tag_script
    script_suffix = b""

    script_sig_len = len(script_prefix) + DGB_EXTRANONCE1_SIZE + DGB_EXTRANONCE2_SIZE + len(script_suffix)
    script_sig_len_enc = _encode_varint(script_sig_len)

    version = (1).to_bytes(4, "little")
    segwit_marker_flag = b"\x00\x01"
    vin_count = _encode_varint(1)
    prevout = b"\x00" * 32 + (0xFFFFFFFF).to_bytes(4, "little")

    sequence = (0xFFFFFFFF).to_bytes(4, "little")
    payout_outputs = 1 + (1 if is_segwit_template else 0)
    vout_count = _encode_varint(payout_outputs)
    value = coinbase_value.to_bytes(8, "little", signed=False)
    script_pubkey_len = _encode_varint(len(script_pubkey))
    witness_commitment_output = b""
    if is_segwit_template:
        witness_commitment_output = (
            (0).to_bytes(8, "little", signed=False)
            + _encode_varint(len(witness_commitment_script))
            + witness_commitment_script
        )
    coinbase_witness = b""
    if is_segwit_template:
        coinbase_witness = _encode_varint(1) + _push_data(witness_reserved_value)
    locktime = (0).to_bytes(4, "little")

    coinb1 = (
        version
        + (segwit_marker_flag if is_segwit_template else b"")
        + vin_count
        + prevout
        + script_sig_len_enc
        + script_prefix
    )
    coinb2 = (
        script_suffix
        + sequence
        + vout_count
        + value
        + script_pubkey_len
        + script_pubkey
        + witness_commitment_output
        + coinbase_witness
        + locktime
    )

    # txid serialization for merkle root must omit marker/flag and witness.
    coinb1_txid = (
        version
        + vin_count
        + prevout
        + script_sig_len_enc
        + script_prefix
    )
    coinb2_txid = (
        script_suffix
        + sequence
        + vout_count
        + value
        + script_pubkey_len
        + script_pubkey
        + witness_commitment_output
        + locktime
    )

    return coinb1.hex(), coinb2.hex(), coinb1_txid.hex(), coinb2_txid.hex()


def _build_coinbase_merkle_branch(tpl: dict[str, Any], coinbase_hash: bytes) -> list[str]:
    tx_hashes: list[bytes] = []
    for tx in tpl.get("transactions", []):
        tx_hex = tx.get("txid") or tx.get("hash")
        if isinstance(tx_hex, str) and len(tx_hex) == 64:
            tx_hashes.append(bytes.fromhex(tx_hex)[::-1])

    level = [coinbase_hash] + tx_hashes
    if not level:
        return []

    index = 0
    branch: list[str] = []

    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])

        sibling = index ^ 1
        branch.append(level[sibling].hex())

        next_level: list[bytes] = []
        for i in range(0, len(level), 2):
            next_level.append(_sha256d(level[i] + level[i + 1]))

        level = next_level
        index //= 2

    return branch


def _dgb_job_from_template(
    tpl: dict[str, Any],
    target_1: int = TARGET_1,
    payout_address: str | None = None,
) -> ActiveJob:
    job_id = uuid.uuid4().hex[:16]
    prevhash_be = str(tpl["previousblockhash"])
    prevhash = _swap_endian_words_32(prevhash_be)
    version = f"{int(tpl['version']) & 0xFFFFFFFF:08x}"
    nbits = str(tpl["bits"])
    ntime = f"{int(tpl['curtime']) & 0xFFFFFFFF:08x}"

    coinb1, coinb2, coinb1_txid, coinb2_txid = _build_dgb_coinbase_parts(
        tpl,
        payout_address=payout_address,
    )
    dummy_ex1 = "00" * DGB_EXTRANONCE1_SIZE
    dummy_ex2 = "00" * DGB_EXTRANONCE2_SIZE
    coinbase_hash = _sha256d(bytes.fromhex(coinb1_txid + dummy_ex1 + dummy_ex2 + coinb2_txid))
    merkle_branch = _build_coinbase_merkle_branch(tpl, coinbase_hash)

    return ActiveJob(
        job_id=job_id,
        prevhash=prevhash,
        prevhash_be=prevhash_be,
        coinb1=coinb1,
        coinb2=coinb2,
        coinb1_txid=coinb1_txid,
        coinb2_txid=coinb2_txid,
        merkle_branch=merkle_branch,
        version=version,
        nbits=nbits,
        ntime=ntime,
        clean_jobs=True,
        template_height=tpl.get("height"),
        target_1=target_1,
        tx_datas=[str(tx.get("data", "")) for tx in tpl.get("transactions", []) if tx.get("data")],
    )


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _DGB_POLLER_TASK

    _difficulty_self_test()
    await _DATASTORE.start()
    await _restart_servers()
    try:
        yield
    finally:
        if _DGB_POLLER_TASK:
            _DGB_POLLER_TASK.cancel()
            try:
                await _DGB_POLLER_TASK
            except asyncio.CancelledError:
                pass

        for server in _SERVERS.values():
            await server.stop()
        await _DATASTORE.stop()


app = FastAPI(title="HMM-Local Stratum Gateway", version="0.2.0", lifespan=_lifespan)

_BIND_HOST = os.getenv("STRATUM_BIND_HOST", "0.0.0.0")
_CONFIGS = _load_overrides_from_disk(_load_coin_configs())
_DATASTORE = StratumDataStore(DATABASE_URL)
_SERVERS: dict[str, StratumServer] = {
    coin: StratumServer(
        config=cfg,
        bind_host=_BIND_HOST,
        rpc_client=RpcClient(cfg),
        data_store=_DATASTORE,
    )
    for coin, cfg in _CONFIGS.items()
}
_DGB_POLLER_TASK: asyncio.Task | None = None
_DGB_LAST_PAYOUT_ADDRESS: str | None = (_CONFIG_DGB_PAYOUT_ADDRESS or None)
_DGB_PROPOSAL_GUARD = ProposalGuardState(
    required_consecutive_passes=PROPOSAL_GUARD_REQUIRED_CONSECUTIVE_PASSES,
)


async def _rpc_test_coin(coin: str) -> dict[str, Any]:
    normalized = coin.upper()
    if normalized not in _CONFIGS:
        raise HTTPException(status_code=404, detail=f"Unknown coin: {coin}")

    cfg = _CONFIGS[normalized]
    client = RpcClient(cfg)

    try:
        chain = await client.call("getblockchaininfo")
        mining = await client.call("getmininginfo")
        if normalized == "DGB":
            gbt = await _fetch_dgb_block_template(client)
        else:
            gbt = await client.call("getblocktemplate", [{"rules": ["segwit"]}])
        return {
            "ok": True,
            "coin": normalized,
            "chain": chain.get("chain"),
            "blocks": chain.get("blocks"),
            "headers": chain.get("headers"),
            "initialblockdownload": chain.get("initialblockdownload"),
            "networkhashps": mining.get("networkhashps"),
            "template_height": gbt.get("height"),
        }
    except Exception as exc:
        return {"ok": False, "coin": normalized, "error": str(exc)}


def _resolve_runtime_dgb_payout_address(server: StratumServer) -> str | None:
    worker_prefixes: dict[str, int] = {}
    for session in server._sessions.values():
        worker_name = (session.worker_name or "").strip()
        if "." not in worker_name:
            continue
        addr = worker_name.split(".", 1)[0].strip()
        if addr.lower().startswith("dgb1"):
            worker_prefixes[addr] = worker_prefixes.get(addr, 0) + 1
    if worker_prefixes:
        return max(worker_prefixes.items(), key=lambda item: item[1])[0]
    return None


async def _run_dgb_proposal_guard_check(
    client: RpcClient,
    tpl: dict[str, Any],
    payout_address: str,
) -> tuple[bool, str]:
    job = _dgb_job_from_template(
        tpl,
        target_1=TARGET_1,
        payout_address=payout_address,
    )
    ex1 = "00" * DGB_EXTRANONCE1_SIZE
    ex2 = "00" * DGB_EXTRANONCE2_SIZE
    coinbase = build_coinbase(job.coinb1, ex1, ex2, job.coinb2)
    coinbase_txid = build_coinbase(job.coinb1_txid, ex1, ex2, job.coinb2_txid)
    merkle = build_merkle_root(coinbase_txid, job.merkle_branch)
    header = build_header(
        int(job.version, 16),
        job.prevhash_be,
        merkle,
        job.ntime,
        job.nbits,
        "00000000",
    )
    tx_count = _encode_varint(1 + len(job.tx_datas)).hex()
    block_hex = header.hex() + tx_count + coinbase.hex() + "".join(job.tx_datas)
    proposal_result = await client.call("getblocktemplate", [{"mode": "proposal", "data": block_hex}])
    if proposal_result in (None, "", "null"):
        return True, "accepted"
    return False, str(proposal_result)


async def _dgb_template_poller() -> None:
    global _DGB_LAST_PAYOUT_ADDRESS
    cfg = _CONFIGS["DGB"]
    server = _SERVERS["DGB"]
    client = RpcClient(cfg)
    last_template_sig: str | None = None
    last_snapshot_minute: datetime | None = None

    logger.info("Starting DGB template poller (%ss)", DGB_TEMPLATE_POLL_SECONDS)
    while True:
        try:
            chain = await client.call("getblockchaininfo")
            mining = await client.call("getmininginfo")
            tpl = await _fetch_dgb_block_template(client)

            now = datetime.now(timezone.utc)
            server.stats.rpc_last_ok_at = now.isoformat()
            server.stats.rpc_last_error = None
            server.stats.chain_height = chain.get("blocks")

            template_sig = (
                f"{tpl.get('previousblockhash')}:{tpl.get('curtime')}:{tpl.get('bits')}:{tpl.get('pow_algo')}"
            )
            template_changed = template_sig != last_template_sig
            if template_sig != last_template_sig:
                last_template_sig = template_sig
                runtime_payout_address = _resolve_runtime_dgb_payout_address(server)
                if runtime_payout_address:
                    _DGB_LAST_PAYOUT_ADDRESS = runtime_payout_address
                elif _DGB_LAST_PAYOUT_ADDRESS:
                    runtime_payout_address = _DGB_LAST_PAYOUT_ADDRESS
                elif _CONFIG_DGB_PAYOUT_ADDRESS:
                    runtime_payout_address = _CONFIG_DGB_PAYOUT_ADDRESS
                    _DGB_LAST_PAYOUT_ADDRESS = runtime_payout_address

                if not runtime_payout_address:
                    reason = "no_runtime_or_cached_payout_address"
                    _DGB_PROPOSAL_GUARD.mark_fail(reason, int(tpl.get("height") or 0))
                    logger.warning("DGB proposal guard fail: %s", reason)
                else:
                    proposal_ok, proposal_reason = await _run_dgb_proposal_guard_check(
                        client,
                        tpl,
                        runtime_payout_address,
                    )
                    if proposal_ok:
                        _DGB_PROPOSAL_GUARD.mark_pass(int(tpl.get("height") or 0))
                    else:
                        _DGB_PROPOSAL_GUARD.mark_fail(proposal_reason, int(tpl.get("height") or 0))
                        logger.warning("DGB proposal guard fail: %s", proposal_reason)

                    job = _dgb_job_from_template(
                        tpl,
                        target_1=TARGET_1,
                        payout_address=runtime_payout_address,
                    )
                    await server.set_job(job)
                    logger.info("DGB new template -> job %s (height=%s)", job.job_id, tpl.get("height"))

            minute_ts = now.replace(second=0, microsecond=0)
            should_snapshot = template_changed or minute_ts != last_snapshot_minute
            if should_snapshot and _DATASTORE.enabled:
                bits = str(tpl.get("bits") or "")
                network_target = _target_from_nbits(bits) if bits else None
                network_difficulty = (
                    float(Decimal(DIFF1_TARGET_INT) / Decimal(max(network_target or 0, 1)))
                    if network_target
                    else None
                )
                network_hash_ps_raw = mining.get("networkhashps")
                network_hash_ps = float(network_hash_ps_raw) if network_hash_ps_raw is not None else None
                await _DATASTORE.enqueue_network_snapshot(
                    {
                        "ts": now,
                        "coin": "DGB",
                        "chain_height": chain.get("blocks"),
                        "template_height": tpl.get("height"),
                        "job_id": server.stats.current_job_id,
                        "bits": bits or None,
                        "network_target": (f"{network_target:064x}" if network_target else None),
                        "network_difficulty": network_difficulty,
                        "network_hash_ps": network_hash_ps,
                        "template_previous_blockhash": tpl.get("previousblockhash"),
                        "template_curtime": tpl.get("curtime"),
                        "template_changed": bool(template_changed),
                        "created_at": now,
                    }
                )
                last_snapshot_minute = minute_ts
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            server.stats.rpc_last_error = str(exc)
            logger.warning("DGB template poll failed: %s", exc)

        await asyncio.sleep(DGB_TEMPLATE_POLL_SECONDS)


async def _fetch_dgb_block_template(client: RpcClient) -> dict[str, Any]:
    attempts: list[tuple[str, list[Any]]] = [
        ("segwit-primary", [{"rules": ["segwit"]}, "sha256d"]),
        ("segwit-no-pow-arg", [{"rules": ["segwit"]}]),
        ("non-segwit-empty-rules", [{"rules": []}, "sha256d"]),
    ]

    last_error: Exception | None = None
    for label, params in attempts:
        try:
            tpl = await client.call("getblocktemplate", params)
            logger.info("DGB getblocktemplate mode=%s", label)
            return tpl
        except Exception as exc:
            last_error = exc
            logger.warning("DGB getblocktemplate mode=%s failed: %s", label, exc)

    raise RuntimeError(f"DGB getblocktemplate failed for all modes: {last_error}")


async def _restart_servers() -> None:
    global _DGB_POLLER_TASK

    if _DGB_POLLER_TASK:
        _DGB_POLLER_TASK.cancel()
        try:
            await _DGB_POLLER_TASK
        except asyncio.CancelledError:
            pass
        _DGB_POLLER_TASK = None

    for server in _SERVERS.values():
        await server.stop()

    for coin, cfg in _CONFIGS.items():
        _SERVERS[coin] = StratumServer(
            config=cfg,
            bind_host=_BIND_HOST,
            rpc_client=RpcClient(cfg),
            data_store=_DATASTORE,
        )

    for server in _SERVERS.values():
        await server.start()

    _DGB_POLLER_TASK = asyncio.create_task(_dgb_template_poller())


def _difficulty_self_test() -> None:
    sample_diff = DGB_STATIC_DIFFICULTY
    share_target_int = target_from_difficulty(sample_diff)
    diff_check = DIFF1_TARGET_INT / max(share_target_int, 1)
    logger.info(
        "difficulty self-test: assigned_diff=%s share_target_hex=%064x diff_check≈%.12f",
        sample_diff,
        share_target_int,
        diff_check,
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "healthy",
        "service": "hmm-local-stratum",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "coins": {
            coin: {
                "algo": server.config.algo,
                "stratum_port": server.config.stratum_port,
            }
            for coin, server in _SERVERS.items()
        },
    }


@app.get("/health/database")
@app.get("/api/health/database")
async def health_database() -> dict[str, Any]:
    return await _DATASTORE.get_database_health_snapshot()


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>HMM Local Stratum</title>
    <link rel=\"icon\" type=\"image/svg+xml\" href=\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0%25' stop-color='%230b1220'/%3E%3Cstop offset='100%25' stop-color='%231e3a8a'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width='64' height='64' rx='14' fill='url(%23g)'/%3E%3Cpath d='M17 22h30l-8 10 8 10H17l8-10z' fill='%2338bdf8'/%3E%3Ccircle cx='49' cy='20' r='6' fill='%2322d3ee'/%3E%3C/svg%3E\">
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background: #0f172a; color: #e2e8f0; }
    .wrap { max-width: 980px; margin: 24px auto; padding: 0 16px; }
    .card { background: #111827; border: 1px solid #334155; border-radius: 12px; padding: 16px; margin-bottom: 16px; }
    h1 { margin: 0 0 12px; font-size: 24px; }
    h2 { margin: 0 0 12px; font-size: 18px; }
    .grid { display: grid; grid-template-columns: 120px 1fr 1fr 1fr; gap: 8px; align-items: center; }
    label { color: #94a3b8; font-size: 13px; }
    input { width: 100%; padding: 8px; border-radius: 8px; border: 1px solid #475569; background: #0b1220; color: #e2e8f0; }
    button { background: #2563eb; color: white; border: 0; padding: 10px 14px; border-radius: 8px; cursor: pointer; }
    button.secondary { background: #334155; }
    .row { display: flex; gap: 8px; margin-top: 12px; }
    pre { background: #020617; border: 1px solid #334155; border-radius: 8px; padding: 10px; overflow: auto; }
    .muted { color: #94a3b8; font-size: 13px; }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <div class=\"card\">
      <h1>HMM Local Stratum Gateway</h1>
      <div class=\"muted\">DGB is locked to SHA256d. Save applies immediately and restarts listeners.</div>
    </div>

    <div class=\"card\">
      <h2>RPC Configuration</h2>
      <div class=\"grid\" style=\"font-weight:600;\"><div>Coin</div><div>RPC URL</div><div>RPC User</div><div>RPC Password</div></div>
      <div class=\"grid\" style=\"margin-top:8px;\">
        <label>BTC</label><input id=\"BTC_rpc_url\" /><input id=\"BTC_rpc_user\" /><input id=\"BTC_rpc_password\" type=\"password\" />
        <label>BCH</label><input id=\"BCH_rpc_url\" /><input id=\"BCH_rpc_user\" /><input id=\"BCH_rpc_password\" type=\"password\" />
        <label>DGB</label><input id=\"DGB_rpc_url\" /><input id=\"DGB_rpc_user\" /><input id=\"DGB_rpc_password\" type=\"password\" />
      </div>
      <div class=\"row\">
        <button onclick=\"saveConfig()\">Save + Restart Listeners</button>
        <button class=\"secondary\" onclick=\"loadConfig()\">Reload</button>
        <button class=\"secondary\" onclick=\"testDgbRpc()\">Test DGB RPC</button>
      </div>
            <div class="row" style="align-items:center;">
                <label style="min-width:220px;">DGB payout address fallback</label>
                <input id="dgb_payout_address" placeholder="dgb1..." />
            </div>
      <div id=\"status\" class=\"muted\" style=\"margin-top:8px;\"></div>
    </div>

    <div class=\"card\">
      <h2>Live Stats</h2>
      <pre id=\"stats\">loading...</pre>
    </div>
  </div>

  <script>
    async function loadConfig() {
      const r = await fetch('/config');
      const cfg = await r.json();
      for (const coin of ['BTC', 'BCH', 'DGB']) {
        document.getElementById(`${coin}_rpc_url`).value = cfg[coin].rpc_url || '';
        document.getElementById(`${coin}_rpc_user`).value = cfg[coin].rpc_user || '';
        document.getElementById(`${coin}_rpc_password`).value = cfg[coin].rpc_password || '';
      }
            document.getElementById('dgb_payout_address').value = cfg.dgb_payout_address || '';
      document.getElementById('status').textContent = 'Config loaded';
    }

    async function saveConfig() {
      const payload = {};
      for (const coin of ['BTC', 'BCH', 'DGB']) {
        payload[coin] = {
          rpc_url: document.getElementById(`${coin}_rpc_url`).value,
          rpc_user: document.getElementById(`${coin}_rpc_user`).value,
          rpc_password: document.getElementById(`${coin}_rpc_password`).value,
        };
      }
            payload.dgb_payout_address = document.getElementById('dgb_payout_address').value;

      const r = await fetch('/config', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (!r.ok) {
        document.getElementById('status').textContent = `Save failed: ${await r.text()}`;
        return;
      }
      document.getElementById('status').textContent = 'Saved and listeners restarted';
      await loadStats();
    }

    async function testDgbRpc() {
      const r = await fetch('/rpc/test/DGB');
      const result = await r.json();
      document.getElementById('status').textContent = result.ok
        ? `DGB RPC OK (height ${result.blocks}, template ${result.template_height})`
        : `DGB RPC failed: ${result.error}`;
      await loadStats();
    }

    async function loadStats() {
      const r = await fetch('/stats');
      const s = await r.json();
      document.getElementById('stats').textContent = JSON.stringify(s, null, 2);
    }

    loadConfig();
    loadStats();
    setInterval(loadStats, 5000);
  </script>
</body>
</html>
"""


@app.get("/config")
async def get_config() -> dict[str, Any]:
    return _serialize_config(_CONFIGS)


@app.post("/config")
async def update_config(payload: dict[str, Any]) -> dict[str, Any]:
    global _CONFIG_DGB_PAYOUT_ADDRESS, _DGB_LAST_PAYOUT_ADDRESS
    for coin in ("BTC", "BCH", "DGB"):
        if coin not in payload:
            continue
        row = payload[coin] or {}
        cfg = _CONFIGS[coin]
        if "rpc_url" in row:
            cfg.rpc_url = str(row.get("rpc_url") or "")
        if "rpc_user" in row:
            cfg.rpc_user = str(row.get("rpc_user") or "")
        if "rpc_password" in row:
            cfg.rpc_password = str(row.get("rpc_password") or "")

    if "dgb_payout_address" in payload:
        candidate = str(payload.get("dgb_payout_address") or "").strip()
        if candidate:
            try:
                _scriptpubkey_from_dgb_address(candidate)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"invalid_dgb_payout_address: {exc}") from exc
        _CONFIG_DGB_PAYOUT_ADDRESS = candidate
        _DGB_LAST_PAYOUT_ADDRESS = candidate or _DGB_LAST_PAYOUT_ADDRESS

    _CONFIGS["DGB"].algo = "sha256d"

    try:
        _save_overrides_to_disk(_CONFIGS)
        await _restart_servers()
    except Exception as exc:
        logger.error("Failed applying config update: %s", exc)
        raise HTTPException(status_code=500, detail=f"config_update_failed: {exc}") from exc

    return {"ok": True, "config": _serialize_config(_CONFIGS)}


@app.get("/rpc/test/{coin}")
async def rpc_test(coin: str) -> dict[str, Any]:
    return await _rpc_test_coin(coin)


@app.get("/debug/last-share/{coin}")
async def debug_last_share(coin: str) -> dict[str, Any]:
    normalized = coin.upper()
    if normalized not in _SERVERS:
        raise HTTPException(status_code=404, detail=f"Unknown coin: {coin}")

    server = _SERVERS[normalized]
    if not server._last_share_debug:
        return {"ok": False, "coin": normalized, "message": "No captured share debug yet"}

    return {"ok": True, "coin": normalized, "data": server._last_share_debug}


@app.get("/debug/last-shares")
async def debug_last_shares(worker: Optional[str] = None, n: int = 50) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for coin, server in _SERVERS.items():
        for row in server.get_last_shares(worker=worker, n=n):
            rows.append({"coin": coin, **row})

    rows.sort(key=lambda r: r.get("ts") or "")
    if len(rows) > n:
        rows = rows[-n:]

    return {"ok": True, "worker": worker, "count": len(rows), "shares": rows}


def _serialize_row(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


class PoolSnapshotHashrateModel(BaseModel):
    ts: str | None = None
    snapshot_age_seconds: float | None = None
    pool_hashrate_hs: float | None = None
    accepted_shares: int | None = None
    accepted_diff_sum: float | None = None


class PoolSnapshotNetworkModel(BaseModel):
    ts: str | None = None
    snapshot_age_seconds: float | None = None
    chain_height: int | None = None
    template_height: int | None = None
    job_id: str | None = None
    network_hash_ps: float | None = None
    network_difficulty: float | None = None
    template_changed: bool | None = None


class PoolSnapshotKPIModel(BaseModel):
    ts: str | None = None
    snapshot_age_seconds: float | None = None
    share_accept_count: int | None = None
    share_reject_count: int | None = None
    share_reject_rate_pct: float | None = None
    block_accept_count_24h: int | None = None
    block_reject_count_24h: int | None = None
    block_accept_rate_pct_24h: float | None = None
    expected_time_to_block_sec: float | None = None
    pool_share_of_network_pct: float | None = None


class PoolSnapshotRejectsModel(BaseModel):
    window_minutes: int
    total_rejected: int
    by_reason: dict[str, int] = Field(default_factory=dict)


class PoolSnapshotWorkerModel(BaseModel):
    worker: str
    ts: str | None = None
    snapshot_age_seconds: float | None = None
    accepted_shares: int
    accepted_diff_sum: float
    est_hashrate_hs: float


class PoolSnapshotWorkersModel(BaseModel):
    count: int
    rows: list[PoolSnapshotWorkerModel] = Field(default_factory=list)


class PoolSnapshotQualityModel(BaseModel):
    data_freshness_seconds: float | None = None
    has_required_inputs: bool
    stale: bool
    readiness: str
    missing_inputs: list[str] = Field(default_factory=list)


class PoolSnapshotResponse(BaseModel):
    ok: bool
    coin: str
    window_minutes: int
    generated_at: str | None = None
    message: str | None = None
    hashrate: PoolSnapshotHashrateModel | None = None
    network: PoolSnapshotNetworkModel | None = None
    kpi: PoolSnapshotKPIModel | None = None
    rejects: PoolSnapshotRejectsModel | None = None
    workers: PoolSnapshotWorkersModel | None = None
    quality: PoolSnapshotQualityModel | None = None


class PoolSnapshotCollectionResponse(BaseModel):
    ok: bool
    window_minutes: int
    count: int
    snapshots: list[PoolSnapshotResponse] = Field(default_factory=list)


def _seconds_since(ts: datetime | None) -> float | None:
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return max(0.0, (now - ts).total_seconds())


def _build_pool_snapshot_sync(coin: str, window_minutes: int = 15) -> dict[str, Any]:
    if not _DATASTORE.enabled or _DATASTORE.engine is None:
        return {"ok": False, "message": "datastore disabled", "coin": coin}

    normalized_coin = coin.upper()
    now = datetime.now(timezone.utc)
    window_minutes = max(1, min(window_minutes, 240))

    with _DATASTORE.engine.begin() as conn:
        kpi_row = conn.execute(
            select(_DATASTORE.kpi_snapshots)
            .where(_DATASTORE.kpi_snapshots.c.coin == normalized_coin)
            .where(_DATASTORE.kpi_snapshots.c.window_minutes == window_minutes)
            .order_by(desc(_DATASTORE.kpi_snapshots.c.ts))
            .limit(1)
        ).mappings().first()

        network_row = conn.execute(
            select(_DATASTORE.network_snapshots)
            .where(_DATASTORE.network_snapshots.c.coin == normalized_coin)
            .order_by(desc(_DATASTORE.network_snapshots.c.ts))
            .limit(1)
        ).mappings().first()

        hashrate_pool_row = conn.execute(
            select(_DATASTORE.hashrate_snapshots)
            .where(_DATASTORE.hashrate_snapshots.c.coin == normalized_coin)
            .where(_DATASTORE.hashrate_snapshots.c.worker == "__pool__")
            .where(_DATASTORE.hashrate_snapshots.c.window_minutes == window_minutes)
            .order_by(desc(_DATASTORE.hashrate_snapshots.c.ts))
            .limit(1)
        ).mappings().first()

        worker_rows = conn.execute(
            select(_DATASTORE.hashrate_snapshots)
            .where(_DATASTORE.hashrate_snapshots.c.coin == normalized_coin)
            .where(_DATASTORE.hashrate_snapshots.c.worker != "__pool__")
            .where(_DATASTORE.hashrate_snapshots.c.window_minutes == window_minutes)
            .order_by(desc(_DATASTORE.hashrate_snapshots.c.ts))
            .limit(200)
        ).mappings().all()

        reject_since = now - timedelta(minutes=window_minutes)
        reject_rows = conn.execute(
            select(_DATASTORE.share_metrics)
            .where(_DATASTORE.share_metrics.c.coin == normalized_coin)
            .where(_DATASTORE.share_metrics.c.ts >= reject_since)
            .where(_DATASTORE.share_metrics.c.accepted == False)
            .order_by(desc(_DATASTORE.share_metrics.c.ts))
            .limit(5000)
        ).mappings().all()

    worker_latest: dict[str, dict[str, Any]] = {}
    for row in worker_rows:
        worker = str(row.get("worker") or "unknown")
        existing = worker_latest.get(worker)
        ts = row.get("ts")
        if existing is None:
            worker_latest[worker] = dict(row)
            continue
        prev_ts = existing.get("ts")
        if isinstance(ts, datetime) and isinstance(prev_ts, datetime) and ts > prev_ts:
            worker_latest[worker] = dict(row)

    workers = []
    for worker, row in sorted(worker_latest.items(), key=lambda kv: kv[0]):
        ts = row.get("ts")
        workers.append(
            {
                "worker": worker,
                "ts": ts.isoformat() if isinstance(ts, datetime) else None,
                "snapshot_age_seconds": _seconds_since(ts),
                "accepted_shares": int(row.get("accepted_shares") or 0),
                "accepted_diff_sum": float(row.get("accepted_diff_sum") or 0.0),
                "est_hashrate_hs": float(row.get("est_hashrate_hs") or 0.0),
            }
        )

    reject_by_reason: dict[str, int] = {}
    for row in reject_rows:
        reason = str(row.get("reject_reason") or "unknown")
        reject_by_reason[reason] = reject_by_reason.get(reason, 0) + 1

    kpi_ts = kpi_row.get("ts") if kpi_row else None
    network_ts = network_row.get("ts") if network_row else None
    hashrate_ts = hashrate_pool_row.get("ts") if hashrate_pool_row else None

    ages = [
        age
        for age in (_seconds_since(kpi_ts), _seconds_since(network_ts), _seconds_since(hashrate_ts))
        if age is not None
    ]
    data_freshness_seconds = max(ages) if ages else None
    missing_inputs: list[str] = []
    if hashrate_pool_row is None:
        missing_inputs.append("hashrate")
    if network_row is None:
        missing_inputs.append("network")
    if kpi_row is None:
        missing_inputs.append("kpi")

    has_required_inputs = len(missing_inputs) == 0
    stale = (not has_required_inputs) or bool(
        data_freshness_seconds is not None and data_freshness_seconds > 300.0
    )
    readiness = "ready"
    if not has_required_inputs:
        readiness = "unready"
    elif stale:
        readiness = "stale"

    return {
        "ok": True,
        "coin": normalized_coin,
        "window_minutes": window_minutes,
        "generated_at": now.isoformat(),
        "hashrate": {
            "ts": hashrate_ts.isoformat() if isinstance(hashrate_ts, datetime) else None,
            "snapshot_age_seconds": _seconds_since(hashrate_ts),
            "pool_hashrate_hs": (
                float(hashrate_pool_row.get("est_hashrate_hs") or 0.0) if hashrate_pool_row else None
            ),
            "accepted_shares": (
                int(hashrate_pool_row.get("accepted_shares") or 0) if hashrate_pool_row else None
            ),
            "accepted_diff_sum": (
                float(hashrate_pool_row.get("accepted_diff_sum") or 0.0) if hashrate_pool_row else None
            ),
        },
        "network": {
            "ts": network_ts.isoformat() if isinstance(network_ts, datetime) else None,
            "snapshot_age_seconds": _seconds_since(network_ts),
            "chain_height": (int(network_row.get("chain_height")) if network_row and network_row.get("chain_height") is not None else None),
            "template_height": (int(network_row.get("template_height")) if network_row and network_row.get("template_height") is not None else None),
            "job_id": (str(network_row.get("job_id")) if network_row and network_row.get("job_id") else None),
            "network_hash_ps": (
                float(network_row.get("network_hash_ps")) if network_row and network_row.get("network_hash_ps") is not None else None
            ),
            "network_difficulty": (
                float(network_row.get("network_difficulty")) if network_row and network_row.get("network_difficulty") is not None else None
            ),
            "template_changed": (bool(network_row.get("template_changed")) if network_row else None),
        },
        "kpi": {
            "ts": kpi_ts.isoformat() if isinstance(kpi_ts, datetime) else None,
            "snapshot_age_seconds": _seconds_since(kpi_ts),
            "share_accept_count": (int(kpi_row.get("share_accept_count") or 0) if kpi_row else None),
            "share_reject_count": (int(kpi_row.get("share_reject_count") or 0) if kpi_row else None),
            "share_reject_rate_pct": (
                float(kpi_row.get("share_reject_rate_pct")) if kpi_row and kpi_row.get("share_reject_rate_pct") is not None else None
            ),
            "block_accept_count_24h": (int(kpi_row.get("block_accept_count_24h") or 0) if kpi_row else None),
            "block_reject_count_24h": (int(kpi_row.get("block_reject_count_24h") or 0) if kpi_row else None),
            "block_accept_rate_pct_24h": (
                float(kpi_row.get("block_accept_rate_pct_24h")) if kpi_row and kpi_row.get("block_accept_rate_pct_24h") is not None else None
            ),
            "expected_time_to_block_sec": (
                float(kpi_row.get("expected_time_to_block_sec")) if kpi_row and kpi_row.get("expected_time_to_block_sec") is not None else None
            ),
            "pool_share_of_network_pct": (
                float(kpi_row.get("pool_share_of_network_pct")) if kpi_row and kpi_row.get("pool_share_of_network_pct") is not None else None
            ),
        },
        "rejects": {
            "window_minutes": window_minutes,
            "total_rejected": len(reject_rows),
            "by_reason": reject_by_reason,
        },
        "workers": {
            "count": len(workers),
            "rows": workers,
        },
        "quality": {
            "data_freshness_seconds": data_freshness_seconds,
            "has_required_inputs": has_required_inputs,
            "stale": stale,
            "readiness": readiness,
            "missing_inputs": missing_inputs,
        },
    }


@app.get("/debug/block-attempts")
async def debug_block_attempts(n: int = 100) -> dict[str, Any]:
    if not _DATASTORE.enabled or _DATASTORE.engine is None:
        return {"ok": False, "message": "datastore disabled"}

    limit = max(1, min(n, 1000))
    with _DATASTORE.engine.begin() as conn:
        rows = conn.execute(
            select(_DATASTORE.block_attempts)
            .order_by(desc(_DATASTORE.block_attempts.c.id))
            .limit(limit)
        ).mappings().all()

    return {
        "ok": True,
        "count": len(rows),
        "attempts": [_serialize_row(r) for r in reversed(rows)],
    }


@app.get("/debug/block-reject-summary")
async def debug_block_reject_summary(hours: int = 24) -> dict[str, Any]:
    if not _DATASTORE.enabled or _DATASTORE.engine is None:
        return {"ok": False, "message": "datastore disabled"}

    hours = max(1, min(hours, 24 * 30))
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    stmt = (
        select(_DATASTORE.block_attempts)
        .where(_DATASTORE.block_attempts.c.ts >= since)
        .order_by(desc(_DATASTORE.block_attempts.c.id))
    )
    with _DATASTORE.engine.begin() as conn:
        rows = conn.execute(stmt).mappings().all()

    by_category: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    accepted = 0
    rejected = 0

    for row in rows:
        category = str(row.get("reject_category") or "unknown")
        reason = str(row.get("reject_reason") or "-")
        by_category[category] = by_category.get(category, 0) + 1
        by_reason[reason] = by_reason.get(reason, 0) + 1
        if bool(row.get("accepted_by_node")):
            accepted += 1
        else:
            rejected += 1

    return {
        "ok": True,
        "hours": hours,
        "since": since.isoformat(),
        "count": len(rows),
        "accepted": accepted,
        "rejected": rejected,
        "by_category": by_category,
        "by_reason": by_reason,
    }


@app.get("/debug/share-metrics")
async def debug_share_metrics(worker: Optional[str] = None, n: int = 200) -> dict[str, Any]:
    if not _DATASTORE.enabled or _DATASTORE.engine is None:
        return {"ok": False, "message": "datastore disabled"}

    limit = max(1, min(n, 5000))
    stmt = select(_DATASTORE.share_metrics).order_by(desc(_DATASTORE.share_metrics.c.id)).limit(limit)
    if worker:
        stmt = (
            select(_DATASTORE.share_metrics)
            .where(_DATASTORE.share_metrics.c.worker == worker)
            .order_by(desc(_DATASTORE.share_metrics.c.id))
            .limit(limit)
        )

    with _DATASTORE.engine.begin() as conn:
        rows = conn.execute(stmt).mappings().all()

    return {
        "ok": True,
        "worker": worker,
        "count": len(rows),
        "rows": [_serialize_row(r) for r in reversed(rows)],
    }


@app.get("/debug/share-rollups")
async def debug_share_rollups(worker: Optional[str] = None, n: int = 500) -> dict[str, Any]:
    if not _DATASTORE.enabled or _DATASTORE.engine is None:
        return {"ok": False, "message": "datastore disabled"}

    limit = max(1, min(n, 5000))
    stmt = (
        select(_DATASTORE.share_rollups_1m)
        .order_by(desc(_DATASTORE.share_rollups_1m.c.id))
        .limit(limit)
    )
    if worker:
        stmt = (
            select(_DATASTORE.share_rollups_1m)
            .where(_DATASTORE.share_rollups_1m.c.worker == worker)
            .order_by(desc(_DATASTORE.share_rollups_1m.c.id))
            .limit(limit)
        )

    with _DATASTORE.engine.begin() as conn:
        rows = conn.execute(stmt).mappings().all()

    return {
        "ok": True,
        "worker": worker,
        "count": len(rows),
        "rows": [_serialize_row(r) for r in reversed(rows)],
    }


@app.get("/debug/hashrate-snapshots")
async def debug_hashrate_snapshots(
    coin: Optional[str] = None,
    worker: Optional[str] = None,
    window_minutes: Optional[int] = None,
    n: int = 500,
) -> dict[str, Any]:
    if not _DATASTORE.enabled or _DATASTORE.engine is None:
        return {"ok": False, "message": "datastore disabled"}

    limit = max(1, min(n, 5000))
    stmt = select(_DATASTORE.hashrate_snapshots)
    if coin:
        stmt = stmt.where(_DATASTORE.hashrate_snapshots.c.coin == coin.upper())
    if worker:
        stmt = stmt.where(_DATASTORE.hashrate_snapshots.c.worker == worker)
    if window_minutes is not None:
        stmt = stmt.where(_DATASTORE.hashrate_snapshots.c.window_minutes == int(window_minutes))

    stmt = stmt.order_by(desc(_DATASTORE.hashrate_snapshots.c.id)).limit(limit)

    with _DATASTORE.engine.begin() as conn:
        rows = conn.execute(stmt).mappings().all()

    return {
        "ok": True,
        "coin": coin.upper() if coin else None,
        "worker": worker,
        "window_minutes": window_minutes,
        "count": len(rows),
        "rows": [_serialize_row(r) for r in reversed(rows)],
    }


@app.get("/debug/network-snapshots")
async def debug_network_snapshots(
    coin: Optional[str] = None,
    n: int = 500,
) -> dict[str, Any]:
    if not _DATASTORE.enabled or _DATASTORE.engine is None:
        return {"ok": False, "message": "datastore disabled"}

    limit = max(1, min(n, 5000))
    stmt = select(_DATASTORE.network_snapshots)
    if coin:
        stmt = stmt.where(_DATASTORE.network_snapshots.c.coin == coin.upper())

    stmt = stmt.order_by(desc(_DATASTORE.network_snapshots.c.id)).limit(limit)

    with _DATASTORE.engine.begin() as conn:
        rows = conn.execute(stmt).mappings().all()

    return {
        "ok": True,
        "coin": coin.upper() if coin else None,
        "count": len(rows),
        "rows": [_serialize_row(r) for r in reversed(rows)],
    }


@app.get("/debug/kpi-snapshots")
async def debug_kpi_snapshots(
    coin: Optional[str] = None,
    n: int = 500,
) -> dict[str, Any]:
    if not _DATASTORE.enabled or _DATASTORE.engine is None:
        return {"ok": False, "message": "datastore disabled"}

    limit = max(1, min(n, 5000))
    stmt = select(_DATASTORE.kpi_snapshots)
    if coin:
        stmt = stmt.where(_DATASTORE.kpi_snapshots.c.coin == coin.upper())

    stmt = stmt.order_by(desc(_DATASTORE.kpi_snapshots.c.id)).limit(limit)

    with _DATASTORE.engine.begin() as conn:
        rows = conn.execute(stmt).mappings().all()

    return {
        "ok": True,
        "coin": coin.upper() if coin else None,
        "count": len(rows),
        "rows": [_serialize_row(r) for r in reversed(rows)],
    }


@app.get("/debug/worker-events")
async def debug_worker_events(
    worker: Optional[str] = None,
    event: Optional[str] = None,
    n: int = 500,
) -> dict[str, Any]:
    if not _DATASTORE.enabled or _DATASTORE.engine is None:
        return {"ok": False, "message": "datastore disabled"}

    limit = max(1, min(n, 5000))
    stmt = select(_DATASTORE.worker_events)
    if worker:
        stmt = stmt.where(_DATASTORE.worker_events.c.worker == worker)
    if event:
        stmt = stmt.where(_DATASTORE.worker_events.c.event == event)

    stmt = stmt.order_by(desc(_DATASTORE.worker_events.c.id)).limit(limit)

    with _DATASTORE.engine.begin() as conn:
        rows = conn.execute(stmt).mappings().all()

    return {
        "ok": True,
        "worker": worker,
        "event": event,
        "count": len(rows),
        "rows": [_serialize_row(r) for r in reversed(rows)],
    }


@app.get("/debug/worker-summary")
async def debug_worker_summary(worker: Optional[str] = None, hours: int = 6) -> dict[str, Any]:
    if not _DATASTORE.enabled or _DATASTORE.engine is None:
        return {"ok": False, "message": "datastore disabled"}

    hours = max(1, min(hours, 24 * 30))
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    metrics_stmt = select(_DATASTORE.share_metrics).where(_DATASTORE.share_metrics.c.ts >= since)
    events_stmt = select(_DATASTORE.worker_events).where(_DATASTORE.worker_events.c.ts >= since)
    rollup_stmt = select(_DATASTORE.share_rollups_1m).where(_DATASTORE.share_rollups_1m.c.bucket_ts >= since)

    if worker:
        metrics_stmt = metrics_stmt.where(_DATASTORE.share_metrics.c.worker == worker)
        events_stmt = events_stmt.where(_DATASTORE.worker_events.c.worker == worker)
        rollup_stmt = rollup_stmt.where(_DATASTORE.share_rollups_1m.c.worker == worker)

    with _DATASTORE.engine.begin() as conn:
        metric_rows = conn.execute(metrics_stmt).mappings().all()
        event_rows = conn.execute(events_stmt).mappings().all()
        rollup_rows = conn.execute(rollup_stmt).mappings().all()

    by_worker: dict[str, dict[str, Any]] = {}

    def _worker_bucket(name: str) -> dict[str, Any]:
        bucket = by_worker.get(name)
        if bucket is None:
            bucket = {
                "worker": name,
                "accepted": 0,
                "rejected": 0,
                "rejected_low_diff": 0,
                "rejected_duplicate": 0,
                "reject_reasons": {},
                "avg_assigned_diff": 0.0,
                "avg_computed_diff": 0.0,
                "assigned_diff_sum": 0.0,
                "computed_diff_sum": 0.0,
                "share_count": 0,
                "event_counts": {},
                "last_share_at": None,
                "last_event_at": None,
                "rollup_points": 0,
                "accepted_spm_window": 0.0,
            }
            by_worker[name] = bucket
        return bucket

    for row in metric_rows:
        w = str(row.get("worker") or "unknown")
        bucket = _worker_bucket(w)

        accepted = bool(row.get("accepted"))
        reason = str(row.get("reject_reason") or "-")
        ts = row.get("ts")
        if accepted:
            bucket["accepted"] += 1
        else:
            bucket["rejected"] += 1
            if reason == "low_difficulty_share":
                bucket["rejected_low_diff"] += 1
            elif reason == "duplicate_share":
                bucket["rejected_duplicate"] += 1
            bucket["reject_reasons"][reason] = bucket["reject_reasons"].get(reason, 0) + 1

        bucket["assigned_diff_sum"] += float(row.get("assigned_diff") or 0.0)
        bucket["computed_diff_sum"] += float(row.get("computed_diff") or 0.0)
        bucket["share_count"] += 1
        if isinstance(ts, datetime):
            iso = ts.isoformat()
            if bucket["last_share_at"] is None or iso > bucket["last_share_at"]:
                bucket["last_share_at"] = iso

    for row in event_rows:
        w = str(row.get("worker") or "unknown")
        bucket = _worker_bucket(w)
        ev = str(row.get("event") or "unknown")
        ts = row.get("ts")
        bucket["event_counts"][ev] = bucket["event_counts"].get(ev, 0) + 1
        if isinstance(ts, datetime):
            iso = ts.isoformat()
            if bucket["last_event_at"] is None or iso > bucket["last_event_at"]:
                bucket["last_event_at"] = iso

    for row in rollup_rows:
        w = str(row.get("worker") or "unknown")
        bucket = _worker_bucket(w)
        bucket["rollup_points"] += 1

    window_minutes = max(float(hours) * 60.0, 1.0)
    summaries: list[dict[str, Any]] = []
    for bucket in by_worker.values():
        share_count = max(int(bucket["share_count"]), 1)
        bucket["avg_assigned_diff"] = float(bucket["assigned_diff_sum"]) / float(share_count)
        bucket["avg_computed_diff"] = float(bucket["computed_diff_sum"]) / float(share_count)
        bucket["accepted_spm_window"] = float(bucket["accepted"]) / window_minutes
        bucket.pop("assigned_diff_sum", None)
        bucket.pop("computed_diff_sum", None)
        bucket.pop("share_count", None)
        summaries.append(bucket)

    summaries.sort(key=lambda r: r.get("worker") or "")
    return {
        "ok": True,
        "hours": hours,
        "since": since.isoformat(),
        "worker": worker,
        "count": len(summaries),
        "workers": summaries,
    }


@app.get("/debug/datastore")
async def debug_datastore() -> dict[str, Any]:
    return {"ok": True, "datastore": _DATASTORE.snapshot()}


@app.get("/api/pool-snapshot/{coin}", response_model=PoolSnapshotResponse)
async def api_pool_snapshot_coin(coin: str, window_minutes: int = 15) -> PoolSnapshotResponse:
    normalized = coin.upper()
    if normalized not in _SERVERS:
        raise HTTPException(status_code=404, detail=f"Unknown coin: {coin}")
    return PoolSnapshotResponse.model_validate(
        _build_pool_snapshot_sync(normalized, window_minutes=window_minutes)
    )


@app.get("/api/pool-snapshot", response_model=PoolSnapshotCollectionResponse)
async def api_pool_snapshot(
    coin: Optional[str] = None,
    window_minutes: int = 15,
) -> PoolSnapshotCollectionResponse:
    bounded_window = max(1, min(window_minutes, 240))
    if coin:
        normalized = coin.upper()
        if normalized not in _SERVERS:
            raise HTTPException(status_code=404, detail=f"Unknown coin: {coin}")
        snapshots_raw = [_build_pool_snapshot_sync(normalized, window_minutes=bounded_window)]
        snapshots = [PoolSnapshotResponse.model_validate(s) for s in snapshots_raw]
        return PoolSnapshotCollectionResponse(
            ok=True,
            window_minutes=bounded_window,
            count=len(snapshots),
            snapshots=snapshots,
        )

    snapshots_raw = []
    for symbol in sorted(_SERVERS.keys()):
        snapshots_raw.append(_build_pool_snapshot_sync(symbol, window_minutes=bounded_window))

    snapshots = [PoolSnapshotResponse.model_validate(s) for s in snapshots_raw]
    return PoolSnapshotCollectionResponse(
        ok=True,
        window_minutes=bounded_window,
        count=len(snapshots),
        snapshots=snapshots,
    )


@app.get("/stats")
async def stats() -> dict[str, Any]:
    return {
        "service": "hmm-local-stratum",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "db_enabled": _DATASTORE.enabled,
        "datastore": _DATASTORE.snapshot(),
        "dgb_proposal_guard": _DGB_PROPOSAL_GUARD.snapshot(),
        "coins": {
            coin: {
                "algo": server.config.algo,
                "stratum_port": server.config.stratum_port,
                "rpc_url": server.config.rpc_url,
                **server.stats.snapshot(),
            }
            for coin, server in _SERVERS.items()
        },
    }


@app.get("/stats/{coin}")
async def stats_coin(coin: str) -> dict[str, Any]:
    normalized = coin.upper()
    if normalized not in _SERVERS:
        raise HTTPException(status_code=404, detail=f"Unknown coin: {coin}")

    server = _SERVERS[normalized]
    return {
        "coin": normalized,
        "db_enabled": _DATASTORE.enabled,
        "datastore": _DATASTORE.snapshot(),
        "algo": server.config.algo,
        "stratum_port": server.config.stratum_port,
        "rpc_url": server.config.rpc_url,
        "proposal_guard": (_DGB_PROPOSAL_GUARD.snapshot() if normalized == "DGB" else None),
        **server.stats.snapshot(),
    }


@app.get("/guard/dgb")
async def dgb_guard_status() -> dict[str, Any]:
    return {
        "ok": True,
        "coin": "DGB",
        "proposal_guard": _DGB_PROPOSAL_GUARD.snapshot(),
    }
