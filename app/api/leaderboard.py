"""
Leaderboard API endpoints for Hall of Pain and Coin Hunter
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional, List, Union
from pydantic import BaseModel
from datetime import datetime
from urllib.parse import urlparse
import logging

import httpx

from core.database import get_db, BlockFound, Pool
from core.high_diff_tracker import get_leaderboard
from core.utils import format_hashrate

router = APIRouter()
logger = logging.getLogger(__name__)


def _extract_host(pool_url: str) -> str:
    raw = (pool_url or "").strip()
    if not raw:
        return ""

    to_parse = raw if "://" in raw else f"stratum+tcp://{raw}"
    parsed = urlparse(to_parse)
    if parsed.hostname:
        return parsed.hostname

    return raw.split(":")[0].split("/")[0]


def _worker_payout_address(worker: Optional[str]) -> Optional[str]:
    if not worker:
        return None
    candidate = worker.split(".", 1)[0].strip()
    return candidate or None


def _block_explorer_url(coin: str, block_hash: Optional[str]) -> Optional[str]:
    if not block_hash:
        return None
    symbol = (coin or "").upper()
    if symbol == "DGB":
        return f"https://digiexplorer.info/block/{block_hash}"
    if symbol == "BTC":
        return f"https://mempool.space/block/{block_hash}"
    if symbol == "BCH":
        return f"https://blockchair.com/bitcoin-cash/block/{block_hash}"
    return None


def _address_explorer_url(coin: str, address: Optional[str]) -> Optional[str]:
    if not address:
        return None
    symbol = (coin or "").upper()
    if symbol == "DGB":
        return f"https://digiexplorer.info/address/{address}"
    if symbol == "BTC":
        return f"https://mempool.space/address/{address}"
    if symbol == "BCH":
        return f"https://blockchair.com/bitcoin-cash/address/{address}"
    return None


class LeaderboardEntry(BaseModel):
    """Single leaderboard entry"""
    id: int  # Share ID for deletion
    rank: int
    miner_id: int
    miner_name: str
    miner_type: str
    coin: str
    pool_name: str
    difficulty: float
    difficulty_formatted: str  # e.g., "1.2M"
    network_difficulty: Optional[float]
    was_block_solve: bool
    percent_of_block: Optional[float]  # (share_diff / network_diff) * 100
    badge: Optional[str]  # 🔥 "So Close" / 🚨 "Pain" / 💀 "Emotional Damage"
    hashrate: Optional[Union[float, dict]]  # Structured format or plain numeric value
    hashrate_unit: str
    miner_mode: Optional[str]
    timestamp: datetime
    days_ago: int  # How many days ago this happened


class LeaderboardResponse(BaseModel):
    """Leaderboard API response"""
    entries: List[LeaderboardEntry]
    total_count: int
    filter_coin: Optional[str]
    filter_days: int


def format_difficulty(diff: float) -> str:
    """Format large difficulty numbers (e.g., 1234567 → 1.2M)"""
    if diff >= 1_000_000_000:
        return f"{diff/1_000_000_000:.1f}B"
    elif diff >= 1_000_000:
        return f"{diff/1_000_000:.1f}M"
    elif diff >= 1_000:
        return f"{diff/1_000:.1f}K"
    else:
        return f"{diff:.0f}"


@router.get("/leaderboard", response_model=LeaderboardResponse)
async def get_high_diff_leaderboard(
    days: int = Query(90, ge=1, le=365, description="Number of days to look back"),
    coin: Optional[str] = Query(None, description="Filter by coin (BTC/BCH/BC2/DGB)"),
    limit: int = Query(10, ge=1, le=50, description="Number of entries to return"),
    db: AsyncSession = Depends(get_db)
):
    """
    Get high difficulty share leaderboard
    
    - **days**: Number of days to look back (default 90)
    - **coin**: Filter by coin (BTC/BCH/BC2/DGB) or None for all
    - **limit**: Number of entries to return (default 10)
    """
    shares = await get_leaderboard(db, days=days, coin=coin, limit=limit)
    
    entries = []
    for rank, share in enumerate(shares, start=1):
        # Calculate days ago
        days_ago = (datetime.utcnow() - share.timestamp).days
        
        # Calculate percent of block if network diff available
        badge = None
        percent_of_block = None
        if share.network_difficulty and share.network_difficulty > 0:
            percent_of_block = (share.difficulty / share.network_difficulty) * 100
            
            # Only assign "pain" badges to shares that DIDN'T solve the block
            if not share.was_block_solve:
                # Assign badge based on how close to solving a block
                # Emotional Damage: >99% but <100% (SO close but didn't solve)
                if percent_of_block > 99 and percent_of_block < 100:
                    badge = "💀 Emotional Damage"
                elif percent_of_block >= 95:
                    badge = "🚨 Pain"
                elif percent_of_block >= 90:
                    badge = "🔥 So Close"
        
        entries.append(
            LeaderboardEntry(
                id=share.id,
                rank=rank,
                miner_id=share.miner_id,
                miner_name=share.miner_name,
                miner_type=share.miner_type,
                coin=share.coin,
                pool_name=share.pool_name,
                difficulty=share.difficulty,
                difficulty_formatted=format_difficulty(share.difficulty),
                network_difficulty=share.network_difficulty,
                was_block_solve=share.was_block_solve,
                percent_of_block=percent_of_block,
                badge=badge,
                hashrate=format_hashrate(share.hashrate, share.hashrate_unit) if share.hashrate else None,
                hashrate_unit=share.hashrate_unit,
                miner_mode=share.miner_mode,
                timestamp=share.timestamp,
                days_ago=days_ago
            )
        )
    
    return LeaderboardResponse(
        entries=entries,
        total_count=len(entries),
        filter_coin=coin,
        filter_days=days
    )


class CoinHunterEntry(BaseModel):
    """Coin Hunter leaderboard entry"""
    rank: int
    miner_id: int
    miner_name: str
    miner_type: str
    btc_blocks: int
    bch_blocks: int
    bc2_blocks: int
    dgb_blocks: int
    total_blocks: int
    total_score: int  # Weighted score: BTC=1000, BCH=100, BC2=50, DGB=1


class CoinHunterResponse(BaseModel):
    """Coin Hunter leaderboard response"""
    entries: List[CoinHunterEntry]
    total_miners: int
    scoring: dict  # {"BTC": 100, "BCH": 10, "DGB": 1}


class BlockVerificationEntry(BaseModel):
    source_pool_id: int
    source_pool_name: str
    id: int
    timestamp: str
    coin: str
    worker: Optional[str]
    payout_address: Optional[str]
    template_height: Optional[int]
    block_hash: str
    accepted_by_node: bool
    reject_reason: Optional[str]
    block_explorer_url: Optional[str]
    payout_explorer_url: Optional[str]


class BlockVerificationFeedResponse(BaseModel):
    total: int
    entries: List[BlockVerificationEntry]


@router.delete("/debug/blocks-found/{block_id}")
async def delete_blocks_found_entry(block_id: int, db: AsyncSession = Depends(get_db)):
    """
    Delete a block from blocks_found table (for removing duplicates)
    """
    from core.database import BlockFound
    from sqlalchemy import select
    
    result = await db.execute(
        select(BlockFound).where(BlockFound.id == block_id)
    )
    block = result.scalar_one_or_none()
    
    if not block:
        return {"error": f"Block {block_id} not found"}, 404
    
    await db.delete(block)
    await db.commit()
    
    return {
        "message": f"Deleted block {block_id}",
        "miner": block.miner_name,
        "difficulty": block.difficulty
    }


@router.get("/debug/blocks-found")
async def debug_blocks_found(db: AsyncSession = Depends(get_db)):
    """
    Debug endpoint to list all blocks in blocks_found table
    """
    from core.database import BlockFound
    from sqlalchemy import select
    
    result = await db.execute(
        select(BlockFound).order_by(BlockFound.timestamp.desc())
    )
    blocks = result.scalars().all()
    
    return {
        "total": len(blocks),
        "blocks": [{
            "id": b.id,
            "miner_id": b.miner_id,
            "miner_name": b.miner_name,
            "coin": b.coin,
            "block_height": b.block_height,
            "difficulty": b.difficulty,
            "network_difficulty": b.network_difficulty,
            "timestamp": b.timestamp.isoformat()
        } for b in blocks]
    }


@router.get("/leaderboard/verification-feed", response_model=BlockVerificationFeedResponse)
async def get_block_verification_feed(
    limit: int = Query(30, ge=1, le=200, description="Number of recent attempts to return"),
    accepted_only: bool = Query(True, description="Show only accepted-by-node block submissions"),
    db: AsyncSession = Depends(get_db)
):
    """
    Quick verification feed for recent Stratum block submissions.

    Aggregates recent `/debug/block-attempts` from enabled HMM-Local Stratum pools and
    enriches each entry with payout address (worker prefix) and explorer links.
    """
    result = await db.execute(
        select(Pool)
        .where(Pool.enabled == True)
        .where(Pool.pool_type == "hmm_local_stratum")
    )
    pools = result.scalars().all()
    if not pools:
        return BlockVerificationFeedResponse(total=0, entries=[])

    feed_entries: list[BlockVerificationEntry] = []
    seen_hosts: set[tuple[str, int]] = set()

    async with httpx.AsyncClient(timeout=12.0) as client:
        for pool in pools:
            host = _extract_host(pool.url)
            if not host:
                continue
            config = pool.pool_config or {}
            api_port = int(config.get("stratum_api_port", 8082))
            host_key = (host, api_port)
            if host_key in seen_hosts:
                continue
            seen_hosts.add(host_key)

            try:
                response = await client.get(
                    f"http://{host}:{api_port}/debug/block-attempts",
                    params={"n": max(limit * 2, 50)}
                )
                response.raise_for_status()
                payload = response.json()
                attempts = payload.get("attempts", []) if isinstance(payload, dict) else []
            except Exception as exc:
                logger.warning("Failed fetching stratum block attempts from %s:%s: %s", host, api_port, exc)
                continue

            for row in attempts:
                if not isinstance(row, dict):
                    continue
                accepted = bool(row.get("accepted_by_node"))
                if accepted_only and not accepted:
                    continue

                coin = str(row.get("coin") or "").upper()
                worker = row.get("worker")
                payout_address = _worker_payout_address(worker)
                block_hash = str(row.get("block_hash") or "")
                if not block_hash:
                    continue

                feed_entries.append(
                    BlockVerificationEntry(
                        source_pool_id=pool.id,
                        source_pool_name=pool.name or f"Pool {pool.id}",
                        id=int(row.get("id") or 0),
                        timestamp=str(row.get("ts") or ""),
                        coin=coin,
                        worker=str(worker) if worker is not None else None,
                        payout_address=payout_address,
                        template_height=int(row.get("template_height")) if row.get("template_height") is not None else None,
                        block_hash=block_hash,
                        accepted_by_node=accepted,
                        reject_reason=str(row.get("reject_reason")) if row.get("reject_reason") else None,
                        block_explorer_url=_block_explorer_url(coin, block_hash),
                        payout_explorer_url=_address_explorer_url(coin, payout_address),
                    )
                )

    feed_entries.sort(key=lambda e: e.timestamp, reverse=True)
    trimmed = feed_entries[:limit]
    return BlockVerificationFeedResponse(total=len(trimmed), entries=trimmed)


@router.get("/coin-hunter", response_model=CoinHunterResponse)
async def get_coin_hunter_leaderboard(
    db: AsyncSession = Depends(get_db)
):
    """
    Get Coin Hunter leaderboard - all-time blocks found with weighted scoring
    
    Scoring (based on normalized difficulty ratios):
    - BTC block = 1,000 points (208,639x harder than DGB)
    - BCH block = 100 points (1,343x harder than DGB)
    - BC2 block = 50 points (54x harder than DGB)
    - DGB block = 1 point (baseline)
    
    Note: Groups by miner_id and displays current miner name from miners table.
    This prevents duplicate entries when miners are renamed.
    """
    from core.database import Miner
    
    # Aggregate blocks by miner_id and coin (NOT by miner_name)
    # Quick-fix: include pending blocks so recent finds appear immediately.
    # Exclude only orphaned rows.
    query = select(
        BlockFound.miner_id,
        BlockFound.coin,
        func.count(BlockFound.id).label('block_count')
    ).where(
        BlockFound.status.in_(['confirmed', 'pending'])
    ).group_by(
        BlockFound.miner_id,
        BlockFound.coin
    )
    
    result = await db.execute(query)
    rows = result.all()
    
    # Get current miner names and types from miners table
    miner_query = select(Miner.id, Miner.name, Miner.miner_type)
    miner_result = await db.execute(miner_query)
    current_miners = {row.id: {'name': row.name, 'type': row.miner_type} for row in miner_result.all()}
    
    # Build miner stats dictionary
    miner_stats = {}
    for row in rows:
        miner_id = row.miner_id
        if miner_id not in miner_stats:
            # Use CURRENT name from miners table, fallback to "Unknown" if miner deleted
            miner_info = current_miners.get(miner_id, {'name': f'Unknown Miner {miner_id}', 'type': 'unknown'})
            miner_stats[miner_id] = {
                'miner_name': miner_info['name'],
                'miner_type': miner_info['type'],
                'BTC': 0,
                'BCH': 0,
                'BC2': 0,
                'DGB': 0
            }
        miner_stats[miner_id][row.coin] = row.block_count
    
    # Calculate scores and rankings
    # Hybrid scoring based on normalized difficulty ratios:
    # BTC: 208,639x harder → 1,000 pts | BCH: 1,343x harder → 100 pts
    # BC2: 54x harder → 50 pts | DGB: 1x baseline → 1 pt
    SCORING = {'BTC': 1000, 'BCH': 100, 'BC2': 50, 'DGB': 1}
    
    entries = []
    for miner_id, stats in miner_stats.items():
        btc = stats['BTC']
        bch = stats['BCH']
        bc2 = stats['BC2']
        dgb = stats['DGB']
        total_blocks = btc + bch + bc2 + dgb
        total_score = (btc * SCORING['BTC']) + (bch * SCORING['BCH']) + (bc2 * SCORING['BC2']) + (dgb * SCORING['DGB'])
        
        entries.append({
            'miner_id': miner_id,
            'miner_name': stats['miner_name'],
            'miner_type': stats['miner_type'],
            'btc_blocks': btc,
            'bch_blocks': bch,
            'bc2_blocks': bc2,
            'dgb_blocks': dgb,
            'total_blocks': total_blocks,
            'total_score': total_score
        })
    
    # Sort by score descending
    entries.sort(key=lambda x: x['total_score'], reverse=True)
    
    # Add rankings
    ranked_entries = []
    for rank, entry in enumerate(entries, start=1):
        ranked_entries.append(
            CoinHunterEntry(
                rank=rank,
                miner_id=entry['miner_id'],
                miner_name=entry['miner_name'],
                miner_type=entry['miner_type'],
                btc_blocks=entry['btc_blocks'],
                bch_blocks=entry['bch_blocks'],
                bc2_blocks=entry['bc2_blocks'],
                dgb_blocks=entry['dgb_blocks'],
                total_blocks=entry['total_blocks'],
                total_score=entry['total_score']
            )
        )
    
    return CoinHunterResponse(
        entries=ranked_entries,
        total_miners=len(ranked_entries),
        scoring=SCORING
    )


@router.post("/leaderboard/backfill-difficulty")
async def backfill_network_difficulty_endpoint(db: AsyncSession = Depends(get_db)):
    """
    Backfill network difficulty for existing shares that don't have it.
    This is a one-time operation to populate % of Block calculations for historical shares.
    """
    from core.high_diff_tracker import backfill_network_difficulty
    
    try:
        await backfill_network_difficulty(db)
        return {"message": "Network difficulty backfill completed successfully"}
    except Exception as e:
        return {"error": f"Backfill failed: {str(e)}"}, 500


@router.post("/leaderboard/sync-blocks")
async def sync_blocks_to_coin_hunter(db: AsyncSession = Depends(get_db)):
    """
    Sync block solves from high_diff_shares to blocks_found table (Coin Hunter leaderboard).
    Ensures all found blocks appear in Coin Hunter.
    """
    from core.high_diff_tracker import sync_block_solves_to_blocks_found
    
    try:
        await sync_block_solves_to_blocks_found(db)
        return {"message": "Block solves synced to Coin Hunter successfully"}
    except Exception as e:
        return {"error": f"Sync failed: {str(e)}"}, 500


@router.post("/leaderboard/{share_id}/update-network-difficulty")
async def update_network_difficulty(
    share_id: int, 
    network_difficulty: float,
    db: AsyncSession = Depends(get_db)
):
    """
    Update the network difficulty for a specific share.
    Useful for correcting stale cached values.
    """
    from core.database import HighDiffShare, BlockFound
    from sqlalchemy import select, update
    
    # Update high_diff_shares
    result = await db.execute(
        select(HighDiffShare).where(HighDiffShare.id == share_id)
    )
    share = result.scalar_one_or_none()
    
    if not share:
        return {"error": f"Share {share_id} not found"}, 404
    
    old_network_diff = share.network_difficulty
    share.network_difficulty = network_difficulty
    
    # Update matching block in blocks_found if it exists
    await db.execute(
        update(BlockFound)
        .where(
            BlockFound.miner_id == share.miner_id,
            BlockFound.coin == share.coin,
            BlockFound.difficulty == share.difficulty,
            BlockFound.timestamp == share.timestamp
        )
        .values(network_difficulty=network_difficulty)
    )
    
    await db.commit()
    
    new_percent = (share.difficulty / network_difficulty * 100) if network_difficulty else 0
    
    return {
        "message": f"Updated network difficulty for share {share_id}",
        "miner": share.miner_name,
        "old_network_diff": old_network_diff,
        "new_network_diff": network_difficulty,
        "share_diff": share.difficulty,
        "new_percent": f"{new_percent:.2f}%"
    }


@router.post("/leaderboard/{share_id}/mark-as-block")
async def mark_share_as_block(share_id: int, db: AsyncSession = Depends(get_db)):
    """
    Manually mark a share as a block solve (for cases where network difficulty was stale).
    This also adds it to the blocks_found table.
    """
    from core.database import HighDiffShare, BlockFound
    from sqlalchemy import select
    
    # Find the share
    result = await db.execute(
        select(HighDiffShare).where(HighDiffShare.id == share_id)
    )
    share = result.scalar_one_or_none()
    
    if not share:
        return {"error": f"Share {share_id} not found"}, 404
    
    # Update to mark as block solve
    share.was_block_solve = True
    
    # Check if already in blocks_found
    existing = await db.execute(
        select(BlockFound).where(
            BlockFound.miner_id == share.miner_id,
            BlockFound.timestamp == share.timestamp,
            BlockFound.difficulty == share.difficulty
        )
    )
    
    if not existing.scalar_one_or_none():
        # Add to blocks_found
        block = BlockFound(
            miner_id=share.miner_id,
            miner_name=share.miner_name,
            miner_type=share.miner_type,
            coin=share.coin,
            pool_name=share.pool_name,
            difficulty=share.difficulty,
            network_difficulty=share.difficulty,  # Use share diff since it solved the block
            block_height=None,
            block_reward=None,
            hashrate=share.hashrate,
            hashrate_unit=share.hashrate_unit,
            miner_mode=share.miner_mode,
            timestamp=share.timestamp
        )
        db.add(block)
    
    await db.commit()
    
    return {
        "message": f"Share {share_id} marked as block solve",
        "miner": share.miner_name,
        "coin": share.coin,
        "difficulty": share.difficulty
    }


@router.delete("/leaderboard/{share_id}")
async def delete_leaderboard_entry(share_id: int, db: AsyncSession = Depends(get_db)):
    """
    Delete a specific high difficulty share from the leaderboard.
    
    - **share_id**: ID of the share to delete
    """
    from core.database import HighDiffShare
    
    try:
        result = await db.execute(
            select(HighDiffShare).where(HighDiffShare.id == share_id)
        )
        share = result.scalar_one_or_none()
        
        if not share:
            return {"error": "Share not found"}, 404
        
        await db.delete(share)
        await db.commit()
        
        return {"message": f"Deleted share {share_id} successfully"}
    except Exception as e:
        await db.rollback()
        return {"error": f"Delete failed: {str(e)}"}, 500
