"""
Avalon Nano 3 / 3S adapter using cgminer TCP API
"""
import socket
import json
import asyncio
import logging
from typing import Dict, List, Optional
from adapters.base import MinerAdapter, MinerTelemetry
from core.utils import format_hashrate

logger = logging.getLogger(__name__)

__version__ = "1.1.3"


class AvalonNanoAdapter(MinerAdapter):
    """Adapter for Avalon Nano 3 / 3S miners"""
    
    miner_type = "avalon_nano"  # Required for driver loader
    STRATEGY_MODE_FIELD = "avalon_nano_mode"
    CHAMPION_LOWEST_MODE = "low"
    MODES = ["low", "med", "high"]
    DEFAULT_PORT = 4028
    DEFAULT_ADMIN_PASSWORD = "admin"  # Default password, can be overridden in config

    @staticmethod
    def _as_int(value, default: int = 0) -> int:
        try:
            if value is None:
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _select_active_pool(pools: List[Dict]) -> Optional[Dict]:
        """
        Select the best current pool from Avalon cgminer pools payload.

        Avalon can report priority 0 as Dead while actively hashing on another
        alive slot during failover/reboot windows. Restricting detection to
        priority-0 may produce false pool_in_use=None.
        """
        if not pools:
            return None

        alive_pools = [p for p in pools if str(p.get("Status", "")).lower() == "alive"]
        if not alive_pools:
            return None

        def score(pool: Dict) -> tuple:
            stratum_active = bool(pool.get("Stratum Active"))
            last_share_time = AvalonNanoAdapter._as_int(pool.get("Last Share Time"), 0)
            accepted = AvalonNanoAdapter._as_int(pool.get("Accepted"), 0)
            priority = AvalonNanoAdapter._as_int(pool.get("Priority"), 99)
            return (
                1 if stratum_active else 0,
                1 if last_share_time > 0 else 0,
                accepted,
                -priority,
            )

        return max(alive_pools, key=score)
    
    def __init__(self, miner_id: int, miner_name: str, ip_address: str, port: Optional[int] = None, config: Optional[Dict] = None):
        super().__init__(miner_id, miner_name, ip_address, port or self.DEFAULT_PORT, config)
        # Get admin password from config, default to "admin"
        self.admin_password = (config or {}).get("admin_password", self.DEFAULT_ADMIN_PASSWORD)
    
    async def get_telemetry(self) -> Optional[MinerTelemetry]:
        """Get telemetry from cgminer API"""
        try:
            # Get summary
            summary = await self._cgminer_command("summary")
            if not summary:
                return None
            
            # Get estats for power calculation
            estats = await self._cgminer_command("estats")

            # Get devs for fallback temperature + raw preservation
            devs = await self._cgminer_command("devs")
            
            # Get pool info
            pools = await self._cgminer_command("pools")
            
            # Parse telemetry
            summary_data = summary.get("SUMMARY", [{}])[0]
            mm_id0_raw = self._get_mm_id0(estats)
            mm_id0_parsed = self._parse_mm_id_tokens(mm_id0_raw)
            
            # Hashrate is in MH/s from cgminer
            hashrate_mhs = summary_data.get("MHS 5s", 0)
            hashrate_formatted = format_hashrate(hashrate_mhs, "MH/s")
            shares_accepted = summary_data.get("Accepted", 0)
            shares_rejected = summary_data.get("Rejected", 0)
            
            # Get temperature, power, and current mode from estats
            temperature = self._get_temperature(estats)
            power_watts = self._calculate_power(estats)
            current_mode = self._detect_current_mode(estats)
            
            # Get active pool and difficulty
            pool_in_use = None
            pool_difficulty = None
            last_share_difficulty = None
            work_difficulty = None
            pool_rejected_pct = None
            pool_stale_pct = None
            stale_shares = None
            if pools and "POOLS" in pools:
                selected_pool = self._select_active_pool(pools.get("POOLS", []))
                if selected_pool:
                    pool_in_use = selected_pool.get("URL")
                    last_share_difficulty = selected_pool.get("Last Share Difficulty")
                    work_difficulty = selected_pool.get("Work Difficulty")
                    pool_rejected_pct = selected_pool.get("Pool Rejected%")
                    pool_stale_pct = selected_pool.get("Pool Stale%")
                    stale_shares = selected_pool.get("Stale")

                    # Try to find pool difficulty from various possible fields
                    pool_difficulty = (
                        selected_pool.get("Diff") or
                        selected_pool.get("Difficulty") or
                        selected_pool.get("Stratum Difficulty") or
                        selected_pool.get("Current Diff") or
                        selected_pool.get("Pool Diff")
                    )

                    # Debug: Log available pool fields once
                    if pool_difficulty is None and not hasattr(self, '_logged_pool_fields'):
                        logger.info(f"Available pool fields for {self.miner_name}: {list(selected_pool.keys())}")
                        self._logged_pool_fields = True
            
            # Extract additional useful stats
            error_rate_pct = None
            try:
                if shares_accepted is not None and shares_rejected is not None:
                    total_shares = shares_accepted + shares_rejected
                    if total_shares > 0:
                        error_rate_pct = (shares_rejected / total_shares) * 100.0
            except Exception:
                error_rate_pct = None

            extra_stats = {
                "raw_source": "cgminer",
                "current_mode": current_mode,
                "firmware_version": mm_id0_parsed.get("Ver"),
                "uptime_seconds": summary_data.get("Elapsed"),
                "best_share_diff": summary_data.get("Best Share"),
                "best_session_diff": summary_data.get("Best Share"),
                "error_rate_pct": error_rate_pct,
                "best_share": summary_data.get("Best Share"),
                "network_difficulty": None,  # Will be fetched by high_diff_tracker if needed
                "hardware_errors": summary_data.get("Hardware Errors", 0),
                "utility": summary_data.get("Utility"),  # Shares per minute
                "found_blocks": summary_data.get("Found Blocks", 0),
                "elapsed": summary_data.get("Elapsed"),  # Uptime in seconds
                "difficulty_accepted": summary_data.get("Difficulty Accepted"),
                "difficulty_rejected": summary_data.get("Difficulty Rejected"),
                "difficulty": pool_difficulty,  # Current pool difficulty
                "pool_difficulty": pool_difficulty,
                "last_share_difficulty": last_share_difficulty,  # Last submitted share difficulty
                "work_difficulty": work_difficulty,  # Current work difficulty target
                "work_utility": summary_data.get("Work Utility"),
                "total_mh": summary_data.get("Total MH"),
                "remote_failures": summary_data.get("Remote Failures", 0),
                "get_failures": summary_data.get("Get Failures", 0),
                "network_blocks": summary_data.get("Network Blocks"),
                "stale_shares": stale_shares or summary_data.get("Stale", 0),
                "pool_rejected_pct": pool_rejected_pct,
                "pool_stale_pct": pool_stale_pct,
                "device_hardware_pct": summary_data.get("Device Hardware%"),
                "device_rejected_pct": summary_data.get("Device Rejected%"),
                "vendor": {
                    "raw": {
                        "summary": summary,
                        "pools": pools,
                        "devs": devs,
                        "estats": estats,
                        "mm_id0": mm_id0_raw,
                    },
                    "parsed": {
                        "mm_id0": mm_id0_parsed
                    }
                }
            }
            
            return MinerTelemetry(
                miner_id=self.miner_id,
                hashrate=hashrate_formatted["value"],  # Normalized to GH/s
                temperature=temperature,
                power_watts=power_watts,
                shares_accepted=shares_accepted,
                shares_rejected=shares_rejected,
                pool_difficulty=pool_difficulty,
                pool_in_use=pool_in_use,
                extra_data={
                    **extra_stats,
                    "hashrate_unit": "GH/s",
                    "hashrate_display": hashrate_formatted["display"],
                    # Legacy aliases (temporary compatibility during migration)
                    "version": mm_id0_parsed.get("Ver")
                }
            )
        except Exception as e:
            print(f"❌ Failed to get telemetry from Avalon Nano {self.ip_address}: {e}")
            return None
    
    def _detect_current_mode(self, estats: Optional[Dict]) -> Optional[str]:
        """Detect current mode from WORKMODE field"""
        if not estats or "STATS" not in estats:
            return None
        
        try:
            stats_data = estats["STATS"][0]
            mm_id = stats_data.get("MM ID0", "")
            
            # Parse WORKMODE from MM ID0 string (e.g., WORKMODE[2])
            # WORKMODE values: 0=low, 1=med, 2=high (most common mapping)
            if "WORKMODE[" in mm_id:
                start = mm_id.index("WORKMODE[") + 9
                end = mm_id.index("]", start)
                workmode = int(mm_id[start:end])
                
                # Map workmode to mode name
                mode_map = {
                    0: "low",
                    1: "med", 
                    2: "high"
                }
                return mode_map.get(workmode)
            
            return None
        except Exception as e:
            print(f"⚠️ Failed to detect mode: {e}")
            return None

    def _get_mm_id0(self, estats: Optional[Dict]) -> str:
        """Extract MM ID0 raw string from estats response."""
        if not estats or "STATS" not in estats:
            return ""

        try:
            stats_data = estats["STATS"][0]
            return stats_data.get("MM ID0", "")
        except Exception:
            return ""

    def _parse_mm_id_tokens(self, mm_id: str) -> Dict[str, str]:
        """Parse key[value] tokens from Avalon MM ID string."""
        parsed: Dict[str, str] = {}
        if not mm_id:
            return parsed

        try:
            import re
            for key, value in re.findall(r'([A-Za-z0-9_]+)\[([^\]]*)\]', mm_id):
                parsed[key] = value
        except Exception:
            return parsed

        return parsed
    
    def _get_temperature(self, estats: Optional[Dict]) -> Optional[float]:
        """Get temperature from estats MM ID string"""
        if not estats or "STATS" not in estats:
            return None
        
        try:
            stats_data = estats["STATS"][0]
            mm_id = stats_data.get("MM ID0", "")
            
            # Parse TAvg from MM ID0 string (e.g., TAvg[89])
            if "TAvg[" in mm_id:
                start = mm_id.index("TAvg[") + 5
                end = mm_id.index("]", start)
                return float(mm_id[start:end])
            
            return None
        except Exception as e:
            print(f"⚠️ Failed to get temperature: {e}")
            return None
    
    def _calculate_power(self, estats: Optional[Dict]) -> Optional[float]:
        """Get power from MPO field in MM ID string"""
        if not estats or "STATS" not in estats:
            return None
        
        try:
            stats_data = estats["STATS"][0]
            mm_id = stats_data.get("MM ID0", "")
            
            # Parse MPO from MM ID0 string (e.g., MPO[62])
            # MPO contains the actual power consumption in watts
            if "MPO[" in mm_id:
                start = mm_id.index("MPO[") + 4
                end = mm_id.index("]", start)
                mpo_str = mm_id[start:end]
                watts = float(mpo_str)
                return watts
            
            return None
        except Exception as e:
            print(f"⚠️ Failed to get power from MPO: {e}")
            return None
    
    async def get_mode(self) -> Optional[str]:
        """Get current operating mode"""
        try:
            result = await self._cgminer_command("estats")
            return self._detect_current_mode(result)
        except Exception as e:
            logger.debug(f"Could not get mode for Avalon Nano: {e}")
        return None
    
    async def set_mode(self, mode: str) -> bool:
        """Set operating mode using workmode parameter"""
        if mode not in self.MODES:
            print(f"❌ Invalid mode: {mode}. Valid modes: {self.MODES}")
            return False
        
        try:
            # Map mode to workmode values (0=low, 1=med, 2=high)
            workmode_map = {
                "low": 0,
                "med": 1,
                "high": 2
            }
            
            workmode = workmode_map.get(mode)
            print(f"📝 Setting Avalon Nano workmode to {workmode} for mode '{mode}'")
            result = await self._cgminer_command(f"ascset|0,workmode,set,{workmode}")
            print(f"✅ Workmode set result: {result}")

            if not result or not isinstance(result, dict):
                logger.warning(
                    "Avalon %s mode change to '%s' returned empty/invalid response",
                    self.miner_name,
                    mode,
                )
                return False

            statuses = result.get("STATUS")
            if not isinstance(statuses, list) or not statuses:
                logger.warning(
                    "Avalon %s mode change to '%s' missing STATUS payload: %s",
                    self.miner_name,
                    mode,
                    result,
                )
                return False

            success = any(str(status.get("STATUS", "")).upper() == "S" for status in statuses if isinstance(status, dict))
            if success:
                return True

            error_messages = []
            for status in statuses:
                if isinstance(status, dict):
                    code = status.get("Code")
                    msg = status.get("Msg")
                    error_messages.append(f"Code={code} Msg={msg}")

            logger.warning(
                "Avalon %s mode change to '%s' rejected by cgminer: %s",
                self.miner_name,
                mode,
                "; ".join(error_messages) if error_messages else result,
            )
            return False
        except Exception as e:
            print(f"❌ Failed to set mode on Avalon Nano: {e}")
            return False
    
    async def get_available_modes(self) -> List[str]:
        """Get available modes"""
        return self.MODES
    
    async def switch_pool(self, pool_url: str, pool_port: int, pool_user: str, pool_password: str) -> bool:
        """Switch mining pool - Avalon Nano supports dynamic pool configuration via setpool command
        
        Strategy: Use setpool command to dynamically configure pool slot 1, then reboot to activate.
        This eliminates the need for pre-configured pool slots.
        """
        try:
            # Construct username as pool_user.miner_name
            full_username = f"{pool_user}.{self.miner_name}"
            
            # Construct full pool URL with stratum protocol and port
            full_pool_url = f"stratum+tcp://{pool_url}:{pool_port}"
            
            logger.info(f"🔄 Configuring pool slot 0 for {self.miner_name}: {full_pool_url} with user: {full_username}")
            
            # Use slot 0 (first slot) since miner defaults to slot 0 after reboot
            # Format: setpool|admin,password,slot,pool_url,worker,pool_password
            setpool_cmd = f"setpool|admin,{self.admin_password},0,{full_pool_url},{full_username},{pool_password}"
            
            # Send setpool command - it doesn't return a JSON response, just sends the command
            result = await self._cgminer_command_raw(setpool_cmd)
            if result is None:
                logger.error(f"❌ Failed to send setpool command to {self.miner_name}")
                return False
            
            logger.info(f"✅ Pool configured for {self.miner_name}, rebooting to activate...")
            
            # Reboot to activate the new pool configuration
            # Format: ascset|0,reboot,0
            reboot_result = await self._cgminer_command_raw("ascset|0,reboot,0")
            
            # Note: Miner will disconnect during reboot, so we may not get a response
            # This is expected behavior
            logger.info(f"🔄 {self.miner_name} rebooting to activate new pool (will take ~30 seconds)")
            
            # Update last_pool_switch timestamp in database to prevent immediate re-switches
            from core.database import Miner, engine
            from sqlalchemy import select, update
            from sqlalchemy.ext.asyncio import AsyncSession
            from datetime import datetime
            
            async with AsyncSession(engine) as db:
                await db.execute(
                    update(Miner)
                    .where(Miner.name == self.miner_name)
                    .values(last_pool_switch=datetime.utcnow())
                )
                await db.commit()
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to switch pool for {self.miner_name}: {e}")
            return False
    
    async def restart(self) -> bool:
        """Restart miner"""
        try:
            result = await self._cgminer_command("restart")
            return result is not None
        except Exception as e:
            print(f"❌ Failed to restart Avalon Nano: {e}")
            return False
    
    async def is_online(self) -> bool:
        """Check if miner is online via TCP port check (fast ping)"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)  # Quick timeout for ping
            sock.connect((self.ip_address, self.port))
            sock.close()
            return True
        except:
            return False
    
    async def _cgminer_command(self, command: str) -> Optional[Dict]:
        """Send command to cgminer API
        
        Most cgminer commands require JSON format: {"command": "summary"}
        Only setpool/reboot use raw string format (handled by _cgminer_command_raw)
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)  # Reduced from 10s to 2s for faster failure detection
            sock.connect((self.ip_address, self.port))
            
            # Send command in JSON format for standard cgminer commands
            cmd = {"command": command.split("|")[0], "parameter": command.split("|")[1] if "|" in command else ""}
            sock.sendall(json.dumps(cmd).encode())
            
            # Receive response
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
            
            sock.close()
            
            # Parse JSON response - cgminer returns multiple JSON objects separated by null bytes
            # Split on null byte and take the first valid JSON
            decoded = response.decode('utf-8', errors='ignore')
            
            # Remove null bytes and control characters (keep only printable chars and whitespace)
            import re
            decoded = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', decoded).strip()
            
            # Try to find the first complete JSON object
            if decoded:
                # cgminer often returns JSON with trailing null bytes or extra data
                # Find the end of the first JSON object
                brace_count = 0
                json_end = -1
                for i, char in enumerate(decoded):
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            json_end = i + 1
                            break
                
                if json_end > 0:
                    json_str = decoded[:json_end]
                    return json.loads(json_str)
            
            return json.loads(decoded)
        except Exception as e:
            print(f"⚠️ cgminer command failed: {e}")
            return None
    
    async def _cgminer_command_raw(self, command: str) -> Optional[bool]:
        """Send raw command to cgminer API without expecting JSON response
        
        Used for setpool and reboot commands which don't return structured data.
        Returns True if command was sent successfully and got a response.
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect((self.ip_address, self.port))
            
            # Send raw command
            sock.sendall(command.encode())
            
            # Receive response
            response = b""
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
            except socket.timeout:
                pass
            
            sock.close()
            
            # Check if we got a valid response
            decoded = response.decode('utf-8', errors='ignore')
            
            # setpool returns: "Please reboot miner to make config work."
            # reboot might not return anything (disconnects immediately)
            if decoded or command.startswith("ascset"):
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"⚠️ cgminer raw command failed: {e}")
            return None
