"""2026-09-01, live competition account: two exits triggered and neither
executed.

    14:00Z  QQQ  R3_stop_loss    ValueError: all legs must share the same qty
    19:15Z  AAPL R2_take_profit  ValueError: all legs must share the same qty

Every underlying had taken a legal same-direction re-entry on 08-31, so each
book was two spreads. `close_legs` reversed all four legs as ONE order; the
two spreads were sized differently (AAPL 3 + 2, QQQ 2 + 1) and `submit_legs`
refused. IWM closed in the same round only because its lots were 4 and 4.
NVDA's two spreads even share the long strike, so the broker showed 6/-3/-3
— unclosable by the same check, and read as "direction unknown" every round.

The book shapes below are the account's actual positions on 09-01 evening.
Fully offline: the executor is injected."""
from __future__ import annotations

from datetime import date

import pytest

from gated_agent import position_manager as pm
from gated_agent.cli_executor import ExecResult
from gated_agent.ledger import Ledger

TODAY = date(2026, 9, 1)
RUN = TODAY.isoformat()

AAPL = [  # 3-lot 320/327.5 exp 09-04  +  2-lot 317.5/327.5 exp 09-11
    {"occ_symbol": "AAPL260904C00320000", "qty": 3, "entry": 3.80},
    {"occ_symbol": "AAPL260904C00327500", "qty": -3, "entry": 1.21},
    {"occ_symbol": "AAPL260911C00317500", "qty": 2, "entry": 5.10},
    {"occ_symbol": "AAPL260911C00327500", "qty": -2, "entry": 1.69},
]
AAPL_MIDS = {"AAPL260904C00320000": 6.35, "AAPL260904C00327500": 2.25,
             "AAPL260911C00317500": 9.85, "AAPL260911C00327500": 4.90}

NVDA = [  # two 3-lot spreads sharing the 220 long: 220/227.5 and 220/230
    {"occ_symbol": "NVDA260911C00220000", "qty": 6, "entry": 4.60},
    {"occ_symbol": "NVDA260911C00227500", "qty": -3, "entry": 1.76},
    {"occ_symbol": "NVDA260911C00230000", "qty": -3, "entry": 1.72},
]
NVDA_MIDS = {"NVDA260911C00220000": 3.25, "NVDA260911C00227500": 1.14,
             "NVDA260911C00230000": 0.75}

QQQ = [  # 2-lot exp 09-08  +  1-lot exp 09-11
    {"occ_symbol": "QQQ260908C00715000", "qty": 2, "entry": 6.36},
    {"occ_symbol": "QQQ260908C00725000", "qty": -2, "entry": 2.17},
    {"occ_symbol": "QQQ260911C00718000", "qty": 1, "entry": 8.29},
    {"occ_symbol": "QQQ260911C00731000", "qty": -1, "entry": 2.87},
]
QQQ_MIDS = {"QQQ260908C00715000": 2.80, "QQQ260908C00725000": 0.56,
            "QQQ260911C00718000": 3.61, "QQQ260911C00731000": 0.77}


def _legs(positions):
    return pm.group_structures(positions).popitem()[1]


def _equal_qty(order):
    return len({l["qty"] for l in order}) == 1


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "STATE", tmp_path / ".position_state.json")
    monkeypatch.setattr(pm, "LOG", tmp_path / "close_log.jsonl")
    calls: list = []

    def executor(legs, *, dry_run=True, **_):
        calls.append(legs)
        return ExecResult(True, dry_run, {"legs": legs}, "captured")

    return {"ledger": Ledger(tmp_path / "l.jsonl"), "calls": calls,
            "executor": executor}


def test_mixed_lot_book_unwinds_as_one_order_per_vertical():
    orders = pm.unwind_orders(_legs(AAPL), AAPL_MIDS)
    assert len(orders) == 2 and all(_equal_qty(o) for o in orders)
    assert [o[0]["qty"] for o in orders] == [3, 2]
    for o in orders:                       # each is a plain vertical close
        assert {l["position_intent"] for l in o} == {"sell_to_close",
                                                     "buy_to_close"}
        assert len({l["occ_symbol"][4:10] for l in o}) == 1   # one expiry
    orders = pm.unwind_orders(_legs(QQQ), QQQ_MIDS)
    assert [o[0]["qty"] for o in orders] == [2, 1]


def test_shared_strike_book_is_split_into_its_two_spreads():
    orders = pm.unwind_orders(_legs(NVDA), NVDA_MIDS)
    assert len(orders) == 2 and all(_equal_qty(o) for o in orders)
    syms = [sorted(l["occ_symbol"] for l in o) for o in orders]
    assert syms == [["NVDA260911C00220000", "NVDA260911C00227500"],
                    ["NVDA260911C00220000", "NVDA260911C00230000"]]
    assert all(l["qty"] == 3 for o in orders for l in o)
    # and the direction that was "unknown" for two days is readable
    assert pm.book_direction(_legs(NVDA)) == "long"
    # the source positions are not mutated by the peel
    assert [l["qty"] for l in NVDA] == [6, -3, -3]


def test_butterfly_still_has_no_direction():
    legs = _legs([
        {"occ_symbol": "SPY260911C00760000", "qty": 1, "entry": 9.0},
        {"occ_symbol": "SPY260911C00770000", "qty": -2, "entry": 4.0},
        {"occ_symbol": "SPY260911C00780000", "qty": 1, "entry": 1.0}])
    assert pm.book_direction(legs) is None
    assert len(pm.unwind_orders(legs, {})) == 2


def test_equal_lot_spread_is_still_one_order():
    """The shape every existing test uses: unchanged, one submission."""
    legs = _legs(AAPL[:2])
    assert len(pm.unwind_orders(legs, AAPL_MIDS)) == 1


def test_take_profit_on_a_mixed_lot_book_reaches_the_broker(sandbox):
    """The AAPL 19:15Z record replayed: R2 fires, and now every vertical goes
    out, exec_ok is true, position_closed is written, P&L is exact."""
    recs = pm.check_positions(dry_run=False, today=TODAY,
                              positions=AAPL, mids=AAPL_MIDS,
                              executor=sandbox["executor"],
                              ledger=sandbox["ledger"], run_date=RUN)
    (rec,) = recs
    assert rec["rule"] == "R2_take_profit" and rec["exec_ok"] is True
    assert len(sandbox["calls"]) == 2
    assert all(_equal_qty(o) for o in sandbox["calls"])
    assert len(rec["legs"]) == 4 and len(rec["orders"]) == 2
    # leg by leg: 3x(6.35-3.80) - 3x(2.25-1.21) + 2x(9.85-5.10) - 2x(4.90-1.69)
    assert rec["pnl"] == pytest.approx(
        100 * (3 * 2.55 - 3 * 1.04 + 2 * 4.75 - 2 * 3.21), abs=0.01)
    closed = [r for r in sandbox["ledger"].day(RUN, "live")
              if r["kind"] == "position_closed"]
    assert len(closed) == 1 and closed[0]["pnl"] == rec["pnl"]


def test_partial_unwind_is_loud_and_never_position_closed(sandbox):
    """Second vertical refused by the broker: the first is already out. That
    must be reported as a partial unwind, not swallowed as one failure, and
    the flip guard must stay engaged (no position_closed)."""
    ledger = sandbox["ledger"]
    n = {"i": 0}

    def flaky(legs, *, dry_run=True, **_):
        n["i"] += 1
        if n["i"] == 2:
            raise RuntimeError("broker said no")
        return ExecResult(True, dry_run, {}, "ok")

    (rec,) = pm.check_positions(dry_run=False, today=TODAY,
                                positions=AAPL, mids=AAPL_MIDS,
                                executor=flaky, ledger=ledger, run_date=RUN)
    assert rec["exec_ok"] is False
    assert "1/2 unwind orders refused" in rec["error"]
    assert "broker said no" in rec["error"]
    kinds = [r["kind"] for r in ledger.day(RUN, "live")]
    assert "position_closed" not in kinds
    err = [r for r in ledger.day(RUN, "live") if r["kind"] == "close_check_error"]
    assert len(err) == 1 and err[0]["partial"] is True
    assert err[0]["rule"] == "R2_take_profit"


def test_book_pnl_matches_structure_pnl_on_equal_lots():
    legs = _legs(AAPL[:2])
    v = pm.evaluate(legs, AAPL_MIDS, TODAY)
    assert pm.book_pnl(legs, AAPL_MIDS) == pytest.approx(
        pm.structure_pnl(legs, v["entry"], v["value"]), abs=0.01)
    assert pm.book_pnl(legs, {"AAPL260904C00320000": 6.35}) is None


def test_shared_strike_book_is_priced_by_contracts_not_by_leg():
    """NVDA on 09-01 read entry 1.12 (4.60 - 1.76 - 1.72, one count per
    leg) against a true per-spread cost of ~2.86: the 6-lot long was weighed
    the same as each 3-lot short. R2/R3 were being judged on that number."""
    v = pm.evaluate(_legs(NVDA), NVDA_MIDS, TODAY)
    assert v["entry"] == pytest.approx((4.60 * 6 - 1.76 * 3 - 1.72 * 3) / 6, abs=1e-4)
    assert v["value"] == pytest.approx((3.25 * 6 - 1.14 * 3 - 0.75 * 3) / 6, abs=1e-4)
    assert v["entry"] == pytest.approx(2.86, abs=1e-4)
    # an equal-lot vertical is numerically untouched: per-unit as before
    v1 = pm.evaluate(_legs(AAPL[:2]), AAPL_MIDS, TODAY)
    assert v1["entry"] == pytest.approx(3.80 - 1.21) and v1["value"] == pytest.approx(6.35 - 2.25)
    # a 3 + 2 book weighs the 3-lot more: ratio is the book's true ratio
    v2 = pm.evaluate(_legs(AAPL), AAPL_MIDS, TODAY)
    true_ratio = (3 * 4.10 + 2 * 4.95) / (3 * 2.59 + 2 * 3.41)
    assert v2["value"] / v2["entry"] == pytest.approx(true_ratio, abs=1e-3)
