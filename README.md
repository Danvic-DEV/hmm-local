# Home Miner Manager v1.0.0

**The complete mining management platform built for profitability.** Intelligent energy optimization, automated solo mining strategies with champion mode, and comprehensive miner management—all in one powerful dashboard.

![Docker](https://img.shields.io/badge/Docker-20.10+-2496ED?logo=docker&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16+-336791?logo=postgresql&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-yellow)
![WCAG](https://img.shields.io/badge/WCAG-AA-green)
![PWA](https://img.shields.io/badge/PWA-Ready-5A0FC8?logo=pwa&logoColor=white)

---

## Why Home Miner Manager?

Mining profitably at home requires more than just hardware—it requires intelligence. Home Miner Manager automatically optimizes when and what you mine based on real-time energy prices and coin profitability.

### 🎯 The Price Band Strategy

**Mine the right coin at the right time:**
- ⚡ **Fully Configurable Bands** - 6 price bands, each fully customizable (coin + modes)
- 💰 **Dynamic Coin Switching** - Auto-switch between OFF/DGB/BC2/BCH/BTC/BTC_POOLED
- 🎛️ **Per-Band Modes** - Configure eco/standard/turbo/oc for each price tier
- 🔄 **Hysteresis Prevention** - Look-ahead logic prevents rapid oscillation between bands
- 🏆 **Champion Mode** - NEW: During expensive periods, only the most efficient miner runs
- 📊 **Band Analytics** - Track band transitions, time in each band, and profitability
- 🏠 **Provider Plugins** - Works with plugin-based energy providers, with automatic price updates
- 💡 **Example Strategy**: OFF above 20p → DGB eco 12-20p → DGB std 7-12p → BCH OC 4-7p → BTC OC below 4p

![Price Band Strategy](screenshots/price-band-strategy.png)

### 🏆 Champion Mode (NEW)

**Maximize efficiency during expensive electricity:**
- 📊 **Efficiency Ranking** - Calculates W/TH (watts per terahash) for all miners
- 👑 **Champion Selection** - Most efficient miner runs in lowest mode during Band 5 (20-30p/kWh)
- 🔌 **Home Assistant Integration** - Automatically turns OFF all other miners
- 🔄 **Auto-Promotion** - If champion fails, next best miner takes over
- 📈 **Profitability** - Keep mining during expensive periods with minimal energy waste
- 🔔 **Notifications** - Get alerts when champion is selected or promoted

### ⚡ Intelligent Energy Management

**Stop wasting money on expensive electricity:**
- 🔋 **Real-time Pricing** - Half-hourly Octopus Agile tariff integration
- 📈 **24-Hour Forecast** - See upcoming prices with visual sparkline charts
- 💰 **Automatic Optimization** - Mine during cheap slots, idle during expensive ones
- ⚙️ **Manual Override** - Force enable/disable independent of price bands
- 📊 **Cost Tracking** - Total energy consumption with per-miner breakdowns
- 💡 **Typical Result**: 20-40% reduction in electricity costs vs always-on mining

![Energy Dashboard](screenshots/energy-dashboard.png)

---

## 🚀 Quick Start

**Runs on anything—Raspberry Pi, spare laptop, NAS, or dedicated server.**

### PostgreSQL (Recommended for Production)

```bash
git clone https://github.com/RenegadeUK/hmm-local.git
cd hmm-local
docker-compose up -d
```

The compose file includes both PostgreSQL and the HMM container. Access at `http://localhost:8080`

> **💡 Platform Updates:** docker-compose includes `hmm-local-updater` companion service that safely handles platform updates via the Settings → Platform Updates page. The updater ensures zero-downtime updates by managing container recreation.

### SQLite (Development/Fallback)

HMM automatically falls back to SQLite if PostgreSQL is unavailable. Perfect for quick testing or resource-constrained environments.

```bash
docker run -d \
  -p 8080:8080 \
  -v ./config:/config \
  -e WEB_PORT=8080 \
  ghcr.io/renegadeuk/hmm-local:main
```

### CI/CD Build Targets

GitHub Actions builds images independently by component:

- `hmm-local` builds only when main app paths change (`app/**`, `ui-react/**`, root runtime files).
- `hmm-local-updater` builds only when `updater/**` changes.

This behavior applies to branch, PR, and tag workflows in `.github/workflows/docker-publish.yml`, so unrelated commits do not rebuild other images.

---

## 📋 Table of Contents

- [Features](#-features)
- [Supported Hardware](#-supported-hardware)
- [Dashboard](#️-dashboard)
- [CPU Dashboard](#-cpu-dashboard)
- [Platform Updates](#-platform-updates)
- [Installation](#-installation)
- [Configuration](#-configuration)
- [Price Band Strategy Setup](#-price-band-strategy-setup)
- [Pool Management](#-pool-management)
- [Notifications](#-notifications)
- [API Documentation](#-api-documentation)
- [Development](#-development)
- [Troubleshooting](#-troubleshooting)
- [License](#-license)

---

## ✨ Features

### 🎯 Configurable Price Band Strategy

The crown jewel—fully database-driven band configuration with champion mode:

- **6 Fully Customizable Bands** - Each band defines a price range with custom coin and modes
- **Champion Mode** - During Band 5 (20-30p/kWh), only the most efficient miner runs:
  - Calculates W/TH (watts per terahash) efficiency from last 6 hours
  - Selects most efficient miner as "champion"
  - Champion runs in lowest mode (eco/low)
  - All other miners turned OFF via Home Assistant
  - Auto-promotes next best miner on failure (3 consecutive)
- **Per-Band Configuration**:
  - Target coin: OFF, DGB, BCH, or BTC
  - Bitaxe/NerdQaxe mode: eco, standard, turbo, overclock
  - Avalon Nano mode: low, medium, high
- **Hysteresis Logic** - Look-ahead confirmation prevents rapid band switching
- **Visual Band Editor** - Dropdowns for each slot, live preview of current band
- **Reset to Defaults** - One-click restore of proven profitable strategy
- **Audit Trail** - Every band change, transition, and override logged with timestamps
- **REST API** - Full CRUD operations for programmatic control

![Band Configuration](screenshots/band-editor.png)

### 📊 Pool Analytics & Monitoring

**Native integrations with detailed statistics:**

- **Solopool (BCH/DGB/BTC)**:
  - Workers online with hashrate sparklines
  - 24-hour hashrate charts
  - Best share tracking
  - Immature/unpaid balance monitoring

![Pool Dashboard](screenshots/pool-analytics.png)

### 🔧 Hardware Management

**Comprehensive multi-brand support:**

- **Avalon Nano 3/3S** - cgminer API with pool slot management
- **Bitaxe (All Models)** - Full REST API integration with frequency tuning
- **NerdQaxe++** - REST API with mode presets
- **NMMiner ESP32** - UDP telemetry + configuration

**Features:**
- 🔍 **Network Auto-Discovery** - Scan and auto-add compatible miners
- 🎛️ **Bulk Operations** - Enable/disable, restart, change modes across multiple miners
- 📦 **Firmware Tracking** - Display and track firmware versions
- 🔧 **Overclocking Profiles** - Save/load/apply custom tuning presets
- 📊 **Real-time Telemetry** - Hashrate, temperature, power, fan speed, chip stats

![Miner Management](screenshots/miner-dashboard.png)
### ⚙️ Dashboard

**Modern React UI with real-time updates:**

- **Drag-and-Drop Widgets** - Fully customizable dashboard with GridStack
- **Multiple Dashboard Support** - Create dashboards for different views (ASIC/All/Custom)
- **12 Widget Types**:
  - Miner status cards (Avalon, Bitaxe, NerdQaxe, NMMiner)
  - Energy pricing and forecast
  - Pool statistics (Braiins, Solopool)
  - System health and notifications
  - Analytics charts
- **Real-time Updates** - WebSocket integration for live data
- **Dark/Light Theme** - Persistent theme preference with smooth transitions
- **Progressive Web App** - Install as native app on mobile/desktop
- **Responsive Design** - Works on any screen size
- **Price Band Strategy Integration** - See current band, champion status, and target modes

![Dashboard](screenshots/asic-dashboard.png)

###  Notifications & Alerts

**Stay informed without being overwhelmed:**

- **Telegram Bot** - Rich messages with inline buttons
- **Discord Webhooks** - Embeds with color-coded severity
- **Configurable Alerts**:
  - Miner offline/online
  - Temperature warnings
  - High reject rates
  - Energy price thresholds
  - Block discoveries
  - Band transitions
- **Rate Limiting** - Prevent notification spam
- **Custom Templates** - Markdown support for formatted messages

![Notifications](screenshots/notifications.png)

### � Platform Updates

**One-click self-updates via companion updater service:**

- 🔍 **GitHub Monitoring** - Auto-detect new releases from GHCR
- 📊 **Version Comparison** - See current vs latest with commit count
- 📋 **Changelog View** - Browse commit history with authors and dates
- 🚀 **Safe Updates** - Updater sidecar handles container recreation without downtime
- ⚙️ **Config Preservation** - All volumes, network settings, and env vars maintained
- 🔔 **Update Notifications** - Alert when new versions available
- 📝 **Audit Trail** - Track all updates with timestamps

**How it works:** The `hmm-local-updater` companion container manages the update process, solving the self-update paradox by surviving the main container restart. See [updater/README.md](updater/README.md) for architecture details.

![Platform Updates](screenshots/platform-updates.png)

### �🔐 Security & Auiting

**Enterprise-grade logging and access control:**

- 📝 **Full Audit Trail** - Every configuration change logged with user, timestamp, before/after
- 🔍 **Searchable Logs** - Filter by action type, user, date range, entity
- 🔒 **API Authentication** - Token-based security (optional for local installs)
- 🛡️ **Rate Limiting** - Prevent abuse and DOS attacks
- 📊 **Activity Monitoring** - Track API usage and system health

---

## 🖥️ Supported Hardware

| Hardware | API Type | Supported Features |
|----------|----------|-------------------|
| **Avalon Nano 3/3S** | cgminer TCP (4028) | Pool switching, mode control (WORKMODE detection), telemetry, multi-slot management |
| **Bitaxe (All)** | REST API | Full control, frequency tuning, voltage adjustment, mode presets (eco/standard/turbo/oc) |
| **NerdQaxe++** | REST API | Mode control (eco/standard/turbo/oc), telemetry, configuration |
| **NMMiner ESP32** | UDP (12345/12347) | Telemetry, configuration, lottery mining tracking |

**System Requirements:**
- Docker 20.10+ and Docker Compose
- PostgreSQL 16+ (included in docker-compose) or SQLite fallback
- 512 MB RAM minimum (runs great on Raspberry Pi)
- Any x86_64 or ARM64 system

**Home Assistant Integration:**
- Miner power control via switch entities
- Entity ID format: `switch.miner_{name_lowercase}`
- Keepalive system prevents state drift
- Required for champion mode automatic shutoff

---

## 📦 Installation

### Docker Compose (Recommended)

1. **Clone the repository:**
```bash
git clone https://github.com/RenegadeUK/hmm-local.git
cd hmm-local
```

2. **Configure environment (optional):**
```bash
# Edit docker-compose.yml if needed
# Default settings work for most users
# PostgreSQL included automatically
```

3. **Start the platform:**
```bash
docker-compose up -d
```

4. **Access the dashboard:**
```
http://localhost:8080
```

5. **Check logs:**
```bash
docker logs -f HMM-Local
```

Look for:
```
✓ Added {column_name} column to {table}
🐘 Using PostgreSQL: hmm@postgres:5432/hmm
```

### Manual Installation (Advanced)

```bash
# Install dependencies
pip install -r requirements.txt

# Configure database (PostgreSQL or SQLite)
export DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/hmm"

# Run the application
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### GitHub Container Registry

Pre-built images available:

```bash
# Latest stable
docker pull ghcr.io/renegadeuk/hmm-local:main

# Specific commit
docker pull ghcr.io/renegadeuk/hmm-local:main-{sha}

# Escape hatch (self-hosted builds)
docker pull ghcr.io/renegadeuk/hmm-local:escape-{sha}
```

---

## ⚙️ Configuration

### Initial Setup

1. **Set Your Octopus Agile Region**
   - Navigate to **Settings → Energy**
   - Select your region (A-P for UK postcode areas)
   - Prices sync automatically every 4 hours

2. **Add Your First Miner**
   - Use **Auto-Discovery**: Settings → Discovery → Scan Network
   - Or manually: Miners → Add Miner
   - Supported types: Avalon Nano, Bitaxe, NerdQaxe, NMMiner

3. **Configure Pools**
   - Pools → Add Pool
   - Enter pool URL, port, wallet address
   - Assign to miners

4. **Enable Champion Mode (Optional)**
  - Navigate to **Price Band Strategy**
   - Enable "Champion Mode"
   - Configure Home Assistant integration for automatic power control

### Configuration Files

**Main Configuration:** `config/config.yaml`
```yaml
app:
  host: "0.0.0.0"
  port: 8080
  
database:
  type: "postgresql"  # or "sqlite"
  host: "hmm-pg"
  port: 5432
  name: "hmm"
  user: "hmm"
  
energy:
  provider: "octopus_agile"
  default_region: "B"
  update_interval_hours: 4
  
notifications:
  telegram:
    enabled: false
    bot_token: "YOUR_BOT_TOKEN"
    chat_id: "YOUR_CHAT_ID"
  discord:
    enabled: false
    webhook_url: "YOUR_WEBHOOK_URL"
    
homeassistant:
  enabled: false
  base_url: "http://homeassistant:8123"
  token: "YOUR_LONG_LIVED_TOKEN"
```

**Environment Variables:** `docker-compose.yml`
```yaml
environment:
  - WEB_PORT=8080
  - TZ=Europe/London
  - PUID=1000
  - PGID=1000
```

**DO NOT add more environment variables.** All configuration should be in config.yaml.

---

## 🎯 Price Band Strategy Setup

The Price Band Strategy is the core intelligence of the platform. Here's how to configure it:

### Understanding Bands

The strategy uses **6 fully customizable bands**, each defining:
- **Price Range** - Min/max energy price in pence per kWh
- **Target Coin** - OFF, DGB, BC2, BCH, BTC, or BTC_POOLED (fully customizable)
- **Miner Modes** - eco/standard/turbo/oc per miner type (fully customizable)

### Default Band Configuration

Out of the box, **all 6 bands default to OFF** with `managed_externally` modes. This allows you to configure your own strategy:

| Band | Price Range | Default Coin | Default Modes | Suggested Use |
|------|-------------|--------------|---------------|---------------|
| 1 | ≤0p | OFF | managed_externally | Negative pricing (rare) |
| 2 | 0-5p | OFF | managed_externally | Very cheap - customize for max mining |
| 3 | 5-10p | OFF | managed_externally | Cheap - good for standard mining |
| 4 | 10-20p | OFF | managed_externally | Moderate - eco modes recommended |
| 5 | 20-30p | OFF | managed_externally | Expensive - champion mode ideal |
| 6 | 30p+ | OFF | managed_externally | Very expensive - stay off |

**Champion Mode:** Can be enabled on any band (commonly Band 5: 20-30p):
- Calculates W/TH efficiency for all miners from last 6 hours
- Selects the most efficient miner as champion
- Champion runs in lowest mode (eco/low)
- All other miners turn OFF via Home Assistant
- Auto-promotes next best miner if champion fails 3 times

**All coins and modes are fully customizable per band.**

### Customizing Bands

1. Navigate to **Strategy → Band Configuration**
2. Click dropdown for coin or mode in any band
3. Select new value
4. Changes save automatically and apply at next 30-minute boundary
5. Monitor transitions in **Strategy → Audit Log**

![Band Configuration Editor](screenshots/band-config.png)

### Strategy Behavior

**Automatic Execution:**
- Runs every 1 minute via APScheduler
- Evaluates current energy price
- Determines target band
- Applies hysteresis (look-ahead confirmation)
- Switches coin and modes if needed
- Handles champion mode selection/promotion
- Logs transition with reason

**Champion Mode Logic:**
When Band 5 active AND champion mode enabled:
1. Query last 6 hours of telemetry for all enabled miners
2. Calculate average W/TH efficiency: `avg_power / (avg_hashrate_th * 1000)`
3. Sort miners by efficiency (lower = better)
4. Select most efficient as champion
5. Champion runs in lowest mode (eco/low)
6. All others turned OFF via Home Assistant `switch.miner_{name}`
7. Champion stays until band exit
8. If champion fails 3 consecutive times, promote next best
9. Send notification on promotion

**Hysteresis Logic:**
When upgrading to a cheaper band, the system checks if the *next* 30-minute slot also qualifies. This prevents rapid switching if a single cheap slot is followed by expensive slots.

**Manual Override:**
- **Enable Strategy** - Force enable regardless of price
- **Disable Strategy** - Force disable regardless of price  
- Override state persists until manually changed or strategy re-enabled

**Reconciliation:**
- Runs every 5 minutes
- Checks if HA device states match database
- Sends corrective commands if drift detected
- Ensures system stays in sync

### Monitoring Strategy Performance

- **Current Band** - Operations page shows active band number and coin
- **Champion Status** - Purple badge shows current champion miner
- **Band Transitions** - Audit log tracks every band change with timestamp and reason
- **Time in Band** - Analytics show duration in each band over time
- **Profitability** - Track earnings per band to optimize configuration

---

## 🏊 Pool Management

### Supported Pools

**Solo Mining Pools:**
- **Solopool** - BCH, DGB, BTC solo mining with statistics API
- **Braiins Pool** - Bitcoin FPPS mining with detailed stats

**Public Pools:**
- Any stratum pool compatible with cgminer

### Adding a Pool

```json
{
  "name": "Solopool DGB",
  "url": "stratum+tcp://eu1.solopool.org",
  "port": 3014,
  "wallet": "your_dgb_address",
  "password": "x",
  "coin": "DGB"
}
```

### Pool Health Monitoring

The platform monitors:
- ✅ Connection status (active/inactive)
- 📊 Share acceptance rate
- ⏱️ Last share timestamp
- 🎯 Best share found
- 💰 Balance (for solo pools with API)

### Pool Analytics

**Solopool Integration:**
- Workers online count with sparkline charts
- 24-hour hashrate graph
- Best share tracking
- Immature/unpaid balance
- Auto-refresh every 5 minutes

![Pool Analytics](screenshots/pool-health.png)

---

## 🔔 Notifications

### Telegram Setup

1. Create bot with [@BotFather](https://t.me/botfather)
2. Get bot token
3. Send message to bot
4. Get chat ID from `https://api.telegram.org/bot<TOKEN>/getUpdates`
5. Add to `config.yaml`:

```yaml
notifications:
  telegram:
    enabled: true
    bot_token: "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    chat_id: "123456789"
```

### Discord Setup

1. Create webhook in Discord server settings
2. Copy webhook URL
3. Add to `config.yaml`:

```yaml
notifications:
  discord:
    enabled: true
    webhook_url: "https://discord.com/api/webhooks/..."
```

### Notification Types

- 🔴 **Critical**: Miner offline, temperature danger
- ⚠️ **Warning**: High reject rate, temperature warning
- ℹ️ **Info**: Miner online, band transition
- ✅ **Success**: Block found, optimal price detected

### Rate Limiting

Notifications are rate-limited to prevent spam:
- Same event: 5-minute cooldown
- Temperature warnings: 15-minute cooldown
- Status changes: Immediate (online→offline→online)

---

## 📚 API Documentation

Full REST API with OpenAPI/Swagger documentation at `/docs`

### Key Endpoints

**Price Band Strategy:**
```bash
# Get strategy status
GET /api/settings/price-band-strategy

# Execute strategy manually
POST /api/settings/price-band-strategy/execute

# Enable/disable strategy
POST /api/settings/price-band-strategy/enable
POST /api/settings/price-band-strategy/disable

# Get all bands
GET /api/settings/price-band-strategy/bands

# Update a band
PATCH /api/settings/price-band-strategy/bands/{band_id}
{
  "target_coin": "BCH",
  "bitaxe_mode": "overclock",
  "avalon_nano_mode": "high"
}

# Reset bands to defaults
POST /api/settings/price-band-strategy/bands/reset
```

**Miners:**
```bash
# List all miners
GET /api/miners

# Get miner details
GET /api/miners/{miner_id}

# Update miner
PATCH /api/miners/{miner_id}

# Bulk operations
POST /api/miners/bulk/enable
POST /api/miners/bulk/disable
POST /api/miners/bulk/restart
```

**Energy:**
```bash
# Get current price
GET /api/energy/current-price

# Get price forecast
GET /api/energy/forecast

# Update prices
POST /api/energy/update-prices
```

### Authentication

API authentication is optional and disabled by default for local installations.

To enable:
```bash
# In .env
API_AUTH_ENABLED=true
API_KEY=your-secure-random-key-here
```

Include token in requests:
```bash
curl -H "Authorization: Bearer your-api-key" http://localhost:8080/api/miners
```

---

## 🛠️ Development

### Tech Stack

- **Backend**: FastAPI 0.115+ with async SQLAlchemy 2.0
- **Database**: PostgreSQL 16 (primary), SQLite 3.x (fallback)
- **Frontend**: React 18 + TypeScript + Vite 5.x
- **Styling**: TailwindCSS 3.x + shadcn/ui components
- **Scheduler**: APScheduler 3.x for background jobs
- **WebSocket**: Real-time updates via FastAPI WebSocket

### Running Locally

```bash
# Clone repository
git clone https://github.com/RenegadeUK/hmm-local.git
cd hmm-local

# Backend development
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080

# Frontend development (separate terminal)
cd ui-react
npm install
npm run dev  # Vite dev server on port 5173

# Build React for production
npm run build  # Output: app/ui/static/app/
```

### Project Structure

```
hmm-local/
├── app/
│   ├── main.py              # FastAPI application entry
│   ├── adapters/            # Hardware adapters
│   │   ├── avalon_nano.py   # cgminer API
│   │   ├── bitaxe.py        # REST API
│   │   ├── nerdqaxe.py      # REST API
│   │   └── nmminer.py       # UDP telemetry
│   ├── api/                 # REST API endpoints
│   │   ├── price_band_strategy.py
│   │   ├── miners.py
│   │   ├── pools.py
│   │   ├── operations.py
│   │   └── analytics.py
│   ├── core/                # Business logic
│   │   ├── price_band_strategy.py  # Main strategy
│   │   ├── price_band_bands.py     # Band config helpers
│   │   ├── database.py      # SQLAlchemy models
│   │   ├── migrations.py    # Database migrations
│   │   ├── scheduler.py     # APScheduler jobs
│   │   └── pool_slots.py    # Avalon pool management
│   ├── integrations/        # External APIs
│   │   ├── braiins.py
│   │   ├── octopus_agile.py
│   │   └── solopool.py
│   └── ui/                  # Frontend
│       ├── static/app/      # React build output
│       └── ui_routes.py     # Serve React SPA
├── ui-react/                # React source
│   ├── src/
│   │   ├── components/      # Reusable components
│   │   ├── pages/           # Page components
│   │   └── services/        # API clients
│   ├── package.json
│   └── vite.config.ts
├── config/
│   ├── config.yaml          # Main configuration
│   ├── data.db              # SQLite (if used)
│   └── logs/                # Application logs
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

### Database Migrations

Migrations run automatically on startup. To add a new migration:

```python
# In app/core/migrations.py

# Migration XX: Description (Date)
async with engine.begin() as conn:
    try:
        await conn.execute(text("""
            ALTER TABLE foo ADD COLUMN IF NOT EXISTS bar VARCHAR(100)
        """))
        print("✓ Added bar column to foo")
        core_migrations_ran = True  # If core model changed
    except Exception as e:
        print(f"⚠ Migration XX skipped: {e}")
```

**PostgreSQL Compatibility:**
- Use `BOOLEAN DEFAULT FALSE` not `DEFAULT 0`
- Use `IF NOT EXISTS` for idempotency
- Test both PostgreSQL and SQLite
- Core model changes require container restart

### Running Tests

```bash
# Run test suite
pytest

# Run specific test file
pytest tests/test_price_band_strategy.py

# Run with coverage
pytest --cov=app tests/
```

### Contributing

1. Fork the repository
2. Create feature branch (`git checkout -b feature/amazing-feature`)
3. **CRITICAL**: Follow agent behavior rules in `.github/copilot-instructions.md`
4. Test changes thoroughly (both PostgreSQL and SQLite)
5. Build React UI (`cd ui-react && npm run build`)
6. Commit changes (`git commit -m 'feat: Add amazing feature'`)
7. Push to branch (`git push origin feature/amazing-feature`)
8. Open Pull Request

**Development Guidelines:**
- Always search existing code before adding new functionality
- Never make changes without understanding the full context
- Implement automatic recovery mechanisms for all failures
- Write clear, modular Python with type hints
- Use async/await patterns consistently
- Test migrations on both database types

---

## 🐛 Troubleshooting

### Container Won't Start After Update

**Problem:** Container starts but shows 500 errors

**Solution:** Core model migration completed, restart required:
```bash
docker-compose restart
# or
docker restart HMM-Local
```

Look for this in logs:
```
================================================================================
⚠️  CORE MODEL MIGRATIONS COMPLETED
⚠️  Container restart required for schema changes to take effect
================================================================================
```

### Champion Mode Not Working

**Problem:** Champion mode enabled but all miners still running

**Solutions:**
- Verify price is in Band 5 range (check Operations page)
- Check Home Assistant integration enabled in config.yaml
- Verify HA switch entities exist: `switch.miner_{name_lowercase}`
- Check logs for "Champion selection" or "HA device control" errors
- Ensure miners have telemetry data (last 6 hours required)

### Database Migration Errors

**Problem:** PostgreSQL migration fails with syntax errors

**Solutions:**
```bash
# Check PostgreSQL connection
docker exec hmm-pg psql -U hmm -d hmm -c '\dt'

# View migration logs
docker logs HMM-Local 2>&1 | grep -E '(Migration|✓|⚠)'

# Verify schema
docker exec hmm-pg psql -U hmm -d hmm -c '\d agile_strategy'

# Manual migration if needed (advanced)
docker exec -i hmm-pg psql -U hmm -d hmm <<EOF
ALTER TABLE agile_strategy ADD COLUMN IF NOT EXISTS champion_mode_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE agile_strategy ADD COLUMN IF NOT EXISTS current_champion_miner_id INTEGER;
EOF
```

### Miner Not Discovered

**Problem:** Auto-discovery doesn't find miners

**Solutions:**
- Verify miner is on same network
- Check IP range includes miner's address
- Ensure firewall allows discovery
- Try manual addition with known IP

### Strategy Not Executing

**Problem:** Price Band Strategy not switching coins/modes

**Solutions:**
- Check strategy is enabled: Strategy → Status
- Verify region is set: Energy → Pricing
- Check current price is within configured bands
- Review audit log for error messages: Strategy → Audit

### High Reject Rate

**Problem:** Pool shows high reject rate (>5%)

**Solutions:**
- Check network latency to pool
- Try different pool server (geographic proximity)
- Verify miner difficulty setting
- Check for network congestion

### Temperature Warnings

**Problem:** Miner temperature exceeds safe limits

**Solutions:**
- Improve airflow around miner
- Lower overclock settings
- Use lower power mode in strategy bands
- Clean dust from heatsinks/fans

### Energy Prices Not Updating

**Problem:** Agile prices showing stale data

**Solutions:**
- Check internet connectivity
- Verify region is correct: Energy → Pricing
- Manually trigger update: Energy → Update Now
- Check logs for API errors: docker logs v0-miner-controller

### Docker Container Won't Start

**Problem:** Container fails to start or crashes

**Solutions:**
```bash
# Check logs
docker logs HMM-Local

# Check PostgreSQL
docker logs hmm-pg

# Restart PostgreSQL first
docker restart hmm-pg
sleep 5
docker restart HMM-Local

# Rebuild container
docker-compose down
docker-compose up -d --build

# Check for port conflicts
lsof -i :8080
```

### Database Errors

**Problem:** SQLite/PostgreSQL errors or corruption

**Solutions:**

**PostgreSQL:**
```bash
# Backup database
docker exec hmm-pg pg_dump -U hmm hmm > backup_$(date +%Y%m%d).sql

# Restore
docker exec -i hmm-pg psql -U hmm hmm < backup_20260202.sql

# Reset (WARNING: loses data)
docker-compose down -v  # Removes volumes
docker-compose up -d
```

**SQLite:**
```bash
# Backup
cp config/data.db config/data.db.backup

# Reset (WARNING: loses data)
rm config/data.db
docker-compose restart
```

---

## 📄 License

MIT License - see [LICENSE](LICENSE) for details.

---

## 🙏 Acknowledgments

- Octopus Energy for public Agile API
- Solopool for solo mining infrastructure
- Braiins Pool for FPPS mining
- cgminer/bfgminer developers
- FastAPI and Python community
- React and Vite maintainers
- shadcn for beautiful UI components

---

## 📧 Support

- **Issues**: [GitHub Issues](https://github.com/RenegadeUK/hmm-local/issues)
- **Discussions**: [GitHub Discussions](https://github.com/RenegadeUK/hmm-local/discussions)
- **Documentation**: `.github/copilot-instructions.md`

---

## 🚀 CI/CD

**Main Workflow:** Automated builds on push to `main`
- Runs on GitHub-hosted runners (`ubuntu-latest`)
- Publishes to `ghcr.io/renegadeuk/hmm-local:main-{sha}`

**Escape Hatch:** Manual fallback for GitHub Actions outages
- Workflow: `.github/workflows/escape-hatch.yml`
- Runs on self-hosted runner with label `hmm-builder`
- Publishes to `ghcr.io/renegadeuk/hmm-local:escape-{sha}`
- Triggered manually via workflow_dispatch
- Optional GHCR push (default: build only)

---

**Built with ❤️ for the home mining community**

**Project Maintainer:** DANVIC.dev  
**Version:** 1.0.0 (Production)  
**Last Updated:** 2 February 2026