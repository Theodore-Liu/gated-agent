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
    broker, ledger = StubCLIBroker(dry_run=True), sandbox["ledger"]
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
    broker, ledger = StubCLIBroker(dry_run=True), sandbox["ledger"]
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
    broker, ledger = StubCLIBroker(dry_run=True), sandbox["ledger"]
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
