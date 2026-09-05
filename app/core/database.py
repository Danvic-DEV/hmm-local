"""
PostgreSQL database setup and models
"""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Integer, BigInteger, Float, DateTime, JSON, Boolean, Index, Text, text
from datetime import datetime
from typing import Optional
import logging
from core.config import settings


logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Base class for all models"""
    pass


class Miner(Base):
    """Miner configuration and state"""
    __tablename__ = "miners"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    miner_type: Mapped[str] = mapped_column(String(50))  # avalon_nano, bitaxe, nerdqaxe, nmminer
    ip_address: Mapped[str] = mapped_column(String(45))
    port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    current_mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    firmware_version: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    manual_power_watts: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # User-provided power estimate for miners without auto-detection
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    last_mode_change: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # Track when mode was last changed
    last_pool_switch: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # Track when pool was last switched (prevents reboot loops)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Pool(Base):
    """Mining pool configuration"""
    __tablename__ = "pools"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(255))  # Hostname or IP address
    port: Mapped[int] = mapped_column(Integer)
    user: Mapped[str] = mapped_column(String(255))
    password: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)  # For load balancing weight
    pool_type: Mapped[str] = mapped_column(String(50), default="unknown")  # solopool, braiins, mmfp, etc.
    pool_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # Plugin-specific config
    show_on_dashboard: Mapped[bool] = mapped_column(Boolean, default=True)  # Show pool tiles on dashboard
    sort_order: Mapped[int] = mapped_column(Integer, default=0)  # Display order on dashboard (0 = first)
    network_difficulty: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # DGB network difficulty
    network_difficulty_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    best_share: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Current best share in round
    best_share_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # When best_share last improved
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class HighDiffShare(Base):
    """High difficulty shares leaderboard (ASIC miners only)"""
    __tablename__ = "high_diff_shares"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    miner_id: Mapped[int] = mapped_column(Integer, index=True)
    miner_name: Mapped[str] = mapped_column(String(100))  # Snapshot in case miner renamed
    miner_type: Mapped[str] = mapped_column(String(50))  # avalon_nano, bitaxe, nerdqaxe
    coin: Mapped[str] = mapped_column(String(10))  # BTC, BCH, BC2, DGB
    pool_name: Mapped[str] = mapped_column(String(100))  # Pool name at time of share
    difficulty: Mapped[float] = mapped_column(Float, index=True)  # Share difficulty
    network_difficulty: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Network difficulty at time
    was_block_solve: Mapped[bool] = mapped_column(Boolean, default=False)  # True if share_diff >= network_diff
    hashrate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Miner hashrate at time
    hashrate_unit: Mapped[str] = mapped_column(String(10), default="GH/s")
    miner_mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # eco/std/turbo/oc/low/med/high
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    
    __table_args__ = (
        Index('idx_difficulty_timestamp', 'difficulty', 'timestamp'),
    )


class BlockFound(Base):
    """Blocks solved by miners (Coin Hunter leaderboard)"""
    __tablename__ = "blocks_found"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    miner_id: Mapped[int] = mapped_column(Integer, index=True)
    miner_name: Mapped[str] = mapped_column(String(100))  # Snapshot in case miner renamed
    miner_type: Mapped[str] = mapped_column(String(50))  # avalon_nano, bitaxe, nerdqaxe
    coin: Mapped[str] = mapped_column(String(10), index=True)  # BTC, BCH, BC2, DGB
    pool_name: Mapped[str] = mapped_column(String(100))  # Pool name
    difficulty: Mapped[float] = mapped_column(Float)  # Share difficulty that solved the block
    network_difficulty: Mapped[float] = mapped_column(Float)  # Network difficulty at time
    block_height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # Block height if available
    block_reward: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Block reward if available
    hashrate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Miner hashrate at time
    hashrate_unit: Mapped[str] = mapped_column(String(10), default="GH/s")
    miner_mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # eco/std/turbo/oc/low/med/high
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/confirmed/orphaned
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    
    __table_args__ = (
        Index('idx_miner_coin', 'miner_id', 'coin'),
    )


class PoolBlockEffort(Base):
    """Cumulative mining effort per pool (resets only when block found)"""
    __tablename__ = "pool_block_effort"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    pool_name: Mapped[str] = mapped_column(String(100), unique=True, index=True)  # Unique per pool
    coin: Mapped[str] = mapped_column(String(10))  # BTC, BCH, DGB, BC2
    effort_start: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)  # When current effort began
    last_reset: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # Last confirmed block found
    total_shares_accepted: Mapped[int] = mapped_column(BigInteger, default=0)  # Sum across ALL miners
    total_hashes: Mapped[str] = mapped_column(String(50), default="0")  # Stored as string due to huge numbers
    current_network_difficulty: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Updated periodically
    blocks_equivalent: Mapped[float] = mapped_column(Float, default=0.0)  # total_hashes / (net_diff × 2^32)
    last_updated: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    __table_args__ = (
        Index('idx_pool_coin', 'pool_name', 'coin'),
    )


class MinerPoolSlot(Base):
    """Cached pool slot configuration for Avalon Nano miners (3 slots per miner)"""
    __tablename__ = "miner_pool_slots"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    miner_id: Mapped[int] = mapped_column(Integer, index=True)
    slot_number: Mapped[int] = mapped_column(Integer)  # 0, 1, 2 for Avalon Nano
    pool_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # References Pool.id if matched
    pool_url: Mapped[str] = mapped_column(String(255))
    pool_port: Mapped[int] = mapped_column(Integer)
    pool_user: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)  # Currently selected slot
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Telemetry(Base):
    """Miner telemetry data"""
    __tablename__ = "telemetry"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    miner_id: Mapped[int] = mapped_column(Integer, index=True)  # Added index for performance
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    hashrate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hashrate_unit: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # KH/s, MH/s, GH/s, TH/s - no default, must be set explicitly
    temperature: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    power_watts: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    energy_cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Cost in pence for this 1-minute period (power_watts / 60 / 1000 * agile_price)
    shares_accepted: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    shares_rejected: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pool_difficulty: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Pool share difficulty at collection time
    pool_in_use: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # low/med/high/eco/turbo/oc captured at poll time
    data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # Additional miner-specific data
    
    # Composite index for common query pattern (miner_id + timestamp)
    __table_args__ = (
        Index('ix_telemetry_miner_timestamp', 'miner_id', 'timestamp'),
    )


class TelemetryHourly(Base):
    """Hourly aggregated miner telemetry data"""
    __tablename__ = "telemetry_hourly"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    miner_id: Mapped[int] = mapped_column(Integer, index=True)
    hour_start: Mapped[datetime] = mapped_column(DateTime, index=True)  # Start of hour (YYYY-MM-DD HH:00:00)
    uptime_minutes: Mapped[int] = mapped_column(Integer)  # Number of 1-minute records in this hour
    avg_hashrate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    min_hashrate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_hashrate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hashrate_unit: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, default="GH/s")
    avg_temperature: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    peak_temperature: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_kwh: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Sum of power consumption
    total_energy_cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Sum of energy_cost in pence
    shares_accepted: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    shares_rejected: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reject_rate_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    __table_args__ = (
        Index('ix_telemetry_hourly_miner_hour', 'miner_id', 'hour_start'),
    )


class TelemetryDaily(Base):
    """Daily aggregated miner telemetry data"""
    __tablename__ = "telemetry_daily"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    miner_id: Mapped[int] = mapped_column(Integer, index=True)
    date: Mapped[datetime] = mapped_column(DateTime, index=True)  # Date (YYYY-MM-DD 00:00:00)
    uptime_minutes: Mapped[int] = mapped_column(Integer)  # Total minutes of uptime for the day
    uptime_percentage: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # % of 1440 minutes
    avg_hashrate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    min_hashrate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_hashrate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hashrate_unit: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, default="GH/s")
    avg_temperature: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    peak_temperature: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_kwh: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_energy_cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Sum in pence
    shares_accepted: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    shares_rejected: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reject_rate_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    health_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 0-100 health score
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    __table_args__ = (
        Index('ix_telemetry_daily_miner_date', 'miner_id', 'date'),
    )


class EnergyPrice(Base):
    """Octopus Agile energy pricing"""
    __tablename__ = "energy_prices"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    region: Mapped[str] = mapped_column(String(1), index=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime, index=True)
    valid_to: Mapped[datetime] = mapped_column(DateTime)
    price_pence: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AutomationRule(Base):
    """Automation rules"""
    __tablename__ = "automation_rules"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    trigger_type: Mapped[str] = mapped_column(String(50))  # price_threshold, time_window, miner_offline, etc.
    trigger_config: Mapped[dict] = mapped_column(JSON)
    action_type: Mapped[str] = mapped_column(String(50))  # apply_mode, switch_pool, alert, etc.
    action_config: Mapped[dict] = mapped_column(JSON)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    last_executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_execution_context: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Event(Base):
    """System events and alerts"""
    __tablename__ = "events"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    event_type: Mapped[str] = mapped_column(String(50))  # info, warning, error, alert
    source: Mapped[str] = mapped_column(String(100))  # miner_id, automation_rule_id, system
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class CryptoPrice(Base):
    """Cached cryptocurrency prices"""
    __tablename__ = "crypto_prices"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    coin_id: Mapped[str] = mapped_column(String(50), unique=True)  # bitcoin, bitcoin-cash, digibyte
    price_gbp: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(50))  # coingecko, coincap, binance
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class NotificationConfig(Base):
    """Notification channel configuration"""
    __tablename__ = "notification_config"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    channel_type: Mapped[str] = mapped_column(String(20))  # telegram, discord
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict] = mapped_column(JSON)  # bot_token, chat_id for Telegram; webhook_url for Discord
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AlertConfig(Base):
    """Alert type configuration"""
    __tablename__ = "alert_config"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    alert_type: Mapped[str] = mapped_column(String(50), unique=True)  # miner_offline, high_temp, pool_failure, etc.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # thresholds, timeouts, etc.
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class NotificationLog(Base):
    """Log of sent notifications"""
    __tablename__ = "notification_log"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    channel_type: Mapped[str] = mapped_column(String(20))
    alert_type: Mapped[str] = mapped_column(String(50))
    message: Mapped[str] = mapped_column(String(1000))
    success: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)


class AlertThrottle(Base):
    """Track alert sending to prevent spam"""
    __tablename__ = "alert_throttle"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    miner_id: Mapped[int] = mapped_column(Integer, index=True)
    alert_type: Mapped[str] = mapped_column(String(50), index=True)
    last_sent: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    send_count: Mapped[int] = mapped_column(Integer, default=1)  # Track how many times sent


class HealthScore(Base):
    """Miner health scores over time"""
    __tablename__ = "health_scores"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    miner_id: Mapped[int] = mapped_column(Integer, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    overall_score: Mapped[float] = mapped_column(Float)  # 0-100
    uptime_score: Mapped[float] = mapped_column(Float)  # 0-100
    temperature_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 0-100, nullable for miners without temp sensors
    hashrate_score: Mapped[float] = mapped_column(Float)  # 0-100
    reject_rate_score: Mapped[float] = mapped_column(Float)  # 0-100
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class PoolHealth(Base):
    """Pool health monitoring"""
    __tablename__ = "pool_health"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    pool_id: Mapped[int] = mapped_column(Integer, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    response_time_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_reachable: Mapped[bool] = mapped_column(Boolean, default=True)
    reject_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Percentage
    shares_accepted: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    shares_rejected: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    health_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 0-100
    luck_percentage: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Pool luck %
    pool_hashrate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Pool-reported hashrate (GH/s)
    error_message: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)


class PoolHealthHourly(Base):
    """Hourly aggregated pool health data"""
    __tablename__ = "pool_health_hourly"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    pool_id: Mapped[int] = mapped_column(Integer, index=True)
    hour_start: Mapped[datetime] = mapped_column(DateTime, index=True)  # Start of hour (YYYY-MM-DD HH:00:00)
    checks_count: Mapped[int] = mapped_column(Integer)  # Number of checks in this hour
    avg_response_time_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_response_time_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    uptime_checks: Mapped[int] = mapped_column(Integer)  # Number of successful checks
    uptime_percentage: Mapped[float] = mapped_column(Float)  # % of checks that were reachable
    avg_health_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Average 0-100 score
    avg_reject_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_shares_accepted: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_shares_rejected: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    __table_args__ = (
        Index('ix_pool_health_hourly_pool_hour', 'pool_id', 'hour_start'),
    )


class PoolHealthDaily(Base):
    """Daily aggregated pool health data"""
    __tablename__ = "pool_health_daily"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    pool_id: Mapped[int] = mapped_column(Integer, index=True)
    date: Mapped[datetime] = mapped_column(DateTime, index=True)  # Date (YYYY-MM-DD 00:00:00)
    checks_count: Mapped[int] = mapped_column(Integer)  # Total checks for the day
    avg_response_time_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_response_time_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    uptime_checks: Mapped[int] = mapped_column(Integer)  # Successful checks
    uptime_percentage: Mapped[float] = mapped_column(Float)  # Daily availability %
    avg_health_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_reject_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_shares_accepted: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_shares_rejected: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    downtime_minutes: Mapped[int] = mapped_column(Integer, default=0)  # Estimated downtime
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    __table_args__ = (
        Index('ix_pool_health_daily_pool_date', 'pool_id', 'date'),
    )


class HourlyMinerAnalytics(Base):
    """Hourly aggregated miner analytics combining power, hash work, and performance metrics"""
    __tablename__ = "hourly_miner_analytics"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    miner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    pool_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # Which pool active during hour
    coin: Mapped[str] = mapped_column(String(10), nullable=False)  # 'BTC', 'XMR'
    hour_start: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # Dominant mode (most frequent during hour)
    mode_changes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # Number of mode transitions this hour
    mode_distribution: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # Minutes per mode: {"high": 30, "low": 30}
    tuning_profile_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    
    # Hash work metrics
    total_hashes_gh: Mapped[float] = mapped_column(Float, nullable=False)  # Gigahashes completed this hour
    avg_hashrate_gh: Mapped[float] = mapped_column(Float, nullable=False)
    peak_hashrate_gh: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    min_hashrate_gh: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    uptime_seconds: Mapped[int] = mapped_column(Integer, nullable=False)  # Actual mining time
    
    # Share statistics
    shares_accepted: Mapped[int] = mapped_column(Integer, default=0)
    shares_rejected: Mapped[int] = mapped_column(Integer, default=0)
    share_difficulty_avg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    best_share_difficulty: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Closest to block
    
    # Network context
    network_difficulty: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    block_height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    
    # Power and thermal metrics
    avg_power_watts: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    min_power_watts: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_power_watts: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_chip_temp_c: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_chip_temp_c: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    # Derived efficiency metrics
    watts_per_gh: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Power efficiency
    hashes_per_share: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Share efficiency
    reject_rate_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    __table_args__ = (
        Index('ix_hourly_analytics_miner_hour', 'miner_id', 'hour_start'),
        Index('ix_hourly_analytics_coin_hour', 'coin', 'hour_start'),
        Index('ix_hourly_analytics_pool_hour', 'pool_id', 'hour_start'),
    )


class DailyMinerAnalytics(Base):
    """Daily aggregated miner analytics for long-term trend analysis"""
    __tablename__ = "daily_miner_analytics"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    miner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    coin: Mapped[str] = mapped_column(String(10), nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    
    # Hash work aggregates
    total_hashes_th: Mapped[float] = mapped_column(Float, nullable=False)  # Terahashes for the day
    avg_hashrate_gh: Mapped[float] = mapped_column(Float, nullable=False)
    peak_hashrate_gh: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    uptime_hours: Mapped[float] = mapped_column(Float, nullable=False)
    
    # Share statistics
    total_shares_accepted: Mapped[int] = mapped_column(Integer, default=0)
    total_shares_rejected: Mapped[int] = mapped_column(Integer, default=0)
    avg_reject_rate_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    best_share_difficulty: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    # Power aggregates
    avg_power_watts: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_power_watts: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_energy_kwh: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    # Thermal aggregates
    avg_temp_c: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_temp_c: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    # Efficiency aggregates
    avg_watts_per_gh: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    # Mode distribution (JSON: {"low": 8, "med": 12, "high": 4} hours per mode)
    mode_distribution: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    __table_args__ = (
        Index('ix_daily_analytics_miner_date', 'miner_id', 'date'),
        Index('ix_daily_analytics_coin_date', 'coin', 'date'),
    )


class MinerModePowerStats(Base):
    """Running power statistics per miner and mode for fast strategy/UI lookups."""
    __tablename__ = "miner_mode_power_stats"

    id: Mapped[int] = mapped_column(primary_key=True)
    miner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_power_watts: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ema_power_watts: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    min_power_watts: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_power_watts: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_power_watts: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_sample_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # If either changes, we reset running stats so power profiles do not mix eras.
    firmware_version: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    profile_signature: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    resets_count: Mapped[int] = mapped_column(Integer, default=0)
    last_reset_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_miner_mode_power_stats_unique", "miner_id", "mode", unique=True),
        Index("ix_miner_mode_power_stats_miner_mode", "miner_id", "mode"),
    )


class TuningProfile(Base):
    """Saved tuning/overclocking profiles"""
    __tablename__ = "tuning_profiles"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    miner_type: Mapped[str] = mapped_column(String(50))  # avalon_nano, bitaxe, nerdqaxe
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    settings: Mapped[dict] = mapped_column(JSON)  # frequency, voltage, mode, etc
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)  # System presets vs user-created
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    """Audit log for tracking configuration changes"""
    __tablename__ = "audit_logs"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    user: Mapped[str] = mapped_column(String(100), default="system")  # Future: actual user auth
    action: Mapped[str] = mapped_column(String(50), index=True)  # create, update, delete, execute
    resource_type: Mapped[str] = mapped_column(String(50), index=True)  # miner, pool, strategy, automation, etc
    resource_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    resource_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    changes: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # before/after values
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="success")  # success, failure
    error_message: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)


class CustomDashboard(Base):
    """Custom dashboard configurations"""
    __tablename__ = "custom_dashboards"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    layout: Mapped[dict] = mapped_column(JSON)  # Grid layout configuration
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DashboardWidget(Base):
    """Widgets for custom dashboards"""
    __tablename__ = "dashboard_widgets"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    dashboard_id: Mapped[int] = mapped_column(Integer, index=True)
    widget_type: Mapped[str] = mapped_column(String(50))  # miner_stats, energy_price, pool_health, chart, etc
    config: Mapped[dict] = mapped_column(JSON)  # Widget-specific configuration
    position: Mapped[dict] = mapped_column(JSON)  # {x, y, w, h} for grid layout
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DailyMinerStats(Base):
    """Daily aggregated miner statistics for long-term analytics"""
    __tablename__ = "daily_miner_stats"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    miner_id: Mapped[int] = mapped_column(Integer, index=True)
    date: Mapped[datetime] = mapped_column(DateTime, index=True)  # Date at midnight UTC
    
    # Hashrate stats
    avg_hashrate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    min_hashrate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_hashrate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hashrate_unit: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, default="GH/s")
    
    # Temperature stats (ASIC only)
    avg_temperature: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_temperature: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    # Power stats (ASIC only)
    avg_power: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_kwh: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    # Uptime
    uptime_percent: Mapped[float] = mapped_column(Float, default=0.0)
    offline_minutes: Mapped[int] = mapped_column(Integer, default=0)
    
    # Shares (using BigInteger to handle values over 2.1 billion)
    total_shares_accepted: Mapped[int] = mapped_column(BigInteger, default=0)
    total_shares_rejected: Mapped[int] = mapped_column(BigInteger, default=0)
    reject_rate_percent: Mapped[float] = mapped_column(Float, default=0.0)
    
    # Economics
    energy_cost_gbp: Mapped[float] = mapped_column(Float, default=0.0)
    earnings_gbp: Mapped[float] = mapped_column(Float, default=0.0)
    profit_gbp: Mapped[float] = mapped_column(Float, default=0.0)
    
    # Metadata
    data_points: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DailyPoolStats(Base):
    """Daily aggregated pool statistics"""
    __tablename__ = "daily_pool_stats"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    pool_id: Mapped[int] = mapped_column(Integer, index=True)
    date: Mapped[datetime] = mapped_column(DateTime, index=True)
    
    # Pool performance
    blocks_found: Mapped[int] = mapped_column(Integer, default=0)
    total_shares_submitted: Mapped[int] = mapped_column(Integer, default=0)
    avg_luck_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    # Health
    avg_health_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    uptime_percent: Mapped[float] = mapped_column(Float, default=100.0)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MonthlyMinerStats(Base):
    """Monthly rollup of miner statistics"""
    __tablename__ = "monthly_miner_stats"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    miner_id: Mapped[int] = mapped_column(Integer, index=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    month: Mapped[int] = mapped_column(Integer, index=True)  # 1-12
    
    # Aggregated from daily stats
    avg_hashrate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hashrate_unit: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    total_kwh: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    uptime_percent: Mapped[float] = mapped_column(Float, default=0.0)
    
    total_shares_accepted: Mapped[int] = mapped_column(Integer, default=0)
    total_shares_rejected: Mapped[int] = mapped_column(Integer, default=0)
    reject_rate_percent: Mapped[float] = mapped_column(Float, default=0.0)
    
    # Economics
    total_energy_cost_gbp: Mapped[float] = mapped_column(Float, default=0.0)
    total_earnings_gbp: Mapped[float] = mapped_column(Float, default=0.0)
    total_profit_gbp: Mapped[float] = mapped_column(Float, default=0.0)
    
    days_active: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PriceBandStrategyConfig(Base):
    """Price band strategy configuration and state"""
    __tablename__ = "price_band_strategy"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    current_price_band: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # Pool name (e.g., "MMFP DGB Local Solo Pool")
    current_band_sort_order: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # Track specific band by sort_order
    hysteresis_counter: Mapped[int] = mapped_column(Integer, default=0)  # 2-slot delay for upgrading bands
    last_action_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_price_checked: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # p/kWh
    last_aggregation_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # When telemetry was last aggregated
    state_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # Additional state tracking
    champion_mode_enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # Enable champion mode for Band 5
    current_champion_miner_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # Current champion (most efficient miner)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PriceBandStrategyBand(Base):
    """Configurable price bands for price band strategy"""
    __tablename__ = "price_band_strategy_bands"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(Integer, index=True)  # FK to PriceBandStrategyConfig
    min_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Minimum price (p/kWh), None for lowest band
    max_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Maximum price (p/kWh), None for highest band
    target_pool_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # FK to Pool (None = OFF state)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)  # Display order (0-based)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Ensure bands are unique per strategy and sort order
    __table_args__ = (
        Index('ix_price_band_strategy_bands_unique', 'strategy_id', 'sort_order', unique=True),
    )


class StrategyBandModeTarget(Base):
    """Dynamic mode targets for strategy bands (miner_type -> mode)."""
    __tablename__ = "strategy_band_mode_targets"

    id: Mapped[int] = mapped_column(primary_key=True)
    band_id: Mapped[int] = mapped_column(Integer, index=True)
    miner_type: Mapped[str] = mapped_column(String(50), index=True)
    mode: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index('ix_strategy_band_mode_targets_unique', 'band_id', 'miner_type', unique=True),
    )


class MinerStrategy(Base):
    """Links miners to price band strategy"""
    __tablename__ = "miner_strategy"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    miner_id: Mapped[int] = mapped_column(Integer, index=True)
    strategy_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    # Ensure one record per miner
    __table_args__ = (
        Index('ix_miner_strategy_unique', 'miner_id', unique=True),
    )


# Database engine and session
def get_database_url() -> str:
    """Get PostgreSQL database URL from configuration"""
    from core.config import app_config
    import logging
    import os
    logger = logging.getLogger(__name__)
    
    pg_config = app_config.get("database.postgresql", {})
    host = pg_config.get("host", "localhost")  # Embedded PostgreSQL in same container
    port = pg_config.get("port", 5432)
    database = pg_config.get("database", "hmm")
    username = pg_config.get("username", "hmm_user")
    
    # Get password from environment variable or config
    # Default to the embedded PostgreSQL password set in entrypoint.sh
    password = os.getenv("POSTGRES_PASSWORD") or pg_config.get("password") or "hmm_secure_password"
    
    logger.info(f"🐘 PostgreSQL: {username}@{host}:{port}/{database}")
    return f"postgresql+asyncpg://{username}:{password}@{host}:{port}/{database}"

# Create PostgreSQL engine
def create_engine_for_database():
    """Create async PostgreSQL engine with connection pooling"""
    db_url = get_database_url()
    
    engine = create_async_engine(
        db_url,
        echo=False,
        pool_size=40,  # Connection pool size
        max_overflow=20,  # Allow burst connections
        pool_timeout=5,  # Fail fast when pool is exhausted
        pool_pre_ping=True
    )
    
    return engine

# Create engine and session maker
# These are initialized when the module is imported
engine = create_engine_for_database()
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Metric(Base):
    """Pre-computed metrics for fast querying"""
    __tablename__ = "metrics"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    metric_type: Mapped[str] = mapped_column(String(50), index=True)  # energy_cost, hashrate, pool_health, etc
    entity_type: Mapped[Optional[str]] = mapped_column(String(20), index=True, nullable=True)  # miner, pool, system
    entity_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)  # Miner ID, Pool ID, or NULL
    period: Mapped[str] = mapped_column(String(20), index=True)  # hourly, daily, weekly, monthly
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)  # Start of period
    value_json: Mapped[dict] = mapped_column(JSON)  # Flexible metric-specific data
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)  # When calculated
    
    __table_args__ = (
        Index('idx_metric_lookup', 'metric_type', 'entity_type', 'entity_id', 'period', 'timestamp'),
    )


class HomeAssistantConfig(Base):
    """Home Assistant integration configuration"""
    __tablename__ = "homeassistant_config"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), default="Home Assistant")
    base_url: Mapped[str] = mapped_column(String(255))  # e.g., http://homeassistant.local:8123
    access_token: Mapped[str] = mapped_column(String(500))  # Long-Lived Access Token
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    keepalive_enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # Monitor HA connectivity and send alerts
    keepalive_last_check: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # Last keepalive check
    keepalive_last_success: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # Last successful check
    keepalive_downtime_start: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # When downtime began
    keepalive_alerts_sent: Mapped[int] = mapped_column(Integer, default=0)  # Number of alerts sent for current downtime
    last_test: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_test_success: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class HomeAssistantDevice(Base):
    """Home Assistant devices tracked by HMM"""
    __tablename__ = "homeassistant_devices"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)  # e.g., switch.miner_power
    name: Mapped[str] = mapped_column(String(255))
    domain: Mapped[str] = mapped_column(String(50))  # switch, light, climate, etc.
    miner_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)  # Link to miner
    enrolled: Mapped[bool] = mapped_column(Boolean, default=False)  # Can automation control this?
    never_auto_control: Mapped[bool] = mapped_column(Boolean, default=False)  # Safety lock
    current_state: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # on, off, unavailable
    last_state_change: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_off_command_timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # Track when OFF command was sent for reconciliation
    capabilities: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MinerHASwitchLink(Base):
    """Link table for HA switch -> miners relationships."""
    __tablename__ = "miner_ha_switch_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    miner_id: Mapped[int] = mapped_column(Integer, index=True)
    ha_device_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_miner_ha_switch_links_unique_pair", "miner_id", "ha_device_id", unique=True),
        Index("ix_miner_ha_switch_links_unique_miner", "miner_id", unique=True),
    )


class MinerBaseline(Base):
    """Statistical baselines for miner performance metrics (per miner, per mode)"""
    __tablename__ = "miner_baselines"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    miner_id: Mapped[int] = mapped_column(Integer, index=True)
    mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # low/med/high/eco/std/turbo/oc/None
    metric_name: Mapped[str] = mapped_column(String(50))  # hashrate_mean, power_mean, w_per_th, temp_mean, reject_rate
    median_value: Mapped[float] = mapped_column(Float)  # Median (robust center)
    mad_value: Mapped[float] = mapped_column(Float)  # Median Absolute Deviation (robust spread)
    sample_count: Mapped[int] = mapped_column(Integer)  # Number of samples used
    window_hours: Mapped[int] = mapped_column(Integer)  # Rolling window size (24h or 168h)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    __table_args__ = (
        Index('ix_baseline_miner_mode_metric', 'miner_id', 'mode', 'metric_name'),
    )


class HealthEvent(Base):
    """Anomaly detection events with health scores and reason codes (historical)"""
    __tablename__ = "health_events"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    miner_id: Mapped[int] = mapped_column(Integer, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    health_score: Mapped[float] = mapped_column(Float)  # 0-100
    status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # healthy, warning, critical
    reasons: Mapped[dict] = mapped_column(JSON)  # Structured reason objects
    suggested_actions: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # Array of action enums
    anomaly_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Isolation Forest score (0-1)
    mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # Mode at time of check
    
    __table_args__ = (
        Index('ix_health_miner_timestamp', 'miner_id', 'timestamp'),
    )


class MinerHealthCurrent(Base):
    """Current health state per miner (canonical output layer) - one row per miner"""
    __tablename__ = "miner_health_current"
    
    miner_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    health_score: Mapped[int] = mapped_column(Integer, nullable=False)  # 0-100
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # healthy, warning, critical
    anomaly_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 0.0-1.0 from ML
    reasons: Mapped[dict] = mapped_column(JSON, nullable=False)  # Array of structured reason objects
    suggested_actions: Mapped[dict] = mapped_column(JSON, nullable=False)  # Array of action enums
    mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PlatformVersionCache(Base):
    """Cached GitHub version information - single row updated every 5 minutes"""
    __tablename__ = "platform_version_cache"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)  # Always 1
    latest_commit: Mapped[str] = mapped_column(String(40))  # Full SHA
    latest_commit_short: Mapped[str] = mapped_column(String(7))  # Short SHA
    latest_message: Mapped[str] = mapped_column(Text)
    latest_author: Mapped[str] = mapped_column(String(255))
    latest_date: Mapped[str] = mapped_column(String(50))  # ISO format
    latest_tag: Mapped[str] = mapped_column(String(50))  # main-abc1234
    latest_image: Mapped[str] = mapped_column(String(255))  # Full image path
    changelog: Mapped[dict] = mapped_column(JSON)  # Last 20 commits
    last_checked: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    github_available: Mapped[bool] = mapped_column(Boolean, default=True)  # False if rate limited
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


async def init_db():
    """
    Initialize database schema.
    
    This creates all tables from SQLAlchemy models on fresh install.
    No migrations - this is a clean deployment.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _backfill_legacy_ha_switch_links(conn)


async def _backfill_legacy_ha_switch_links(conn) -> None:
    """Backfill link-table rows from legacy HomeAssistantDevice.miner_id mappings."""
    try:
        result = await conn.execute(
            text(
                """
                SELECT id, miner_id
                FROM homeassistant_devices
                WHERE miner_id IS NOT NULL
                """
            )
        )
        legacy_links = result.fetchall()

        if not legacy_links:
            return

        inserted = 0
        skipped = 0

        for row in legacy_links:
            existing = await conn.execute(
                text(
                    """
                    SELECT id
                    FROM miner_ha_switch_links
                    WHERE miner_id = :miner_id
                    """
                ),
                {"miner_id": row.miner_id},
            )
            if existing.first() is not None:
                skipped += 1
                continue

            await conn.execute(
                text(
                    """
                    INSERT INTO miner_ha_switch_links (miner_id, ha_device_id, created_at)
                    VALUES (:miner_id, :ha_device_id, :created_at)
                    """
                ),
                {
                    "miner_id": row.miner_id,
                    "ha_device_id": row.id,
                    "created_at": datetime.utcnow(),
                },
            )
            inserted += 1

        if inserted or skipped:
            logger.info(
                "HA legacy link backfill complete: inserted=%s skipped=%s",
                inserted,
                skipped,
            )
    except Exception as exc:
        logger.error("HA legacy link backfill failed: %s", exc)
        raise


async def get_db() -> AsyncSession:
    """Get database session"""
    async with AsyncSessionLocal() as session:
        yield session
