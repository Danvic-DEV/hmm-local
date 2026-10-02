#!/bin/bash
set -e

# Trap SIGTERM and SIGINT to gracefully shutdown PostgreSQL
shutdown() {
    echo "🛑 Shutting down gracefully..."
    echo "   Stopping application..."
    kill -TERM "$APP_PID" 2>/dev/null || true
    wait "$APP_PID" 2>/dev/null || true
    
    echo "   Stopping PostgreSQL..."
    su - postgres -c "/usr/lib/postgresql/*/bin/pg_ctl -D $PGDATA stop -m fast" || true
    echo "✅ Shutdown complete"
    exit 0
}

trap shutdown SIGTERM SIGINT

# PostgreSQL setup
PGDATA="/config/postgres"
PG_USER="hmm_user"
PG_DB="hmm"
PG_PASSWORD="${POSTGRES_PASSWORD:-hmm_secure_password}"

echo "🐘 Setting up PostgreSQL..."

# Initialize PostgreSQL data directory if it doesn't exist
if [ ! -f "$PGDATA/PG_VERSION" ]; then
    echo "📁 Initializing PostgreSQL data directory..."
    
    # Clean any partial/incompatible data
    rm -rf "$PGDATA"/*
    mkdir -p "$PGDATA"
    chown -R postgres:postgres "$PGDATA"
    
    su - postgres -c "/usr/lib/postgresql/*/bin/initdb -D $PGDATA"
    
    # Configure PostgreSQL
    echo "⚙️ Configuring PostgreSQL..."
    cat >> "$PGDATA/postgresql.conf" <<EOF
listen_addresses = 'localhost'
port = 5432
max_connections = 100
shared_buffers = 128MB
EOF
    
    # Start PostgreSQL temporarily to create user and database
    su - postgres -c "/usr/lib/postgresql/*/bin/pg_ctl -D $PGDATA -l /config/postgres/logfile -w -t 60 start"
    sleep 3
    
    # Create user and database
    su - postgres -c "psql -c \"CREATE USER $PG_USER WITH PASSWORD '$PG_PASSWORD';\""
    su - postgres -c "psql -c \"CREATE DATABASE $PG_DB OWNER $PG_USER;\""
    su - postgres -c "psql -c \"GRANT ALL PRIVILEGES ON DATABASE $PG_DB TO $PG_USER;\""
    
    # Stop PostgreSQL
    su - postgres -c "/usr/lib/postgresql/*/bin/pg_ctl -D $PGDATA stop"
    sleep 2
    
    echo "✅ PostgreSQL initialized"
else
    echo "✅ PostgreSQL data directory exists"
fi

# Start PostgreSQL
echo "🚀 Starting PostgreSQL..."
su - postgres -c "/usr/lib/postgresql/*/bin/pg_ctl -D $PGDATA -l /config/postgres/logfile -w -t 300 start"
sleep 5  # Brief pause after startup

# Wait for PostgreSQL to be ready
echo "⏳ Waiting for PostgreSQL to be ready..."
for i in {1..30}; do
    if su - postgres -c "psql -U $PG_USER -d $PG_DB -c 'SELECT 1' >/dev/null 2>&1"; then
        echo "✅ PostgreSQL is ready"
        break
    fi
    echo "   Attempt $i/30..."
    sleep 1
done

# Deploy bundled pool drivers on first run or if directory is empty
mkdir -p /config/drivers
POOL_DRIVER_COUNT=$(find /config/drivers -maxdepth 1 -name "*_driver.py" | wc -l)
if [ "$POOL_DRIVER_COUNT" -eq 0 ]; then
    echo "📦 Deploying bundled pool drivers..."
    cp /app/bundled_config/drivers/pools/*_driver.py /config/drivers/
    echo "✅ Drivers deployed to /config/drivers"
fi

# Deploy bundled miner drivers on first run or if directory is empty
mkdir -p /config/drivers/miners
MINER_DRIVER_COUNT=$(find /config/drivers/miners -maxdepth 1 -name "*_driver.py" | wc -l)
if [ "$MINER_DRIVER_COUNT" -eq 0 ]; then
    echo "📦 Deploying bundled miner drivers..."
    cp /app/bundled_config/drivers/miners/*.py /config/drivers/miners/
    cp /app/bundled_config/drivers/miners/*.md /config/drivers/miners/ 2>/dev/null || true
    echo "✅ Miner drivers deployed to /config/drivers/miners"
fi

# Deploy bundled energy providers on first run or if directory is empty
mkdir -p /config/providers/energy
ENERGY_PROVIDER_COUNT=$(find /config/providers/energy -maxdepth 1 -name "*_provider.py" | wc -l)
if [ "$ENERGY_PROVIDER_COUNT" -eq 0 ]; then
    echo "📦 Deploying bundled energy providers..."
    cp /app/bundled_config/providers/energy/*_provider.py /config/providers/energy/
    cp /app/bundled_config/providers/energy/*.md /config/providers/energy/ 2>/dev/null || true
    echo "✅ Energy providers deployed to /config/providers/energy"
fi

# Deploy bundled strategy plugins on first run or if directory is empty
mkdir -p /config/strategies
STRATEGY_COUNT=$(find /config/strategies -maxdepth 1 -name "*_strategy.py" | wc -l)
if [ "$STRATEGY_COUNT" -eq 0 ]; then
    echo "📦 Deploying bundled strategy plugins..."
    cp /app/bundled_config/strategies/*_strategy.py /config/strategies/
    cp /app/bundled_config/strategies/*.md /config/strategies/ 2>/dev/null || true
    echo "✅ Strategy plugins deployed to /config/strategies"
fi

mkdir -p /config/pools
POOL_EXAMPLE_COUNT=$(find /config/pools -maxdepth 1 -type f | wc -l)
if [ "$POOL_EXAMPLE_COUNT" -eq 0 ]; then
    echo "📦 Deploying example pool configurations..."
    cp -r /app/bundled_config/pools/. /config/pools/
    echo "✅ Example pool configs deployed to /config/pools"
    echo "ℹ️  Rename .yaml.example files to .yaml to activate pools"
fi

# Start the main application
echo "🚀 Starting Home Miner Manager..."
uvicorn main:app --host 0.0.0.0 --port ${WEB_PORT} &
APP_PID=$!

# Wait for application (allows trap to catch signals)
wait "$APP_PID"
