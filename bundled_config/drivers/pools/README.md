# Pool Drivers

This directory contains pool driver plugins. Each driver is a Python file that knows how to communicate with a specific type of mining pool.

## Driver Structure

Each driver must:
1. Import `BasePoolIntegration` from the core codebase
2. Implement required methods (health check, stats retrieval, etc.)
3. Define its `driver_type` (used by pool configs)
4. Optionally implement `get_dashboard_data()` for dashboard tile integration

### Minimal Driver Example

```python
from integrations.base_pool import BasePoolIntegration, PoolHealthStatus

class MyPoolDriver(BasePoolIntegration):
    driver_type = "mypool"  # Referenced in pool YAML configs
    
    async def check_health(self, pool_id: str, wallet: str) -> PoolHealthStatus:
        # Implement health check logic
        pass
    
    # ... implement other required methods
```

### Dashboard Tile Integration (Optional)

To show real-time stats on the dashboard, implement `get_dashboard_data()`:

```python
from integrations.base_pool import DashboardTileData

async def get_dashboard_data(
    self,
    url: str,
    coin: str,
    username: Optional[str] = None,
    **kwargs
) -> Optional[DashboardTileData]:
    """
    Fetch USER-SPECIFIC stats for dashboard tiles.
    
    IMPORTANT: Return the USER's hashrate/shares, not pool-wide stats!
    """
    # Fetch user's worker stats from pool API
    worker_stats = await self._fetch_worker_stats(username, coin)
    
    # Convert hashrate to TH/s (if API returns H/s)
    user_hashrate_ths = worker_stats["hashrate"] / 1_000_000_000_000
    
    return DashboardTileData(
        health_status=True,
        pool_hashrate=user_hashrate_ths,  # USER's hashrate in TH/s
        active_workers=worker_stats.get("workers_online", 0),
        shares_valid=worker_stats.get("valid_shares", 0),
        shares_invalid=worker_stats.get("invalid_shares", 0),
        currency=coin.upper()
    )
```

**Key Points:**
- `pool_hashrate` = **USER's hashrate** (not pool's total hashrate!)
- Convert H/s to TH/s: divide by 1,000,000,000,000
- `active_workers` = count of user's online workers
- Check API docs for exact field names (some use camelCase)
- Return `None` or `DashboardTileData(health_status=False)` on error

See `solopool_driver.py` for a complete implementation example.

## Shipping Drivers

Default drivers included:
- `solopool_driver.py` - Solopool.org pools (dashboard integration: ✅)
- `braiins_driver.py` - Braiins Pool (dashboard integration: ⏳)
- `mmfp_driver.py` - Local MMFP pools (dashboard integration: ⏳)
- `dgb_stack_driver.py` - Local DGB stack CKPool stratum + manager API (dashboard integration: ✅)
- `bch_stack_driver.py` - Local BCH stack CKPool stratum + manager API (dashboard integration: ✅)
- `btc_stack_driver.py` - Local BTC stack CKPool stratum + manager API (dashboard integration: ✅)

## Custom Drivers

Users can add custom drivers by:
1. Creating a new `.py` file in this directory
2. Implementing the `BasePoolIntegration` interface
3. Restarting the container

The system automatically discovers and loads all drivers on startup.
