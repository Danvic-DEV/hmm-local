"""
Settings API endpoints
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import httpx
import logging
import os
import signal

from core.database import get_db, Miner, Telemetry, Event, AsyncSessionLocal, CryptoPrice
from core.config import app_config
from core.utils import format_hashrate
from datetime import datetime

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/restart")
async def restart_application():
    """Restart the application container"""
    logger.info("Application restart requested via API")
    
    # Log the restart event
    async with AsyncSessionLocal() as db:
        event = Event(
            event_type="info",
            source="api",
            message="Application restart initiated from settings"
        )
        db.add(event)
        await db.commit()
    
    # Send SIGTERM to trigger graceful shutdown, Docker will restart us
    os.kill(os.getpid(), signal.SIGTERM)
    
    return {"message": "Restarting application..."}


class SolopoolSettings(BaseModel):
    enabled: bool


@router.get("/solopool")
async def get_solopool_settings():
    """Get Solopool.org integration settings"""
    return {
        "enabled": app_config.get("solopool_enabled", False)
    }


@router.post("/solopool")
async def save_solopool_settings(settings: SolopoolSettings):
    """Save Solopool.org integration settings"""
    app_config.set("solopool_enabled", settings.enabled)
    app_config.save()
    
    return {
        "message": "Solopool settings saved",
        "enabled": settings.enabled
    }


class CloudPushSettings(BaseModel):
    enabled: bool
    endpoint: Optional[str] = None
    api_key: Optional[str] = None
    push_interval: Optional[int] = 300


@router.get("/cloud")
async def get_cloud_settings():
    """Get cloud push settings"""
    return {
        "enabled": app_config.get("cloud.enabled", False),
        "endpoint": app_config.get("cloud.endpoint", "https://stage.miningpool.uk"),
        "api_key": app_config.get("cloud.api_key", ""),
        "push_interval": app_config.get("cloud.push_interval", 300)
    }


@router.post("/cloud")
async def save_cloud_settings(settings: CloudPushSettings):
    """Save cloud push settings"""
    if settings.enabled and (not settings.endpoint or not settings.api_key):
        return {
            "message": "Endpoint and API key are required when cloud push is enabled",
            "enabled": False
        }
    
    app_config.set("cloud.enabled", settings.enabled)
    app_config.set("cloud.endpoint", settings.endpoint or "")
    app_config.set("cloud.api_key", settings.api_key or "")
    app_config.set("cloud.push_interval", settings.push_interval or 300)
    app_config.save()

    # Restart cloud push service to apply settings
    try:
        from core.cloud_push import cloud_push_service
        await cloud_push_service.reload_config()
    except Exception as e:
        logger.error(f"Failed to reload cloud push config: {e}")
    
    return {
        "message": "Cloud push settings saved",
        "enabled": settings.enabled,
        "endpoint": settings.endpoint,
        "push_interval": settings.push_interval
    }


@router.get("/crypto-prices")
async def get_crypto_prices():
    """Return cached crypto prices (updated every 10 minutes by scheduler)"""
    prices = {
        "bitcoin-cash": 0,
        "bitcoinii": 0,
        "digibyte": 0,
        "bitcoin": 0,
        "success": False,
        "error": None,
        "source": None,
        "cache_age": None
    }
    
    # Get cached prices from database
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(CryptoPrice))
        cached_prices = {cp.coin_id: cp for cp in result.scalars().all()}
        
        if cached_prices:
            prices["bitcoin"] = cached_prices.get("bitcoin").price_gbp if "bitcoin" in cached_prices else 0
            prices["bitcoin-cash"] = cached_prices.get("bitcoin-cash").price_gbp if "bitcoin-cash" in cached_prices else 0
            prices["bitcoinii"] = cached_prices.get("bitcoinii").price_gbp if "bitcoinii" in cached_prices else 0
            prices["digibyte"] = cached_prices.get("digibyte").price_gbp if "digibyte" in cached_prices else 0
            prices["success"] = True
            prices["source"] = cached_prices.get("bitcoin").source if "bitcoin" in cached_prices else "cache"
            
            # Calculate cache age
            if "bitcoin" in cached_prices:
                age = datetime.utcnow() - cached_prices["bitcoin"].updated_at
                age_minutes = int(age.total_seconds() / 60)
                prices["cache_age"] = f"{age_minutes}m ago"
                prices["age_minutes"] = age_minutes
            
            return prices
        else:
            prices["error"] = "No cached prices available yet"
            return prices


async def fetch_and_cache_crypto_prices():
    """Fetch crypto prices in GBP with fallback across multiple free APIs and cache them"""
    prices = {
        "bitcoin-cash": 0,
        "bitcoinii": 0,
        "digibyte": 0,
        "bitcoin": 0,
        "success": False,
        "error": None,
        "source": None
    }
    
    # Try CoinGecko first
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                'https://api.coingecko.com/api/v3/simple/price',
                params={
                    'ids': 'bitcoin-cash,bitcoinii,digibyte,bitcoin',
                    'vs_currencies': 'gbp'
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                prices["bitcoin-cash"] = data.get("bitcoin-cash", {}).get("gbp", 0)
                prices["bitcoinii"] = data.get("bitcoinii", {}).get("gbp", 0)
                prices["digibyte"] = data.get("digibyte", {}).get("gbp", 0)
                prices["bitcoin"] = data.get("bitcoin", {}).get("gbp", 0)
                prices["success"] = True
                prices["source"] = "coingecko"
                
                logger.info(f"Fetched crypto prices from CoinGecko: BCH=£{prices['bitcoin-cash']}, BC2=£{prices['bitcoinii']}, DGB=£{prices['digibyte']}, BTC=£{prices['bitcoin']}")
                return prices
            else:
                error_msg = f"CoinGecko API returned status {response.status_code}: {response.text[:200]}"
                logger.warning(error_msg)
                
                async with AsyncSessionLocal() as session:
                    event = Event(
                        event_type="api_warning",
                        source="coingecko",
                        message=error_msg
                    )
                    session.add(event)
                    await session.commit()
                    
    except Exception as e:
        logger.warning(f"CoinGecko API failed: {str(e)}")
    
    # Fallback to CoinCap API
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # CoinCap uses IDs: bitcoin-cash, bitcoinii, digibyte, bitcoin
            bch_response = await client.get('https://api.coincap.io/v2/assets/bitcoin-cash')
            bc2_response = await client.get('https://api.coincap.io/v2/assets/bitcoinii')
            dgb_response = await client.get('https://api.coincap.io/v2/assets/digibyte')
            btc_response = await client.get('https://api.coincap.io/v2/assets/bitcoin')
            
            # Get GBP exchange rate
            gbp_response = await client.get('https://api.coincap.io/v2/rates/british-pound-sterling')
            
            if all(r.status_code == 200 for r in [bch_response, bc2_response, dgb_response, btc_response, gbp_response]):
                gbp_rate = float(gbp_response.json()["data"]["rateUsd"])
                
                bch_usd = float(bch_response.json()["data"]["priceUsd"])
                bc2_usd = float(bc2_response.json()["data"]["priceUsd"])
                dgb_usd = float(dgb_response.json()["data"]["priceUsd"])
                btc_usd = float(btc_response.json()["data"]["priceUsd"])
                
                prices["bitcoin-cash"] = bch_usd / gbp_rate
                prices["bitcoinii"] = bc2_usd / gbp_rate
                prices["digibyte"] = dgb_usd / gbp_rate
                prices["bitcoin"] = btc_usd / gbp_rate
                prices["success"] = True
                prices["source"] = "coincap"
                
                logger.info(f"Fetched crypto prices from CoinCap: BCH=£{prices['bitcoin-cash']:.2f}, BC2=£{prices['bitcoinii']:.6f}, DGB=£{prices['digibyte']:.6f}, BTC=£{prices['bitcoin']:.2f}")
                return prices
            else:
                logger.warning("CoinCap API returned non-200 status")
                
    except Exception as e:
        logger.warning(f"CoinCap API failed: {str(e)}")
    
    # Fallback to Binance API (convert via USDT then to GBP)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Binance provides direct GBP pairs for BTC, BCH
            btc_response = await client.get('https://api.binance.com/api/v3/ticker/price?symbol=BTCGBP')
            bch_response = await client.get('https://api.binance.com/api/v3/ticker/price?symbol=BCHGBP')
            
            # DGB and BC2 not on Binance with GBP, get USDT price and convert
            dgb_usdt_response = await client.get('https://api.binance.com/api/v3/ticker/price?symbol=DGBUSDT')
            # BC2 likely not on Binance, try but don't fail if not available
            try:
                bc2_usdt_response = await client.get('https://api.binance.com/api/v3/ticker/price?symbol=BLS2USDT')
            except:
                bc2_usdt_response = None
            usdt_gbp_response = await client.get('https://api.binance.com/api/v3/ticker/price?symbol=GBPUSDT')
            
            if all(r.status_code == 200 for r in [btc_response, bch_response, dgb_usdt_response, usdt_gbp_response]):
                prices["bitcoin"] = float(btc_response.json()["price"])
                prices["bitcoin-cash"] = float(bch_response.json()["price"])
                
                dgb_usdt = float(dgb_usdt_response.json()["price"])
                gbp_usdt = float(usdt_gbp_response.json()["price"])
                prices["digibyte"] = dgb_usdt / gbp_usdt
                
                # Try to get BC2 price if available
                if bc2_usdt_response and bc2_usdt_response.status_code == 200:
                    bc2_usdt = float(bc2_usdt_response.json()["price"])
                    prices["bitcoinii"] = bc2_usdt / gbp_usdt
                
                prices["success"] = True
                prices["source"] = "binance"
                
                logger.info(f"Fetched crypto prices from Binance: BCH=£{prices['bitcoin-cash']:.2f}, BC2=£{prices['bitcoinii']:.6f}, DGB=£{prices['digibyte']:.6f}, BTC=£{prices['bitcoin']:.2f}")
                return prices
            else:
                logger.warning("Binance API returned non-200 status")
                
    except Exception as e:
        logger.warning(f"Binance API failed: {str(e)}")
    
    # All APIs failed
    error_msg = "All crypto price APIs failed (CoinGecko, CoinCap, Binance)"
    logger.error(error_msg)
    
    async with AsyncSessionLocal() as session:
        event = Event(
            event_type="api_error",
            source="crypto_pricing",
            message=error_msg
        )
        session.add(event)
        await session.commit()
    
    prices["error"] = error_msg
    return prices


# This function is called by the scheduler, not exposed as an endpoint
async def update_crypto_prices_cache():
    """Background task to update cached crypto prices"""
    logger.info("Updating crypto price cache...")
    
    prices = await fetch_and_cache_crypto_prices()
    
    if prices["success"]:
        # Store in database
        async with AsyncSessionLocal() as session:
            for coin_id in ["bitcoin", "bitcoin-cash", "bitcoinii", "digibyte"]:
                price_value = prices.get(coin_id, 0)
                if price_value > 0:
                    # Check if exists
                    result = await session.execute(
                        select(CryptoPrice).where(CryptoPrice.coin_id == coin_id)
                    )
                    cached_price = result.scalar_one_or_none()
                    
                    if cached_price:
                        cached_price.price_gbp = price_value
                        cached_price.source = prices["source"]
                        cached_price.updated_at = datetime.utcnow()
                    else:
                        new_price = CryptoPrice(
                            coin_id=coin_id,
                            price_gbp=price_value,
                            source=prices["source"]
                        )
                        session.add(new_price)
            
            await session.commit()
            logger.info(f"Crypto price cache updated from {prices['source']}")
    else:
        logger.warning(f"Failed to update crypto price cache: {prices.get('error')}")


@router.post("/trigger-aggregation")
async def trigger_telemetry_aggregation():
    """
    Manually trigger telemetry aggregation job.
    
    This is normally run daily at 00:05, but can be triggered manually for testing.
    Aggregates yesterday's telemetry into hourly and daily tables.
    """
    from core.scheduler import scheduler_service
    
    try:
        if scheduler_service and scheduler_service.scheduler:
            # Get the aggregation function directly
            job = scheduler_service.scheduler.get_job("aggregate_telemetry")
            if job:
                # Run the job immediately
                await scheduler_service._aggregate_telemetry()
                return {
                    "status": "success",
                    "message": "Telemetry aggregation triggered successfully"
                }
            else:
                return {
                    "status": "error",
                    "message": "Aggregation job not found in scheduler"
                }
        else:
            return {
                "status": "error",
                "message": "Scheduler service not available"
            }
    except Exception as e:
        logger.error(f"Failed to trigger aggregation: {e}", exc_info=True)
        return {
            "status": "error",
            "message": f"Failed to trigger aggregation: {str(e)}"
        }


@router.get("/ai/status")
async def ai_status():
    """
    Stub endpoint for AI configuration status.
    
    TODO: Implement AI assistant functionality.
    For now, returns empty config to prevent 404 errors from frontend.
    """
    return {
        "enabled": False,
        "config": {
            "enabled": False,
            "provider": "openai",
            "model": "gpt-4o",
            "max_tokens": 1000
        }
    }

