"""Unit tests for the pre-registered closing rules R1-R4 (pure evaluate()).
Ported verbatim from orchestra's staging suite; imports adapted to the
gated_agent package."""
from __future__ import annotations

from datetime import date

from gated_agent.position_manager import (
    close_legs,
    evaluate,
    group_structures,
    parse_occ,
)

TODAY = date(2026, 8, 31)


def _debit_legs(dte: int = 10):
    """Call debit spread: bought 764C @4.00, sold 783C @1.20 -> entry +2.80."""
    exp = date(2026, 9, 1 + dte - 1)
    ymd = exp.strftime("%y%m%d")
    return [
        {"occ_symbol": f"SPY{ymd}C00764000", "qty": 2, "entry": 4.00,
         "underlying": "SPY", "expiry": exp, "type": "call", "strike": 764.0},
        {"occ_symbol": f"SPY{ymd}C00783000", "qty": -2, "entry": 1.20,
         "underlying": "SPY", "expiry": exp, "type": "call", "strike": 783.0},
    ]


def _credit_legs(dte: int = 10):
    """Credit put spread: sold 745P @2.00, bought 744P @1.75 -> entry -0.25."""
    exp = date(2026, 9, 1 + dte - 1)
    ymd = exp.strftime("%y%m%d")
    return [
        {"occ_symbol": f"SPY{ymd}P00745000", "qty": -1, "entry": 2.00,
         "underlying": "SPY", "expiry": exp, "type": "put", "strike": 745.0},
        {"occ_symbol": f"SPY{ymd}P00744000", "qty": 1, "entry": 1.75,
         "underlying": "SPY", "expiry": exp, "type": "put", "strike": 744.0},
    ]


def _mids(legs, prices):
    return {l["occ_symbol"]: p for l, p in zip(legs, prices)}


# ---------- OCC parsing / grouping ----------

def test_parse_occ():
    d = parse_occ("SPY260918C00764000")
    assert d == {"underlying": "SPY", "expiry": date(2026, 9, 18),
                 "type": "call", "strike": 764.0}
    assert parse_occ("SPY") is None            # equity symbol -> not an option


def test_group_structures_by_underlying():
    legs = [{"occ_symbol": "SPY260918C00764000", "qty": 1, "entry": 1.0},
            {"occ_symbol": "QQQ260918P00500000", "qty": -1, "entry": 2.0},
            {"occ_symbol": "SPY260918C00783000", "qty": -1, "entry": 0.5}]
    g = group_structures(legs)
    assert set(g) == {"SPY", "QQQ"} and len(g["SPY"]) == 2


# ---------- R1 time gate ----------

def test_r1_time_gate_fires_at_dte_2():
    legs = _debit_legs(dte=2)
    v = evaluate(legs, _mids(legs, [4.0, 1.2]), TODAY)
    assert v["action"] == "close" and v["rule"] == "R1_time"


def test_r1_precedes_everything():
    legs = _debit_legs(dte=1)
    v = evaluate(legs, _mids(legs, [9.0, 1.0]), TODAY, flip=True)  # also TP+flip
    assert v["rule"] == "R1_time"


# ---------- R2 take profit ----------

def test_r2_debit_take_profit():
    legs = _debit_legs()                       # entry +2.80, target 4.20
    v = evaluate(legs, _mids(legs, [5.5, 1.2]), TODAY)   # value 4.30
    assert v["action"] == "close" and v["rule"] == "R2_take_profit"
    assert v["kind"] == "debit"


def test_r2_credit_take_profit():
    legs = _credit_legs()                      # entry -0.25, target -0.125
    v = evaluate(legs, _mids(legs, [0.60, 0.50]), TODAY)  # value -0.10
    assert v["action"] == "close" and v["rule"] == "R2_take_profit"
    assert v["kind"] == "credit"


# ---------- R3 stop loss ----------

def test_r3_debit_stop_loss():
    legs = _debit_legs()                       # entry +2.80, stop 1.40
    v = evaluate(legs, _mids(legs, [1.8, 0.5]), TODAY)    # value 1.30
    assert v["action"] == "close" and v["rule"] == "R3_stop_loss"


def test_r3_credit_stop_loss():
    legs = _credit_legs()                      # entry -0.25, stop -0.50
    v = evaluate(legs, _mids(legs, [1.90, 1.35]), TODAY)  # value -0.55
    assert v["action"] == "close" and v["rule"] == "R3_stop_loss"


# ---------- R4 signal flip ----------

def test_r4_flip_closes_when_no_other_rule():
    legs = _debit_legs()
    v = evaluate(legs, _mids(legs, [4.0, 1.2]), TODAY, flip=True)  # flat P&L
    assert v["action"] == "close" and v["rule"] == "R4_signal_flip"


def test_hold_when_nothing_triggers():
    legs = _debit_legs()
    v = evaluate(legs, _mids(legs, [4.0, 1.2]), TODAY)    # value == entry
    assert v["action"] == "hold" and v["rule"] is None


# ---------- quote-gap ladder ----------

def test_quote_gap_skips_then_force_closes():
    legs = _debit_legs()
    mids = _mids(legs, [4.0, None])            # short leg unquoted
    v1 = evaluate(legs, mids, TODAY, quote_gaps=0)
    assert v1["action"] == "skip" and v1["quote_gaps"] == 1
    v3 = evaluate(legs, mids, TODAY, quote_gaps=2)
    assert v3["action"] == "close" and v3["rule"] == "quote_gap"
    assert v3["order_type"] == "market"


def test_quote_gap_counter_resets_on_full_quote():
    legs = _debit_legs()
    v = evaluate(legs, _mids(legs, [4.0, 1.2]), TODAY, quote_gaps=2)
    assert v["action"] == "hold" and v["quote_gaps"] == 0


# ---------- unwind legs ----------

def test_close_legs_reverse_sides_and_intents():
    legs = _credit_legs()
    mids = _mids(legs, [1.00, 0.80])
    out = close_legs(legs, mids)
    by = {l["occ_symbol"]: l for l in out}
    short, long_ = legs[0]["occ_symbol"], legs[1]["occ_symbol"]
    assert by[short]["side"] == "buy" and by[short]["position_intent"] == "buy_to_close"
    assert by[long_]["side"] == "sell" and by[long_]["position_intent"] == "sell_to_close"
    assert all(l["qty"] == 1 for l in out)
    # closing a credit spread = paying a debit: net limit must be positive
    from gated_agent.cli_executor import _net_limit
    assert _net_limit(out) == 0.20


def test_close_legs_debit_unwind_is_credit():
    legs = _debit_legs()
    out = close_legs(legs, _mids(legs, [5.5, 1.2]))
    from gated_agent.cli_executor import _net_limit
    assert _net_limit(out) == -4.30            # selling the spread -> credit
