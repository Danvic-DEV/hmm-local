"""
Analytics API endpoints
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Tuple, Union
from pydantic import BaseModel
import io
import csv
import statistics

from core.database import get_db, Miner, Telemetry, HealthScore
from core.health import HealthScoringService
from core.utils import format_hashrate
from api.time_utils import to_utc_iso8601


router = APIRouter()


class HealthScoreResponse(BaseModel):
    overall_score: float
    uptime_score: float
    temperature_score: Optional[float] = None
    hashrate_score: float
    reject_rate_score: float
    data_points: int
    period_hours: int


class HashrateFormatted(BaseModel):
    display: str
    value: float
    unit: str


class TelemetryStatsResponse(BaseModel):
    avg_hashrate: Union[HashrateFormatted, float, None]
    min_hashrate: Union[HashrateFormatted, float, None]
    max_hashrate: Union[HashrateFormatted, float, None]
    avg_temperature: Optional[float]
    max_temperature: Optional[float]
    avg_power: Optional[float]
    total_accepted: Optional[int]
    total_rejected: Optional[int]
    reject_rate: Optional[float]
    uptime_percent: float
    data_points: int


def _filter_high_outliers_mad(values: List[float]) -> List[float]:
    """Filter extreme high outliers using a robust median/MAD rule.

    This is intentionally conservative: it only removes obviously-corrupt spikes
    that would otherwise dominate the mean (e.g., a single 400M GH/s point).
    """
    if len(values) < 10:
        return values

    median = statistics.median(values)
    deviations = [abs(v - median) for v in values]
    mad = statistics.median(deviations)

    # If MAD is zero (flat series), only drop absurdly large spikes.
    if mad == 0:
        if median <= 0:
            return values
        filtered = [v for v in values if v <= (median * 10)]
        return filtered or values

    # Robust z-score constant for normal distributions
    z_threshold = 10.0
    filtered: List[float] = []
    for v in values:
        z = 0.6745 * (v - median) / mad
        if z <= z_threshold:
            filtered.append(v)

    return filtered or values


@router.get("/miners/{miner_id}/health", response_model=HealthScoreResponse)
async def get_miner_health(
    miner_id: int,
    hours: int = 24,
    db: AsyncSession = Depends(get_db)
):
    """Get current health score for a miner"""
    # Check miner exists
    result = await db.execute(select(Miner).where(Miner.id == miner_id))
    miner = result.scalar_one_or_none()
    
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")
    
    score_data = await HealthScoringService.calculate_health_score(miner_id, db, hours)
    
    if not score_data:
        raise HTTPException(status_code=404, detail="Insufficient data to calculate health score")
    
    return score_data


@router.get("/miners/{miner_id}/health/trend")
async def get_miner_health_trend(
    miner_id: int,
    days: int = 7,
    db: AsyncSession = Depends(get_db)
):
    """Get health score trend over time"""
    result = await db.execute(select(Miner).where(Miner.id == miner_id))
    miner = result.scalar_one_or_none()
    
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")
    
    trend_data = await HealthScoringService.get_health_trend(miner_id, db, days)
    
    return {"miner_id": miner_id, "miner_name": miner.name, "trend": trend_data}


@router.get("/miners/{miner_id}/telemetry/stats", response_model=TelemetryStatsResponse)
async def get_telemetry_stats(
    miner_id: int,
    hours: int = 24,
    db: AsyncSession = Depends(get_db)
):
    """Get aggregated telemetry statistics"""
    result = await db.execute(select(Miner).where(Miner.id == miner_id))
    miner = result.scalar_one_or_none()
    
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")
    
    cutoff_time = datetime.utcnow() - timedelta(hours=hours)
    
    # Get telemetry data
    result = await db.execute(
        select(Telemetry)
        .where(Telemetry.miner_id == miner_id)
        .where(Telemetry.timestamp >= cutoff_time)
        .order_by(Telemetry.timestamp.asc())
    )
    telemetry_data = result.scalars().all()
    
    if not telemetry_data:
        raise HTTPException(status_code=404, detail="No telemetry data found")
    
    # Calculate statistics
    # Hashrate is stored as numeric + unit; normalize to GH/s before aggregating.
    hashrates: List[float] = []
    for t in telemetry_data:
        if t.hashrate is None:
            continue
        unit = t.hashrate_unit or "GH/s"
        normalized = format_hashrate(t.hashrate, unit)["value"]
        if normalized and normalized > 0:
            hashrates.append(normalized)

    hashrates = _filter_high_outliers_mad(hashrates)
    temperatures = [t.temperature for t in telemetry_data if t.temperature is not None]
    powers = [t.power_watts for t in telemetry_data if t.power_watts is not None]
    
    first = telemetry_data[0]
    last = telemetry_data[-1]
    
    accepted_delta = (last.shares_accepted or 0) - (first.shares_accepted or 0)
    rejected_delta = (last.shares_rejected or 0) - (first.shares_rejected or 0)

    # Handle miner restarts where counters reset (delta becomes negative)
    if accepted_delta < 0:
        accepted_delta = last.shares_accepted or 0
    if rejected_delta < 0:
        rejected_delta = last.shares_rejected or 0
    total_shares = accepted_delta + rejected_delta
    reject_rate = (rejected_delta / total_shares * 100) if total_shares > 0 else 0
    
    # Calculate uptime
    expected_points = hours * 120  # One every 30 seconds
    actual_points = len(telemetry_data)
    uptime_percent = min((actual_points / expected_points) * 100, 100)
    
    # Format hashrate values
    avg_hr = sum(hashrates) / len(hashrates) if hashrates else 0
    min_hr = min(hashrates) if hashrates else 0
    max_hr = max(hashrates) if hashrates else 0
    
    return {
        "avg_hashrate": format_hashrate(avg_hr, "GH/s"),
        "min_hashrate": format_hashrate(min_hr, "GH/s"),
        "max_hashrate": format_hashrate(max_hr, "GH/s"),
        "avg_temperature": sum(temperatures) / len(temperatures) if temperatures else None,
        "max_temperature": max(temperatures) if temperatures else None,
        "avg_power": sum(powers) / len(powers) if powers else None,
        "total_accepted": accepted_delta,
        "total_rejected": rejected_delta,
        "reject_rate": round(reject_rate, 2),
        "uptime_percent": round(uptime_percent, 2),
        "data_points": len(telemetry_data)
    }


@router.get("/miners/{miner_id}/telemetry/timeseries")
async def get_telemetry_timeseries(
    miner_id: int,
    hours: int = 24,
    metric: str = "hashrate",
    db: AsyncSession = Depends(get_db)
):
    """Get time-series data for charts"""
    result = await db.execute(select(Miner).where(Miner.id == miner_id))
    miner = result.scalar_one_or_none()
    
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")
    
    cutoff_time = datetime.utcnow() - timedelta(hours=hours)
    
    result = await db.execute(
        select(Telemetry)
        .where(Telemetry.miner_id == miner_id)
        .where(Telemetry.timestamp >= cutoff_time)
        .order_by(Telemetry.timestamp.asc())
    )
    telemetry_data = result.scalars().all()
    
    # Map metric to field
    metric_map = {
        "hashrate": "hashrate",
        "temperature": "temperature",
        "power": "power_watts"
    }
    
    if metric not in metric_map:
        raise HTTPException(status_code=400, detail=f"Invalid metric. Choose from: {', '.join(metric_map.keys())}")
    
    field = metric_map[metric]
    
    data_points = [
        {
            "timestamp": to_utc_iso8601(t.timestamp),
            "value": getattr(t, field)
        }
        for t in telemetry_data
        if getattr(t, field) is not None
    ]
    
    return {
        "miner_id": miner_id,
        "miner_name": miner.name,
        "metric": metric,
        "data": data_points
    }


@router.post("/miners/{miner_id}/telemetry/repair")
async def repair_telemetry_outliers(
    miner_id: int,
    hours: int = Query(default=24, ge=1, le=24 * 90),
    dry_run: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
):
    """Delete extreme hashrate outlier rows that would otherwise skew analytics.

    Uses the same median/MAD logic as analytics aggregation. This is designed to
    remove obviously corrupt spikes (e.g. a single 400M GH/s point).
    """
    result = await db.execute(select(Miner).where(Miner.id == miner_id))
    miner = result.scalar_one_or_none()
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")

    cutoff_time = datetime.utcnow() - timedelta(hours=hours)
    result = await db.execute(
        select(Telemetry)
        .where(Telemetry.miner_id == miner_id)
        .where(Telemetry.timestamp >= cutoff_time)
        .where(Telemetry.hashrate.is_not(None))
        .order_by(Telemetry.timestamp.asc())
    )
    rows = result.scalars().all()
    if not rows:
        return {
            "miner_id": miner_id,
            "miner_name": miner.name,
            "hours": hours,
            "dry_run": dry_run,
            "deleted": 0,
            "reason": "no telemetry rows in window",
        }

    normalized: List[Tuple[Telemetry, float]] = []
    for t in rows:
        unit = t.hashrate_unit or "GH/s"
        v = format_hashrate(t.hashrate or 0.0, unit)["value"]
        if v and v > 0:
            normalized.append((t, float(v)))

    if len(normalized) < 10:
        return {
            "miner_id": miner_id,
            "miner_name": miner.name,
            "hours": hours,
            "dry_run": dry_run,
            "deleted": 0,
            "reason": "insufficient non-zero hashrate points for robust outlier detection",
            "data_points": len(normalized),
        }

    values = [v for _, v in normalized]
    median = statistics.median(values)
    deviations = [abs(v - median) for v in values]
    mad = statistics.median(deviations)

    outliers: List[Telemetry] = []
    if mad == 0:
        if median > 0:
            outliers = [t for t, v in normalized if v > (median * 10)]
    else:
        for t, v in normalized:
            z = 0.6745 * (v - median) / mad
            if z > 10.0:
                outliers.append(t)

    if dry_run:
        return {
            "miner_id": miner_id,
            "miner_name": miner.name,
            "hours": hours,
            "dry_run": True,
            "would_delete": len(outliers),
            "median_ghs": median,
            "mad_ghs": mad,
            "max_ghs": max(values) if values else None,
            "outlier_timestamps": [to_utc_iso8601(t.timestamp) for t in outliers[:50]],
        }

    deleted = 0
    for t in outliers:
        await db.delete(t)
        deleted += 1

    await db.commit()
    return {
        "miner_id": miner_id,
        "miner_name": miner.name,
        "hours": hours,
        "dry_run": False,
        "deleted": deleted,
        "median_ghs": median,
        "mad_ghs": mad,
    }


@router.get("/miners/{miner_id}/export/csv")
async def export_telemetry_csv(
    miner_id: int,
    hours: int = 24,
    db: AsyncSession = Depends(get_db)
):
    """Export telemetry data as CSV"""
    result = await db.execute(select(Miner).where(Miner.id == miner_id))
    miner = result.scalar_one_or_none()
    
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")
    
    cutoff_time = datetime.utcnow() - timedelta(hours=hours)
    
    result = await db.execute(
        select(Telemetry)
        .where(Telemetry.miner_id == miner_id)
        .where(Telemetry.timestamp >= cutoff_time)
        .order_by(Telemetry.timestamp.asc())
    )
    telemetry_data = result.scalars().all()
    
    # Create CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Write header
    writer.writerow([
        "Timestamp",
        "Hashrate",
        "Temperature (°C)",
        "Power (W)",
        "Shares Accepted",
        "Shares Rejected",
        "Pool"
    ])
    
    # Write data
    for t in telemetry_data:
        hashrate_formatted = format_hashrate(t.hashrate or 0, "GH/s") if t.hashrate else {"display": ""}
        writer.writerow([
            to_utc_iso8601(t.timestamp),
            hashrate_formatted["display"],
            t.temperature or "",
            t.power_watts or "",
            t.shares_accepted or "",
            t.shares_rejected or "",
            t.pool_in_use or ""
        ])
    
    output.seek(0)
    
    filename = f"{miner.name}_telemetry_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@router.get("/overview/stats")
async def get_overview_stats(db: AsyncSession = Depends(get_db)):
    """Get overview statistics for all miners"""
    from sqlalchemy import func
    result = await db.execute(select(Miner).where(Miner.enabled == True).order_by(func.lower(Miner.name)))
    miners = result.scalars().all()
    
    total_miners = len(miners)
    online_miners = 0
    total_hashrate = 0.0
    avg_temperature = 0.0
    total_power = 0.0
    
    cutoff_time = datetime.utcnow() - timedelta(minutes=5)
    
    for miner in miners:
        # Get latest telemetry
        result = await db.execute(
            select(Telemetry)
            .where(Telemetry.miner_id == miner.id)
            .where(Telemetry.timestamp >= cutoff_time)
            .order_by(Telemetry.timestamp.desc())
            .limit(1)
        )
        latest = result.scalar_one_or_none()
        
        if latest:
            online_miners += 1
            if latest.hashrate:
                total_hashrate += latest.hashrate
            if latest.temperature:
                avg_temperature += latest.temperature
            if latest.power_watts:
                total_power += latest.power_watts
    
    avg_temperature = avg_temperature / online_miners if online_miners > 0 else 0
    
    return {
        "total_miners": total_miners,
        "online_miners": online_miners,
        "offline_miners": total_miners - online_miners,
        "total_hashrate": format_hashrate(total_hashrate, "GH/s"),
        "avg_temperature": round(avg_temperature, 1),
        "total_power": round(total_power, 2)
    }
