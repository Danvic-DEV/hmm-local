"""
API endpoints for miner anomaly detection and health monitoring
"""

import logging
import os
import sys
import asyncio
import resource
import subprocess
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy import select, and_, desc, text
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta
from typing import List, Optional, Any, Dict

from core.database import AsyncSessionLocal, Miner, HealthEvent, MinerBaseline, MinerHealthCurrent, engine
from core.db_pool_metrics import update_peaks, get_metrics

logger = logging.getLogger(__name__)

router = APIRouter()


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


def _get_process_rss_mb() -> float:
    """Get current process RSS in MB with best-effort fallbacks."""
    try:
        import psutil  # type: ignore

        return round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 2)
    except Exception:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # Linux returns KB, macOS returns bytes.
        if sys.platform == "darwin":
            return round(usage.ru_maxrss / (1024 * 1024), 2)
        return round(usage.ru_maxrss / 1024, 2)


def _get_top_processes(limit: int = 8) -> List[Dict[str, Any]]:
    """Get top RSS processes from inside the runtime environment."""
    try:
        import psutil  # type: ignore

        entries: List[Dict[str, Any]] = []
        for proc in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                info = proc.info
                rss_bytes = int(getattr(info.get("memory_info"), "rss", 0) or 0)
                entries.append(
                    {
                        "pid": int(info.get("pid") or 0),
                        "name": str(info.get("name") or "unknown"),
                        "rss_mb": round(rss_bytes / (1024 * 1024), 2),
                    }
                )
            except Exception:
                continue

        entries.sort(key=lambda row: row.get("rss_mb", 0.0), reverse=True)
        return entries[:limit]
    except Exception:
        try:
            # Fallback that works in minimal images.
            result = subprocess.run(
                ["ps", "-axo", "pid=,rss=,comm="],
                capture_output=True,
                text=True,
                check=True,
            )
            rows: List[Dict[str, Any]] = []
            for line in result.stdout.splitlines():
                parts = line.strip().split(maxsplit=2)
                if len(parts) < 3:
                    continue
                pid_str, rss_kb_str, name = parts
                try:
                    rss_mb = round(int(rss_kb_str) / 1024, 2)
                    rows.append({"pid": int(pid_str), "name": name, "rss_mb": rss_mb})
                except Exception:
                    continue

            rows.sort(key=lambda row: row.get("rss_mb", 0.0), reverse=True)
            return rows[:limit]
        except Exception:
            return []


@router.get("/runtime")
async def runtime_diagnostics():
    """Runtime diagnostics for memory and in-process cache visibility."""
    response: Dict[str, Any] = {
        "status": "healthy",
        "process": {
            "pid": os.getpid(),
            "rss_mb": _get_process_rss_mb(),
            "asyncio_task_count": len(asyncio.all_tasks()),
        },
        "system": {
            "top_processes_by_rss": _get_top_processes(limit=8),
        },
        "caches": {},
        "websocket": {},
        "scheduler": {},
    }

    # WebSocket manager instrumentation
    try:
        from api.websocket import manager as websocket_manager

        response["websocket"] = websocket_manager.get_stats()
    except Exception as e:
        response["websocket"] = {"error": str(e)}

    # Dashboard API caches
    try:
        from api import dashboard as dashboard_api

        response["caches"]["dashboard_all_cache_entries"] = len(getattr(dashboard_api, "_DASHBOARD_ALL_CACHE", {}))
        response["caches"]["dashboard_all_lock_entries"] = len(getattr(dashboard_api, "_DASHBOARD_ALL_COMPUTE_LOCKS", {}))
        response["caches"]["dashboard_earnings_cache_entries"] = len(getattr(dashboard_api, "_DASHBOARD_EARNINGS_CACHE", {}))
    except Exception as e:
        response["caches"]["dashboard"] = {"error": str(e)}

    # Pool dashboard cache
    try:
        from core.dashboard_pool_service import _POOL_DASHBOARD_CACHE

        response["caches"]["pool_dashboard_cache_entries"] = len(_POOL_DASHBOARD_CACHE)
    except Exception as e:
        response["caches"]["pool_dashboard"] = {"error": str(e)}

    # High-difficulty network difficulty cache
    try:
        from core.high_diff_tracker import _network_diff_cache

        response["caches"]["network_diff_cache_entries"] = len(_network_diff_cache)
    except Exception as e:
        response["caches"]["network_diff"] = {"error": str(e)}

    # Generic API cache stats
    try:
        from core.cache import api_cache

        response["caches"]["api_cache"] = await api_cache.get_stats()
    except Exception as e:
        response["caches"]["api_cache"] = {"error": str(e)}

    # Scheduler job memory deltas
    try:
        from core.scheduler import scheduler as scheduler_service

        response["scheduler"]["job_memory"] = scheduler_service.get_job_memory_diagnostics(limit=30)
    except Exception as e:
        response["scheduler"]["job_memory"] = {"error": str(e)}

    return response


@router.get("/database")
async def get_database_health():
    """Get database pool health metrics for UI widgets"""
    try:
        pool = engine.pool
        pool_size = pool.size() if hasattr(pool, "size") else 0
        checked_out = pool.checkedout() if hasattr(pool, "checkedout") else 0
        overflow = pool.overflow() if hasattr(pool, "overflow") else 0
        total_capacity = pool_size + overflow
        utilization_pct = (checked_out / total_capacity * 100) if total_capacity > 0 else 0

        configured_pool_size = None
        configured_max_overflow = None
        try:
            configured_pool_size = getattr(getattr(pool, "_pool", None), "maxsize", None)
            if configured_pool_size is None:
                configured_pool_size = getattr(pool, "_maxsize", None)
            configured_max_overflow = getattr(pool, "_max_overflow", None)
        except Exception:
            configured_pool_size = None
            configured_max_overflow = None

        if configured_pool_size is None:
            configured_pool_size = pool_size
        if configured_max_overflow is None:
            configured_max_overflow = overflow
        configured_capacity = configured_pool_size + configured_max_overflow

        if utilization_pct > 90:
            status = "critical"
        elif utilization_pct > 80:
            status = "warning"
        else:
            status = "healthy"

        response = {
            "status": status,
            "pool": {
                "size": pool_size,
                "checked_out": checked_out,
                "overflow": overflow,
                "total_capacity": total_capacity,
                "max_size_configured": configured_pool_size,
                "max_overflow_configured": configured_max_overflow,
                "max_capacity_configured": configured_capacity,
                "utilization_percent": round(utilization_pct, 1)
            },
            "database_type": "postgresql"
        }

        # PostgreSQL-specific connection stats
        async with engine.begin() as conn:
            active_result = await conn.execute(text("""
                SELECT count(*) as active_connections
                FROM pg_stat_activity
                WHERE datname = current_database()
                AND state = 'active'
            """))
            active_row = active_result.fetchone()
            active_connections = active_row[0] if active_row else 0

            size_result = await conn.execute(text("""
                SELECT pg_database_size(current_database()) / 1024 / 1024 as size_mb
            """))
            size_row = size_result.fetchone()
            database_size_mb = float(size_row[0]) if size_row else 0.0

            long_result = await conn.execute(text("""
                SELECT count(*) as long_queries
                FROM pg_stat_activity
                WHERE datname = current_database()
                AND state = 'active'
                AND query_start < NOW() - INTERVAL '1 minute'
                AND query NOT LIKE '%pg_stat_activity%'
            """))
            long_row = long_result.fetchone()
            long_running_queries = long_row[0] if long_row else 0

        response["postgresql"] = {
            "active_connections": active_connections,
            "database_size_mb": round(database_size_mb, 1),
            "long_running_queries": long_running_queries
        }

        update_peaks(
            in_use=checked_out,
            active_queries=active_connections,
            slow_queries=long_running_queries
        )

        metrics = get_metrics()
        response["high_water_marks"] = {
            "last_24h": metrics.last_24h.to_dict(),
            "since_boot": metrics.since_boot.to_dict(),
            "last_24h_date": metrics.last_24h_date
        }

        return response
    except Exception as e:
        logger.error(f"Database health check failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get database health")


@router.get("/all")
async def get_all_miners_health(db: AsyncSession = Depends(get_db)):
    """Get latest health status for all miners"""
    # Get all miners
    result = await db.execute(
        select(Miner).where(Miner.enabled == True)
    )
    miners = result.scalars().all()
    
    health_data = []
    
    for miner in miners:
        # Get latest health event
        result = await db.execute(
            select(HealthEvent)
            .where(HealthEvent.miner_id == miner.id)
            .order_by(desc(HealthEvent.timestamp))
            .limit(1)
        )
        event = result.scalar_one_or_none()
        
        if event:
            health_data.append({
                "miner_id": miner.id,
                "miner_name": miner.name,
                "miner_type": miner.miner_type,
                "timestamp": event.timestamp.isoformat(),
                "health_score": event.health_score,
                "reasons": event.reasons,
                "anomaly_score": event.anomaly_score,
                "mode": event.mode,
                "has_issues": len(event.reasons) > 0
            })
    
    return {
        "total_miners": len(miners),
        "monitored_miners": len(health_data),
        "miners": health_data
    }


@router.get("/{miner_id}")
async def get_miner_health(
    miner_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get latest health status for a miner with full details"""
    # Get miner info
    miner_result = await db.execute(
        select(Miner).where(Miner.id == miner_id)
    )
    miner = miner_result.scalar_one_or_none()
    
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")
    
    # Get latest health event
    result = await db.execute(
        select(HealthEvent)
        .where(HealthEvent.miner_id == miner_id)
        .order_by(desc(HealthEvent.timestamp))
        .limit(1)
    )
    event = result.scalar_one_or_none()
    
    if not event:
        raise HTTPException(status_code=404, detail="No health data available")
    
    # Get suggested actions from health event if available
    from core.anomaly_detection import REASON_TO_ACTIONS
    suggested_actions = []
    if event.reasons:
        reason_codes = []
        for reason in event.reasons:
            if isinstance(reason, dict) and 'code' in reason:
                reason_codes.append(reason['code'])
        
        # Derive actions from reason codes
        actions_set = set()
        for code in reason_codes:
            if code in REASON_TO_ACTIONS:
                actions_set.update(REASON_TO_ACTIONS[code])
        suggested_actions = sorted(list(actions_set))
    
    return {
        "miner_id": miner.id,
        "miner_name": miner.name,
        "miner_type": miner.miner_type,
        "health_score": event.health_score,
        "status": event.status if hasattr(event, 'status') else _get_status_from_score(event.health_score),
        "anomaly_score": event.anomaly_score,
        "reasons": event.reasons or [],
        "suggested_actions": list(set(suggested_actions)),  # dedupe
        "mode": event.mode,
        "last_check": event.timestamp.isoformat()
    }


def _get_status_from_score(score: int) -> str:
    """Derive status from health score"""
    if score >= 80:
        return "healthy"
    elif score >= 60:
        return "warning"
    else:
        return "critical"


@router.get("/{miner_id}/history")
async def get_miner_health_history(
    miner_id: int,
    hours: int = 24,
    db: AsyncSession = Depends(get_db)
):
    """Get health history for a miner"""
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    
    result = await db.execute(
        select(HealthEvent)
        .where(
            and_(
                HealthEvent.miner_id == miner_id,
                HealthEvent.timestamp >= cutoff
            )
        )
        .order_by(HealthEvent.timestamp)
    )
    events = result.scalars().all()
    
    return [
        {
            "timestamp": event.timestamp.isoformat(),
            "health_score": event.health_score,
            "anomaly_score": event.anomaly_score,
            "status": event.status if hasattr(event, 'status') else _get_status_from_score(event.health_score)
        }
        for event in events
    ]
    result = await db.execute(
        select(HealthEvent)
        .where(
            and_(
                HealthEvent.miner_id == miner_id,
                HealthEvent.timestamp >= cutoff
            )
        )
        .order_by(HealthEvent.timestamp.desc())
    )
    events = result.scalars().all()
    
    return {
        "miner_id": miner_id,
        "hours": hours,
        "event_count": len(events),
        "events": [
            {
                "timestamp": e.timestamp.isoformat(),
                "health_score": e.health_score,
                "reasons": e.reasons,
                "anomaly_score": e.anomaly_score,
                "mode": e.mode
            }
            for e in events
        ]
    }


@router.get("/baselines/{miner_id}")
async def get_miner_baselines(
    miner_id: int,
    mode: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """Get performance baselines for a miner"""
    query = select(MinerBaseline).where(MinerBaseline.miner_id == miner_id)
    
    if mode is not None:
        query = query.where(MinerBaseline.mode == mode)
    
    result = await db.execute(query)
    baselines = result.scalars().all()
    
    if not baselines:
        raise HTTPException(status_code=404, detail="No baseline data available")
    
    return {
        "miner_id": miner_id,
        "mode": mode,
        "baselines": [
            {
                "metric_name": b.metric_name,
                "mode": b.mode,
                "median_value": b.median_value,
                "mad_value": b.mad_value,
                "sample_count": b.sample_count,
                "window_hours": b.window_hours,
                "updated_at": b.updated_at.isoformat()
            }
            for b in baselines
        ]
    }


@router.post("/baselines/update")
async def trigger_baseline_update(db: AsyncSession = Depends(get_db)):
    """Manually trigger baseline recalculation for all miners"""
    from core.anomaly_detection import update_baselines_for_all_miners
    
    try:
        await update_baselines_for_all_miners(db)
        return {"status": "success", "message": "Baselines updated"}
    except Exception as e:
        logger.error(f"Failed to update baselines: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/check")
async def trigger_health_check(db: AsyncSession = Depends(get_db)):
    """Manually trigger health check for all miners"""
    from core.anomaly_detection import check_all_miners_health
    
    try:
        await check_all_miners_health(db)
        return {"status": "success", "message": "Health check completed"}
    except Exception as e:
        logger.error(f"Failed to check health: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ml/train")
async def trigger_ml_training(db: AsyncSession = Depends(get_db)):
    """Manually trigger ML model training for all miners"""
    from core.ml_anomaly import train_all_models
    
    try:
        await train_all_models(db)
        return {"status": "success", "message": "ML model training completed"}
    except Exception as e:
        logger.error(f"Failed to train ML models: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ml/models")
async def list_ml_models():
    """List all trained ML models"""
    from core.ml_anomaly import MODELS_DIR
    import pickle
    
    models = []
    
    if not MODELS_DIR.exists():
        return {"models": []}
    
    for model_file in MODELS_DIR.glob("*.pkl"):
        meta_file = model_file.with_suffix(".meta")
        
        if meta_file.exists():
            try:
                with open(meta_file, "rb") as f:
                    metadata = pickle.load(f)
                
                models.append({
                    "name": model_file.stem,
                    "type": "per-miner" if model_file.stem.startswith("miner_") else "type",
                    "trained_at": metadata.get("trained_at"),
                    "sample_count": metadata.get("sample_count"),
                    "window_days": metadata.get("window_days")
                })
            except Exception as e:
                logger.error(f"Failed to read metadata for {model_file}: {e}")
    
    return {"models": models, "total": len(models)}

# ============================================================================
# PHASE C: CANONICAL MINER HEALTH ENDPOINTS (Output Layer)
# ============================================================================

@router.get("/miners/{miner_id}")
async def get_current_miner_health(
    miner_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get current canonical MinerHealth for a specific miner"""
    result = await db.execute(
        select(MinerHealthCurrent).where(MinerHealthCurrent.miner_id == miner_id)
    )
    current = result.scalar_one_or_none()
    
    if not current:
        raise HTTPException(status_code=404, detail="No health data available for this miner")
    
    # Return canonical MinerHealth object
    return {
        "miner_id": current.miner_id,
        "timestamp": current.timestamp.isoformat(),
        "health_score": current.health_score,
        "status": current.status,
        "anomaly_score": current.anomaly_score,
        "reasons": current.reasons,  # Array of structured reason objects
        "suggested_actions": current.suggested_actions,  # Array of action strings
        "mode": current.mode
    }


@router.get("/miners")
async def get_all_miners_health(
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Get current canonical MinerHealth for all miners
    
    Query parameters:
    - status: Filter by status ("healthy", "warning", "critical")
    """
    query = select(MinerHealthCurrent)
    
    # Apply status filter if provided
    if status:
        if status not in ["healthy", "warning", "critical"]:
            raise HTTPException(status_code=400, detail="Invalid status. Must be: healthy, warning, or critical")
        query = query.where(MinerHealthCurrent.status == status)
    
    result = await db.execute(query.order_by(MinerHealthCurrent.health_score))  # Worst first
    miners = result.scalars().all()
    
    # Get miner names for enrichment
    miner_ids = [m.miner_id for m in miners]
    result = await db.execute(select(Miner).where(Miner.id.in_(miner_ids)))
    miner_map = {m.id: m.name for m in result.scalars().all()}
    
    # Build canonical response
    return {
        "miners": [
            {
                "miner_id": m.miner_id,
                "miner_name": miner_map.get(m.miner_id, "Unknown"),
                "timestamp": m.timestamp.isoformat(),
                "health_score": m.health_score,
                "status": m.status,
                "anomaly_score": m.anomaly_score,
                "reasons": m.reasons,
                "suggested_actions": m.suggested_actions,
                "mode": m.mode,
                "updated_at": m.updated_at.isoformat()
            }
            for m in miners
        ],
        "total": len(miners),
        "filtered_by_status": status
    }


@router.get("/database")
async def database_health():
    """Get database connection pool and performance health"""
    from core.database import engine
    from sqlalchemy import text
    
    try:
        pool = engine.pool
        pool_size = pool.size()
        checked_out = pool.checkedout()
        overflow = pool.overflow() if hasattr(pool, 'overflow') else 0
        
        total_capacity = pool_size + overflow
        utilization_pct = (checked_out / total_capacity * 100) if total_capacity > 0 else 0
        
        # Determine status
        if utilization_pct > 90:
            status = "critical"
        elif utilization_pct > 80:
            status = "warning"
        else:
            status = "healthy"
        
        health_data = {
            "status": status,
            "pool": {
                "size": pool_size,
                "checked_out": checked_out,
                "overflow": overflow,
                "total_capacity": total_capacity,
                "utilization_percent": round(utilization_pct, 1)
            },
            "database_type": "postgresql"
        }
        
        # PostgreSQL-specific stats
        async with engine.begin() as conn:
            async with engine.begin() as conn:
                # Active connections
                result = await conn.execute(text("""
                    SELECT count(*) as active_connections
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                    AND state = 'active'
                """))
                row = result.fetchone()
                active_conns = row[0] if row else 0
                
                # Database size
                result = await conn.execute(text("""
                    SELECT pg_database_size(current_database()) as db_size
                """))
                row = result.fetchone()
                db_size_bytes = row[0] if row else 0
                db_size_mb = db_size_bytes / (1024 * 1024)
                
                # Long-running queries
                result = await conn.execute(text("""
                    SELECT count(*) as long_queries
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                    AND state = 'active'
                    AND query_start < NOW() - INTERVAL '1 minute'
                    AND query NOT LIKE '%pg_stat_activity%'
                """))
                row = result.fetchone()
                long_queries = row[0] if row else 0
                
                health_data["postgresql"] = {
                    "active_connections": active_conns,
                    "database_size_mb": round(db_size_mb, 2),
                    "long_running_queries": long_queries
                }
        
        return health_data
    
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        return {
            "status": "error",
            "error": str(e)
        }


@router.get("/database/indexes")
async def database_indexes():
    """Get PostgreSQL index health and usage statistics"""
    from core.database import engine
    from sqlalchemy import text
    
    try:
        async with engine.begin() as conn:
            # Unused indexes
            result = await conn.execute(text("""
                SELECT
                    schemaname,
                    tablename,
                    indexname,
                    idx_scan as scans,
                    pg_size_pretty(pg_relation_size(indexrelid)) as size,
                    pg_relation_size(indexrelid) as size_bytes
                FROM pg_stat_user_indexes
                WHERE idx_scan = 0
                AND indexrelid::regclass::text NOT LIKE '%_pkey'
                ORDER BY pg_relation_size(indexrelid) DESC
                LIMIT 20
            """))
            
            unused = [
                {
                    "schema": row[0],
                    "table": row[1],
                    "index": row[2],
                    "scans": row[3],
                    "size": row[4],
                    "size_bytes": row[5]
                }
                for row in result.fetchall()
            ]
            
            # Most used indexes
            result = await conn.execute(text("""
                SELECT
                    schemaname,
                    tablename,
                    indexname,
                    idx_scan as scans,
                    pg_size_pretty(pg_relation_size(indexrelid)) as size
                FROM pg_stat_user_indexes
                WHERE idx_scan > 0
                ORDER BY idx_scan DESC
                LIMIT 10
            """))
            
            most_used = [
                {
                    "schema": row[0],
                    "table": row[1],
                    "index": row[2],
                    "scans": row[3],
                    "size": row[4]
                }
                for row in result.fetchall()
            ]
            
            # Tables needing indexes (high sequential scans)
            result = await conn.execute(text("""
                SELECT
                    schemaname,
                    tablename,
                    seq_scan,
                    seq_tup_read,
                    idx_scan,
                    pg_size_pretty(pg_relation_size(relid)) as size
                FROM pg_stat_user_tables
                WHERE seq_scan > 100
                AND pg_relation_size(relid) > 1048576
                AND (idx_scan = 0 OR seq_scan > idx_scan * 5)
                ORDER BY seq_tup_read DESC
                LIMIT 10
            """))
            
            needs_indexes = [
                {
                    "schema": row[0],
                    "table": row[1],
                    "seq_scans": row[2],
                    "seq_rows_read": row[3],
                    "index_scans": row[4],
                    "size": row[5]
                }
                for row in result.fetchall()
            ]
            
            return {
                "unused_indexes": unused,
                "most_used_indexes": most_used,
                "tables_needing_indexes": needs_indexes,
                "summary": {
                    "unused_count": len(unused),
                    "unused_wasted_mb": round(sum(i['size_bytes'] for i in unused) / (1024 * 1024), 2)
                }
            }
    
    except Exception as e:
        logger.error(f"Index health check failed: {e}")
        return {"error": str(e)}
