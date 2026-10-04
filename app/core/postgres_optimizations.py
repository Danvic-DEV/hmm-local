"""
PostgreSQL-specific optimizations (partitioning, materialized views, indexes)
"""
import logging
from datetime import datetime, timedelta
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def is_postgresql(session: AsyncSession) -> bool:
    """Check if current database is PostgreSQL"""
    from core.database import engine
    return 'postgresql' in str(engine.url)


async def _column_exists(session: AsyncSession, table_name: str, column_name: str) -> bool:
    """Check whether a column exists in the public schema (PostgreSQL)."""
    result = await session.execute(
        text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = :table_name
              AND column_name = :column_name
            LIMIT 1
            """
        ),
        {"table_name": table_name, "column_name": column_name},
    )
    return result.scalar_one_or_none() is not None


def _compiled_column_type(column) -> str:
    from sqlalchemy.dialects import postgresql
    return column.type.compile(dialect=postgresql.dialect())


def _column_default_sql(column) -> str | None:
    """Mirrors scripts/generate_schema_reconcile_sql.py's _column_default -
    same logic, kept in sync so the automatic path and the manual
    review-first script agree on what's safe to apply."""
    if column.server_default is not None and getattr(column.server_default, "arg", None) is not None:
        arg = column.server_default.arg
        if hasattr(arg, "text"):
            return str(arg.text)
        return str(arg)

    if column.default is not None and getattr(column.default, "is_scalar", False):
        value = column.default.arg
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return str(value)
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"

    return None


async def sync_missing_columns(session: AsyncSession) -> None:
    """
    Add any column that exists on a SQLAlchemy model but not yet on the
    live table - e.g. a new field added to an existing model after that
    table was already created on a prior version. Runs automatically on
    every startup (see initialize_postgres_optimizations) so a plain
    `docker compose pull && up -d` upgrade just works, the same as
    anyone cloning this repo fresh gets via create_all() - no manual SQL
    step required for this class of change.

    Deliberately narrow: only ADD COLUMN IF NOT EXISTS, with the column's
    own default so existing rows backfill in the same statement - never
    drops, renames, or alters an existing column. That's the ceiling of
    what's safe to apply without a human reviewing it first; anything
    bigger still belongs in scripts/generate_schema_reconcile_sql.py for
    manual review.
    """
    if not await is_postgresql(session):
        return

    try:
        from core.database import Base

        result = await session.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        )
        existing_tables = {row[0] for row in result.fetchall()}

        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # brand-new table - create_all() in init_db() already handled it

            for column in table.columns:
                if await _column_exists(session, table.name, column.name):
                    continue

                col_type = _compiled_column_type(column)
                default_sql = _column_default_sql(column)
                stmt = f'ALTER TABLE "{table.name}" ADD COLUMN IF NOT EXISTS "{column.name}" {col_type}'
                if default_sql is not None:
                    stmt += f" DEFAULT {default_sql}"
                stmt += " NULL" if (column.nullable or default_sql is None) else " NOT NULL"

                try:
                    await session.execute(text(stmt))
                    await session.commit()
                    logger.info(f"✅ Added missing column {table.name}.{column.name}")
                except Exception as e:
                    await session.rollback()
                    logger.warning(f"Could not add column {table.name}.{column.name}: {e}")

    except Exception as e:
        logger.error(f"Error syncing missing columns: {e}")
        await session.rollback()


async def migrate_to_partitioned_telemetry(session: AsyncSession) -> None:
    """
    Migrate existing telemetry table to partitioned version.
    Steps:
    1. Rename telemetry → telemetry_old
    2. Create partitioned telemetry table
    3. Determine date range of existing data
    4. Create partitions for that range
    5. Copy data from old → new
    6. Drop old table
    """
    if not await is_postgresql(session):
        return
    
    try:
        logger.info("🔄 Migrating telemetry to partitioned table...")
        
        # Get date range of existing data
        date_range_query = text("""
            SELECT 
                DATE_TRUNC('month', MIN(timestamp)) as min_date,
                DATE_TRUNC('month', MAX(timestamp)) as max_date,
                COUNT(*) as row_count
            FROM telemetry_old
        """)
        result = await session.execute(date_range_query)
        row = result.fetchone()
        
        if not row or not row[0]:
            logger.info("No data in telemetry_old, skipping partition creation")
        else:
            min_date = row[0]
            max_date = row[1]
            row_count = row[2]
            
            logger.info(f"📊 Data range: {min_date.date()} to {max_date.date()} ({row_count:,} rows)")
            
            # Create partitions for each month in the range
            current = min_date
            partitions_created = 0
            while current <= max_date:
                partition_name = f"telemetry_{current.year}_{current.month:02d}"
                
                # Calculate partition boundaries
                start_date = current
                if current.month == 12:
                    end_date = datetime(current.year + 1, 1, 1)
                else:
                    end_date = datetime(current.year, current.month + 1, 1)
                
                # Create partition
                create_partition_query = text(f"""
                    CREATE TABLE IF NOT EXISTS {partition_name}
                    PARTITION OF telemetry
                    FOR VALUES FROM ('{start_date.isoformat()}') TO ('{end_date.isoformat()}')
                """)
                await session.execute(create_partition_query)
                logger.info(f"  ✅ Created partition: {partition_name}")
                partitions_created += 1
                
                # Move to next month
                if current.month == 12:
                    current = datetime(current.year + 1, 1, 1)
                else:
                    current = datetime(current.year, current.month + 1, 1)
            
            logger.info(f"📦 Created {partitions_created} partitions")
        
        # Copy data from old table to new partitioned table
        logger.info("📋 Copying data from telemetry_old to partitioned telemetry...")
        copy_query = text("""
            INSERT INTO telemetry 
            SELECT * FROM telemetry_old
        """)
        await session.execute(copy_query)
        await session.commit()
        
        # Verify row counts match
        old_count_query = text("SELECT COUNT(*) FROM telemetry_old")
        new_count_query = text("SELECT COUNT(*) FROM telemetry")
        
        old_count = (await session.execute(old_count_query)).scalar()
        new_count = (await session.execute(new_count_query)).scalar()
        
        if old_count == new_count:
            logger.info(f"✅ Data migrated: {new_count:,} rows verified")
            
            # Drop old table
            logger.info("🗑️  Dropping telemetry_old...")
            await session.execute(text("DROP TABLE telemetry_old"))
            await session.commit()
            logger.info("✅ Partitioning migration complete!")
        else:
            logger.error(f"❌ Row count mismatch! Old: {old_count}, New: {new_count}")
            await session.rollback()
            raise Exception("Partitioning migration failed - row count mismatch")
        
    except Exception as e:
        logger.error(f"Error migrating to partitioned table: {e}")
        await session.rollback()
        raise


async def setup_telemetry_partitioning(session: AsyncSession) -> None:
    """
    Set up monthly partitioning for telemetry table.
    Automatically migrates existing table if needed.
    """
    if not await is_postgresql(session):
        logger.info("Skipping partitioning (non-PostgreSQL database)")
        return
    
    try:
        logger.info("Setting up telemetry partitioning...")
        
        # Check if table is already partitioned
        check_query = text("""
            SELECT COUNT(*) 
            FROM pg_partitioned_table 
            WHERE partrelid = 'telemetry'::regclass
        """)
        result = await session.execute(check_query)
        is_partitioned = result.scalar() > 0
        
        if is_partitioned:
            logger.info("✅ Telemetry table already partitioned")
            return
        
        # Check if table is empty (fresh install)
        check_empty_query = text("SELECT COUNT(*) FROM telemetry")
        result = await session.execute(check_empty_query)
        row_count = result.scalar()
        
        if row_count == 0:
            logger.info("✅ Fresh install detected - telemetry table is empty, no migration needed")
            return
        
        # Check if telemetry_old exists (migration already started)
        check_old_query = text("""
            SELECT COUNT(*) 
            FROM pg_tables 
            WHERE tablename = 'telemetry_old'
        """)
        result = await session.execute(check_old_query)
        old_table_exists = result.scalar() > 0
        
        if old_table_exists:
            logger.warning("⚠️ Found telemetry_old - resuming partitioning migration")
            await migrate_to_partitioned_telemetry(session)
            return
        
        # Start fresh partitioning migration for existing data
        logger.info("🚀 Starting automatic partitioning migration...")
        
        # Step 1: Rename existing table
        logger.info("1️⃣ Renaming telemetry → telemetry_old...")
        await session.execute(text("ALTER TABLE telemetry RENAME TO telemetry_old"))
        await session.commit()
        
        # Step 2: Create partitioned table
        logger.info("2️⃣ Creating partitioned telemetry table...")
        
        # Get table structure from existing table
        create_table_query = text("""
            CREATE TABLE telemetry (
                id SERIAL,
                miner_id INTEGER NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                hashrate DOUBLE PRECISION,
                hashrate_unit VARCHAR(10),
                temperature DOUBLE PRECISION,
                power_watts DOUBLE PRECISION,
                energy_cost DOUBLE PRECISION,
                shares_accepted INTEGER,
                shares_rejected INTEGER,
                pool_difficulty DOUBLE PRECISION,
                pool_in_use VARCHAR(255),
                mode VARCHAR(20),
                data JSONB,
                PRIMARY KEY (id, timestamp)
            ) PARTITION BY RANGE (timestamp)
        """)
        await session.execute(create_table_query)
        await session.commit()
        logger.info("✅ Partitioned table created")
        
        # Step 3-6: Migrate data
        await migrate_to_partitioned_telemetry(session)
        
    except Exception as e:
        logger.error(f"Error setting up partitioning: {e}")
        await session.rollback()
        raise


async def create_monthly_partition(session: AsyncSession, year: int, month: int) -> bool:
    """
    Create a monthly partition for telemetry table.
    Returns True if partition was created, False if it already exists.
    """
    if not await is_postgresql(session):
        return False
    
    try:
        partition_name = f"telemetry_{year}_{month:02d}"
        
        # Check if partition exists
        check_query = text("""
            SELECT COUNT(*) 
            FROM pg_tables 
            WHERE tablename = :partition_name
        """)
        result = await session.execute(check_query, {"partition_name": partition_name})
        exists = result.scalar() > 0
        
        if exists:
            logger.debug(f"Partition {partition_name} already exists")
            return False
        
        # Calculate partition boundaries
        start_date = datetime(year, month, 1)
        if month == 12:
            end_date = datetime(year + 1, 1, 1)
        else:
            end_date = datetime(year, month + 1, 1)
        
        # Create partition
        create_query = text(f"""
            CREATE TABLE IF NOT EXISTS {partition_name} 
            PARTITION OF telemetry
            FOR VALUES FROM ('{start_date.isoformat()}') TO ('{end_date.isoformat()}')
        """)
        await session.execute(create_query)
        await session.commit()
        
        logger.info(f"✅ Created partition: {partition_name} ({start_date.date()} to {end_date.date()})")
        return True
        
    except Exception as e:
        logger.error(f"Error creating partition for {year}-{month:02d}: {e}")
        await session.rollback()
        return False


async def ensure_future_partitions(session: AsyncSession) -> None:
    """
    Ensure partitions exist for current month, next month, and month after.
    Called by scheduler monthly.
    """
    if not await is_postgresql(session):
        return
    
    try:
        now = datetime.utcnow()
        
        # Create partitions for current, next, and month after
        for offset in [0, 1, 2]:
            target_date = now + timedelta(days=30 * offset)
            await create_monthly_partition(session, target_date.year, target_date.month)
        
    except Exception as e:
        logger.error(f"Error ensuring future partitions: {e}")


async def create_dashboard_materialized_view(session: AsyncSession) -> None:
    """
    Create materialized view for dashboard aggregations.
    Pre-computes expensive queries for instant dashboard loading.
    """
    if not await is_postgresql(session):
        logger.info("Skipping materialized view (non-PostgreSQL database)")
        return
    
    try:
        logger.info("Creating dashboard materialized view...")
        
        # Drop if exists
        await session.execute(text("DROP MATERIALIZED VIEW IF EXISTS dashboard_stats_mv"))
        
        # Create materialized view with latest telemetry stats per miner
        create_mv_query = text("""
            CREATE MATERIALIZED VIEW dashboard_stats_mv AS
            WITH latest_telemetry AS (
                SELECT DISTINCT ON (miner_id)
                    miner_id,
                    timestamp,
                    hashrate,
                    hashrate_unit,
                    temperature,
                    power_watts,
                    shares_accepted,
                    shares_rejected,
                    mode
                FROM telemetry
                ORDER BY miner_id, timestamp DESC
            ),
            hourly_stats AS (
                SELECT 
                    miner_id,
                    COUNT(*) as telemetry_count_1h,
                    AVG(hashrate) as avg_hashrate_1h,
                    AVG(temperature) as avg_temp_1h,
                    SUM(shares_accepted) as shares_1h,
                    SUM(shares_rejected) as rejects_1h
                FROM telemetry
                WHERE timestamp >= NOW() - INTERVAL '1 hour'
                GROUP BY miner_id
            ),
            daily_stats AS (
                SELECT 
                    miner_id,
                    AVG(hashrate) as avg_hashrate_24h,
                    SUM(energy_cost) as energy_cost_24h
                FROM telemetry
                WHERE timestamp >= NOW() - INTERVAL '24 hours'
                GROUP BY miner_id
            )
            SELECT 
                m.id as miner_id,
                m.name as miner_name,
                m.miner_type,
                m.enabled,
                m.current_mode,
                lt.timestamp as last_seen,
                lt.hashrate as current_hashrate,
                lt.hashrate_unit,
                lt.temperature as current_temperature,
                lt.power_watts as current_power,
                lt.shares_accepted as total_shares,
                lt.shares_rejected as total_rejects,
                hs.avg_hashrate_1h,
                hs.avg_temp_1h,
                hs.shares_1h,
                hs.rejects_1h,
                CASE 
                    WHEN hs.shares_1h + hs.rejects_1h > 0 
                    THEN (hs.rejects_1h::float / (hs.shares_1h + hs.rejects_1h) * 100)
                    ELSE 0 
                END as reject_rate_1h,
                ds.avg_hashrate_24h,
                ds.energy_cost_24h,
                EXTRACT(EPOCH FROM (NOW() - lt.timestamp)) as seconds_since_last_telemetry
            FROM miners m
            LEFT JOIN latest_telemetry lt ON m.id = lt.miner_id
            LEFT JOIN hourly_stats hs ON m.id = hs.miner_id
            LEFT JOIN daily_stats ds ON m.id = ds.miner_id
            WHERE m.enabled = true
        """)
        
        await session.execute(create_mv_query)
        
        # Create indexes on materialized view
        # CONCURRENT refresh requires at least one unique index that covers all rows.
        await session.execute(text(
            "CREATE UNIQUE INDEX idx_dashboard_mv_miner_id_unique ON dashboard_stats_mv(miner_id)"
        ))
        await session.execute(text("CREATE INDEX idx_dashboard_mv_last_seen ON dashboard_stats_mv(last_seen)"))
        
        await session.commit()
        logger.info("✅ Created dashboard_stats_mv with indexes")
        
    except Exception as e:
        logger.error(f"Error creating materialized view: {e}")
        await session.rollback()


async def refresh_dashboard_materialized_view(session: AsyncSession) -> None:
    """
    Refresh dashboard materialized view.
    Should be called by scheduler every 5 minutes.
    """
    if not await is_postgresql(session):
        return
    
    try:
        await session.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY dashboard_stats_mv"))
        await session.commit()
        logger.debug("Refreshed dashboard_stats_mv (concurrent)")
    except Exception as e:
        await session.rollback()
        # Fallback to non-concurrent refresh so optimization still functions.
        logger.warning("Concurrent MV refresh failed, retrying non-concurrent: %s", e)
        try:
            await session.execute(text("REFRESH MATERIALIZED VIEW dashboard_stats_mv"))
            await session.commit()
            logger.debug("Refreshed dashboard_stats_mv (non-concurrent fallback)")
        except Exception as fallback_error:
            logger.error("Error refreshing materialized view: %s", fallback_error)
            await session.rollback()


async def create_json_indexes(session: AsyncSession) -> None:
    """
    Create GIN indexes on JSON columns for faster searches.
    PostgreSQL only - uses GIN (Generalized Inverted Index).
    """
    if not await is_postgresql(session):
        logger.info("Skipping JSON indexes (non-PostgreSQL database)")
        return
    
    try:
        logger.info("Creating JSON GIN indexes...")
        
        indexes = [
            # Telemetry data column (contains miner-specific extra data)
            ("CREATE INDEX IF NOT EXISTS idx_telemetry_data_gin ON telemetry USING GIN ((data::jsonb))", "telemetry.data::jsonb"),

            # Miner config column
            ("CREATE INDEX IF NOT EXISTS idx_miner_config_gin ON miners USING GIN ((config::jsonb))", "miners.config::jsonb"),

            # Health events reasons
            ("CREATE INDEX IF NOT EXISTS idx_health_events_reasons_gin ON health_events USING GIN ((reasons::jsonb))", "health_events.reasons::jsonb"),
        ]

        # Automation rules JSON columns vary by schema version.
        # Prefer current names, but support legacy names if present.
        if await _column_exists(session, "automation_rules", "trigger_config"):
            indexes.append(
                (
                    "CREATE INDEX IF NOT EXISTS idx_automation_trigger_config_gin "
                    "ON automation_rules USING GIN ((trigger_config::jsonb))",
                    "automation_rules.trigger_config::jsonb",
                )
            )
        elif await _column_exists(session, "automation_rules", "condition"):
            indexes.append(
                (
                    "CREATE INDEX IF NOT EXISTS idx_automation_condition_gin "
                    "ON automation_rules USING GIN ((condition::jsonb))",
                    "automation_rules.condition::jsonb",
                )
            )

        if await _column_exists(session, "automation_rules", "action_config"):
            indexes.append(
                (
                    "CREATE INDEX IF NOT EXISTS idx_automation_action_config_gin "
                    "ON automation_rules USING GIN ((action_config::jsonb))",
                    "automation_rules.action_config::jsonb",
                )
            )
        elif await _column_exists(session, "automation_rules", "action"):
            indexes.append(
                (
                    "CREATE INDEX IF NOT EXISTS idx_automation_action_gin "
                    "ON automation_rules USING GIN ((action::jsonb))",
                    "automation_rules.action::jsonb",
                )
            )
        
        for query, description in indexes:
            try:
                await session.execute(text(query))
                await session.commit()
                logger.info(f"✅ Created GIN index: {description}")
            except Exception as e:
                await session.rollback()
                logger.warning(f"Could not create index {description}: {e}")

        logger.info("JSON indexing complete")
        
    except Exception as e:
        logger.error(f"Error creating JSON indexes: {e}")
        await session.rollback()


async def create_partial_indexes(session: AsyncSession) -> None:
    """
    Create partial indexes for common query patterns.
    Only indexes rows that match specific conditions.
    """
    if not await is_postgresql(session):
        logger.info("Skipping partial indexes (non-PostgreSQL database)")
        return
    
    try:
        logger.info("Creating partial indexes...")
        
        indexes = [
            # Only index enabled miners (most queries filter on this)
            ("CREATE INDEX IF NOT EXISTS idx_miners_enabled ON miners(id) WHERE enabled = true", 
             "miners.enabled=true"),

            # Recent telemetry queries are served by an ordered timestamp index.
            # Partial predicates cannot use NOW() because index predicates must be IMMUTABLE.
            ("CREATE INDEX IF NOT EXISTS idx_telemetry_recent ON telemetry(miner_id, timestamp DESC)",
             "telemetry (miner_id, timestamp desc)"),
            
            # Only index active automation rules
            ("CREATE INDEX IF NOT EXISTS idx_automation_active ON automation_rules(id) WHERE enabled = true",
             "automation_rules.enabled=true"),

            # Same immutable-rule applies here; use ordered timestamp index.
            ("CREATE INDEX IF NOT EXISTS idx_pool_health_recent ON pool_health(pool_id, timestamp DESC)",
             "pool_health (pool_id, timestamp desc)"),
        ]
        
        for query, description in indexes:
            try:
                await session.execute(text(query))
                await session.commit()
                logger.info(f"✅ Created partial index: {description}")
            except Exception as e:
                await session.rollback()
                logger.warning(f"Could not create partial index {description}: {e}")

        logger.info("Partial indexing complete")
        
    except Exception as e:
        logger.error(f"Error creating partial indexes: {e}")
        await session.rollback()


async def create_covering_indexes(session: AsyncSession) -> None:
    """
    Create covering indexes (INCLUDE columns) for index-only scans.
    Avoids table lookups by including all needed columns in the index.
    """
    if not await is_postgresql(session):
        logger.info("Skipping covering indexes (non-PostgreSQL database)")
        return
    
    try:
        logger.info("Creating covering indexes...")
        
        indexes = [
            # Dashboard query: needs miner_id + timestamp + all display columns
            ("""CREATE INDEX IF NOT EXISTS idx_telemetry_dashboard 
                ON telemetry(miner_id, timestamp DESC) 
                INCLUDE (hashrate, temperature, power_watts, shares_accepted, shares_rejected, mode)""",
             "telemetry dashboard covering index"),
            
            # Miner list query: needs enabled + all display columns
            ("""CREATE INDEX IF NOT EXISTS idx_miners_list 
                ON miners(enabled) 
                INCLUDE (name, miner_type, current_mode, ip_address)""",
             "miners list covering index"),
        ]
        
        for query, description in indexes:
            try:
                await session.execute(text(query))
                logger.info(f"✅ Created covering index: {description}")
            except Exception as e:
                logger.warning(f"Could not create covering index {description}: {e}")
        
        await session.commit()
        logger.info("Covering indexes complete")
        
    except Exception as e:
        logger.error(f"Error creating covering indexes: {e}")
        await session.rollback()


async def setup_notify_triggers(session: AsyncSession) -> None:
    """
    Create PostgreSQL NOTIFY triggers for real-time updates.
    Sends notifications when telemetry/miner state changes.
    """
    if not await is_postgresql(session):
        logger.info("Skipping NOTIFY triggers (non-PostgreSQL database)")
        return
    
    try:
        logger.info("Creating NOTIFY triggers...")
        
        # Telemetry insert trigger
        await session.execute(text("""
            CREATE OR REPLACE FUNCTION notify_telemetry_change()
            RETURNS trigger AS $$
            BEGIN
                PERFORM pg_notify('telemetry_update', json_build_object(
                    'miner_id', NEW.miner_id,
                    'timestamp', NEW.timestamp,
                    'hashrate', NEW.hashrate,
                    'temperature', NEW.temperature
                )::text);
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """))
        
        await session.execute(text("""
            DROP TRIGGER IF EXISTS telemetry_notify_trigger ON telemetry
        """))
        
        await session.execute(text("""
            CREATE TRIGGER telemetry_notify_trigger
            AFTER INSERT ON telemetry
            FOR EACH ROW
            EXECUTE FUNCTION notify_telemetry_change()
        """))
        
        # Miner state change trigger
        await session.execute(text("""
            CREATE OR REPLACE FUNCTION notify_miner_change()
            RETURNS trigger AS $$
            BEGIN
                IF (TG_OP = 'UPDATE' AND (
                    OLD.enabled != NEW.enabled OR 
                    OLD.current_mode != NEW.current_mode
                )) OR TG_OP = 'INSERT' THEN
                    PERFORM pg_notify('miner_update', json_build_object(
                        'miner_id', NEW.id,
                        'name', NEW.name,
                        'enabled', NEW.enabled,
                        'current_mode', NEW.current_mode
                    )::text);
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """))
        
        await session.execute(text("""
            DROP TRIGGER IF EXISTS miner_notify_trigger ON miners
        """))
        
        await session.execute(text("""
            CREATE TRIGGER miner_notify_trigger
            AFTER INSERT OR UPDATE ON miners
            FOR EACH ROW
            EXECUTE FUNCTION notify_miner_change()
        """))
        
        await session.commit()
        logger.info("✅ Created NOTIFY triggers for telemetry and miners")
        
    except Exception as e:
        logger.error(f"Error creating NOTIFY triggers: {e}")
        await session.rollback()


async def sync_postgres_sequences(session: AsyncSession) -> None:
    """
    Sync PostgreSQL sequences to the current max(id) for all tables.
    Prevents duplicate key errors after data import or migration.
    """
    if not await is_postgresql(session):
        logger.info("Skipping sequence sync (non-PostgreSQL database)")
        return

    try:
        logger.info("Syncing PostgreSQL sequences to max IDs...")
        await session.execute(text("""
            DO $$
            DECLARE r RECORD;
            BEGIN
                FOR r IN
                    SELECT table_name, column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND column_default LIKE 'nextval%'
                LOOP
                    EXECUTE format(
                        'SELECT setval(pg_get_serial_sequence(''%I'',''%I''), COALESCE(MAX(%I), 1), true) FROM %I',
                        r.table_name, r.column_name, r.column_name, r.table_name
                    );
                END LOOP;
            END $$;
        """))
        await session.commit()
        logger.info("✅ Sequence sync complete")
    except Exception as e:
        logger.error(f"Error syncing sequences: {e}")
        await session.rollback()


async def initialize_postgres_optimizations(session: AsyncSession) -> None:
    """
    Initialize all PostgreSQL optimizations.
    Called once at startup if using PostgreSQL.
    """
    if not await is_postgresql(session):
        logger.info("Non-PostgreSQL database - skipping PostgreSQL optimizations")
        return
    
    logger.info("🚀 Initializing PostgreSQL optimizations...")

    # 0. Add any column a model declares that the live table doesn't have yet
    await sync_missing_columns(session)

    # 1. Set up partitioning (informational only - needs manual migration)
    await setup_telemetry_partitioning(session)
    
    # 2. Ensure partitions exist
    await ensure_future_partitions(session)
    
    # 3. Create materialized view
    await create_dashboard_materialized_view(session)
    
    # 4. Create JSON indexes
    await create_json_indexes(session)
    
    # 5. Create partial indexes
    await create_partial_indexes(session)
    
    # 6. Create covering indexes
    await create_covering_indexes(session)
    
    # 7. Set up NOTIFY triggers
    await setup_notify_triggers(session)

    # 8. Sync sequences (ensure autoincrement IDs don't collide)
    await sync_postgres_sequences(session)
    
    logger.info("✅ PostgreSQL optimizations complete")
