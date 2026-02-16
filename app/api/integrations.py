"""
API endpoints for external integrations (Home Assistant, etc.)
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from pydantic import BaseModel
from typing import List, Optional
import logging
import asyncio
from datetime import datetime
from urllib.parse import urlparse

import httpx

from core.config import app_config
from core.database import get_db, HomeAssistantConfig, HomeAssistantDevice, Pool
from integrations.homeassistant import HomeAssistantIntegration

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


# Pydantic schemas
class HomeAssistantConfigCreate(BaseModel):
    name: str
    base_url: str
    access_token: Optional[str] = None  # Optional for updates
    enabled: bool = True
    keepalive_enabled: bool = False


class HomeAssistantConfigResponse(BaseModel):
    id: int
    name: str
    base_url: str
    enabled: bool
    keepalive_enabled: bool
    keepalive_last_check: Optional[str] = None
    keepalive_last_success: Optional[str] = None
    keepalive_downtime_start: Optional[str] = None
    keepalive_alerts_sent: int
    last_test: Optional[str] = None
    last_test_success: Optional[bool] = None
    
    class Config:
        from_attributes = True


class HomeAssistantDeviceResponse(BaseModel):
    id: int
    entity_id: str
    name: str
    domain: str
    miner_id: Optional[int]
    enrolled: bool
    never_auto_control: bool
    current_state: Optional[str]
    capabilities: Optional[dict]
    
    class Config:
        from_attributes = True


class DeviceEnrollRequest(BaseModel):
    enrolled: bool
    never_auto_control: Optional[bool] = None


class DeviceLinkRequest(BaseModel):
    miner_id: Optional[int] = None  # None to unlink


class StratumDashboardSettingsRequest(BaseModel):
    enabled: bool


# ============================================================================


def _stratum_dashboards_enabled() -> bool:
    return bool(app_config.get("ui.hmm_local_stratum_dashboards_enabled", False))


def _extract_host(pool_url: str) -> str:
    raw = (pool_url or "").strip()
    if not raw:
        return ""

    to_parse = raw if "://" in raw else f"stratum+tcp://{raw}"
    parsed = urlparse(to_parse)
    if parsed.hostname:
        return parsed.hostname

    # Fallback for odd strings
    return raw.split(":")[0].split("/")[0]


def _pool_matches_coin(pool: Pool, coin: str) -> bool:
    coin_lower = coin.lower()
    config = pool.pool_config or {}
    joined = " ".join(
        [
            pool.name or "",
            pool.url or "",
            pool.user or "",
            str(config.get("coin", "")),
            str(config.get("symbol", "")),
        ]
    ).lower()
    return coin_lower in joined


def _to_ms(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


@router.get("/hmm-local-stratum/settings")
async def get_hmm_local_stratum_settings():
    """Get HMM-Local Stratum dashboard feature visibility settings."""
    return {
        "enabled": _stratum_dashboards_enabled()
    }


@router.post("/hmm-local-stratum/settings")
async def save_hmm_local_stratum_settings(request: StratumDashboardSettingsRequest):
    """Save HMM-Local Stratum dashboard feature visibility settings."""
    app_config.set("ui.hmm_local_stratum_dashboards_enabled", request.enabled)
    app_config.save()
    return {
        "success": True,
        "enabled": request.enabled,
        "message": "HMM-Local Stratum dashboard setting saved"
    }


@router.get("/hmm-local-stratum/dashboard/{coin}")
async def get_hmm_local_stratum_coin_dashboard(
    coin: str,
    window_minutes: int = 15,
    hours: int = 6,
    db: AsyncSession = Depends(get_db),
):
    """Proxy Stratum operational + miner analytics for a specific coin."""
    normalized_coin = coin.upper()
    if normalized_coin not in {"BTC", "BCH", "DGB"}:
        raise HTTPException(status_code=400, detail="Supported coins: BTC, BCH, DGB")

    result = await db.execute(
        select(Pool)
        .where(Pool.enabled == True)
        .where(Pool.pool_type == "hmm_local_stratum")
    )
    pools = result.scalars().all()
    if not pools:
        raise HTTPException(status_code=404, detail="No enabled HMM-Local Stratum pools found")

    matching_pool = next((p for p in pools if _pool_matches_coin(p, normalized_coin)), pools[0])
    host = _extract_host(matching_pool.url)
    if not host:
        raise HTTPException(status_code=400, detail="Could not resolve Stratum host from pool URL")

    pool_config = matching_pool.pool_config or {}
    api_port = int(pool_config.get("stratum_api_port", 8082))
    api_base = f"http://{host}:{api_port}"

    async def fetch_json(path: str, params: dict | None = None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                response = await client.get(f"{api_base}{path}", params=params)
                response.raise_for_status()
                return response.json()
        except Exception as exc:
            logger.warning("Stratum API fetch failed %s%s: %s", api_base, path, exc)
            return {"ok": False, "error": str(exc)}

    snapshot, worker_summary, hashrate_snapshots, share_metrics = await asyncio.gather(
        fetch_json(f"/api/pool-snapshot/{normalized_coin}", {"window_minutes": max(1, min(window_minutes, 240))}),
        fetch_json("/debug/worker-summary", {"hours": max(1, min(hours, 24 * 7))}),
        fetch_json("/debug/hashrate-snapshots", {"coin": normalized_coin, "window_minutes": max(1, min(window_minutes, 240)), "n": 2000}),
        fetch_json("/debug/share-metrics", {"n": 5000}),
    )

    worker_summary_map = {
        str(w.get("worker") or "unknown"): w
        for w in worker_summary.get("workers", [])
        if isinstance(w, dict)
    }

    hashrate_rows = [
        r for r in hashrate_snapshots.get("rows", [])
        if isinstance(r, dict) and str(r.get("coin") or "").upper() == normalized_coin
    ]
    share_rows = [
        r for r in share_metrics.get("rows", [])
        if isinstance(r, dict) and str(r.get("coin") or "").upper() == normalized_coin
    ]

    pool_hashrate_chart: list[dict] = []
    worker_hashrate_chart: dict[str, list[dict]] = {}
    for row in hashrate_rows:
        worker = str(row.get("worker") or "unknown")
        ts_ms = _to_ms(row.get("ts"))
        hs = float(row.get("est_hashrate_hs") or 0.0)
        if ts_ms is None:
            continue
        point = {"x": ts_ms, "y": hs}
        if worker == "__pool__":
            pool_hashrate_chart.append(point)
        else:
            worker_hashrate_chart.setdefault(worker, []).append(point)

    worker_vardiff_chart: dict[str, list[dict]] = {}
    worker_highest_diff: dict[str, float] = {}
    for row in share_rows:
        worker = str(row.get("worker") or "unknown")
        ts_ms = _to_ms(row.get("ts"))
        assigned = float(row.get("assigned_diff") or 0.0)
        computed = float(row.get("computed_diff") or 0.0)
        if ts_ms is not None:
            worker_vardiff_chart.setdefault(worker, []).append({"x": ts_ms, "y": assigned})
        if computed > 0:
            worker_highest_diff[worker] = max(worker_highest_diff.get(worker, 0.0), computed)

    snapshot_workers = snapshot.get("workers", {}).get("rows", []) if isinstance(snapshot, dict) else []
    snapshot_worker_map = {
        str(w.get("worker") or "unknown"): w
        for w in snapshot_workers
        if isinstance(w, dict)
    }

    worker_names = set(worker_summary_map.keys()) | set(snapshot_worker_map.keys()) | set(worker_hashrate_chart.keys())
    workers_payload = []
    for worker_name in sorted(worker_names):
        summary = worker_summary_map.get(worker_name, {})
        snap = snapshot_worker_map.get(worker_name, {})
        accepted = int(summary.get("accepted") if summary.get("accepted") is not None else snap.get("accepted_shares") or 0)
        rejected = int(summary.get("rejected") or 0)
        total = accepted + rejected
        reject_rate = (rejected / total * 100.0) if total > 0 else None
        hashrate_points = sorted(worker_hashrate_chart.get(worker_name, []), key=lambda p: p["x"])
        vardiff_points = sorted(worker_vardiff_chart.get(worker_name, []), key=lambda p: p["x"])

        workers_payload.append({
            "worker": worker_name,
            "accepted": accepted,
            "rejected": rejected,
            "reject_rate_pct": reject_rate,
            "highest_diff": worker_highest_diff.get(worker_name),
            "current_hashrate_hs": hashrate_points[-1]["y"] if hashrate_points else float(snap.get("est_hashrate_hs") or 0.0),
            "avg_assigned_diff": summary.get("avg_assigned_diff"),
            "avg_computed_diff": summary.get("avg_computed_diff"),
            "last_share_at": summary.get("last_share_at"),
            "hashrate_chart": hashrate_points[-180:],
            "vardiff_chart": vardiff_points[-180:],
        })

    workers_payload.sort(key=lambda w: str(w.get("worker") or ""))

    return {
        "ok": bool(snapshot.get("ok", False)),
        "coin": normalized_coin,
        "api_base": api_base,
        "pool": {
            "id": matching_pool.id,
            "name": matching_pool.name,
            "url": matching_pool.url,
            "user": matching_pool.user,
        },
        "quality": snapshot.get("quality") if isinstance(snapshot, dict) else None,
        "hashrate": snapshot.get("hashrate") if isinstance(snapshot, dict) else None,
        "network": snapshot.get("network") if isinstance(snapshot, dict) else None,
        "kpi": snapshot.get("kpi") if isinstance(snapshot, dict) else None,
        "rejects": snapshot.get("rejects") if isinstance(snapshot, dict) else None,
        "workers": {
            "count": len(workers_payload),
            "rows": workers_payload,
        },
        "charts": {
            "pool_hashrate_hs": sorted(pool_hashrate_chart, key=lambda p: p["x"])[-360:],
        },
        "fetched_at": datetime.utcnow().isoformat(),
    }
# Home Assistant Configuration Endpoints
# ============================================================================

@router.get("/homeassistant/config")
async def get_ha_config(db: AsyncSession = Depends(get_db)):
    """Get Home Assistant configuration"""
    result = await db.execute(select(HomeAssistantConfig))
    config = result.scalar_one_or_none()
    
    if not config:
        return {"configured": False}
    
    return {
        "configured": True,
        "id": config.id,
        "name": config.name,
        "base_url": config.base_url,
        "enabled": config.enabled,
        "keepalive_enabled": config.keepalive_enabled,
        "last_test": config.last_test.isoformat() if config.last_test else None,
        "last_test_success": config.last_test_success
    }


@router.post("/homeassistant/config")
async def create_or_update_ha_config(
    config_data: HomeAssistantConfigCreate,
    db: AsyncSession = Depends(get_db)
):
    """Create or update Home Assistant configuration"""
    result = await db.execute(select(HomeAssistantConfig))
    existing = result.scalar_one_or_none()
    
    if existing:
        # Update existing
        existing.name = config_data.name
        existing.base_url = config_data.base_url.rstrip('/')
        # Only update token if a new one is provided
        if config_data.access_token and config_data.access_token.strip():
            existing.access_token = config_data.access_token
        existing.enabled = config_data.enabled
        existing.keepalive_enabled = config_data.keepalive_enabled
        # Reset keepalive state if being disabled
        if not config_data.keepalive_enabled:
            existing.keepalive_downtime_start = None
            existing.keepalive_alerts_sent = 0
        config = existing
    else:
        # Create new (token required)
        if not config_data.access_token or not config_data.access_token.strip():
            return {
                "success": False,
                "message": "Access token is required for new configuration"
            }
        
        config = HomeAssistantConfig(
            name=config_data.name,
            base_url=config_data.base_url.rstrip('/'),
            access_token=config_data.access_token,
            enabled=config_data.enabled,
            keepalive_enabled=config_data.keepalive_enabled
        )
        db.add(config)
    
    await db.commit()
    await db.refresh(config)
    
    logger.info(f"Home Assistant config saved: {config_data.base_url}")
    
    return {
        "success": True,
        "id": config.id,
        "message": "Configuration saved successfully"
    }


@router.post("/homeassistant/test")
async def test_ha_connection(db: AsyncSession = Depends(get_db)):
    """Test Home Assistant connection"""
    from datetime import datetime
    
    result = await db.execute(select(HomeAssistantConfig))
    config = result.scalar_one_or_none()
    
    if not config:
        raise HTTPException(status_code=404, detail="Home Assistant not configured")
    
    # Test connection
    ha = HomeAssistantIntegration(config.base_url, config.access_token)
    success = await ha.test_connection()
    
    # Update test results
    config.last_test = datetime.utcnow()
    config.last_test_success = success
    await db.commit()
    
    if success:
        return {
            "success": True,
            "message": "Successfully connected to Home Assistant"
        }
    else:
        return {
            "success": False,
            "message": "Failed to connect to Home Assistant"
        }


@router.delete("/homeassistant/config")
async def delete_ha_config(db: AsyncSession = Depends(get_db)):
    """Delete Home Assistant configuration"""
    # Delete all devices first
    await db.execute(delete(HomeAssistantDevice))
    
    # Delete config
    await db.execute(delete(HomeAssistantConfig))
    await db.commit()
    
    logger.info("Home Assistant configuration deleted")
    
    return {
        "success": True,
        "message": "Configuration deleted"
    }


# ============================================================================
# Home Assistant Device Endpoints
# ============================================================================

@router.post("/homeassistant/discover")
async def discover_ha_devices(db: AsyncSession = Depends(get_db)):
    """Discover devices from Home Assistant"""
    result = await db.execute(select(HomeAssistantConfig))
    config = result.scalar_one_or_none()
    
    if not config or not config.enabled:
        raise HTTPException(status_code=404, detail="Home Assistant not configured")
    
    # Connect and discover
    ha = HomeAssistantIntegration(config.base_url, config.access_token)
    devices = await ha.discover_devices()
    
    if not devices:
        return {
            "success": False,
            "message": "No devices discovered",
            "count": 0
        }
    
    # Store devices in database
    added = 0
    updated = 0
    
    for device in devices:
        result = await db.execute(
            select(HomeAssistantDevice).where(HomeAssistantDevice.entity_id == device.entity_id)
        )
        existing = result.scalar_one_or_none()
        
        if existing:
            # Update existing
            existing.name = device.name
            existing.domain = device.domain
            existing.capabilities = {"capabilities": device.capabilities}
            updated += 1
        else:
            # Add new
            new_device = HomeAssistantDevice(
                entity_id=device.entity_id,
                name=device.name,
                domain=device.domain,
                capabilities={"capabilities": device.capabilities}
            )
            db.add(new_device)
            added += 1
    
    await db.commit()
    
    logger.info(f"Discovered {len(devices)} HA devices: {added} added, {updated} updated")
    
    return {
        "success": True,
        "total": len(devices),
        "added": added,
        "updated": updated,
        "message": f"Discovered {len(devices)} devices ({added} new, {updated} updated)"
    }


@router.get("/homeassistant/devices")
async def get_ha_devices(
    enrolled_only: bool = False,
    db: AsyncSession = Depends(get_db)
):
    """Get all Home Assistant devices"""
    query = select(HomeAssistantDevice)
    
    if enrolled_only:
        query = query.where(HomeAssistantDevice.enrolled == True)
    
    result = await db.execute(query)
    devices = result.scalars().all()
    
    return {
        "devices": [
            {
                "id": d.id,
                "entity_id": d.entity_id,
                "name": d.name,
                "domain": d.domain,
                "miner_id": d.miner_id,
                "enrolled": d.enrolled,
                "never_auto_control": d.never_auto_control,
                "current_state": d.current_state,
                "capabilities": d.capabilities
            }
            for d in devices
        ]
    }


@router.post("/homeassistant/devices/{device_id}/enroll")
async def enroll_ha_device(
    device_id: int,
    request: DeviceEnrollRequest,
    db: AsyncSession = Depends(get_db)
):
    """Enroll or un-enroll a device for automation control"""
    result = await db.execute(
        select(HomeAssistantDevice).where(HomeAssistantDevice.id == device_id)
    )
    device = result.scalar_one_or_none()
    
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    device.enrolled = request.enrolled
    if request.never_auto_control is not None:
        device.never_auto_control = request.never_auto_control
    
    await db.commit()
    
    logger.info(f"Device {device.entity_id} enrollment: {request.enrolled}")
    
    return {
        "success": True,
        "message": f"Device {'enrolled' if request.enrolled else 'un-enrolled'}"
    }


@router.post("/homeassistant/devices/{device_id}/link")
async def link_ha_device_to_miner(
    device_id: int,
    request: DeviceLinkRequest,
    db: AsyncSession = Depends(get_db)
):
    """Link a Home Assistant device to a miner"""
    from core.database import Miner
    
    # Get device
    result = await db.execute(
        select(HomeAssistantDevice).where(HomeAssistantDevice.id == device_id)
    )
    device = result.scalar_one_or_none()
    
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    # Validate miner exists if linking
    if request.miner_id is not None:
        result = await db.execute(
            select(Miner).where(Miner.id == request.miner_id)
        )
        miner = result.scalar_one_or_none()
        
        if not miner:
            raise HTTPException(status_code=404, detail="Miner not found")
        
        device.miner_id = request.miner_id
        await db.commit()
        
        logger.info(f"Linked device {device.entity_id} to miner {miner.name}")
        
        return {
            "success": True,
            "message": f"Device linked to miner '{miner.name}'"
        }
    else:
        # Unlink
        device.miner_id = None
        await db.commit()
        
        logger.info(f"Unlinked device {device.entity_id} from miner")
        
        return {
            "success": True,
            "message": "Device unlinked from miner"
        }


@router.post("/homeassistant/devices/{device_id}/control")
async def control_ha_device(
    device_id: int,
    action: str,  # "turn_on" or "turn_off"
    db: AsyncSession = Depends(get_db)
):
    """Manually control a Home Assistant device"""
    from datetime import datetime
    
    # Get device
    result = await db.execute(
        select(HomeAssistantDevice).where(HomeAssistantDevice.id == device_id)
    )
    device = result.scalar_one_or_none()
    
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    # Get HA config
    result = await db.execute(select(HomeAssistantConfig))
    config = result.scalar_one_or_none()
    
    if not config or not config.enabled:
        raise HTTPException(status_code=404, detail="Home Assistant not configured")
    
    # Control device
    ha = HomeAssistantIntegration(config.base_url, config.access_token)
    
    if action == "turn_on":
        success = await ha.turn_on(device.entity_id)
        new_state = "on"
    elif action == "turn_off":
        success = await ha.turn_off(device.entity_id)
        new_state = "off"
    else:
        raise HTTPException(status_code=400, detail="Invalid action")
    
    if success:
        device.current_state = new_state
        device.last_state_change = datetime.utcnow()
        await db.commit()
        
        logger.info(f"Controlled {device.entity_id}: {action}")
        
        return {
            "success": True,
            "message": f"Device turned {new_state}"
        }
    else:
        return {
            "success": False,
            "message": "Failed to control device"
        }


@router.get("/homeassistant/devices/{device_id}/state")
async def get_ha_device_state(
    device_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get current state of a device from Home Assistant"""
    from datetime import datetime
    
    # Get device
    result = await db.execute(
        select(HomeAssistantDevice).where(HomeAssistantDevice.id == device_id)
    )
    device = result.scalar_one_or_none()
    
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    # Get HA config
    result = await db.execute(select(HomeAssistantConfig))
    config = result.scalar_one_or_none()
    
    if not config or not config.enabled:
        raise HTTPException(status_code=404, detail="Home Assistant not configured")
    
    # Get state
    ha = HomeAssistantIntegration(config.base_url, config.access_token)
    state = await ha.get_device_state(device.entity_id)
    
    if state:
        # Update database
        device.current_state = state.state
        device.last_state_change = datetime.utcnow()
        await db.commit()
        
        return {
            "success": True,
            "entity_id": state.entity_id,
            "name": state.name,
            "state": state.state,
            "attributes": state.attributes,
            "last_updated": state.last_updated.isoformat()
        }
    else:
        return {
            "success": False,
            "message": "Failed to get device state"
        }


# ============================================================================
