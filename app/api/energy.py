"""
Energy Optimization API endpoints
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.energy import EnergyOptimizationService


router = APIRouter()


@router.get("/miners/{miner_id}/profitability")
async def get_miner_profitability(
    miner_id: int,
    hours: int = 24,
    db: AsyncSession = Depends(get_db)
):
    """Get mining profitability calculation"""
    # Get coin prices from cache (if available)
    from api.settings import crypto_prices_cache
    
    coin_prices = None
    if crypto_prices_cache.get("data"):
        prices = crypto_prices_cache["data"]
        coin_prices = {
            "BTC": prices.get("bitcoin", 0),
            "BCH": prices.get("bitcoin-cash", 0),
            "BC2": prices.get("bitcoinii", 0),
            "DGB": prices.get("digibyte", 0)
        }
    
    result = await EnergyOptimizationService.calculate_profitability(
        miner_id, db, hours, coin_prices
    )
    
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    
    return result


@router.get("/price-forecast")
async def get_price_forecast(
    hours: int = 24,
    db: AsyncSession = Depends(get_db)
):
    """Get energy price forecast for next N hours"""
    forecast = await EnergyOptimizationService.get_price_forecast(db, hours)
    
    if not forecast:
        raise HTTPException(status_code=404, detail="No price forecast available")
    
    return {
        "hours_ahead": hours,
        "forecast": forecast
    }


@router.get("/miners/{miner_id}/schedule-recommendation")
async def get_schedule_recommendation(
    miner_id: int,
    target_hours: int = 12,
    db: AsyncSession = Depends(get_db)
):
    """Get recommended mining schedule based on energy prices"""
    result = await EnergyOptimizationService.recommend_schedule(
        miner_id, db, target_hours
    )
    
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    
    return result


@router.get("/should-mine-now")
async def should_mine_now(
    price_threshold: float = 15.0,
    db: AsyncSession = Depends(get_db)
):
    """Check if current energy price is favorable for mining"""
    result = await EnergyOptimizationService.should_mine_now(db, price_threshold)
    
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    
    return result


@router.get("/overview")
async def get_energy_overview(db: AsyncSession = Depends(get_db)):
    """Get energy optimization overview for all miners"""
    from core.database import Miner
    from sqlalchemy import select
    
    # Get all enabled miners
    result = await db.execute(select(Miner).where(Miner.enabled == True))
    miners = result.scalars().all()
    
    # Get coin prices from database
    from core.database import CryptoPrice
    coin_prices = None
    result_prices = await db.execute(select(CryptoPrice))
    crypto_prices = result_prices.scalars().all()
    if crypto_prices:
        prices = {cp.coin_id: cp.price_gbp for cp in crypto_prices}
        coin_prices = {
            "BTC": prices.get("bitcoin", 0),
            "BCH": prices.get("bitcoin-cash", 0),
            "BC2": prices.get("bitcoinii", 0),
            "DGB": prices.get("digibyte", 0)
        }
    
    # Calculate profitability for each miner
    miner_stats = []
    total_energy_cost = 0
    total_profit = 0
    
    for miner in miners:
        profitability = await EnergyOptimizationService.calculate_profitability(
            miner.id, db, 24, coin_prices
        )
        
        if "error" not in profitability:
            miner_stats.append(profitability)
            total_energy_cost += profitability.get("energy_cost_gbp", 0)
            total_profit += profitability.get("profit_gbp", 0)
    
    # Get current price recommendation
    current_recommendation = await EnergyOptimizationService.should_mine_now(db)
    
    return {
        "total_miners": len(miners),
        "total_energy_cost_24h": round(total_energy_cost, 2),
        "total_profit_24h": round(total_profit, 2),
        "miners": miner_stats,
        "current_recommendation": current_recommendation
    }


@router.get("/auto-optimization/status")
async def get_auto_optimization_status():
    """Get auto-optimization status"""
    from core.config import app_config
    
    enabled = app_config.get("energy_optimization.enabled", False)
    price_threshold = app_config.get("energy_optimization.price_threshold", 15.0)
    
    return {
        "enabled": enabled,
        "price_threshold": price_threshold
    }


@router.post("/auto-optimization/toggle")
async def toggle_auto_optimization(request: dict, db: AsyncSession = Depends(get_db)):
    """Toggle auto-optimization on/off"""
    from core.config import app_config
    from core.database import AutomationRule
    from sqlalchemy import select
    
    enabled = request.get("enabled", False)
    
    # If enabling, check for conflicting automation rules
    if enabled:
        result = await db.execute(
            select(AutomationRule).where(
                AutomationRule.enabled == True,
                AutomationRule.trigger_type == "price_threshold"
            )
        )
        conflicting_rules = result.scalars().all()
        
        if conflicting_rules:
            rule_names = ", ".join([rule.name for rule in conflicting_rules])
            raise HTTPException(
                status_code=409,
                detail=f"Cannot enable auto-optimization while energy-based automation rules are active: {rule_names}. Please disable these rules first to avoid conflicts."
            )
    
    app_config.set("energy_optimization.enabled", enabled)
    
    return {"enabled": enabled}


@router.post("/auto-optimization/config")
async def save_auto_optimization_config(request: dict):
    """Save auto-optimization configuration"""
    from core.config import app_config
    
    price_threshold = request.get("price_threshold", 15.0)
    app_config.set("energy_optimization.price_threshold", price_threshold)
    
    return {"price_threshold": price_threshold}


@router.post("/auto-optimization/trigger")
async def trigger_auto_optimization(db: AsyncSession = Depends(get_db)):
    """Manually trigger auto-optimization immediately"""
    from core.scheduler import scheduler
    
    try:
        # Run the auto-optimization job immediately
        await scheduler._auto_optimize_miners()
        return {"message": "Auto-optimization executed successfully. Check miner modes for changes."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

