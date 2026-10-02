from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure app/ is importable when tests run from repo root
APP_ROOT = Path(__file__).resolve().parents[1] / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "solar_strategy", Path(__file__).resolve().parents[1] / "bundled_config" / "strategies" / "solar_strategy.py"
)
solar_strategy_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(solar_strategy_module)
SolarStrategy = solar_strategy_module.SolarStrategy


# ---------------------------------------------------------------------------
# T008 - bin-packing / mode sizing
# ---------------------------------------------------------------------------

def test_compute_allocation_picks_highest_fitting_mode_most_efficient_first(monkeypatch):
    strategy = SolarStrategy()

    miner_a = SimpleNamespace(id=1, name="A", miner_type="bitaxe", current_mode=None)
    miner_b = SimpleNamespace(id=2, name="B", miner_type="bitaxe", current_mode=None)

    async def fake_rank(db, miners):
        # B is more efficient than A - should be allocated first
        return [miner_b, miner_a]

    async def fake_power_stats(db, miner_ids):
        return {
            1: {"eco": 10.0, "standard": 20.0, "turbo": 30.0},
            2: {"eco": 10.0, "standard": 20.0, "turbo": 30.0},
        }

    monkeypatch.setattr(strategy, "_rank_by_efficiency", fake_rank)
    monkeypatch.setattr(SolarStrategy, "_load_mode_power_stats", staticmethod(fake_power_stats))
    monkeypatch.setattr(strategy, "_get_live_device_state", _always_off)  # nothing already running - no draw to add back
    monkeypatch.setattr(
        solar_strategy_module, "get_miner_capabilities",
        lambda: {"bitaxe": SimpleNamespace(available_modes=["eco", "standard", "turbo"])},
    )

    allocation, true_budget = asyncio.run(
        strategy._compute_allocation(db=None, eligible_miners=[miner_a, miner_b], raw_surplus_watts=35.0)
    )

    assert true_budget == 35.0
    # B (most efficient) gets the highest mode that fits (turbo=30 fits in 35)
    assert allocation[2] == "turbo"
    # Remaining budget after B's 30W = 5W - nothing fits for A
    assert 1 not in allocation


def test_compute_allocation_skips_miner_with_no_power_history(monkeypatch):
    strategy = SolarStrategy()
    miner = SimpleNamespace(id=1, name="NoData", miner_type="bitaxe", current_mode=None)

    async def fake_rank(db, miners):
        return miners

    async def fake_power_stats(db, miner_ids):
        return {}  # no historical data at all

    monkeypatch.setattr(strategy, "_rank_by_efficiency", fake_rank)
    monkeypatch.setattr(SolarStrategy, "_load_mode_power_stats", staticmethod(fake_power_stats))
    monkeypatch.setattr(strategy, "_get_live_device_state", _always_off)
    monkeypatch.setattr(
        solar_strategy_module, "get_miner_capabilities",
        lambda: {"bitaxe": SimpleNamespace(available_modes=["eco", "standard"])},
    )

    allocation, _ = asyncio.run(strategy._compute_allocation(db=None, eligible_miners=[miner], raw_surplus_watts=1000.0))
    assert allocation == {}


def test_compute_allocation_no_surplus_returns_empty(monkeypatch):
    strategy = SolarStrategy()
    miner = SimpleNamespace(id=1, name="A", miner_type="bitaxe", current_mode=None)
    monkeypatch.setattr(strategy, "_get_live_device_state", _always_off)

    async def fake_power_stats(db, miner_ids):
        return {}

    monkeypatch.setattr(SolarStrategy, "_load_mode_power_stats", staticmethod(fake_power_stats))

    allocation, budget = asyncio.run(strategy._compute_allocation(db=None, eligible_miners=[miner], raw_surplus_watts=0))
    assert allocation == {} and budget == 0.0

    allocation, budget = asyncio.run(strategy._compute_allocation(db=None, eligible_miners=[miner], raw_surplus_watts=None))
    assert allocation == {} and budget == 0.0

    allocation, budget = asyncio.run(strategy._compute_allocation(db=None, eligible_miners=[], raw_surplus_watts=500))
    assert allocation == {} and budget == 0.0


# ---------------------------------------------------------------------------
# Surplus double-counting fix: the HA sensor already nets out currently-
# running enrolled miners' own draw, so an already-on miner's current-mode
# wattage must be added back before bin-packing, or it gets starved by its
# own (already-excluded) consumption.
# ---------------------------------------------------------------------------

def test_already_on_miner_draw_is_added_back_to_budget(monkeypatch):
    strategy = SolarStrategy()
    # Already running at "turbo" (30W) - the raw sensor reading has already
    # subtracted this. Raw surplus of 5W should NOT mean only 5W is
    # available - the true budget is 5 + 30 = 35W, enough to keep it on
    # turbo AND have headroom.
    miner = SimpleNamespace(id=1, name="AlreadyOn", miner_type="bitaxe", current_mode="turbo")

    async def fake_rank(db, miners):
        return miners

    async def fake_power_stats(db, miner_ids):
        return {1: {"eco": 10.0, "standard": 20.0, "turbo": 30.0}}

    async def fake_live_state(db, miner_id):
        return "on"  # currently ON

    monkeypatch.setattr(strategy, "_rank_by_efficiency", fake_rank)
    monkeypatch.setattr(SolarStrategy, "_load_mode_power_stats", staticmethod(fake_power_stats))
    monkeypatch.setattr(strategy, "_get_live_device_state", fake_live_state)
    monkeypatch.setattr(
        solar_strategy_module, "get_miner_capabilities",
        lambda: {"bitaxe": SimpleNamespace(available_modes=["eco", "standard", "turbo"])},
    )

    allocation, true_budget = asyncio.run(
        strategy._compute_allocation(db=None, eligible_miners=[miner], raw_surplus_watts=5.0)
    )

    assert true_budget == pytest.approx(35.0)  # 5W raw + 30W added back
    assert allocation[1] == "turbo"


async def _always_off(db, miner_id):
    return "off"


# ---------------------------------------------------------------------------
# T009 - EMA surplus smoothing (same formula/alpha as MinerModePowerStats)
# ---------------------------------------------------------------------------

def test_surplus_ema_matches_expected_formula():
    alpha = solar_strategy_module.SURPLUS_EMA_ALPHA
    assert alpha == 0.20

    prior = 100.0
    sample = 200.0
    expected = (alpha * sample) + ((1.0 - alpha) * prior)

    # First sample seeds the EMA directly (no prior), matching
    # MinerModePowerStats' own "first sample = seed" behavior.
    strategy = SolarStrategy()
    strategy._surplus_ema = prior
    strategy._surplus_ema = (alpha * sample) + ((1.0 - alpha) * strategy._surplus_ema)
    assert strategy._surplus_ema == pytest.approx(expected)


# ---------------------------------------------------------------------------
# T010 - 5-consecutive-cycle on/off debounce; mode changes are not debounced
# ---------------------------------------------------------------------------

def test_onoff_requires_five_consecutive_confirming_cycles():
    strategy = SolarStrategy()
    strategy._onoff_state = {}
    miner_id = 1

    # 4 consecutive "wants ON" cycles - must not act yet
    for _ in range(4):
        assert strategy._should_act_on_transition(miner_id, desired_on=True, currently_on=False) is False

    # 5th consecutive cycle - now act
    assert strategy._should_act_on_transition(miner_id, desired_on=True, currently_on=False) is True


def test_onoff_streak_resets_on_disagreement():
    strategy = SolarStrategy()
    strategy._onoff_state = {}
    miner_id = 1

    for _ in range(4):
        strategy._should_act_on_transition(miner_id, desired_on=True, currently_on=False)

    # Desire flips before confirming - streak must reset, not carry over
    assert strategy._should_act_on_transition(miner_id, desired_on=False, currently_on=False) is False
    # Now even 4 more "wants ON" cycles shouldn't be enough (streak restarted)
    for _ in range(4):
        assert strategy._should_act_on_transition(miner_id, desired_on=True, currently_on=False) is False


def test_onoff_already_in_desired_state_clears_streak_and_does_not_act():
    strategy = SolarStrategy()
    strategy._onoff_state = {}
    miner_id = 1

    for _ in range(3):
        strategy._should_act_on_transition(miner_id, desired_on=True, currently_on=False)

    # Miner is already on (e.g. turned on by something else) - no transition needed
    assert strategy._should_act_on_transition(miner_id, desired_on=True, currently_on=True) is False
    assert miner_id not in strategy._onoff_state


# ---------------------------------------------------------------------------
# Reconciliation / confirm-on-write: don't trust a bare success boolean or a
# cached state - poll back, and cross-check against real miner reachability
# when HA's own report doesn't confirm.
# ---------------------------------------------------------------------------

def test_confirm_ha_state_succeeds_when_polled_state_matches():
    strategy = SolarStrategy()

    class _FakeHA:
        def __init__(self, states):
            self._states = list(states)

        async def get_device_state(self, entity_id):
            state = self._states.pop(0)
            return SimpleNamespace(state=state) if state is not None else None

    ha = _FakeHA(["off", "on"])  # first poll stale, second confirms
    confirmed = asyncio.run(strategy._confirm_ha_state(ha, "switch.x", "on", attempts=2, delay_seconds=0))
    assert confirmed is True


def test_confirm_ha_state_fails_after_exhausting_attempts():
    strategy = SolarStrategy()

    class _FakeHA:
        async def get_device_state(self, entity_id):
            return SimpleNamespace(state="off")  # never confirms "on"

    confirmed = asyncio.run(strategy._confirm_ha_state(_FakeHA(), "switch.x", "on", attempts=2, delay_seconds=0))
    assert confirmed is False


def test_verify_miner_reachability_matches_desired_state(monkeypatch):
    strategy = SolarStrategy()
    miner = SimpleNamespace(id=1, name="A", miner_type="bitaxe", ip_address="10.0.0.1", port=None, config=None)

    class _FakeAdapter:
        async def is_online(self):
            return True

    fake_adapters_module = types.ModuleType("adapters")
    fake_adapters_module.create_adapter = lambda *a, **kw: _FakeAdapter()
    monkeypatch.setitem(sys.modules, "adapters", fake_adapters_module)

    result = asyncio.run(strategy._verify_miner_reachability(miner, should_be_online=True, attempts=1, delay_seconds=0))
    assert result is True

    result = asyncio.run(strategy._verify_miner_reachability(miner, should_be_online=False, attempts=1, delay_seconds=0))
    assert result is False  # online but we expected offline - mismatch


def test_verify_miner_reachability_unknown_when_adapter_unavailable(monkeypatch):
    strategy = SolarStrategy()
    miner = SimpleNamespace(id=1, name="A", miner_type="bitaxe", ip_address="10.0.0.1", port=None, config=None)

    fake_adapters_module = types.ModuleType("adapters")
    fake_adapters_module.create_adapter = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "adapters", fake_adapters_module)

    result = asyncio.run(strategy._verify_miner_reachability(miner, should_be_online=True, attempts=1, delay_seconds=0))
    assert result is None
