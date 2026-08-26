"""Integration: close rules R1-R4 running through the pipeline — unwind legs
reach the (injected) mleg executor, `position_closed` records coordinate with
the direction-flip gate, quote-gap state persists across rounds, and the
McpRedTeam fail-closed path vetoes through `process()`. Fully offline."""
from __future__ import annotations

from datetime import date

import pytest

from gated_agent import position_manager as pm
from gated_agent import redteam_mcp as rt
from gated_agent.cli_executor import ExecResult
from gated_agent.ledger import Ledger
from gated_agent.order_cli import StubCLIBroker
from gated_agent.redteam_mcp import McpRedTeam, StubRedTeam
from gated_agent.run import close_checks, process

TODAY = date(2026, 8, 31)
RUN_DATE = TODAY.isoformat()
LONG_SIG = {"symbol": "SPY", "direction": "long", "strength": 0.9,
            "spot": 640.0}
SHORT_SIG = {"symbol": "SPY", "direction": "short", "strength": 0.9,
             "spot": 640.0}

# Open call debit spread on SPY, 10 DTE from TODAY: entry +2.80 per unit.
EXP = date(2026, 9, 10)
YMD = EXP.strftime("%y%m%d")
POSITIONS = [
    {"occ_symbol": f"SPY{YMD}C00764000", "qty": 2, "entry": 4.00},
    {"occ_symbol": f"SPY{YMD}C00783000", "qty": -2, "entry": 1.20},
]
MIDS_TP = {f"SPY{YMD}C00764000": 5.50, f"SPY{YMD}C00783000": 1.20}   # V=4.30
MIDS_FLAT = {f"SPY{YMD}C00764000": 4.00, f"SPY{YMD}C00783000": 1.20}  # V=E
MIDS_GAP = {f"SPY{YMD}C00764000": 4.00, f"SPY{YMD}C00783000": None}


class AcceptedStubBroker(StubCLIBroker):
    """Stub whose orders come back ACCEPTED by the broker.

    Needed since the 08-26 review: a `dry_run` status opens nothing in the
    ledger's believed book, because a rehearsal on a box with real keys used
    to write an order_intent carrying a direction and froze every symbol
    behind the flip guard. Tests about the flip guard therefore have to set up
    a position the way a LIVE run does, not the way a rehearsal does.
    """

    def submit_order(self, symbol, legs, key):
        record = super().submit_order(symbol, legs, key)
        record["status"] = "submitted"
        return record


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Route position_manager's runtime state/log into tmp; capture executor
    calls instead of spawning the Alpaca CLI."""
    monkeypatch.setattr(pm, "STATE", tmp_path / ".position_state.json")
    monkeypatch.setattr(pm, "LOG", tmp_path / "close_log.jsonl")
    calls: list = []

    def executor(legs, *, dry_run=True, **_):
        calls.append(legs)
        return ExecResult(True, dry_run, {"legs": legs}, "captured")

    return {"ledger": Ledger(tmp_path / "l.jsonl"), "calls": calls,
            "executor": executor, "tmp": tmp_path}


def test_r2_close_flows_through_mleg_executor(sandbox):
    recs = close_checks({"SPY": dict(LONG_SIG)}, ledger=sandbox["ledger"],
                        run_date=RUN_DATE, today=TODAY,
                        positions=POSITIONS, mids=MIDS_TP,
                        executor=sandbox["executor"])
    assert len(recs) == 1 and recs[0]["rule"] == "R2_take_profit"
    (unwind,) = sandbox["calls"]                 # one atomic mleg submission
    intents = {l["occ_symbol"]: l["position_intent"] for l in unwind}
    assert intents[f"SPY{YMD}C00764000"] == "sell_to_close"
    assert intents[f"SPY{YMD}C00783000"] == "buy_to_close"
    closed = [r for r in sandbox["ledger"].day(RUN_DATE, "live")
              if r["kind"] == "position_closed"]
    assert len(closed) == 1 and closed[0]["symbol"] == "SPY"
    assert closed[0]["rule"] == "R2_take_profit"


def test_hold_writes_no_position_closed(sandbox):
    recs = close_checks({"SPY": dict(LONG_SIG)}, ledger=sandbox["ledger"],
                        run_date=RUN_DATE, today=TODAY,
                        positions=POSITIONS, mids=MIDS_FLAT,
                        executor=sandbox["executor"])
    assert recs[0]["action"] == "hold"
    assert sandbox["calls"] == []                # executor never touched
    assert all(r["kind"] != "position_closed"
               for r in sandbox["ledger"].records())


def test_flip_close_ordering_admits_reverse_entry(sandbox):
    """Day 1: open long. Day 2: reverse signal -> close checks run FIRST,
    R4 closes the long and records position_closed -> the flip gate then
    admits the short open in the same run."""
    broker, ledger = AcceptedStubBroker(dry_run=True), sandbox["ledger"]
    rt_stub = StubRedTeam()
    day1 = process("SPY", dict(LONG_SIG), "live", broker=broker, ledger=ledger,
                   redteam=rt_stub, run_date="2026-08-30",
                   today=date(2026, 8, 30))
    assert day1 is not None                      # long position on the books

    # day 2, reverse signal: close phase first (as run.main orders it)
    recs = close_checks({"SPY": dict(SHORT_SIG)}, ledger=ledger,
                        run_date=RUN_DATE, today=TODAY,
                        positions=POSITIONS, mids=MIDS_FLAT,
                        executor=sandbox["executor"])
    assert recs[0]["rule"] == "R4_signal_flip"   # flat P&L: only R4 fires
    # ...then the open phase: flip guard sees position_closed and admits it
    day2 = process("SPY", dict(SHORT_SIG), "live", broker=broker,
                   ledger=ledger, redteam=rt_stub, run_date=RUN_DATE,
                   today=TODAY)
    assert day2 is not None
    assert len(broker.submitted) == 2


def test_same_direction_signal_does_not_flip_close(sandbox):
    """flip_close only fires on a REVERSE signal vs. the ledger's open
    direction; a same-direction signal leaves the structure alone."""
    broker, ledger = AcceptedStubBroker(dry_run=True), sandbox["ledger"]
    process("SPY", dict(LONG_SIG), "live", broker=broker, ledger=ledger,
            redteam=StubRedTeam(), run_date="2026-08-30",
            today=date(2026, 8, 30))
    recs = close_checks({"SPY": dict(LONG_SIG)}, ledger=ledger,
                        run_date=RUN_DATE, today=TODAY,
                        positions=POSITIONS, mids=MIDS_FLAT,
                        executor=sandbox["executor"])
    assert recs[0]["action"] == "hold" and sandbox["calls"] == []


def test_failed_close_keeps_flip_guard_engaged(sandbox):
    """If the unwind order errors, no position_closed is written and the
    reverse open stays blocked — fail-closed end to end."""
    broker, ledger = AcceptedStubBroker(dry_run=True), sandbox["ledger"]
    rt_stub = StubRedTeam()
    process("SPY", dict(LONG_SIG), "live", broker=broker, ledger=ledger,
            redteam=rt_stub, run_date="2026-08-30", today=date(2026, 8, 30))

    def failing_executor(legs, *, dry_run=True, **_):
        return ExecResult(False, dry_run, None, "simulated CLI error")

    recs = close_checks({"SPY": dict(SHORT_SIG)}, ledger=ledger,
                        run_date=RUN_DATE, today=TODAY,
                        positions=POSITIONS, mids=MIDS_FLAT,
                        executor=failing_executor)
    assert recs[0]["action"] == "close" and recs[0]["exec_ok"] is False
    assert all(r["kind"] != "position_closed" for r in ledger.records())
    blocked = process("SPY", dict(SHORT_SIG), "live", broker=broker,
                      ledger=ledger, redteam=rt_stub, run_date=RUN_DATE,
                      today=TODAY)
    assert blocked is None                       # flip guard still engaged
    assert len(broker.submitted) == 1


def test_quote_gap_state_persists_across_rounds(sandbox):
    """Rounds 1-2 with a gapped quote skip; round 3 force-closes at market —
    the counter survives via the state file between invocations."""
    for expect_action in ("skip", "skip", "close"):
        recs = close_checks({"SPY": dict(LONG_SIG)}, ledger=sandbox["ledger"],
                            run_date=RUN_DATE, today=TODAY,
                            positions=POSITIONS, mids=MIDS_GAP,
                            executor=sandbox["executor"])
        assert recs[0]["action"] == expect_action
    assert recs[0]["rule"] == "quote_gap"
    assert recs[0]["order_type"] == "market"
    assert len(sandbox["calls"]) == 1            # only the force-close


def test_mcp_redteam_fail_closed_through_pipeline(tmp_path, monkeypatch):
    """McpRedTeam wired into process(): with no claude binary the review
    fail-closes and the pipeline records a red-team veto — no order intent."""
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.setattr(rt, "CLAUDE_BIN", str(tmp_path / "no-claude"))
    monkeypatch.setattr(rt, "_ROOT", tmp_path)
    broker, ledger = StubCLIBroker(dry_run=True), Ledger(tmp_path / "l.jsonl")
    result = process("SPY", dict(LONG_SIG), "live", broker=broker,
                     ledger=ledger, redteam=McpRedTeam(timeout=5),
                     run_date=RUN_DATE, today=TODAY)
    assert result is None and broker.submitted == []
    kinds = [r["kind"] for r in ledger.day(RUN_DATE, "live")]
    assert kinds == ["signal", "gate_check", "redteam"]   # stopped at veto
    report = [r for r in ledger.day(RUN_DATE, "live")
              if r["kind"] == "redteam"][0]["report"]
    assert report["verdict"] == "veto" and report["protocol"] == "redteam.v1"


# ── 08-26 live round: a close priced at mid rests instead of filling ────────

def test_close_prices_at_the_executable_side_not_mid():
    """Measured live: the close asked 4.00 credit while the executable credit
    was 3.82, so it sat unfilled. Selling a long leg fills at the BID; buying
    back a short fills at the ASK. Mid asks the market for a better-than-
    market print."""
    legs = [{"occ_symbol": "NVDA260831C00212500", "qty": 2, "entry": 6.05,
             "type": "call", "strike": 212.5},
            {"occ_symbol": "NVDA260831C00225000", "qty": -2, "entry": 2.05,
             "type": "call", "strike": 225.0}]
    mids = {"NVDA260831C00212500": 5.92, "NVDA260831C00225000": 1.99}
    quotes = {"NVDA260831C00212500": {"bid": 5.82, "ask": 6.02},
              "NVDA260831C00225000": {"bid": 1.98, "ask": 2.00}}
    out = pm.close_legs(legs, mids, quotes)
    by = {l["occ_symbol"]: l for l in out}
    assert by["NVDA260831C00212500"]["side"] == "sell"
    assert by["NVDA260831C00212500"]["limit"] == 5.82      # the bid
    assert by["NVDA260831C00225000"]["side"] == "buy"
    assert by["NVDA260831C00225000"]["limit"] == 2.00      # the ask
    net = 5.82 - 2.00
    assert net < (5.92 - 1.99), "crossing must ask for LESS credit than mid"


def test_close_falls_back_to_mid_without_a_book():
    """No bid/ask (offline, injected mids, a feed hiccup) -> old behaviour,
    never a crash and never an unpriced leg."""
    legs = [{"occ_symbol": "SPY260904C00640000", "qty": 1, "entry": 3.0,
             "type": "call", "strike": 640.0}]
    out = pm.close_legs(legs, {"SPY260904C00640000": 2.5}, {})
    assert out[0]["limit"] == 2.5


def test_r1_escalates_to_a_market_order():
    """R1 exists to GUARANTEE we are out before pin/assignment week. A limit
    that never fills leaves the position riding into expiry — the one outcome
    R1 forbids. R2/R3/R4 can rest a round; R1 cannot."""
    exp = date(2026, 9, 1)
    ymd = exp.strftime("%y%m%d")
    legs = [{"occ_symbol": f"SPY{ymd}C00764000", "qty": 1, "entry": 4.0,
             "type": "call", "strike": 764.0, "expiry": exp}]
    v = pm.evaluate(legs, {f"SPY{ymd}C00764000": 4.0}, date(2026, 8, 31))
    assert v["rule"] == "R1_time"
    assert v["order_type"] == "market"


def test_frozen_thresholds_were_not_touched():
    """The execution fix must not have moved a single pre-registered rule."""
    assert (pm.DTE_CLOSE, pm.TP_MULT_DEBIT, pm.TP_MULT_CREDIT,
            pm.SL_MULT_DEBIT, pm.SL_MULT_CREDIT, pm.MAX_QUOTE_GAPS) == \
        (2, 1.5, 0.5, 0.5, 2.0, 3)
    assert pm.FLIP_CLOSE is True
    assert pm.CLOSE_CONFIG["valuation"] == "snapshot_mid"


# ── 08-26: two same-direction structures must not read as "flat" ────────────

def _spread(exp_ymd, k1, k2, qty):
    from datetime import datetime as _dt
    e = _dt.strptime(exp_ymd, "%y%m%d").date()
    return [{"occ_symbol": f"SPY{exp_ymd}C00{int(k1*1000):06d}", "qty": qty,
             "strike": k1, "type": "call", "expiry": e, "entry": 4.0},
            {"occ_symbol": f"SPY{exp_ymd}C00{int(k2*1000):06d}", "qty": -qty,
             "strike": k2, "type": "call", "expiry": e, "entry": 1.0}]


def test_two_same_direction_spreads_read_as_that_direction():
    """Measured live 2026-08-26: a legal same-direction re-entry left SPY with
    four legs across two expiries. structure_direction wants exactly one long
    and one short, returned None, and reconciliation concluded the broker had
    no SPY — releasing the flip guard on a position the account was holding.
    That is precisely the hedged-book state gate 4 exists to prevent."""
    legs = _spread("260831", 766.0, 773.0, 3) + _spread("260911", 768.0, 780.0, 2)
    assert pm.structure_direction(legs) is None      # the old, conflating read
    assert pm.book_direction(legs) == "long"         # the whole book's sense
    assert len(pm.substructures(legs)) == 2


def test_genuinely_opposed_structures_stay_unknown():
    """Disagreeing structures must NOT be summarized into a direction — that
    would be guessing about exactly the state we refuse to hold."""
    bull = _spread("260831", 766.0, 773.0, 2)
    bear = _spread("260911", 780.0, 768.0, 2)       # long higher strike = short
    assert pm.book_direction(bull + bear) is None


def test_single_spread_direction_is_unchanged():
    assert pm.book_direction(_spread("260831", 766.0, 773.0, 3)) == "long"
