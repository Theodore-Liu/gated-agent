"""Behaviour under adversity. The agent runs unattended for a week; the
question for each hostile condition is not "does it survive" but "does it fail
LOUDLY and SAFELY" — no order sent, no crash-loop, no silent trading.

Covered: (a) Alpaca 5xx, (b) market closed, (c) red-team timeout,
(d) ledger locked mid-order, (e) order rejected by Alpaca.
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
from datetime import date

import pytest

from gated_agent import redteam_mcp as rt, run as run_mod, signals
from gated_agent.cli_executor import ExecResult
from gated_agent.ledger import Ledger
from gated_agent.order_cli import AlpacaCLIBroker, StubCLIBroker
from gated_agent.redteam_mcp import McpRedTeam, StubRedTeam
from gated_agent.run import process

TODAY = date(2026, 8, 24)
RUN_DATE = TODAY.isoformat()
STRONG_LONG = {"symbol": "SPY", "direction": "long", "strength": 0.9,
               "spot": 640.0}


def _http500():
    return urllib.error.HTTPError("https://paper-api.alpaca.markets", 500,
                                  "Internal Server Error", {}, None)


# ── (a) Alpaca API down / 5xx ────────────────────────────────────────────

class DeadBroker(StubCLIBroker):
    def get_equity(self):
        raise _http500()


def test_alpaca_down_sends_no_order_and_propagates(tmp_path):
    """A 5xx must not be swallowed into a stand-aside — that would look like a
    considered decision. It propagates; the caller isolates it."""
    broker, ledger = DeadBroker(dry_run=True), Ledger(tmp_path / "l.jsonl")
    with pytest.raises(urllib.error.HTTPError):
        process("SPY", dict(STRONG_LONG), "live", broker=broker,
                ledger=ledger, redteam=StubRedTeam(), run_date=RUN_DATE,
                today=TODAY)
    assert broker.submitted == []


def test_one_dead_symbol_does_not_abandon_the_rest(tmp_path, monkeypatch,
                                                   capsys):
    """An Alpaca 5xx on symbol 2 of 3 must not cost symbols 1 and 3, and the
    day must stay open for retry (no run_complete) with a non-zero exit code."""
    ledger_path = tmp_path / "l.jsonl"

    class FlakyBroker(StubCLIBroker):
        def get_option_chain(self, symbol, spot, today):
            if symbol == "QQQ":
                raise _http500()
            return super().get_option_chain(symbol, spot, today)

    monkeypatch.setattr(signals, "UNIVERSE", ("SPY", "QQQ", "IWM"))
    monkeypatch.setattr(signals, "live_signal",
                        lambda s: {**STRONG_LONG, "symbol": s})
    monkeypatch.setattr(run_mod, "broker_from_env",
                        lambda **kw: FlakyBroker(dry_run=True))
    monkeypatch.setattr(run_mod, "redteam_from_env", StubRedTeam)

    rc = run_mod.main(["--dry-run", "--ledger", str(ledger_path)])
    assert rc == 1                                   # loud: non-zero exit

    led = Ledger(ledger_path)
    kinds = [(r["kind"], r.get("symbol")) for r in led.records()]
    assert ("pipeline_error", "QQQ") in kinds        # recorded, not hidden
    assert ("order_intent", "SPY") in kinds          # earlier symbol kept
    assert ("order_intent", "IWM") in kinds          # later symbol reached
    assert not led.run_complete(RUN_DATE)            # day left open for retry


def test_signal_source_failure_does_not_disable_the_exit_rules(
        tmp_path, monkeypatch):
    """yfinance is a third party. If it is down, the agent must still evaluate
    R1-R4 on positions that are already at risk — an unreachable data source is
    no reason to stop managing open money."""
    calls = []

    def boom(_symbol):
        raise TimeoutError("yfinance unreachable")

    monkeypatch.setattr(signals, "UNIVERSE", ("SPY",))
    monkeypatch.setattr(signals, "live_signal", boom)
    monkeypatch.setattr(run_mod, "broker_from_env",
                        lambda **kw: AlpacaCLIBroker(
                            dry_run=True, executor=lambda *a, **k: None))
    monkeypatch.setattr(run_mod, "redteam_from_env", StubRedTeam)
    monkeypatch.setattr(run_mod, "close_checks",
                        lambda *a, **k: calls.append("close") or [])

    rc = run_mod.main(["--dry-run", "--ledger", str(tmp_path / "l.jsonl")])
    assert rc == 1                                   # loud
    assert calls == ["close"]                        # exits still evaluated
    kinds = [r["kind"] for r in Ledger(tmp_path / "l.jsonl").records()]
    assert "signal_unavailable" in kinds


# ── (b) market closed ────────────────────────────────────────────────────

class ClosedMarketBroker(StubCLIBroker):
    """After hours the snapshot feed returns contracts with no bid/ask."""

    def get_option_chain(self, symbol, spot, today):
        chain = super().get_option_chain(symbol, spot, today)
        for c in chain:
            c["bid"], c["ask"] = 0.0, 0.0
        return chain


def test_market_closed_stands_aside_without_ordering(tmp_path):
    broker = ClosedMarketBroker(dry_run=True)
    ledger = Ledger(tmp_path / "l.jsonl")
    result = process("SPY", dict(STRONG_LONG), "live", broker=broker,
                     ledger=ledger, redteam=StubRedTeam(), run_date=RUN_DATE,
                     today=TODAY)
    assert result is None
    assert broker.submitted == []
    kinds = [r["kind"] for r in ledger.records()]
    assert kinds == ["signal", "stand_aside"]        # unpriceable -> no trade
    assert "order_submitting" not in kinds           # dedup key not burned


# ── (c) MCP red-team timeout ─────────────────────────────────────────────

def test_redteam_timeout_fail_closes(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "_ROOT", tmp_path)

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=300)

    monkeypatch.setattr(subprocess, "run", timeout)
    report = McpRedTeam(timeout=1).review(
        symbol="SPY", dedup_key="k", legs=[], chain_by_symbol={},
        max_loss=100.0, equity=100_000.0)
    assert report["verdict"] == "veto"
    assert len(report["questions"]) == 3
    assert all(q["verdict"] == "veto" for q in report["questions"])
    assert "TimeoutExpired" in report["veto_reasons"][0]


def test_redteam_timeout_blocks_the_order_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "_ROOT", tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        subprocess.TimeoutExpired(cmd="claude", timeout=300)))
    broker, ledger = StubCLIBroker(dry_run=True), Ledger(tmp_path / "l.jsonl")
    result = process("SPY", dict(STRONG_LONG), "live", broker=broker,
                     ledger=ledger, redteam=McpRedTeam(timeout=1),
                     run_date=RUN_DATE, today=TODAY)
    assert result is None
    assert broker.submitted == []
    assert "order_submitting" not in [r["kind"] for r in ledger.records()]


def test_redteam_garbage_output_fail_closes(monkeypatch, tmp_path):
    """A truncated or non-JSON LLM answer must veto, never approve."""
    monkeypatch.setattr(rt, "_ROOT", tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type(
        "R", (), {"stdout": json.dumps({"result": "sure thing, looks fine"}),
                  "stderr": "", "returncode": 0})())
    report = McpRedTeam(timeout=1).review(
        symbol="SPY", dedup_key="k", legs=[], chain_by_symbol={},
        max_loss=100.0, equity=100_000.0)
    assert report["verdict"] == "veto"


# ── (d) ledger locked / write fails mid-order ────────────────────────────

def test_ledger_append_retries_a_transient_lock(tmp_path, monkeypatch):
    """A dashboard or editor holding the file on Windows is transient."""
    import gated_agent.ledger as led_mod
    led = Ledger(tmp_path / "l.jsonl")
    real_open, attempts = open, {"n": 0}

    def flaky(*a, **k):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise PermissionError("sharing violation")
        return real_open(*a, **k)

    monkeypatch.setattr(led_mod, "open", flaky, raising=False)
    monkeypatch.setattr(led_mod.time, "sleep", lambda _s: None)
    led.append(RUN_DATE, "live", "probe")
    assert attempts["n"] == 2
    assert len(led.records()) == 1


def test_crash_between_submit_and_receipt_cannot_duplicate(tmp_path):
    """The dangerous window: Alpaca has the order, the receipt write dies.

    The dedup key is burned BEFORE the broker call, so the retry that follows
    a crashed run stands the order down instead of sending it twice.
    """
    ledger_path = tmp_path / "l.jsonl"

    class DyingLedger(Ledger):
        def append(self, run_date, book, kind, **payload):
            if kind == "order_intent":
                raise PermissionError("ledger locked")
            return super().append(run_date, book, kind, **payload)

    broker = StubCLIBroker(dry_run=True)
    with pytest.raises(PermissionError):
        process("SPY", dict(STRONG_LONG), "live", broker=broker,
                ledger=DyingLedger(ledger_path), redteam=StubRedTeam(),
                run_date=RUN_DATE, today=TODAY)
    assert len(broker.submitted) == 1               # the order DID go out

    # the retry: same day, same legs -> same dedup key -> stood down
    again = process("SPY", dict(STRONG_LONG), "live", broker=broker,
                    ledger=Ledger(ledger_path), redteam=StubRedTeam(),
                    run_date=RUN_DATE, today=TODAY)
    assert again is None
    assert len(broker.submitted) == 1               # NOT two


def test_order_submitting_row_keeps_the_order_traceable(tmp_path):
    """Even when the receipt never lands, the ledger shows an order left."""
    led = Ledger(tmp_path / "l.jsonl")
    process("SPY", dict(STRONG_LONG), "live", broker=StubCLIBroker(dry_run=True),
            ledger=led, redteam=StubRedTeam(), run_date=RUN_DATE, today=TODAY)
    pre = [r for r in led.records() if r["kind"] == "order_submitting"]
    assert len(pre) == 1
    assert pre[0]["dedup_key"] and pre[0]["symbol"] == "SPY"
    assert pre[0]["legs"]


# ── (e) order rejected by Alpaca ─────────────────────────────────────────

def _rejecting_executor(legs, **kw):
    return ExecResult(ok=False, dry_run=False, request=None,
                      raw="422 buying power insufficient")


def test_rejected_order_is_logged_as_an_error(tmp_path, capsys):
    broker = AlpacaCLIBroker(dry_run=False, executor=_rejecting_executor)
    broker.get_equity = lambda: 100_000.0
    broker.get_option_chain = StubCLIBroker().get_option_chain
    ledger = Ledger(tmp_path / "l.jsonl")
    process("SPY", dict(STRONG_LONG), "live", broker=broker, ledger=ledger,
            redteam=StubRedTeam(), run_date=RUN_DATE, today=TODAY)

    intent = [r for r in ledger.records() if r["kind"] == "order_intent"][0]
    assert intent["status"] == "error"
    assert "ORDER REJECTED" in capsys.readouterr().out   # not "ORDER INTENT"


def test_rejected_order_opens_no_phantom_position(tmp_path):
    """A rejection must not engage the flip guard against a position that does
    not exist — position_manager could never close it, so the symbol would be
    frozen in one direction for the rest of the competition."""
    broker = AlpacaCLIBroker(dry_run=False, executor=_rejecting_executor)
    broker.get_equity = lambda: 100_000.0
    broker.get_option_chain = StubCLIBroker().get_option_chain
    ledger = Ledger(tmp_path / "l.jsonl")
    process("SPY", dict(STRONG_LONG), "live", broker=broker, ledger=ledger,
            redteam=StubRedTeam(), run_date=RUN_DATE, today=TODAY)

    assert ledger.open_direction("SPY") is None      # nothing opened
    assert ledger.seen_order(
        [r for r in ledger.records()
         if r["kind"] == "order_intent"][0]["dedup_key"]) is True


def test_live_order_still_needs_the_second_switch(monkeypatch):
    """Belt and braces: dry_run=False alone never reaches Alpaca."""
    from gated_agent import cli_executor
    monkeypatch.delenv("ALPACA_HACKATHON_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="ALPACA_HACKATHON_LIVE"):
        cli_executor.submit_legs(
            [{"occ_symbol": "SPY260904C00640000", "side": "buy", "qty": 1,
              "limit": 1.0}], dry_run=False)
