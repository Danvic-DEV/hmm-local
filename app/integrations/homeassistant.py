"""
Home Assistant REST API Integration
Supports controlling Home Assistant devices (switches, lights, climate, etc.)
"""
import asyncio
import httpx
from typing import List, Optional, Dict, Any
import logging
from datetime import datetime

from .base import IntegrationAdapter, DeviceInfo, DeviceState

logger = logging.getLogger(__name__)


class HomeAssistantIntegration(IntegrationAdapter):
    """Home Assistant platform adapter"""
    
    def __init__(self, base_url: str, access_token: str, timeout: float = 10.0, retries: int = 3):
        """
        Initialize Home Assistant integration
        
        Args:
            base_url: Base URL (e.g., "http://homeassistant.local:8123")
            access_token: Long-Lived Access Token from HA
            timeout: Request timeout in seconds
            retries: Number of retry attempts for transient failures
        """
        self.base_url = base_url.rstrip('/')
        self.token = access_token
        self.timeout = timeout
        self.retries = max(1, retries)
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        timeout = httpx.Timeout(self.timeout)
        self._client = httpx.AsyncClient(timeout=timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, json: Optional[dict] = None) -> Optional[httpx.Response]:
        url = f"{self.base_url}{path}"
        timeout = httpx.Timeout(self.timeout)

        for attempt in range(1, self.retries + 1):
            try:
                if self._client is not None:
                    response = await self._client.request(
                        method,
                        url,
                        headers=self.headers,
                        json=json,
                    )
                else:
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        response = await client.request(
                            method,
                            url,
                            headers=self.headers,
                            json=json,
                        )
                response.raise_for_status()
                return response
            except asyncio.CancelledError:
                logger.warning(f"HA request cancelled: {method} {url}")
                return None
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt < self.retries:
                    backoff = 0.5 * (2 ** (attempt - 1))
                    logger.warning(
                        f"HA request failed (attempt {attempt}/{self.retries}): {method} {url} - {e}"
                    )
                    await asyncio.sleep(backoff)
                    continue
                logger.error(f"HA request failed after {self.retries} attempts: {method} {url} - {e}")
                return None
            except Exception as e:
                logger.error(f"HA request error: {method} {url} - {e}")
                return None
    
    async def test_connection(self) -> bool:
        """Test connection to Home Assistant"""
        response = await self._request("GET", "/api/")
        if not response:
            return False
        try:
            data = response.json()
            logger.info(f"Connected to Home Assistant: {data.get('message', 'OK')}")
            return True
        except Exception as e:
            logger.error(f"Failed to parse Home Assistant response: {e}")
            return False
    
    async def discover_devices(self, domain: Optional[str] = None) -> List[DeviceInfo]:
        """
        Discover all entities in Home Assistant
        
        Args:
            domain: Optional filter (e.g., "switch" to only get switches)
        """
        response = await self._request("GET", "/api/states")
        if not response:
            return []
        try:
            entities = response.json()
            
            devices = []
            for entity in entities:
                entity_id = entity.get("entity_id", "")
                entity_domain = entity_id.split(".")[0] if "." in entity_id else ""
                
                # Filter by domain if specified
                if domain and entity_domain != domain:
                    continue
                
                # Get capabilities from attributes
                attributes = entity.get("attributes", {})
                capabilities = []
                
                # Common capabilities
                if entity_domain in ["switch", "light", "fan"]:
                    capabilities.extend(["turn_on", "turn_off"])
                if entity_domain == "light":
                    if "brightness" in attributes:
                        capabilities.append("brightness")
                    if "color_temp" in attributes:
                        capabilities.append("color_temp")
                if entity_domain == "climate":
                    capabilities.extend(["set_temperature", "set_hvac_mode"])
                
                devices.append(DeviceInfo(
                    entity_id=entity_id,
                    name=attributes.get("friendly_name", entity_id),
                    domain=entity_domain,
                    platform="homeassistant",
                    capabilities=capabilities
                ))
            
            logger.info(f"Discovered {len(devices)} devices" + 
                       (f" (filtered to {domain})" if domain else ""))
            return devices
        except Exception as e:
            logger.error(f"Failed to parse Home Assistant devices: {e}")
            return []
    
    async def get_device_state(self, entity_id: str) -> Optional[DeviceState]:
        """Get current state of an entity"""
        response = await self._request("GET", f"/api/states/{entity_id}")
        if not response:
            return None
        try:
            data = response.json()
            return DeviceState(
                entity_id=entity_id,
                name=data.get("attributes", {}).get("friendly_name", entity_id),
                state=data.get("state", "unknown"),
                attributes=data.get("attributes", {}),
                last_updated=datetime.fromisoformat(
                    data.get("last_updated", datetime.utcnow().isoformat()).replace("Z", "+00:00")
                )
            )
        except Exception as e:
            logger.error(f"Failed to parse state for {entity_id}: {e}")
            return None

    async def get_device_state_value(self, entity_id: str) -> Optional[str]:
        """Get current device state string only (memory-optimized path)."""
        response = await self._request("GET", f"/api/states/{entity_id}")
        if not response:
            return None
        try:
            data = response.json()
            state = data.get("state")
            return str(state) if state is not None else None
        except Exception as e:
            logger.error(f"Failed to parse state value for {entity_id}: {e}")
            return None
    
    async def turn_on(self, entity_id: str) -> bool:
        """Turn device on"""
        domain = entity_id.split(".")[0] if "." in entity_id else "switch"
        return await self.call_service(domain, "turn_on", entity_id)
    
    async def turn_off(self, entity_id: str) -> bool:
        """Turn device off"""
        domain = entity_id.split(".")[0] if "." in entity_id else "switch"
        return await self.call_service(domain, "turn_off", entity_id)
    
    async def call_service(
        self, 
        domain: str, 
        service: str, 
        entity_id: str,
        data: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Call a Home Assistant service
        
        Example:
            await call_service("switch", "turn_on", "switch.miner_power")
            await call_service("climate", "set_temperature", "climate.bedroom", 
                             data={"temperature": 22})
        """
        payload = {
            "entity_id": entity_id
        }
        if data:
            payload.update(data)

        response = await self._request("POST", f"/api/services/{domain}/{service}", json=payload)
        if not response:
            return False

        logger.info(f"Called {domain}.{service} on {entity_id}")
        return True
