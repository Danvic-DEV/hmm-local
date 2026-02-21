"""
HMM-Local Updater Service
Simple web UI for updating the hmm-local container
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import subprocess
import asyncio
import json
import logging
import httpx
from datetime import datetime
from typing import List, Optional
import os
import re

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="HMM-Local Updater", version="1.0.0")

# WebSocket connections for streaming logs
active_connections: List[WebSocket] = []

# Update status
update_in_progress = False


class UpdateRequest(BaseModel):
    container_name: Optional[str] = None
    new_image: Optional[str] = None


CONTAINER_IMAGE_REPOS = {
    "hmm-local": "ghcr.io/renegadeuk/hmm-local",
    "dgb-local-stack": "ghcr.io/renegadeuk/hmm-local-dgb-stack",
}


async def resolve_target_image(container_name: str, requested_image: Optional[str]) -> str:
    """Resolve target image for requested container.

    Priority:
    1) Explicit image from request body
    2) Latest main commit tag (main-<sha>) for known container image repo
    3) Fallback to :main for known repo (or TARGET_IMAGE env for hmm-local)
    """
    if requested_image:
        return requested_image

    image_repo = CONTAINER_IMAGE_REPOS.get(container_name)

    if not image_repo:
        default_image = os.environ.get("TARGET_IMAGE")
        if default_image:
            return default_image
        raise ValueError(f"No image mapping configured for container: {container_name}")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.github.com/repos/renegadeuk/hmm-local/commits/main"
            )
            response.raise_for_status()
            data = response.json()
            latest_sha = data["sha"][:7]
            return f"{image_repo}:main-{latest_sha}"
    except Exception as e:
        logger.warning("Failed to fetch latest commit SHA for %s: %s", container_name, e)
        if container_name == "hmm-local":
            return os.environ.get("TARGET_IMAGE", "ghcr.io/renegadeuk/hmm-local:main")
        return f"{image_repo}:main"


async def broadcast_log(message: str, level: str = "info"):
    """Broadcast log message to all connected WebSocket clients"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_data = {
        "timestamp": timestamp,
        "level": level,
        "message": message
    }
    
    # Also log to server console
    if level == "error":
        logger.error(message)
    elif level == "warning":
        logger.warning(message)
    else:
        logger.info(message)
    
    # Broadcast to WebSocket clients
    disconnected = []
    for connection in active_connections:
        try:
            await connection.send_json(log_data)
        except:
            disconnected.append(connection)
    
    # Remove disconnected clients
    for conn in disconnected:
        active_connections.remove(conn)


async def broadcast_progress(stage: str, progress: int, message: Optional[str] = None):
    """Broadcast progress updates to all connected WebSocket clients."""
    payload = {
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "level": "info",
        "stage": stage,
        "progress": max(0, min(100, int(progress))),
        "message": message or stage,
    }

    disconnected = []
    for connection in active_connections:
        try:
            await connection.send_json(payload)
        except Exception:
            disconnected.append(connection)

    for conn in disconnected:
        if conn in active_connections:
            active_connections.remove(conn)


_PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)%")
_LAYER_RE = re.compile(r"^([a-f0-9]{12,64}):")
_SIZE_RE = re.compile(r"([0-9]*\.?[0-9]+)\s*([kKmMgGtT]?B)")


def _size_to_bytes(value: float, unit: str) -> float:
    unit_upper = unit.upper()
    factors = {
        "B": 1,
        "KB": 1024,
        "MB": 1024 ** 2,
        "GB": 1024 ** 3,
        "TB": 1024 ** 4,
    }
    return value * factors.get(unit_upper, 1)


def _extract_layer_progress(line: str, layer_progress: dict[str, float]) -> Optional[int]:
    """Parse docker pull output and return aggregate pull progress percent (0-100)."""
    layer_match = _LAYER_RE.search(line)
    if not layer_match:
        return None

    layer_id = layer_match.group(1)

    if "Pull complete" in line or "Already exists" in line:
        layer_progress[layer_id] = 100.0
    else:
        percent_match = _PERCENT_RE.search(line)
        if percent_match:
            try:
                layer_progress[layer_id] = max(0.0, min(100.0, float(percent_match.group(1))))
            except ValueError:
                pass
        else:
            sizes = _SIZE_RE.findall(line)
            if len(sizes) >= 2:
                try:
                    current_value = float(sizes[0][0])
                    current_unit = sizes[0][1]
                    total_value = float(sizes[1][0])
                    total_unit = sizes[1][1]
                    current_bytes = _size_to_bytes(current_value, current_unit)
                    total_bytes = _size_to_bytes(total_value, total_unit)
                    if total_bytes > 0:
                        layer_progress[layer_id] = max(0.0, min(100.0, (current_bytes / total_bytes) * 100.0))
                except (ValueError, ZeroDivisionError):
                    pass

    if not layer_progress:
        return None

    aggregate = sum(layer_progress.values()) / len(layer_progress)
    return int(round(aggregate))


@app.get("/", response_class=HTMLResponse)
async def get_index():
    """Serve the main UI"""
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>HMM-Local Updater</title>
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0%25' stop-color='%23667eea'/%3E%3Cstop offset='100%25' stop-color='%23764ba2'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width='64' height='64' rx='14' fill='url(%23g)'/%3E%3Cpath d='M44 20v10H34' stroke='white' stroke-width='5' stroke-linecap='round' stroke-linejoin='round' fill='none'/%3E%3Cpath d='M20 44V34h10' stroke='white' stroke-width='5' stroke-linecap='round' stroke-linejoin='round' fill='none'/%3E%3Cpath d='M42 30a12 12 0 0 0-20-8' stroke='white' stroke-width='5' stroke-linecap='round' fill='none'/%3E%3Cpath d='M22 34a12 12 0 0 0 20 8' stroke='white' stroke-width='5' stroke-linecap='round' fill='none'/%3E%3C/svg%3E">
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        
        .container {
            background: white;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            max-width: 800px;
            width: 100%;
            overflow: hidden;
        }
        
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 30px;
            text-align: center;
            color: white;
        }
        
        .header h1 {
            font-size: 28px;
            font-weight: 600;
            margin-bottom: 8px;
        }
        
        .header p {
            opacity: 0.9;
            font-size: 14px;
        }
        
        .content {
            padding: 30px;
        }

        .update-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 16px;
            margin-bottom: 30px;
        }

        @media (max-width: 700px) {
            .update-grid {
                grid-template-columns: 1fr;
            }
        }

        .update-card {
            background: #f8fafc;
            border: 1px solid #e5e7eb;
            border-radius: 14px;
            padding: 16px;
        }

        .update-card h3 {
            font-size: 16px;
            color: #1f2937;
            margin-bottom: 8px;
        }

        .update-card p {
            color: #6b7280;
            font-size: 13px;
            margin-bottom: 12px;
            min-height: 34px;
        }
        
        .update-button {
            width: 100%;
            padding: 16px 32px;
            font-size: 15px;
            font-weight: 600;
            color: white;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border: none;
            border-radius: 12px;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        
        .update-button:hover:not(:disabled) {
            transform: translateY(-2px);
            box-shadow: 0 10px 20px rgba(102, 126, 234, 0.4);
        }
        
        .update-button:active:not(:disabled) {
            transform: translateY(0);
        }
        
        .update-button:disabled {
            opacity: 0.6;
            cursor: not-allowed;
        }
        
        .update-button.updating {
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
        }
        
        .logs-container {
            background: #1a1a1a;
            border-radius: 12px;
            padding: 20px;
            height: 400px;
            overflow-y: auto;
            font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
            font-size: 13px;
            line-height: 1.6;
        }

        .progress-wrap {
            margin-bottom: 16px;
            background: #f8fafc;
            border: 1px solid #e5e7eb;
            border-radius: 12px;
            padding: 12px;
        }

        .progress-head {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
            font-size: 13px;
            color: #4b5563;
        }

        .progress-bar {
            width: 100%;
            height: 10px;
            background: #e5e7eb;
            border-radius: 999px;
            overflow: hidden;
        }

        .progress-fill {
            height: 100%;
            width: 0%;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            transition: width 0.35s ease;
        }

        .progress-subtext {
            margin-top: 8px;
            font-size: 12px;
            color: #6b7280;
            min-height: 16px;
        }
        
        .log-entry {
            margin-bottom: 8px;
            display: flex;
            gap: 12px;
        }
        
        .log-timestamp {
            color: #666;
            flex-shrink: 0;
        }
        
        .log-message {
            color: #e0e0e0;
        }
        
        .log-entry.info .log-message {
            color: #4CAF50;
        }
        
        .log-entry.warning .log-message {
            color: #FF9800;
        }
        
        .log-entry.error .log-message {
            color: #f44336;
        }
        
        .status-indicator {
            display: inline-block;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            margin-right: 8px;
        }
        
        .status-indicator.connected {
            background: #4CAF50;
            box-shadow: 0 0 10px #4CAF50;
        }
        
        .status-indicator.disconnected {
            background: #f44336;
        }
        
        .footer {
            padding: 20px 30px;
            background: #f5f5f5;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 13px;
            color: #666;
        }
        
        .status-text {
            display: flex;
            align-items: center;
        }
        
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        
        .spinner {
            display: inline-block;
            width: 16px;
            height: 16px;
            border: 2px solid rgba(255, 255, 255, 0.3);
            border-top-color: white;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            margin-right: 8px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🔄 HMM-Local Updater</h1>
            <p>Update your HMM containers with one click</p>
        </div>
        
        <div class="content">
            <div class="update-grid">
                <div class="update-card">
                    <h3>HMM-Local</h3>
                    <p>Main application container.</p>
                    <button id="updateBtnLocal" class="update-button" onclick="startUpdate('hmm-local', 'updateBtnLocal', 'Update HMM-Local')">
                        Update HMM-Local
                    </button>
                </div>

                <div class="update-card">
                    <h3>DGB Stack</h3>
                    <p>DigiByte node + CKPool stack container.</p>
                    <button id="updateBtnDgb" class="update-button" onclick="startUpdate('dgb-local-stack', 'updateBtnDgb', 'Update DGB Stack')">
                        Update DGB Stack
                    </button>
                </div>
            </div>

            <div class="progress-wrap" id="progressWrap">
                <div class="progress-head">
                    <span id="progressStage">Idle</span>
                    <span id="progressPercent">0%</span>
                </div>
                <div class="progress-bar">
                    <div id="progressFill" class="progress-fill"></div>
                </div>
                <div id="progressSubtext" class="progress-subtext">Ready to start update.</div>
            </div>
            
            <div class="logs-container" id="logsContainer">
                <div class="log-entry info">
                    <span class="log-timestamp">--:--:--</span>
                    <span class="log-message">Ready to update. Choose a container tile above to begin.</span>
                </div>
            </div>
        </div>
        
        <div class="footer">
            <div class="status-text">
                <span class="status-indicator" id="statusIndicator"></span>
                <span id="statusText">Connecting...</span>
            </div>
            <div>HMM-Local Updater v1.0.0</div>
        </div>
    </div>
    
    <script>
        let ws = null;
        let isUpdating = false;
        let activeButtonId = null;
        let currentProgress = 0;
        const buttonLabels = {
            updateBtnLocal: 'Update HMM-Local',
            updateBtnDgb: 'Update DGB Stack'
        };

        function updateProgress(progress, stage, detail) {
            const progressFill = document.getElementById('progressFill');
            const progressPercent = document.getElementById('progressPercent');
            const progressStage = document.getElementById('progressStage');
            const progressSubtext = document.getElementById('progressSubtext');
            if (!progressFill || !progressPercent || !progressStage || !progressSubtext) return;

            const safeProgress = Math.max(0, Math.min(100, Number(progress) || 0));
            if (safeProgress < currentProgress) return;
            currentProgress = safeProgress;

            progressFill.style.width = `${safeProgress}%`;
            progressPercent.textContent = `${safeProgress}%`;
            if (stage) progressStage.textContent = stage;
            if (detail) progressSubtext.textContent = detail;
        }

        function resetProgress() {
            currentProgress = 0;
            updateProgress(0, 'Idle', 'Ready to start update.');
        }

        function setButtonsDisabled(disabled) {
            const localBtn = document.getElementById('updateBtnLocal');
            if (localBtn) localBtn.disabled = disabled;
            const dgbBtn = document.getElementById('updateBtnDgb');
            if (dgbBtn) dgbBtn.disabled = disabled;
        }
        
        function connectWebSocket() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${protocol}//${window.location.host}/ws/logs`);
            
            ws.onopen = () => {
                console.log('WebSocket connected');
                document.getElementById('statusIndicator').classList.add('connected');
                document.getElementById('statusIndicator').classList.remove('disconnected');
                document.getElementById('statusText').textContent = 'Connected';
                addLog('info', 'Connected to updater service');
            };
            
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                addLog(data.level, data.message, data.timestamp);

                if (typeof data.progress === 'number') {
                    updateProgress(data.progress, data.stage || 'Updating', data.message || 'Updating...');
                }

                if (data.level === 'error' ||
                    (typeof data.message === 'string' &&
                     (data.message.includes('Update completed successfully') ||
                      data.message.includes('Update failed')))) {
                    setTimeout(resetButtons, 800);
                }
            };
            
            ws.onclose = () => {
                console.log('WebSocket disconnected');
                document.getElementById('statusIndicator').classList.remove('connected');
                document.getElementById('statusIndicator').classList.add('disconnected');
                document.getElementById('statusText').textContent = 'Disconnected';
                
                // Attempt reconnect after 2 seconds
                setTimeout(connectWebSocket, 2000);
            };
            
            ws.onerror = (error) => {
                console.error('WebSocket error:', error);
            };
        }
        
        function addLog(level, message, timestamp = null) {
            const logsContainer = document.getElementById('logsContainer');
            const logEntry = document.createElement('div');
            logEntry.className = `log-entry ${level}`;
            
            if (!timestamp) {
                const now = new Date();
                timestamp = now.toTimeString().split(' ')[0];
            }
            
            logEntry.innerHTML = `
                <span class="log-timestamp">${timestamp}</span>
                <span class="log-message">${message}</span>
            `;
            
            logsContainer.appendChild(logEntry);
            logsContainer.scrollTop = logsContainer.scrollHeight;
        }
        
        async function startUpdate(containerName, buttonId, buttonLabel) {
            if (isUpdating) return;
            
            const btn = document.getElementById(buttonId);
            if (!btn) return;

            setButtonsDisabled(true);
            btn.classList.add('updating');
            btn.innerHTML = '<span class="spinner"></span> Updating...';
            isUpdating = true;
            activeButtonId = buttonId;
            resetProgress();
            updateProgress(1, 'Starting update', `Starting update for ${containerName}`);

            addLog('info', `Starting update for ${containerName}`);
            
            try {
                const response = await fetch('/update', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        container_name: containerName
                    })
                });
                
                const data = await response.json();
                
                if (response.ok) {
                    // Update will be in progress, logs will stream via WebSocket
                } else {
                    addLog('error', `Update failed: ${data.detail || data.error || 'Unknown error'}`);
                    resetButtons();
                }
            } catch (error) {
                addLog('error', `Request failed: ${error.message}`);
                resetButtons();
            }
        }
        
        function resetButtons() {
            ['updateBtnLocal', 'updateBtnDgb'].forEach((id) => {
                const btn = document.getElementById(id);
                if (!btn) return;
                btn.disabled = false;
                btn.classList.remove('updating');
                btn.textContent = buttonLabels[id];
            });
            isUpdating = false;
            activeButtonId = null;
        }
        
        // Initialize WebSocket connection
        connectWebSocket();
        
        // Reset button after update completes (listen for completion message)
        window.addEventListener('message', (event) => {
            if (event.data === 'update-complete') {
                setTimeout(resetButtons, 2000);
            }
        });
    </script>
</body>
</html>
"""


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "hmm-local-updater",
        "version": "1.0.0",
        "timestamp": datetime.now().isoformat(),
        "update_in_progress": update_in_progress
    }


@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    """WebSocket endpoint for streaming logs"""
    await websocket.accept()
    active_connections.append(websocket)
    
    try:
        # Send welcome message
        await websocket.send_json({
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "level": "info",
            "message": "WebSocket connection established"
        })
        
        # Keep connection alive
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_connections.remove(websocket)


@app.post("/update")
async def update_container(update_request: Optional[UpdateRequest] = None):
    """
    Update a target container
    - Pull latest image from GHCR
    - Restart the container with new image
    """
    global update_in_progress
    
    if update_in_progress:
        return {"error": "Update already in progress"}, 409

    target_container = (
        update_request.container_name
        if update_request and update_request.container_name
        else os.environ.get("TARGET_CONTAINER", "hmm-local")
    )

    requested_image = update_request.new_image if update_request else None
    
    # Start update in background
    asyncio.create_task(perform_update(target_container, requested_image))
    
    return {
        "success": True,
        "message": "Update started. Check logs for progress.",
        "container_name": target_container,
        "requested_image": requested_image,
    }


async def perform_update(container_name: str, requested_image: Optional[str] = None):
    """Perform the actual container update with full config preservation"""
    global update_in_progress
    update_in_progress = True
    
    try:
        # Resolve image target before changing container state
        await broadcast_log("🔍 Resolving target image...", "info")
        await broadcast_progress("Preparing update", 3, "Preparing update")
        try:
            new_image = await resolve_target_image(container_name, requested_image)
        except Exception as e:
            await broadcast_log(f"❌ Could not resolve target image: {e}", "error")
            await broadcast_progress("Update failed", 100, "Failed to resolve target image")
            return
        
        await broadcast_log(f"🚀 Starting update for container: {container_name}", "info")
        await broadcast_log(f"📦 Target image: {new_image}", "info")
        await broadcast_progress("Inspecting container", 10, "Inspecting current container")
        
        # Step 1: Inspect current container to get configuration
        await broadcast_log("🔍 Inspecting current container configuration...", "info")
        process = await asyncio.create_subprocess_exec(
            "docker", "inspect", container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            error_msg = stderr.decode() if stderr else "Unknown error"
            await broadcast_log(f"❌ Failed to inspect container: {error_msg}", "error")
            return
        
        import json
        container_info = json.loads(stdout.decode())[0]
        
        # Extract configuration (be defensive: docker inspect may return nulls)
        config = container_info.get('Config') or {}
        host_config = container_info.get('HostConfig') or {}
        network_settings = container_info.get('NetworkSettings') or {}
        
        # Extract key settings
        env_vars = config.get('Env', []) if isinstance(config, dict) else []
        if not isinstance(env_vars, list):
            env_vars = []

        binds = host_config.get('Binds', []) if isinstance(host_config, dict) else []
        if not isinstance(binds, list):
            binds = []

        restart_policy = host_config.get('RestartPolicy', {}) if isinstance(host_config, dict) else {}
        if not isinstance(restart_policy, dict):
            restart_policy = {}
        restart_policy_name = restart_policy.get('Name', 'no')

        # Extract port bindings
        port_bindings = host_config.get('PortBindings', {}) if isinstance(host_config, dict) else {}
        if not isinstance(port_bindings, dict):
            port_bindings = {}
        
        await broadcast_log(f"   Environment variables: {len(env_vars)}", "info")
        await broadcast_log(f"   Volume binds: {len(binds)}", "info")
        await broadcast_log(f"   Port bindings: {len(port_bindings)}", "info")
        await broadcast_log(f"   Restart policy: {restart_policy_name}", "info")
        
        # Extract network settings (including static IP)
        networks = network_settings.get('Networks', {}) if isinstance(network_settings, dict) else {}
        if not isinstance(networks, dict):
            networks = {}
        network_name = None
        ip_address = None
        
        if networks:
            network_name = list(networks.keys())[0]
            network_info = networks.get(network_name) or {}
            if not isinstance(network_info, dict):
                network_info = {}
            # Try IPAMConfig first (static IP configuration), fallback to IPAddress
            ipam_config = network_info.get('IPAMConfig') or {}
            if not isinstance(ipam_config, dict):
                ipam_config = {}
            ip_address = ipam_config.get('IPv4Address') or network_info.get('IPAddress')
            if ip_address:
                await broadcast_log(f"   Static IP: {ip_address}", "info")
        
        await broadcast_log("✅ Configuration extracted successfully", "info")
        await broadcast_progress("Configuration loaded", 18, "Configuration loaded")
        
        async def _pull_image(image: str, stage_prefix: str) -> tuple[bool, list[str]]:
            await broadcast_log(f"⬇️  Pulling image from GHCR ({stage_prefix})...", "info")
            await broadcast_progress("Pulling latest image from GHCR", 20, "Pulling latest image from GHCR")
            process = await asyncio.create_subprocess_exec(
                "docker", "pull", image,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            pull_output: list[str] = []
            layer_progress: dict[str, float] = {}
            last_pull_progress = -1

            while True:
                line = await process.stdout.readline()
                if not line:
                    break

                decoded_line = line.decode(errors="replace").strip()
                if not decoded_line:
                    continue

                pull_output.append(decoded_line)

                pull_percent = _extract_layer_progress(decoded_line, layer_progress)
                if pull_percent is not None and pull_percent != last_pull_progress:
                    # Allocate 20%..70% of overall update to image pull.
                    overall_progress = 20 + int((pull_percent / 100.0) * 50)
                    await broadcast_progress(
                        "Pulling latest image from GHCR",
                        overall_progress,
                        f"Pulling latest image from GHCR ({pull_percent}%)",
                    )
                    last_pull_progress = pull_percent

            await process.wait()
            return process.returncode == 0, pull_output

        # Step 2: Pull latest image (with fallback)
        ok, pull_output = await _pull_image(new_image, "primary")

        if not ok:
            tail = "\n".join(pull_output[-30:])
            await broadcast_log(f"❌ Failed to pull image: {new_image}", "error")
            if tail:
                await broadcast_log(f"Pull output (tail):\n{tail}", "error")

            # If we auto-resolved a SHA tag that doesn't exist (common with path-filtered CI),
            # retry with a stable tag.
            fallback_image: Optional[str] = None
            if not requested_image and re.search(r":main-[0-9a-f]{7,}$", new_image):
                image_repo = CONTAINER_IMAGE_REPOS.get(container_name)
                if image_repo:
                    if container_name == "hmm-local":
                        fallback_image = os.environ.get("TARGET_IMAGE", f"{image_repo}:main")
                    else:
                        fallback_image = f"{image_repo}:main"

            if fallback_image and fallback_image != new_image:
                await broadcast_log(
                    f"↩️ Retrying pull with fallback tag: {fallback_image}",
                    "warning",
                )
                ok2, pull_output2 = await _pull_image(fallback_image, "fallback")
                if not ok2:
                    tail2 = "\n".join(pull_output2[-30:])
                    await broadcast_log(f"❌ Fallback pull also failed: {fallback_image}", "error")
                    if tail2:
                        await broadcast_log(f"Fallback pull output (tail):\n{tail2}", "error")
                    await broadcast_progress("Update failed", 100, "Failed while pulling image")
                    return

                new_image = fallback_image
            else:
                await broadcast_progress("Update failed", 100, "Failed while pulling image")
                return
        
        await broadcast_log("✅ Image pulled successfully", "info")
        await broadcast_progress("Image pulled", 70, "Image pulled successfully")
        
        # Step 3: Stop current container
        await broadcast_log(f"⏸️  Stopping container {container_name}...", "info")
        await broadcast_progress("Stopping container", 78, "Stopping container")
        process = await asyncio.create_subprocess_exec(
            "docker", "stop", container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            error_msg = stderr.decode() if stderr else "Unknown error"
            await broadcast_log(f"⚠️  Warning: Could not stop container: {error_msg}", "warning")
        else:
            await broadcast_log("✅ Container stopped", "info")
            await broadcast_progress("Container stopped", 82, "Container stopped")
        
        # Step 4: Remove old container
        await broadcast_log(f"🗑️  Removing old container...", "info")
        await broadcast_progress("Removing old container", 86, "Removing old container")
        process = await asyncio.create_subprocess_exec(
            "docker", "rm", container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        await process.communicate()
        await broadcast_log("✅ Old container removed", "info")
        await broadcast_progress("Old container removed", 90, "Old container removed")
        
        # Small delay to ensure cleanup
        await asyncio.sleep(1)
        
        # Step 5: Recreate container with preserved configuration
        await broadcast_log(f"🚀 Creating new container with preserved configuration...", "info")
        await broadcast_progress("Creating new container", 94, "Creating new container")
        
        # Build docker run command
        docker_cmd = ["docker", "run", "-d", "--name", container_name]
        
        # Add environment variables
        for env in env_vars:
            docker_cmd.extend(["-e", env])
        
        # Add volume binds
        for bind in binds:
            docker_cmd.extend(["-v", bind])
        
        # Add restart policy
        if restart_policy_name != 'no':
            max_retry = restart_policy.get('MaximumRetryCount', 0)
            if max_retry > 0:
                docker_cmd.extend(["--restart", f"{restart_policy_name}:{max_retry}"])
            else:
                docker_cmd.extend(["--restart", restart_policy_name])
        
        # Add port bindings
        for container_port, host_bindings in port_bindings.items():
            if not host_bindings:
                continue
            if not isinstance(host_bindings, list):
                continue
            for binding in host_bindings:
                if not isinstance(binding, dict):
                    continue
                host_port = binding.get('HostPort')
                if host_port:
                    docker_cmd.extend(["-p", f"{host_port}:{container_port}"])
        
        # Add network with static IP if applicable
        if network_name and network_name not in ['bridge', 'host', 'none']:
            docker_cmd.extend(["--network", network_name])
            if ip_address:
                docker_cmd.extend(["--ip", ip_address])
                await broadcast_log(f"   Preserving static IP: {ip_address}", "info")
        elif network_name:
            docker_cmd.extend(["--network", network_name])
        
        # Add image
        docker_cmd.append(new_image)
        
        # Execute docker run
        process = await asyncio.create_subprocess_exec(
            *docker_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            error_msg = stderr.decode() if stderr else "Unknown error"
            await broadcast_log(f"❌ Failed to create container: {error_msg}", "error")
            await broadcast_progress("Update failed", 100, "Failed while creating container")
            return
        
        await broadcast_log("✅ New container created successfully", "info")
        await broadcast_log("🎉 Update completed successfully!", "info")
        await broadcast_log(f"ℹ️  Container {container_name} is now running {new_image}", "info")
        await broadcast_log("✅ All settings preserved (volumes, network, static IP, environment, restart policy)", "info")
        await broadcast_progress("Update complete", 100, "Update complete")
        
    except Exception as e:
        await broadcast_log(f"❌ Update failed: {str(e)}", "error")
        await broadcast_progress("Update failed", 100, "Update failed")
        logger.exception("Update failed")
    
    finally:
        update_in_progress = False


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8081))
    uvicorn.run(app, host="0.0.0.0", port=port)
