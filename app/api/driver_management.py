"""
Driver Management API

Provides endpoints for checking, updating, and installing pool drivers.
"""
import logging
import shutil
import re
from pathlib import Path
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
import importlib.util
import sys

from core.database import get_db
from core.audit import log_audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/drivers", tags=["Driver Management"])

BUNDLED_POOL_DRIVERS_PATH = Path("/app/bundled_config/drivers/pools")
CONFIG_POOL_DRIVERS_PATH = Path("/config/drivers")
BUNDLED_MINER_DRIVERS_PATH = Path("/app/bundled_config/drivers/miners")
CONFIG_MINER_DRIVERS_PATH = Path("/config/drivers/miners")
MINER_SCHEMA_FILENAME = "TELEMETRY_SCHEMA.md"
BUNDLED_MINER_SCHEMA_PATH = BUNDLED_MINER_DRIVERS_PATH / MINER_SCHEMA_FILENAME
CONFIG_MINER_SCHEMA_PATH = CONFIG_MINER_DRIVERS_PATH / MINER_SCHEMA_FILENAME
BUNDLED_ENERGY_PROVIDERS_PATH = Path("/app/bundled_config/providers/energy")
CONFIG_ENERGY_PROVIDERS_PATH = Path("/config/providers/energy")
BUNDLED_STRATEGIES_PATH = Path("/app/bundled_config/strategies")
CONFIG_STRATEGIES_PATH = Path("/config/strategies")


class DriverInfo(BaseModel):
    """Information about a driver"""
    name: str  # e.g., "mmfp_driver.py"
    driver_type: str  # e.g., "mmfp" or "pool" | "miner"
    category: str  # "pool" or "miner"
    display_name: str  # e.g., "MMFP Solutions"
    current_version: Optional[str]  # Version in /config (None if not installed)
    available_version: str  # Version in bundled
    status: str  # "up_to_date", "update_available", "not_installed"
    description: Optional[str]


class DriverUpdateResponse(BaseModel):
    """Response from driver update/install operation"""
    success: bool
    message: str
    driver_name: str
    old_version: Optional[str]
    new_version: str


class EnergyProviderInfo(BaseModel):
    """Information about an energy provider plugin."""
    name: str  # e.g., "octopus_agile_provider.py"
    provider_id: str  # e.g., "octopus_agile"
    display_name: str  # e.g., "Octopus Agile (UK)"
    current_version: Optional[str]  # Version in /config (None if not installed)
    available_version: str  # Version in bundled
    status: str  # "up_to_date", "update_available", "not_installed"
    description: Optional[str]
    supported_regions: List[str] = []


class EnergyProviderUpdateResponse(BaseModel):
    """Response from energy provider update/install operation."""
    success: bool
    message: str
    provider_name: str
    old_version: Optional[str]
    new_version: str


class StrategyPluginInfo(BaseModel):
    """Information about a strategy plugin."""
    name: str  # e.g., "solar_strategy.py"
    strategy_id: str  # e.g., "solar"
    display_name: str  # e.g., "Solar Strategy"
    current_version: Optional[str]  # Version in /config (None if not installed)
    available_version: str  # Version in bundled
    status: str  # "up_to_date", "update_available", "not_installed"
    description: Optional[str]


class StrategyPluginUpdateResponse(BaseModel):
    """Response from strategy plugin update/install operation."""
    success: bool
    message: str
    strategy_name: str
    old_version: Optional[str]
    new_version: Optional[str]


class TelemetrySchemaStatus(BaseModel):
    """Status of miner telemetry schema document in bundled vs deployed locations."""
    name: str
    current_version: Optional[str]
    available_version: Optional[str]
    status: str  # "up_to_date", "update_available", "not_installed", "missing_bundle"


class TelemetrySchemaUpdateResponse(BaseModel):
    """Response from telemetry schema update operation."""
    success: bool
    message: str
    name: str
    old_version: Optional[str]
    new_version: Optional[str]


def load_driver_module(file_path: Path) -> Optional[Any]:
    """Load a driver module from file path"""
    try:
        spec = importlib.util.spec_from_file_location(file_path.stem, file_path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    except Exception as e:
        logger.error(f"Failed to load driver module {file_path}: {e}")
    return None


def get_driver_version(module: Any) -> str:
    """Extract version from driver module"""
    # Try __version__ first
    if hasattr(module, '__version__'):
        return module.__version__
    
    # Try class attribute driver_version
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if isinstance(attr, type) and hasattr(attr, 'driver_version'):
            return attr.driver_version
    
    return "unknown"


def get_driver_info_from_module(module: Any) -> Dict[str, str]:
    """Extract driver metadata from module (works for both pool and miner drivers)"""
    info = {
        "driver_type": "unknown",
        "display_name": "Unknown Driver",
        "description": None
    }
    
    # Try to find BasePoolIntegration subclass (pool drivers)
    try:
        from integrations.base_pool import BasePoolIntegration
        
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (isinstance(attr, type) and 
                issubclass(attr, BasePoolIntegration) and 
                attr is not BasePoolIntegration):
                
                if hasattr(attr, 'pool_type'):
                    info["driver_type"] = attr.pool_type
                if hasattr(attr, 'display_name'):
                    info["display_name"] = attr.display_name
                if hasattr(attr, '__doc__'):
                    info["description"] = attr.__doc__.strip() if attr.__doc__ else None
                return info
    except ImportError:
        pass
    
    # Try to find MinerAdapter subclass (miner drivers)
    try:
        from adapters.base import MinerAdapter
        
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (isinstance(attr, type) and 
                issubclass(attr, MinerAdapter) and 
                attr is not MinerAdapter):
                
                if hasattr(attr, 'miner_type'):
                    info["driver_type"] = attr.miner_type
                # Generate display name from class name
                class_name = attr.__name__.replace('Adapter', '').replace('Miner', '')
                info["display_name"] = class_name
                if hasattr(attr, '__doc__'):
                    info["description"] = attr.__doc__.strip() if attr.__doc__ else None
                return info
    except ImportError:
        pass
    
    return info


def get_schema_version(file_path: Path) -> Optional[str]:
    """Extract schema version from markdown document.

    Expected line format: Schema Version: 1.0.0
    """
    try:
        if not file_path.exists():
            return None

        content = file_path.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"^Schema Version:\s*([0-9]+\.[0-9]+\.[0-9]+)", content, flags=re.MULTILINE)
        if match:
            return match.group(1)
        return "unknown"
    except Exception as e:
        logger.error(f"Failed to read schema version from {file_path}: {e}")
        return None


def get_energy_provider_info_from_module(module: Any) -> Dict[str, Any]:
    """Extract energy provider metadata from module."""
    info: Dict[str, Any] = {
        "provider_id": "unknown",
        "display_name": "Unknown Energy Provider",
        "description": None,
        "supported_regions": [],
    }

    try:
        from providers.energy.base import EnergyPriceProvider

        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, EnergyPriceProvider)
                and attr is not EnergyPriceProvider
            ):
                instance = attr()
                metadata = instance.get_metadata()
                info["provider_id"] = metadata.provider_id
                info["display_name"] = metadata.display_name
                info["description"] = metadata.description
                info["supported_regions"] = metadata.supported_regions or []
                return info
    except Exception as e:
        logger.error(f"Failed to extract energy provider metadata: {e}")

    return info


def get_strategy_info_from_module(module: Any) -> Optional[Dict[str, Any]]:
    """Extract strategy plugin metadata via its get_metadata() contract
    (see core/strategy_plugin_base.py) - unlike pool/miner drivers and energy
    providers, a StrategyPlugin always carries its own version in
    StrategyMetadata, so there's no separate get_driver_version lookup."""
    try:
        from core.strategy_plugin_base import StrategyPlugin

        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, StrategyPlugin)
                and attr is not StrategyPlugin
            ):
                instance = attr()
                metadata = instance.get_metadata()
                return {
                    "strategy_id": metadata.strategy_id,
                    "display_name": metadata.display_name,
                    "version": metadata.version,
                    "description": metadata.description,
                }
    except Exception as e:
        logger.error(f"Failed to extract strategy plugin metadata: {e}")

    return None


def _reload_strategy_loader() -> None:
    """Best-effort in-process reload after strategy plugin file changes -
    same pattern as _reload_energy_provider_loader, no restart needed."""
    try:
        from core.strategy_loader import get_strategy_loader

        loader = get_strategy_loader()
        loader.load_all()
    except Exception as e:
        logger.warning(f"Strategy loader reload skipped: {e}")


def _compare_versions(current_version: Optional[str], available_version: str) -> str:
    """Compare semantic versions and return status."""
    if not current_version:
        return "not_installed"

    if current_version == available_version:
        return "up_to_date"

    if current_version == "unknown":
        return "update_available"

    try:
        current_parts = [int(x) for x in current_version.split('.')]
        available_parts = [int(x) for x in available_version.split('.')]
        return "update_available" if available_parts > current_parts else "up_to_date"
    except Exception:
        return "update_available"


def _reload_energy_provider_loader() -> None:
    """Best-effort in-process reload after provider file changes."""
    try:
        from providers.energy.loader import get_energy_provider_loader

        loader = get_energy_provider_loader()
        loader.load_all()
    except Exception as e:
        logger.warning(f"Energy provider loader reload skipped: {e}")


@router.get("/status", response_model=List[DriverInfo])
async def get_driver_status():
    """
    Check status of all drivers - compare bundled vs deployed versions.
    
    Returns list of all bundled drivers (pool + miner) with their current status:
    - up_to_date: Installed and matches bundled version
    - update_available: Installed but bundled version is newer
    - not_installed: Available in bundle but not in /config
    """
    drivers = []
    
    # Process pool drivers
    if BUNDLED_POOL_DRIVERS_PATH.exists():
        for bundled_file in BUNDLED_POOL_DRIVERS_PATH.glob("*_driver.py"):
            driver_info = _get_driver_info(bundled_file, CONFIG_POOL_DRIVERS_PATH, "pool")
            if driver_info:
                drivers.append(driver_info)
    
    # Process miner drivers
    if BUNDLED_MINER_DRIVERS_PATH.exists():
        for bundled_file in BUNDLED_MINER_DRIVERS_PATH.glob("*_driver.py"):
            driver_info = _get_driver_info(bundled_file, CONFIG_MINER_DRIVERS_PATH, "miner")
            if driver_info:
                drivers.append(driver_info)
    
    return drivers


def _get_driver_info(bundled_file: Path, config_path: Path, category: str) -> Optional[DriverInfo]:
    """Helper function to get driver info for a single driver"""
    driver_name = bundled_file.name
    config_file = config_path / driver_name
    
    # Load bundled driver module
    bundled_module = load_driver_module(bundled_file)
    if not bundled_module:
        logger.warning(f"Failed to load bundled {category} driver: {driver_name}")
        return None
    
    available_version = get_driver_version(bundled_module)
    driver_metadata = get_driver_info_from_module(bundled_module)
    
    # Check if driver is installed in /config
    current_version = None
    status = "not_installed"
    
    if config_file.exists():
        config_module = load_driver_module(config_file)
        if config_module:
            current_version = get_driver_version(config_module)
            
            # Compare versions
            if current_version == available_version:
                status = "up_to_date"
            elif current_version == "unknown":
                status = "update_available"  # Old driver without version
            else:
                # Simple version comparison (assumes semantic versioning)
                try:
                    current_parts = [int(x) for x in current_version.split('.')]
                    available_parts = [int(x) for x in available_version.split('.')]
                    
                    if available_parts > current_parts:
                        status = "update_available"
                    else:
                        status = "up_to_date"
                except:
                    # If version parsing fails, assume update available
                    status = "update_available"
    
    return DriverInfo(
        name=driver_name,
        driver_type=driver_metadata["driver_type"],
        category=category,
        display_name=driver_metadata["display_name"],
        current_version=current_version,
        available_version=available_version,
        status=status,
        description=driver_metadata["description"]
    )


@router.get("/energy-providers/status", response_model=List[EnergyProviderInfo])
async def get_energy_provider_status():
    """
    Check status of all bundled energy providers vs deployed providers in /config.
    """
    providers: List[EnergyProviderInfo] = []

    if not BUNDLED_ENERGY_PROVIDERS_PATH.exists():
        return providers

    for bundled_file in BUNDLED_ENERGY_PROVIDERS_PATH.glob("*_provider.py"):
        provider_name = bundled_file.name
        config_file = CONFIG_ENERGY_PROVIDERS_PATH / provider_name

        bundled_module = load_driver_module(bundled_file)
        if not bundled_module:
            logger.warning(f"Failed to load bundled energy provider: {provider_name}")
            continue

        available_version = get_driver_version(bundled_module)
        metadata = get_energy_provider_info_from_module(bundled_module)

        current_version: Optional[str] = None
        if config_file.exists():
            config_module = load_driver_module(config_file)
            if config_module:
                current_version = get_driver_version(config_module)

        status = _compare_versions(current_version, available_version)

        providers.append(
            EnergyProviderInfo(
                name=provider_name,
                provider_id=metadata["provider_id"],
                display_name=metadata["display_name"],
                current_version=current_version,
                available_version=available_version,
                status=status,
                description=metadata["description"],
                supported_regions=metadata.get("supported_regions") or [],
            )
        )

    return providers


@router.post("/energy-providers/update/{provider_name}", response_model=EnergyProviderUpdateResponse)
async def update_energy_provider(provider_name: str, db: AsyncSession = Depends(get_db)):
    """
    Update/install a specific energy provider by copying from bundled to /config.
    """
    if not provider_name.endswith("_provider.py"):
        raise HTTPException(status_code=400, detail="Invalid provider name format")

    bundled_file = BUNDLED_ENERGY_PROVIDERS_PATH / provider_name
    config_file = CONFIG_ENERGY_PROVIDERS_PATH / provider_name

    if not bundled_file.exists():
        raise HTTPException(status_code=404, detail=f"Energy provider {provider_name} not found in bundle")

    old_version: Optional[str] = None
    if config_file.exists():
        old_module = load_driver_module(config_file)
        if old_module:
            old_version = get_driver_version(old_module)

    new_module = load_driver_module(bundled_file)
    if not new_module:
        raise HTTPException(status_code=500, detail=f"Failed to load bundled provider {provider_name}")

    new_version = get_driver_version(new_module)

    try:
        CONFIG_ENERGY_PROVIDERS_PATH.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundled_file, config_file)
        _reload_energy_provider_loader()

        await log_audit(
            db=db,
            action="update" if old_version else "install",
            resource_type="energy_provider",
            resource_name=provider_name,
            changes={
                "version": {
                    "before": old_version or "not installed",
                    "after": new_version
                }
            }
        )

        return EnergyProviderUpdateResponse(
            success=True,
            message=f"Energy provider {provider_name} updated successfully.",
            provider_name=provider_name,
            old_version=old_version,
            new_version=new_version,
        )
    except Exception as e:
        logger.error(f"Failed to update energy provider {provider_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/energy-providers/update-all")
async def update_all_energy_providers(db: AsyncSession = Depends(get_db)):
    """Update all energy providers that have updates available."""
    status = await get_energy_provider_status()

    updated = []
    failed = []

    for provider in status:
        if provider.status == "update_available":
            try:
                result = await update_energy_provider(provider.name, db)
                updated.append({
                    "name": provider.name,
                    "provider_id": provider.provider_id,
                    "old_version": result.old_version,
                    "new_version": result.new_version,
                })
            except Exception as e:
                failed.append({
                    "name": provider.name,
                    "provider_id": provider.provider_id,
                    "error": str(e),
                })

    return {
        "success": len(failed) == 0,
        "updated": updated,
        "failed": failed,
        "message": (
            f"Updated {len(updated)} energy provider(s). {len(failed)} failed."
            if failed
            else f"Successfully updated {len(updated)} energy provider(s)."
        ),
    }


@router.get("/strategies/status", response_model=List[StrategyPluginInfo])
async def get_strategy_plugin_status():
    """
    Check status of all bundled strategy plugins vs deployed plugins in /config.
    """
    plugins: List[StrategyPluginInfo] = []

    if not BUNDLED_STRATEGIES_PATH.exists():
        return plugins

    for bundled_file in BUNDLED_STRATEGIES_PATH.glob("*_strategy.py"):
        plugin_name = bundled_file.name
        config_file = CONFIG_STRATEGIES_PATH / plugin_name

        bundled_module = load_driver_module(bundled_file)
        if not bundled_module:
            logger.warning(f"Failed to load bundled strategy plugin: {plugin_name}")
            continue

        metadata = get_strategy_info_from_module(bundled_module)
        if not metadata:
            logger.warning(f"No StrategyPlugin subclass found in bundled {plugin_name}")
            continue

        available_version = metadata["version"]

        current_version: Optional[str] = None
        if config_file.exists():
            config_module = load_driver_module(config_file)
            if config_module:
                config_metadata = get_strategy_info_from_module(config_module)
                if config_metadata:
                    current_version = config_metadata["version"]

        status = _compare_versions(current_version, available_version)

        plugins.append(
            StrategyPluginInfo(
                name=plugin_name,
                strategy_id=metadata["strategy_id"],
                display_name=metadata["display_name"],
                current_version=current_version,
                available_version=available_version,
                status=status,
                description=metadata["description"],
            )
        )

    return plugins


@router.post("/strategies/update/{strategy_name}", response_model=StrategyPluginUpdateResponse)
async def update_strategy_plugin(strategy_name: str, db: AsyncSession = Depends(get_db)):
    """
    Update/install a specific strategy plugin by copying from bundled to
    /config/strategies, then reloading the strategy loader in-process - no
    restart needed, same as energy providers.
    """
    if not strategy_name.endswith("_strategy.py"):
        raise HTTPException(status_code=400, detail="Invalid strategy plugin name format")

    bundled_file = BUNDLED_STRATEGIES_PATH / strategy_name
    config_file = CONFIG_STRATEGIES_PATH / strategy_name

    if not bundled_file.exists():
        raise HTTPException(status_code=404, detail=f"Strategy plugin {strategy_name} not found in bundle")

    old_version: Optional[str] = None
    if config_file.exists():
        old_module = load_driver_module(config_file)
        if old_module:
            old_metadata = get_strategy_info_from_module(old_module)
            if old_metadata:
                old_version = old_metadata["version"]

    new_module = load_driver_module(bundled_file)
    if not new_module:
        raise HTTPException(status_code=500, detail=f"Failed to load bundled strategy plugin {strategy_name}")

    new_metadata = get_strategy_info_from_module(new_module)
    if not new_metadata:
        raise HTTPException(status_code=500, detail=f"No StrategyPlugin subclass found in {strategy_name}")

    new_version = new_metadata["version"]

    try:
        CONFIG_STRATEGIES_PATH.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundled_file, config_file)
        _reload_strategy_loader()

        await log_audit(
            db=db,
            action="update" if old_version else "install",
            resource_type="strategy_plugin",
            resource_name=strategy_name,
            changes={
                "version": {
                    "before": old_version or "not installed",
                    "after": new_version,
                }
            },
        )

        return StrategyPluginUpdateResponse(
            success=True,
            message=f"Strategy plugin {strategy_name} updated successfully.",
            strategy_name=strategy_name,
            old_version=old_version,
            new_version=new_version,
        )
    except Exception as e:
        logger.error(f"Failed to update strategy plugin {strategy_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/strategies/update-all")
async def update_all_strategy_plugins(db: AsyncSession = Depends(get_db)):
    """Update all strategy plugins that have updates available."""
    status = await get_strategy_plugin_status()

    updated = []
    failed = []

    for plugin in status:
        if plugin.status == "update_available":
            try:
                result = await update_strategy_plugin(plugin.name, db)
                updated.append({
                    "name": plugin.name,
                    "strategy_id": plugin.strategy_id,
                    "old_version": result.old_version,
                    "new_version": result.new_version,
                })
            except Exception as e:
                failed.append({
                    "name": plugin.name,
                    "strategy_id": plugin.strategy_id,
                    "error": str(e),
                })

    return {
        "success": len(failed) == 0,
        "updated": updated,
        "failed": failed,
        "message": (
            f"Updated {len(updated)} strategy plugin(s). {len(failed)} failed."
            if failed
            else f"Successfully updated {len(updated)} strategy plugin(s)."
        ),
    }


@router.post("/update/{category}/{driver_name}", response_model=DriverUpdateResponse)
async def update_driver(category: str, driver_name: str, db: AsyncSession = Depends(get_db)):
    """
    Update a specific driver by copying from bundled to /config.
    
    Args:
        category: "pool" or "miner"
        driver_name: Name of the driver file (e.g., "mmfp_driver.py")
    """
    # Validate category
    if category not in ["pool", "miner"]:
        raise HTTPException(status_code=400, detail="Category must be 'pool' or 'miner'")
    
    # Validate driver name
    if not driver_name.endswith("_driver.py"):
        raise HTTPException(status_code=400, detail="Invalid driver name format")
    
    # Set paths based on category
    if category == "pool":
        bundled_file = BUNDLED_POOL_DRIVERS_PATH / driver_name
        config_file = CONFIG_POOL_DRIVERS_PATH / driver_name
        config_path = CONFIG_POOL_DRIVERS_PATH
    else:  # miner
        bundled_file = BUNDLED_MINER_DRIVERS_PATH / driver_name
        config_file = CONFIG_MINER_DRIVERS_PATH / driver_name
        config_path = CONFIG_MINER_DRIVERS_PATH
    
    if not bundled_file.exists():
        raise HTTPException(status_code=404, detail=f"Driver {driver_name} not found in {category} bundle")
    
    # Get old version before update
    old_version = None
    if config_file.exists():
        old_module = load_driver_module(config_file)
        if old_module:
            old_version = get_driver_version(old_module)
    
    # Get new version
    new_module = load_driver_module(bundled_file)
    if not new_module:
        raise HTTPException(status_code=500, detail=f"Failed to load bundled driver {driver_name}")
    
    new_version = get_driver_version(new_module)
    
    try:
        # Ensure config directory exists
        config_path.mkdir(parents=True, exist_ok=True)
        
        # Copy driver file
        shutil.copy2(bundled_file, config_file)
        logger.info(f"Updated {category} driver {driver_name} from {old_version or 'not installed'} to {new_version}")
        
        # Log audit event
        await log_audit(
            db=db,
            action="update" if old_version else "install",
            resource_type=f"{category}_driver",
            resource_name=driver_name,
            changes={
                "version": {
                    "before": old_version or "not installed",
                    "after": new_version
                }
            }
        )
        
        return DriverUpdateResponse(
            success=True,
            message=f"Driver {driver_name} updated successfully. Restart required to load new version.",
            driver_name=driver_name,
            old_version=old_version,
            new_version=new_version
        )
    
    except Exception as e:
        logger.error(f"Failed to update driver {driver_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/update-all")
async def update_all_drivers(db: AsyncSession = Depends(get_db)):
    """
    Update all drivers (pool + miner) that have updates available.
    
    Returns summary of updated drivers.
    """
    status = await get_driver_status()
    
    updated = []
    failed = []
    
    for driver in status:
        if driver.status == "update_available":
            try:
                result = await update_driver(driver.category, driver.name, db)
                updated.append({
                    "name": driver.name,
                    "category": driver.category,
                    "old_version": result.old_version,
                    "new_version": result.new_version
                })
            except Exception as e:
                failed.append({
                    "name": driver.name,
                    "category": driver.category,
                    "error": str(e)
                })
    
    return {
        "success": len(failed) == 0,
        "updated": updated,
        "failed": failed,
        "message": f"Updated {len(updated)} driver(s). {len(failed)} failed." if failed else f"Successfully updated {len(updated)} driver(s). Restart required."
    }


@router.post("/install/{category}/{driver_name}", response_model=DriverUpdateResponse)
async def install_driver(category: str, driver_name: str, db: AsyncSession = Depends(get_db)):
    """
    Install a new driver from bundle (same as update, but semantically different).
    
    Args:
        category: "pool" or "miner"
        driver_name: Name of the driver file (e.g., "mmfp_driver.py")
    """
    # Installation is the same as updating - just copy from bundle
    return await update_driver(category, driver_name, db)


@router.get("/miner-telemetry-schema/status", response_model=TelemetrySchemaStatus)
async def get_miner_telemetry_schema_status():
    """Get bundled vs deployed telemetry schema document status and versions."""
    if not BUNDLED_MINER_SCHEMA_PATH.exists():
        return TelemetrySchemaStatus(
            name=MINER_SCHEMA_FILENAME,
            current_version=get_schema_version(CONFIG_MINER_SCHEMA_PATH),
            available_version=None,
            status="missing_bundle"
        )

    available_version = get_schema_version(BUNDLED_MINER_SCHEMA_PATH)
    current_version = get_schema_version(CONFIG_MINER_SCHEMA_PATH)

    if current_version is None:
        status = "not_installed"
    elif current_version == available_version:
        status = "up_to_date"
    else:
        status = "update_available"

    return TelemetrySchemaStatus(
        name=MINER_SCHEMA_FILENAME,
        current_version=current_version,
        available_version=available_version,
        status=status
    )


@router.post("/miner-telemetry-schema/update", response_model=TelemetrySchemaUpdateResponse)
async def update_miner_telemetry_schema(db: AsyncSession = Depends(get_db)):
    """Copy latest bundled telemetry schema document to /config/drivers/miners/."""
    if not BUNDLED_MINER_SCHEMA_PATH.exists():
        raise HTTPException(status_code=404, detail=f"Bundled schema file not found: {MINER_SCHEMA_FILENAME}")

    old_version = get_schema_version(CONFIG_MINER_SCHEMA_PATH)
    new_version = get_schema_version(BUNDLED_MINER_SCHEMA_PATH)

    try:
        CONFIG_MINER_DRIVERS_PATH.mkdir(parents=True, exist_ok=True)
        shutil.copy2(BUNDLED_MINER_SCHEMA_PATH, CONFIG_MINER_SCHEMA_PATH)

        await log_audit(
            db=db,
            action="update" if old_version else "install",
            resource_type="miner_telemetry_schema",
            resource_name=MINER_SCHEMA_FILENAME,
            changes={
                "version": {
                    "before": old_version or "not installed",
                    "after": new_version or "unknown"
                }
            }
        )

        return TelemetrySchemaUpdateResponse(
            success=True,
            message="Miner telemetry schema updated successfully.",
            name=MINER_SCHEMA_FILENAME,
            old_version=old_version,
            new_version=new_version
        )
    except Exception as e:
        logger.error(f"Failed to update telemetry schema document: {e}")
        raise HTTPException(status_code=500, detail=str(e))
