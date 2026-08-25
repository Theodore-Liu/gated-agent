"""Risk gate arithmetic: max-loss pricing, 5% position cap, 2% daily halt,
idempotent dedup. Pure, no network."""
from __future__ import annotations

from gated_agent.gates import (
    daily_loss_halt_gate,
    dedup_gate,
    dedup_key,
    direction_flip_gate,
    estimate_max_loss,
    position_size_gate,
    run_gates,
)

STRIKES = {
    "C640": {"strike": 640.0, "type": "call"},
    "C655": {"strike": 655.0, "type": "call"},
    "P625": {"strike": 625.0, "type": "put"},
}


def test_max_loss_debit_spread():
    legs = [{"occ_symbol": "C640", "side": "buy", "qty": 2, "limit": 5.00},
            {"occ_symbol": "C655", "side": "sell", "qty": 2, "limit": 2.00}]
    # net debit 3.00 * 100 * 2
    assert estimate_max_loss(legs, STRIKES) == 600.0


def test_max_loss_credit_spread():
    legs = [{"occ_symbol": "C640", "side": "sell", "qty": 1, "limit": 3.00},
            {"occ_symbol": "C655", "side": "buy", "qty": 1, "limit": 1.00}]
    # width 15 -> 1500, credit 200 -> max loss 1300
    assert estimate_max_loss(legs, STRIKES) == 1300.0


def test_max_loss_cash_secured_put():
    legs = [{"occ_symbol": "P625", "side": "sell", "qty": 1, "limit": 2.00}]
    # strike*100 - premium = 62500 - 200
    assert estimate_max_loss(legs, STRIKES) == 62300.0


def test_max_loss_unknown_structure_is_none():
    naked_call = [{"occ_symbol": "C640", "side": "sell", "qty": 1, "limit": 3.0}]
    assert estimate_max_loss(naked_call, STRIKES) is None
    missing = [{"occ_symbol": "NOPE", "side": "buy", "qty": 1, "limit": 1.0}]
    assert estimate_max_loss(missing, STRIKES) is None


def test_position_gate_five_percent_boundary():
    equity = 100_000.0
    assert position_size_gate(5_000.0, equity).allowed          # == 5% passes
    assert not position_size_gate(5_000.01, equity).allowed     # > 5% vetoed


def test_position_gate_fails_closed_on_unpriceable():
    assert not position_size_gate(None, 100_000.0).allowed


def test_daily_loss_halt_two_percent():
    equity = 100_000.0
    assert daily_loss_halt_gate(-1_999.0, equity).allowed
    assert not daily_loss_halt_gate(-2_000.0, equity).allowed   # halt at -2%
    assert not daily_loss_halt_gate(-9_999.0, equity).allowed


def test_dedup_key_stable_and_distinct():
    legs = [{"occ_symbol": "C640", "side": "buy", "qty": 1, "limit": 5.0}]
    a = dedup_key("2026-08-24", "SPY", legs)
    b = dedup_key("2026-08-24", "SPY", list(legs))
    assert a == b                                   # same order, same key
    assert a != dedup_key("2026-08-25", "SPY", legs)  # new day, new key
    assert a != dedup_key("2026-08-24", "QQQ", legs)  # other symbol, new key


def test_dedup_gate_vetoes_seen():
    assert dedup_gate("abc", already_seen=False).allowed
    assert not dedup_gate("abc", already_seen=True).allowed


def test_flip_gate_vetoes_reverse_open():
    # contract v1: while a long position is open, a short open is refused
    # until the exit rules have closed it (and vice versa)
    assert not direction_flip_gate("short", "long").allowed
    assert not direction_flip_gate("long", "short").allowed


def test_flip_gate_allows_same_direction_and_flat():
    assert direction_flip_gate("long", "long").allowed     # add-on/re-entry:
    assert direction_flip_gate("short", "short").allowed   # dedup gate's job
    assert direction_flip_gate("long", None).allowed       # flat book
    assert direction_flip_gate("neutral", "long").allowed  # neutral never opens


def test_run_gates_includes_flip_verdict():
    legs = [{"occ_symbol": "C640", "side": "buy", "qty": 1, "limit": 5.0}]
    allowed, results, _ = run_gates(
        legs=legs, strikes=STRIKES, equity=100_000.0, realized_pnl_today=0.0,
        key="k", already_seen=False, direction="short", open_direction="long")
    assert not allowed
    by_gate = {r.gate: r for r in results}
    assert not by_gate["direction_flip"].allowed
    assert "no hedged positions" in by_gate["direction_flip"].reason
