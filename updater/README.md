# HMM-Local Updater Service

Companion container that handles platform updates for HMM-Local with a simple web UI.

## Features

- **Simple Web UI**: One-click update button with real-time log streaming
- **WebSocket Logs**: Live progress updates during container recreation
- **Health Monitoring**: Health check endpoint for status monitoring
- **Survives Restarts**: Manages HMM-Local updates while staying online

## Why?

**The Problem:** Container self-update is paradoxical:
- Stop container → Process dies → Recreation never happens ❌
- Rename first → Static IP conflict with new container ❌

**The Solution:** Separate updater service that:
- ✅ Survives main container restart
- ✅ Handles static IP assignments correctly
- ✅ Manages stop → remove → recreate flow
- ✅ Works with both docker-compose and production deployments
- ✅ **NEW: Provides beautiful web UI for manual updates**

## Architecture

```
┌─────────────────┐         HTTP POST        ┌─────────────────┐
│                 │   /update              │                 │
│   HMM-Local     │───────────────────────▶│  Updater        │
│   (Main App)    │   {container, image}   │  (Sidecar)      │
│                 │                        │                 │
└─────────────────┘                        └─────────────────┘
         │                                          │
         │                                          │
         ▼                                          ▼
┌─────────────────────────────────────────────────────────────┐
│                    Docker Socket                            │
│  /var/run/docker.sock                                       │
└─────────────────────────────────────────────────────────────┘
```

## Web UI

Access the updater web interface at: **http://localhost:8081** (or your configured port)

**Features:**
- 🎯 One-click update button
- 📊 Real-time log streaming via WebSocket
- 🎨 Beautiful, modern interface
- 📱 Responsive design

Simply click **"Update HMM-Local Container"** and watch the logs stream in real-time!

## API

### `GET /`
Web UI for manual updates.

### `GET /health`
Health check endpoint.

**Response:**
```json
{
  "status": "healthy",
  "service": "hmm-local-updater",
  "timestamp": "2026-02-06T19:31:54.036650",
  "version": "1.0.0",
  "update_in_progress": false
}
```

### `WS /ws/logs`
WebSocket endpoint for streaming logs during updates.

**Message Format:**
```json
{
  "timestamp": "19:32:15",
  "level": "info",
  "message": "✅ Container stopped"
}
```

### `POST /update`
Recreate a target container with a new image while preserving runtime settings.

Supports both:
- `hmm-local`
- `dgb-local-stack`

**Request:**
```json
{
  "container_name": "hmm-local",
  "new_image": "ghcr.io/renegadeuk/hmm-local:main-abc123"
}
```

**DGB Stack example:**
```json
{
  "container_name": "dgb-local-stack",
  "new_image": "ghcr.io/renegadeuk/hmm-local-dgb-stack:main-abc123"
}
```

`container_name` and `new_image` are optional:
- If `container_name` is omitted, defaults to `TARGET_CONTAINER` env or `hmm-local`.
- If `new_image` is omitted, updater resolves latest `main-<sha>` image for the target container.

**Response (Success):**
```json
{
  "success": true,
  "message": "Container hmm-local updated to ghcr.io/renegadeuk/hmm-local:main-abc123",
  "container_id": "dab07393e0ce...",
  "timestamp": "2026-02-06T19:32:15.123456"
}
```

**Response (Error):**
```json
{
  "success": false,
  "error": "Container 'hmm-local' not found"
}
```

## Update Flow

1. **HMM-Local** calls updater API with target image
2. **Updater** gets current container configuration
3. **Updater** stops old container (disconnects IP)
4. **Updater** removes old container
5. **Updater** pulls new image
6. **Updater** creates new container with same config (IP now available)
7. **Updater** returns success

## Docker Compose Deployment

```yaml
services:
  updater:
    image: ghcr.io/renegadeuk/hmm-local-updater:latest
    container_name: hmm-local-updater
    ports:
      - "8081:8081"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8081/health"]
      interval: 30s
      timeout: 5s
      retries: 3

  hmm-local:
    image: ghcr.io/renegadeuk/hmm-local:latest
    container_name: hmm-local
    environment:
      - UPDATER_URL=http://updater:8081
    depends_on:
      - updater
    # ... rest of config
```

## Production Deployment (Static IP)

```bash
# 1. Start updater
docker run -d \
  --name hmm-local-updater \
  --net=br0 --ip=10.200.204.23 \
  -p 8081:8081 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --restart unless-stopped \
  ghcr.io/renegadeuk/hmm-local-updater:latest

# 2. Start HMM-Local
docker run -d \
  --name hmm-local \
  --net=br0 --ip=10.200.204.22 \
  -v /data/hmm-local:/config \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e WEB_PORT=8080 -e TZ=Europe/London \
  -e UPDATER_URL=http://10.200.204.23:8081 \
  --restart unless-stopped \
  ghcr.io/renegadeuk/hmm-local:latest
```

## Dependencies

- **FastAPI** - HTTP API + Web UI
- **Uvicorn** - ASGI server
- **websockets** - WebSocket log streaming
- **httpx** - GitHub API client for resolving `main-<sha>` tags

## Logs

All operations logged to stdout:
```
2026-02-06 19:31:27 - INFO - ✅ Connected to Docker daemon
2026-02-06 19:31:27 - INFO - 🚀 Starting HMM-Local Updater Service on port 8081
2026-02-06 19:32:15 - INFO - 📦 Update request: hmm-local → ghcr.io/renegadeuk/hmm-local:main-abc123
2026-02-06 19:32:15 - INFO - 🔍 Getting configuration for hmm-local
2026-02-06 19:32:16 - INFO - ⏹️  Stopping hmm-local
2026-02-06 19:32:20 - INFO - 🗑️  Removing hmm-local
2026-02-06 19:32:21 - INFO - 📥 Pulling image: ghcr.io/renegadeuk/hmm-local:main-abc123
2026-02-06 19:32:35 - INFO - 🚀 Starting new container with ghcr.io/renegadeuk/hmm-local:main-abc123
2026-02-06 19:32:36 - INFO - ✅ Update completed successfully
```

## Security

- **Requires Docker socket access** - Mount `/var/run/docker.sock`
- **No authentication** - Should run on trusted network
- **Container access only** - Cannot execute arbitrary commands
- **Single purpose** - Only handles container recreation

## Troubleshooting

**Updater keeps restarting:**
```bash
docker logs hmm-local-updater
# Check for Docker socket mount issues
```

**Update fails with "Container not found":**
- Verify container name matches exactly
- Check container is running before update

**Static IP conflict:**
- This is exactly what the updater solves!
- Old container is fully removed before new one starts

**Updater not responding:**
```bash
curl http://localhost:8081/health
# Should return: {"status": "healthy", ...}
```

## Development

Build locally:
```bash
cd updater/
docker build -t hmm-local-updater:dev .
docker run -d -p 8081:8081 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  hmm-local-updater:dev
```

Test update:
```bash
curl -X POST http://localhost:8081/update \
  -H "Content-Type: application/json" \
  -d '{
    "container_name": "test-container",
    "new_image": "nginx:alpine"
  }'
```
