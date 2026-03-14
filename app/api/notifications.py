"""
Notifications API endpoints
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Optional, Dict
from pydantic import BaseModel

from core.database import get_db, NotificationConfig, AlertConfig, NotificationLog


router = APIRouter()

SECRET_FIELDS_BY_CHANNEL = {
    "telegram": {"bot_token"},
    "discord": {"webhook_url"},
}


def _get_secret_fields(channel_type: str) -> set[str]:
    return SECRET_FIELDS_BY_CHANNEL.get(channel_type, set())


def _has_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _merge_channel_config(channel_type: str, existing_config: dict, incoming_config: dict) -> dict:
    secret_fields = _get_secret_fields(channel_type)
    merged = dict(existing_config or {})

    for key, value in (incoming_config or {}).items():
        if key in secret_fields and not _has_value(value):
            continue
        merged[key] = value

    return merged


def _serialize_channel(channel: NotificationConfig) -> Dict:
    secret_fields = _get_secret_fields(channel.channel_type)
    config = dict(channel.config or {})
    stored_secrets = {}

    for field_name in secret_fields:
        has_secret = _has_value(config.get(field_name))
        stored_secrets[field_name] = has_secret
        if field_name in config:
            config[field_name] = ""

    return {
        "id": channel.id,
        "channel_type": channel.channel_type,
        "enabled": channel.enabled,
        "config": config,
        "stored_secrets": stored_secrets,
    }


class NotificationChannelCreate(BaseModel):
    channel_type: str  # telegram or discord
    enabled: bool = False
    config: dict  # bot_token, chat_id for Telegram; webhook_url for Discord


class NotificationChannelUpdate(BaseModel):
    enabled: Optional[bool] = None
    config: Optional[dict] = None


class NotificationChannelResponse(BaseModel):
    id: int
    channel_type: str
    enabled: bool
    config: dict
    stored_secrets: Dict[str, bool] = {}
    
    class Config:
        from_attributes = True


class AlertConfigCreate(BaseModel):
    alert_type: str
    enabled: bool = True
    config: Optional[dict] = None


class AlertConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    config: Optional[dict] = None


class AlertConfigResponse(BaseModel):
    id: int
    alert_type: str
    enabled: bool
    config: Optional[dict] = None
    
    class Config:
        from_attributes = True


class NotificationLogResponse(BaseModel):
    id: int
    timestamp: str
    channel_type: str
    alert_type: str
    message: str
    success: bool
    error: Optional[str] = None
    
    class Config:
        from_attributes = True


# Notification Channels
@router.get("/channels", response_model=List[NotificationChannelResponse])
async def list_notification_channels(db: AsyncSession = Depends(get_db)):
    """List all notification channels"""
    result = await db.execute(select(NotificationConfig))
    channels = result.scalars().all()
    return [_serialize_channel(channel) for channel in channels]


@router.get("/channels/{channel_type}", response_model=NotificationChannelResponse)
async def get_notification_channel(channel_type: str, db: AsyncSession = Depends(get_db)):
    """Get notification channel by type"""
    result = await db.execute(
        select(NotificationConfig).where(NotificationConfig.channel_type == channel_type)
    )
    channel = result.scalar_one_or_none()
    
    if not channel:
        raise HTTPException(status_code=404, detail="Notification channel not found")
    
    return _serialize_channel(channel)


@router.post("/channels", response_model=NotificationChannelResponse)
async def create_notification_channel(channel: NotificationChannelCreate, db: AsyncSession = Depends(get_db)):
    """Create or update notification channel"""
    # Check if channel already exists
    result = await db.execute(
        select(NotificationConfig).where(NotificationConfig.channel_type == channel.channel_type)
    )
    existing = result.scalar_one_or_none()
    
    if existing:
        # Update existing
        existing.enabled = channel.enabled
        existing.config = _merge_channel_config(existing.channel_type, existing.config or {}, channel.config or {})
        await db.commit()
        await db.refresh(existing)
        return _serialize_channel(existing)
    
    # Create new
    db_channel = NotificationConfig(
        channel_type=channel.channel_type,
        enabled=channel.enabled,
        config=_merge_channel_config(channel.channel_type, {}, channel.config or {})
    )
    
    db.add(db_channel)
    await db.commit()
    await db.refresh(db_channel)
    
    return _serialize_channel(db_channel)


@router.put("/channels/{channel_type}", response_model=NotificationChannelResponse)
async def update_notification_channel(
    channel_type: str,
    channel: NotificationChannelUpdate,
    db: AsyncSession = Depends(get_db)
):
    """Update notification channel"""
    result = await db.execute(
        select(NotificationConfig).where(NotificationConfig.channel_type == channel_type)
    )
    db_channel = result.scalar_one_or_none()
    
    if not db_channel:
        raise HTTPException(status_code=404, detail="Notification channel not found")
    
    if channel.enabled is not None:
        db_channel.enabled = channel.enabled
    if channel.config is not None:
        db_channel.config = _merge_channel_config(db_channel.channel_type, db_channel.config or {}, channel.config)
    
    await db.commit()
    await db.refresh(db_channel)
    
    return _serialize_channel(db_channel)


@router.delete("/channels/{channel_type}")
async def delete_notification_channel(channel_type: str, db: AsyncSession = Depends(get_db)):
    """Delete notification channel"""
    result = await db.execute(
        select(NotificationConfig).where(NotificationConfig.channel_type == channel_type)
    )
    db_channel = result.scalar_one_or_none()
    
    if not db_channel:
        raise HTTPException(status_code=404, detail="Notification channel not found")
    
    await db.delete(db_channel)
    await db.commit()
    
    return {"status": "success"}


# Alert Configuration
@router.get("/alerts", response_model=List[AlertConfigResponse])
async def list_alert_configs(db: AsyncSession = Depends(get_db)):
    """List all alert configurations"""
    result = await db.execute(select(AlertConfig))
    alerts = result.scalars().all()
    return alerts


@router.get("/alerts/{alert_type}", response_model=AlertConfigResponse)
async def get_alert_config(alert_type: str, db: AsyncSession = Depends(get_db)):
    """Get alert configuration by type"""
    result = await db.execute(
        select(AlertConfig).where(AlertConfig.alert_type == alert_type)
    )
    alert = result.scalar_one_or_none()
    
    if not alert:
        raise HTTPException(status_code=404, detail="Alert configuration not found")
    
    return alert


@router.post("/alerts", response_model=AlertConfigResponse)
async def create_alert_config(alert: AlertConfigCreate, db: AsyncSession = Depends(get_db)):
    """Create or update alert configuration"""
    # Check if alert already exists
    result = await db.execute(
        select(AlertConfig).where(AlertConfig.alert_type == alert.alert_type)
    )
    existing = result.scalar_one_or_none()
    
    if existing:
        # Update existing
        existing.enabled = alert.enabled
        existing.config = alert.config
        await db.commit()
        await db.refresh(existing)
        return existing
    
    # Create new
    db_alert = AlertConfig(
        alert_type=alert.alert_type,
        enabled=alert.enabled,
        config=alert.config
    )
    
    db.add(db_alert)
    await db.commit()
    await db.refresh(db_alert)
    
    return db_alert


@router.put("/alerts/{alert_type}", response_model=AlertConfigResponse)
async def update_alert_config(
    alert_type: str,
    alert: AlertConfigUpdate,
    db: AsyncSession = Depends(get_db)
):
    """Update alert configuration"""
    result = await db.execute(
        select(AlertConfig).where(AlertConfig.alert_type == alert_type)
    )
    db_alert = result.scalar_one_or_none()
    
    if not db_alert:
        raise HTTPException(status_code=404, detail="Alert configuration not found")
    
    if alert.enabled is not None:
        db_alert.enabled = alert.enabled
    if alert.config is not None:
        db_alert.config = alert.config
    
    await db.commit()
    await db.refresh(db_alert)
    
    return db_alert


# Notification Logs
@router.get("/logs", response_model=List[NotificationLogResponse])
async def list_notification_logs(limit: int = 100, db: AsyncSession = Depends(get_db)):
    """List notification logs"""
    result = await db.execute(
        select(NotificationLog)
        .order_by(NotificationLog.timestamp.desc())
        .limit(limit)
    )
    logs = result.scalars().all()
    
    # Convert timestamp to string
    return [
        {
            **log.__dict__,
            "timestamp": log.timestamp.isoformat()
        }
        for log in logs
    ]


@router.post("/test/{channel_type}")
async def test_notification(channel_type: str, db: AsyncSession = Depends(get_db)):
    """Send a test notification"""
    from core.notifications import NotificationService
    
    result = await db.execute(
        select(NotificationConfig).where(NotificationConfig.channel_type == channel_type)
    )
    channel = result.scalar_one_or_none()
    
    if not channel:
        raise HTTPException(status_code=404, detail="Notification channel not found")
    
    if not channel.enabled:
        raise HTTPException(status_code=400, detail="Notification channel is disabled")
    
    service = NotificationService()
    success = await service.send_notification(
        channel_type=channel_type,
        message="🧪 Test notification from HMM-Local",
        alert_type="test"
    )
    
    if not success:
        raise HTTPException(status_code=500, detail="Failed to send test notification")
    
    return {"status": "success", "message": "Test notification sent"}
