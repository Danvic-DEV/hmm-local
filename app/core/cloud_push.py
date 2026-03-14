"""
Cloud Push Service - Push telemetry and events to HMM Cloud Aggregator
"""
import os
import hmac
import hashlib
import time
import httpx
import json
from typing import Dict, List, Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class CloudPushService:
    """Service for pushing data to HMM Cloud Aggregator"""
    
    def __init__(self, config: dict):
        """
        Initialize cloud push service.
        
        Args:
            config: Cloud configuration dict with keys:
                - enabled: bool
                - api_key: str (device API key from cloud)
                - endpoint: str (cloud ingest service URL)
                - installation_name: str
                - installation_location: str (optional)
        """
        self.enabled = config.get("enabled", False)
        self.api_key = config.get("api_key", "")
        self.endpoint = config.get("endpoint", "").rstrip("/")
        self.installation_name = config.get("installation_name", "")
        self.installation_location = config.get("installation_location", "")
        self.timeout = config.get("timeout", 30)
        
    def _generate_hmac_signature(self, payload: bytes, timestamp: str) -> str:
        """
        Generate HMAC-SHA256 signature for payload.
        
        Args:
            payload: JSON payload bytes
            timestamp: Unix timestamp string
            
        Returns:
            HMAC signature hex string
        """
        message = payload + timestamp.encode()
        signature = hmac.new(
            self.api_key.encode(),
            message,
            hashlib.sha256
        ).hexdigest()
        return signature
    
    def _get_headers(self, payload: bytes) -> Dict[str, str]:
        """
        Generate request headers with HMAC signature.
        
        Args:
            payload: JSON payload bytes
            
        Returns:
            Headers dict with API key, signature, timestamp
        """
        timestamp = str(int(time.time()))
        signature = self._generate_hmac_signature(payload, timestamp)
        
        return {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
            "X-Signature": signature,
            "X-Timestamp": timestamp
        }
    
    async def push_telemetry(self, miners: List[dict], aggregate: Optional[dict] = None) -> bool:
        """
        Push miner telemetry to cloud.
        
        Args:
            miners: List of miner dicts with keys:
                - name: str
                - type: str (miner_type)
                - ip_address: str
                - telemetry: dict {
                    timestamp: int,
                    hashrate: float,
                    temperature: float,
                    power: float,
                    shares_accepted: int,
                    shares_rejected: int,
                    uptime: int
                  }
            aggregate: Optional dict with pre-calculated totals:
                - total_hashrate_ghs: float
                - total_power_watts: float
                - miners_online: int
                - total_miners: int
        
        Returns:
            True if successful, False otherwise
        """
        if not self.enabled or not self.api_key:
            logger.debug("Cloud push disabled or no API key configured")
            return False
        
        try:
            # Build payload
            payload_dict = {
                "installation_name": self.installation_name,
                "installation_location": self.installation_location,
                "miners": miners
            }
            
            # Include aggregate if provided
            if aggregate:
                payload_dict["aggregate"] = aggregate
            
            payload = json.dumps(payload_dict).encode()
            headers = self._get_headers(payload)
            
            # Send request
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.endpoint}/telemetry",
                    content=payload,
                    headers=headers
                )
                
                if response.status_code == 200:
                    logger.info(f"✅ Pushed telemetry for {len(miners)} miners to cloud")
                    return True
                elif response.status_code == 429:
                    logger.warning("⚠️ Cloud rate limit exceeded")
                    return False
                else:
                    logger.error(f"❌ Cloud push failed: {response.status_code} - {response.text}")
                    return False
                    
        except httpx.TimeoutException:
            logger.error("❌ Cloud push timed out")
            return False
        except Exception as e:
            logger.error(f"❌ Cloud push error: {e}")
            return False
    
    async def push_events(self, events: List[dict]) -> bool:
        """
        Push events/alerts to cloud.
        
        Args:
            events: List of event dicts with keys:
                - event_type: str
                - severity: str (info/warning/error)
                - message: str
                - timestamp: int
                - data: dict (optional extra data)
        
        Returns:
            True if successful, False otherwise
        """
        if not self.enabled or not self.api_key or not events:
            return False
        
        try:
            payload_dict = {
                "installation_name": self.installation_name,
                "events": events
            }
            
            payload = json.dumps(payload_dict).encode()
            headers = self._get_headers(payload)
            
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.endpoint}/events",
                    content=payload,
                    headers=headers
                )
                
                if response.status_code == 200:
                    logger.info(f"✅ Pushed {len(events)} events to cloud")
                    return True
                else:
                    logger.error(f"❌ Cloud event push failed: {response.status_code}")
                    return False
                    
        except Exception as e:
            logger.error(f"❌ Cloud event push error: {e}")
            return False
    
    async def push_pools(self, pools: List[dict]) -> bool:
        """
        Push pool configurations to cloud.
        
        Args:
            pools: List of pool dicts with keys:
                - name: str
                - url: str
                - priority: int
                - active: bool
        
        Returns:
            True if successful, False otherwise
        """
        if not self.enabled or not self.api_key or not pools:
            return False
        
        try:
            payload_dict = {
                "installation_name": self.installation_name,
                "pools": pools
            }
            
            payload = json.dumps(payload_dict).encode()
            headers = self._get_headers(payload)
            
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.endpoint}/pools",
                    content=payload,
                    headers=headers
                )
                
                if response.status_code == 200:
                    logger.info(f"✅ Pushed {len(pools)} pool configs to cloud")
                    return True
                else:
                    logger.error(f"❌ Cloud pool push failed: {response.status_code}")
                    return False
                    
        except Exception as e:
            logger.error(f"❌ Cloud pool push error: {e}")
            return False
    
    async def test_connection(self) -> Dict[str, any]:
        """
        Test connection to cloud.
        
        Returns:
            Dict with keys:
                - success: bool
                - message: str
                - latency_ms: float (if successful)
        """
        if not self.api_key or not self.endpoint:
            return {
                "success": False,
                "message": "API key or endpoint not configured"
            }
        
        try:
            start = time.time()
            
            # Send minimal telemetry payload as test
            test_payload = {
                "installation_name": self.installation_name,
                "installation_location": self.installation_location,
                "miners": []
            }
            
            payload = json.dumps(test_payload).encode()
            headers = self._get_headers(payload)
            
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    f"{self.endpoint}/telemetry",
                    content=payload,
                    headers=headers
                )
                
                latency_ms = (time.time() - start) * 1000
                
                if response.status_code == 200:
                    return {
                        "success": True,
                        "message": "Connection successful",
                        "latency_ms": round(latency_ms, 2)
                    }
                elif response.status_code == 401:
                    return {
                        "success": False,
                        "message": "Invalid API key"
                    }
                else:
                    return {
                        "success": False,
                        "message": f"HTTP {response.status_code}: {response.text[:100]}"
                    }
                    
        except httpx.TimeoutException:
            return {
                "success": False,
                "message": "Connection timeout"
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Connection error: {str(e)}"
            }

    async def push_audit_logs(self, audit_logs: List[dict]) -> bool:
        """
        Push audit log entries to cloud.

        Args:
            audit_logs: List of audit log dicts with keys:
                - local_id: int (AuditLog.id from local DB)
                - timestamp: int (unix timestamp)
                - action: str
                - resource_type: str
                - resource_name: str (optional)
                - status: str
                - changes: dict (optional)
                - error_message: str (optional)

        Returns:
            True if successful, False otherwise
        """
        if not self.enabled or not self.api_key or not audit_logs:
            return False

        try:
            payload_dict = {
                "installation_name": self.installation_name,
                "audit_logs": audit_logs,
            }

            payload = json.dumps(payload_dict).encode()
            headers = self._get_headers(payload)

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.endpoint}/audit-logs",
                    content=payload,
                    headers=headers,
                )

                if response.status_code == 200:
                    logger.info(f"✅ Pushed {len(audit_logs)} audit log entries to cloud")
                    return True
                else:
                    logger.error(f"❌ Cloud audit log push failed: {response.status_code}")
                    return False

        except Exception as e:
            logger.error(f"❌ Cloud audit log push error: {e}")
            return False


# Global instance (initialized by config)
_cloud_service: Optional[CloudPushService] = None


def init_cloud_service(config: dict):
    """Initialize global cloud service instance."""
    global _cloud_service
    _cloud_service = CloudPushService(config)
    logger.info(f"Cloud push service initialized (enabled={config.get('enabled', False)})")


def get_cloud_service() -> Optional[CloudPushService]:
    """Get global cloud service instance."""
    return _cloud_service
