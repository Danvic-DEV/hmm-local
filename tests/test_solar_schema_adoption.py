from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from core.database import Base, SolarStrategyConfig, SolarMinerEnrollment  # noqa: E402


def test_solar_tables_are_created_automatically_on_a_fresh_database():
    """T028 - simulates what happens on container restart after adopting
    this feature: Base.metadata.create_all() (what init_db() calls) must
    produce the new tables with zero manual SQL. Uses a synchronous
    in-memory SQLite engine purely to exercise table creation - the actual
    app uses PostgreSQL/async, but CREATE TABLE behavior from the same
    SQLAlchemy metadata is driver-agnostic."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    assert "solar_strategy_config" in table_names
    assert "solar_miner_enrollment" in table_names

    solar_config_columns = {c["name"] for c in inspector.get_columns("solar_strategy_config")}
    assert {"id", "enabled", "solar_surplus_entity_id"}.issubset(solar_config_columns)
    # No pool concept at all - Solar Strategy never switches pool (see module docstring in solar_strategy.py)
    assert "pool_id" not in solar_config_columns

    enrollment_columns = {c["name"] for c in inspector.get_columns("solar_miner_enrollment")}
    assert {"id", "miner_id", "enabled"}.issubset(enrollment_columns)


def test_no_existing_table_or_column_was_altered():
    """Guard against ever reintroducing the mistake from the first (reverted)
    attempt: this feature must add new tables only, never touch an existing
    table's columns. Spot-checks the two tables that were wrongly modified
    last time."""
    price_band_config_columns = {c.name for c in Base.metadata.tables["price_band_strategy"].columns}
    miner_strategy_columns = {c.name for c in Base.metadata.tables["miner_strategy"].columns}

    assert "use_excess_solar" not in price_band_config_columns
    assert "solar_surplus_entity_id" not in price_band_config_columns
    assert "solar_enabled" not in miner_strategy_columns
