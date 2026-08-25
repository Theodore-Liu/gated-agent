"""End-to-end pipeline (offline): live-book order intent, idempotent dedup on
re-run, negative-control isolation (shadow book can never reach the broker),
and the red-team protocol shape."""
from __future__ import annotations

from datetime import date

from gated_agent import negctl
from gated_agent.ledger import Ledger
from gated_agent.order_cli import StubCLIBroker, cli_command, occ_symbol
from gated_agent.redteam_mcp import StubRedTeam
from gated_agent.run import process

TODAY = date(2026, 8, 24)
RUN_DATE = TODAY.isoformat()
STRONG_LONG = {"symbol": "SPY", "direction": "long", "strength": 0.9,
               "spot": 640.0}


def setup(tmp_path):
    return (StubCLIBroker(dry_run=True), Ledger(tmp_path / "l.jsonl"),
            StubRedTeam())


def test_live_book_produces_dry_run_order(tmp_path):
    broker, ledger, rt = setup(tmp_path)
    result = process("SPY", dict(STRONG_LONG), "live", broker=broker,
                     ledger=ledger, redteam=rt, run_date=RUN_DATE, today=TODAY)
    assert result is not None and result["status"] == "dry_run"
    assert len(broker.submitted) == 1
    kinds = [r["kind"] for r in ledger.day(RUN_DATE, "live")]
    assert kinds == ["signal", "gate_check", "redteam", "order_intent"]


def test_rerun_is_idempotent_via_dedup(tmp_path):
    broker, ledger, rt = setup(tmp_path)
    first = process("SPY", dict(STRONG_LONG), "live", broker=broker,
                    ledger=ledger, redteam=rt, run_date=RUN_DATE, today=TODAY)
    second = process("SPY", dict(STRONG_LONG), "live", broker=broker,
                     ledger=ledger, redteam=rt, run_date=RUN_DATE, today=TODAY)
    assert first is not None and second is None      # second run: dedup veto
    assert len(broker.submitted) == 1                # broker saw exactly one


def test_shadow_book_never_reaches_broker(tmp_path):
    broker, ledger, rt = setup(tmp_path)
    result = process("SPY", dict(STRONG_LONG), "shadow", broker=broker,
                     ledger=ledger, redteam=rt, run_date=RUN_DATE, today=TODAY)
    assert result is None
    assert broker.submitted == []                    # isolation: nothing sent
    shadow_kinds = [r["kind"] for r in ledger.day(RUN_DATE, "shadow")]
    assert "shadow_would_trade" in shadow_kinds      # ...but fully logged
    assert ledger.day(RUN_DATE, "live") == []        # live book untouched


def test_flip_guard_blocks_reverse_open_across_days(tmp_path):
    broker, ledger, rt = setup(tmp_path)
    first = process("SPY", dict(STRONG_LONG), "live", broker=broker,
                    ledger=ledger, redteam=rt, run_date=RUN_DATE, today=TODAY)
    assert first is not None                          # long position now open

    from datetime import date as _date
    day2 = _date(2026, 8, 25)
    strong_short = {"symbol": "SPY", "direction": "short", "strength": 0.9,
                    "spot": 640.0}
    second = process("SPY", strong_short, "live", broker=broker, ledger=ledger,
                     redteam=rt, run_date=day2.isoformat(), today=day2)
    assert second is None                             # flip refused
    assert len(broker.submitted) == 1
    checks = [r for r in ledger.day(day2.isoformat(), "live")
              if r["kind"] == "gate_check"]
    flip = [g for g in checks[0]["results"] if g["gate"] == "direction_flip"]
    assert flip and not flip[0]["allowed"]

    # after the exit rules close the position, the short may open
    ledger.append(day2.isoformat(), "live", "position_closed", symbol="SPY")
    day3 = _date(2026, 8, 26)
    third = process("SPY", strong_short, "live", broker=broker, ledger=ledger,
                    redteam=rt, run_date=day3.isoformat(), today=day3)
    assert third is not None


def test_negctl_signal_contract_and_determinism():
    a = negctl.random_signal("2026-08-24", "SPY", 640.0)
    b = negctl.random_signal("2026-08-24", "SPY", 640.0)
    assert a == b                                    # seeded: reproducible
    assert set(a.keys()) == {"symbol", "direction", "strength", "spot"}
    assert a["direction"] in ("long", "short", "neutral")
    assert 0.0 <= a["strength"] <= 1.0
    c = negctl.random_signal("2026-08-25", "SPY", 640.0)
    d = negctl.random_signal("2026-08-24", "QQQ", 640.0)
    assert (a["direction"], a["strength"]) != (c["direction"], c["strength"]) \
        or (a["direction"], a["strength"]) != (d["direction"], d["strength"])


def test_redteam_protocol_shape(tmp_path):
    broker, ledger, rt = setup(tmp_path)
    process("SPY", dict(STRONG_LONG), "live", broker=broker, ledger=ledger,
            redteam=rt, run_date=RUN_DATE, today=TODAY)
    reports = [r for r in ledger.day(RUN_DATE, "live") if r["kind"] == "redteam"]
    assert len(reports) == 1
    rep = reports[0]["report"]
    assert rep["protocol"] == "redteam.v1"
    assert rep["verdict"] in ("approve", "veto")
    qids = [q["id"] for q in rep["questions"]]
    assert qids == ["max_loss_scenario", "greeks_exposure", "liquidity_exit"]
    for q in rep["questions"]:
        assert q["verdict"] in ("pass", "veto") and q["answer"]


def test_occ_symbol_and_cli_command():
    sym = occ_symbol("SPY", date(2026, 9, 4), "call", 640.0)
    assert sym == "SPY260904C00640000"
    cmds = cli_command([{"occ_symbol": sym, "side": "buy", "qty": 2,
                         "limit": 5.25}])
    assert cmds == [["alpaca", "orders", "create", "--symbol", sym,
                     "--side", "buy", "--qty", "2", "--type", "limit",
                     "--limit-price", "5.25", "--time-in-force", "day"]]


def test_order_intent_carries_broker_receipt(tmp_path):
    """08-25 live-test finding: the order id lived only in Alpaca — the ledger
    row must carry the broker receipt so every decision closes the loop."""
    from gated_agent.cli_executor import ExecResult
    from gated_agent.order_cli import AlpacaCLIBroker

    def fake_executor(legs, dry_run=True, **kw):
        return ExecResult(True, False, {"id": "abc-123", "status": "accepted",
                                        "legs": []}, "{}")

    broker = AlpacaCLIBroker(dry_run=False, executor=fake_executor)
    broker.get_option_chain = StubCLIBroker(dry_run=True).get_option_chain
    broker.get_equity = lambda: 100_000.0
    ledger, rt = Ledger(tmp_path / "l.jsonl"), StubRedTeam()
    result = process("SPY", dict(STRONG_LONG), "live", broker=broker,
                     ledger=ledger, redteam=rt, run_date=RUN_DATE, today=TODAY)
    assert result is not None and result["status"] == "submitted"
    intents = [r for r in ledger.day(RUN_DATE, "live")
               if r["kind"] == "order_intent"]
    assert intents[0]["broker_receipt"] == {"id": "abc-123",
                                            "status": "accepted"}
