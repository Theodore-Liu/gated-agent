"""Signal layer: contract shape + Faber SMA arithmetic. Pure, no network."""
from __future__ import annotations

import pytest

from gated_agent.signals import SMA_DAYS, compute_signal


def closes_ending(last: float, base: float = 100.0) -> list[float]:
    return [base] * (SMA_DAYS - 1) + [last]


def test_contract_shape_exact():
    sig = compute_signal("SPY", closes_ending(104.0))
    assert set(sig.keys()) == {"symbol", "direction", "strength", "spot"}
    assert sig["symbol"] == "SPY"
    assert sig["direction"] in ("long", "short", "neutral")
    assert isinstance(sig["strength"], float) and 0.0 <= sig["strength"] <= 1.0
    assert isinstance(sig["spot"], float) and sig["spot"] == 104.0


def test_above_sma_is_long():
    sig = compute_signal("SPY", closes_ending(104.0))
    assert sig["direction"] == "long"
    assert 0.0 < sig["strength"] < 1.0


def test_below_sma_is_short():
    sig = compute_signal("QQQ", closes_ending(95.0))
    assert sig["direction"] == "short"
    assert 0.0 < sig["strength"] <= 1.0


def test_neutral_dead_band():
    sig = compute_signal("IWM", [100.0] * SMA_DAYS)
    assert sig["direction"] == "neutral"
    assert sig["strength"] == 0.0


def test_strength_saturates_at_one():
    sig = compute_signal("SPY", closes_ending(150.0))   # far above SMA
    assert sig["strength"] == 1.0


def test_insufficient_history_raises():
    with pytest.raises(ValueError):
        compute_signal("SPY", [100.0] * (SMA_DAYS - 1))


def test_universe_covers_etfs_and_single_names():
    """Competition universe: calm ETF core + two mega-cap single names with
    no earnings inside the 8/28-9/4 window (checked 2026-08-25). The mapper
    and gates are underlying-agnostic; this pins the demo scope."""
    from gated_agent.signals import UNIVERSE
    assert UNIVERSE == ("SPY", "QQQ", "IWM", "AAPL", "NVDA")
