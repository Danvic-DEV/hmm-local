"""
Audit logs API endpoints
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, and_
from core.database import get_db, AuditLog
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime, timedelta
from api.time_utils import to_utc_iso8601

router = APIRouter(prefix="/api/audit", tags=["audit"])


class AuditLogResponse(BaseModel):
    id: int
    timestamp: str
    user: str
    action: str
    resource_type: str
    resource_id: Optional[int]
    resource_name: Optional[str]
    changes: Optional[dict]
    ip_address: Optional[str]
    status: str
    error_message: Optional[str]
    
    class Config:
        from_attributes = True


@router.get("/logs", response_model=List[AuditLogResponse])
async def get_audit_logs(
    resource_type: Optional[str] = None,
    action: Optional[str] = None,
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db)
):
    """Get audit logs with optional filtering"""
    
    # Build query
    conditions = []
    
    # Filter by time range
    since = datetime.utcnow() - timedelta(days=days)
    conditions.append(AuditLog.timestamp >= since)
    
    # Filter by resource type if specified
    if resource_type:
        conditions.append(AuditLog.resource_type == resource_type)
    
    # Filter by action if specified
    if action:
        conditions.append(AuditLog.action == action)
    
    # Execute query
    query = select(AuditLog).where(and_(*conditions)).order_by(desc(AuditLog.timestamp)).limit(limit)
    result = await db.execute(query)
    logs = result.scalars().all()
    
    return [
        {
            "id": log.id,
            "timestamp": to_utc_iso8601(log.timestamp),
            "user": log.user,
            "action": log.action,
            "resource_type": log.resource_type,
            "resource_id": log.resource_id,
            "resource_name": log.resource_name,
            "changes": log.changes,
            "ip_address": log.ip_address,
            "status": log.status,
            "error_message": log.error_message
        }
        for log in logs
    ]


@router.get("/stats")
async def get_audit_stats(
    days: int = Query(7, ge=1, le=90),
    db: AsyncSession = Depends(get_db)
):
    """Get audit log statistics"""
    
    since = datetime.utcnow() - timedelta(days=days)
    
    result = await db.execute(
        select(AuditLog).where(AuditLog.timestamp >= since)
    )
    logs = result.scalars().all()
    
    # Calculate stats
    total = len(logs)
    by_action = {}
    by_resource_type = {}
    by_status = {"success": 0, "failure": 0}
    by_user = {}
    
    for log in logs:
        # Count by action
        by_action[log.action] = by_action.get(log.action, 0) + 1
        
        # Count by resource type
        by_resource_type[log.resource_type] = by_resource_type.get(log.resource_type, 0) + 1
        
        # Count by status
        by_status[log.status] = by_status.get(log.status, 0) + 1
        
        # Count by user
        by_user[log.user] = by_user.get(log.user, 0) + 1
    
    return {
        "total_events": total,
        "days": days,
        "by_action": by_action,
        "by_resource_type": by_resource_type,
        "by_status": by_status,
        "by_user": by_user,
        "success_rate": (by_status["success"] / total * 100) if total > 0 else 0
    }
