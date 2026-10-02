"""
Shared enrollment-exclusivity check between Price Band Strategy and Solar
Strategy - a miner may be enrolled in at most one of the two at a time.

This is the one deliberate, narrow touchpoint this feature makes on
existing Price Band Strategy code (its enrollment endpoint calls the
reject_conflicting_solar_enrollment() counterpart below) - not its
decision logic.
"""
from typing import Iterable, List

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import Miner, MinerStrategy, SolarMinerEnrollment


async def _names_for(db: AsyncSession, miner_ids: Iterable[int]) -> List[str]:
    miner_ids = list(miner_ids)
    if not miner_ids:
        return []
    result = await db.execute(select(Miner.name).where(Miner.id.in_(miner_ids)))
    return [row[0] for row in result.all()]


async def reject_conflicting_price_band_enrollment(db: AsyncSession, solar_miner_ids: List[int]) -> None:
    """Raise if any of solar_miner_ids is currently enrolled in Price Band Strategy."""
    if not solar_miner_ids:
        return

    result = await db.execute(
        select(MinerStrategy.miner_id)
        .where(MinerStrategy.miner_id.in_(solar_miner_ids))
        .where(MinerStrategy.strategy_enabled == True)
    )
    conflicting_ids = [row[0] for row in result.all()]
    if not conflicting_ids:
        return

    names = await _names_for(db, conflicting_ids)
    raise HTTPException(
        status_code=409,
        detail=(
            "Cannot enroll in Solar Strategy while enrolled in Price Band Strategy: "
            f"{', '.join(names) or conflicting_ids}. Unenroll from Price Band Strategy first."
        ),
    )


async def reject_conflicting_solar_enrollment(db: AsyncSession, price_band_miner_ids: List[int]) -> None:
    """Raise if any of price_band_miner_ids is currently enrolled in Solar Strategy."""
    if not price_band_miner_ids:
        return

    result = await db.execute(
        select(SolarMinerEnrollment.miner_id)
        .where(SolarMinerEnrollment.miner_id.in_(price_band_miner_ids))
        .where(SolarMinerEnrollment.enabled == True)
    )
    conflicting_ids = [row[0] for row in result.all()]
    if not conflicting_ids:
        return

    names = await _names_for(db, conflicting_ids)
    raise HTTPException(
        status_code=409,
        detail=(
            "Cannot enroll in Price Band Strategy while enrolled in Solar Strategy: "
            f"{', '.join(names) or conflicting_ids}. Unenroll from Solar Strategy first."
        ),
    )
