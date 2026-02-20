"""
API endpoints for external integrations (Home Assistant, etc.)
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from pydantic import BaseModel
from typing import Optional
import logging
from datetime import datetime

from core.database import get_db, HomeAssistantConfig, HomeAssistantDevice
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
