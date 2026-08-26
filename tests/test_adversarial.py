"""Adversarial review, 2026-08-26 — written from the position of someone who
wants this agent to lose money or embarrass its author in front of judges.

Every test in this file was written to FAIL against the tree at commit
edcef10 and to describe the behaviour the agent must have instead. The
review that produced them is docs/ADVERSARIAL-REVIEW.md.

Grouping mirrors the attack surfaces:
  (1) time and calendar        (2) money and math
  (3) state corruption         (4) the red-team loop itself
  (5) judge-facing surfaces    (6) the negative-control shadow book
"""
from __future__ import annotations

import ast
import json
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from gated_agent import (chain_fetcher, market_calendar as mc, paths,
                         position_manager as pm, redteam_mcp as rt,
                         run as run_mod, signals)
from gated_agent.cli_executor import ExecResult
from gated_agent.gates import (dedup_key, estimate_max_loss,
                               position_size_gate, run_gates)
from gated_agent.ledger import Ledger, LedgerCorruption
from gated_agent.order_cli import AlpacaCLIBroker, StubCLIBroker
from gated_agent.redteam_mcp import McpRedTeam, StubRedTeam
from gated_agent.run import close_checks, process

TODAY = date(2026, 8, 31)
RUN_DATE = TODAY.isoformat()
LONG_SIG = {"symbol": "SPY", "direction": "long", "strength": 0.9, "spot": 640.0}
SHORT_SIG = {"symbol": "SPY", "direction": "short", "strength": 0.9, "spot": 640.0}

EXP = date(2026, 9, 10)
YMD = EXP.strftime("%y%m%d")
LONG_LEG, SHORT_LEG = f"SPY{YMD}C00764000", f"SPY{YMD}C00783000"
# call debit spread: long the lower strike -> a bullish structure
POSITIONS = [{"occ_symbol": LONG_LEG, "qty": 2, "entry": 4.00},
             {"occ_symbol": SHORT_LEG, "qty": -2, "entry": 1.20}]
MIDS_FLAT = {LONG_LEG: 4.00, SHORT_LEG: 1.20}
MIDS_TP = {LONG_LEG: 5.50, SHORT_LEG: 1.20}
MIDS_GAP = {LONG_LEG: 4.00, SHORT_LEG: None}

OPEN_CLOCK = {"is_open": True, "timestamp": "2026-08-31T10:00:00-04:00",
              "next_open": "2026-09-01T09:30:00-04:00",
              "next_close": "2026-08-31T16:00:00-04:00"}
SHUT_CLOCK = {"is_open": False, "timestamp": "2026-09-07T10:00:00-04:00",
              "next_open": "2026-09-08T09:30:00-04:00",
              "next_close": "2026-09-08T16:00:00-04:00"}


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """position_manager runtime state in tmp; executor captured, never spawned."""
    monkeypatch.setattr(pm, "STATE", tmp_path / ".position_state.json")
    monkeypatch.setattr(pm, "LOG", tmp_path / "close_log.jsonl")
    calls: list = []

    def executor(legs, *, dry_run=True, order_type="limit", **_):
        calls.append({"legs": legs, "dry_run": dry_run, "order_type": order_type})
        return ExecResult(True, dry_run, {"legs": legs}, "captured")

    return {"ledger": Ledger(tmp_path / "l.jsonl"), "calls": calls,
            "executor": executor, "tmp": tmp_path}


def _stub_run(monkeypatch, broker, *, universe=("SPY",), sig=None):
    """Wire run.main() to an injected broker and a fixed signal."""
    monkeypatch.setattr(signals, "UNIVERSE", universe)
    monkeypatch.setattr(signals, "live_signal",
                        lambda s: {**(sig or LONG_SIG), "symbol": s})
    monkeypatch.setattr(run_mod, "broker_from_env", lambda **kw: broker)
    monkeypatch.setattr(run_mod, "redteam_from_env", StubRedTeam)


# ══ (1) time and calendar ════════════════════════════════════════════════
# The tasks fire weekdays 07:00 and 12:15 PT unconditionally. Nothing in the
# tree asks whether the market is actually open.

# The two task times, expressed as ET wall clock (the /ST values are PT and
# US Pacific is always exactly three hours behind US Eastern).
TASK_MORNING_ET = (10, 0)     # 07:00 PT
TASK_AFTERNOON_ET = (15, 15)  # 12:15 PT


def test_labor_day_2026_is_a_market_holiday_the_tasks_still_fire_on():
    """2026-09-07 is a Monday: GatedAgentDaily fires, the market is shut."""
    assert date(2026, 9, 7).weekday() == 0
    assert mc.is_holiday(date(2026, 9, 7))
    open_, why = mc.session_state(mc.et_datetime(date(2026, 9, 7),
                                                 *TASK_MORNING_ET))
    assert open_ is False and "holiday" in why.lower()


def test_a_normal_window_weekday_is_open_at_the_task_times():
    """08-28 .. 09-04 weekdays: both task times must land inside the session."""
    for d in (date(2026, 8, 28), date(2026, 8, 31), date(2026, 9, 4)):
        for hh, mm in (TASK_MORNING_ET, TASK_AFTERNOON_ET):
            assert mc.session_state(mc.et_datetime(d, hh, mm))[0] is True, (d, hh)


def test_early_close_afternoon_round_is_after_the_bell():
    """Half-days close 13:00 ET. The 12:15 PT (= 15:15 ET) close-check round
    would fire two hours after the market shut and price an unwind off a dead
    tape. The calendar must know about early closes, not just holidays."""
    half = mc.EARLY_CLOSES_2026[0]
    assert mc.session_state(mc.et_datetime(half, *TASK_AFTERNOON_ET))[0] is False
    assert mc.session_state(mc.et_datetime(half, *TASK_MORNING_ET))[0] is True


def test_et_offset_tracks_dst_without_a_tz_database():
    """The box is US Pacific and the /ST values assume a fixed 3h offset to ET.
    The calendar computes ET itself so a box in any zone gets the same answer."""
    assert mc.et_offset(date(2026, 8, 31)) == timedelta(hours=-4)   # EDT
    assert mc.et_offset(date(2026, 12, 1)) == timedelta(hours=-5)   # EST
    assert mc.et_offset(date(2026, 3, 8)) == timedelta(hours=-5)    # switch day
    assert mc.et_offset(date(2026, 3, 9)) == timedelta(hours=-4)


def test_pacific_is_always_three_hours_behind_eastern():
    """The assumption the /ST values in register_task.cmd are built on."""
    for d in (date(2026, 1, 15), date(2026, 3, 8), date(2026, 3, 9),
              date(2026, 8, 31), date(2026, 11, 1), date(2026, 12, 24)):
        assert mc.et_offset(d) - mc.pt_offset(d) == timedelta(hours=3)


def test_weekend_is_closed():
    sat = mc.et_datetime(date(2026, 8, 29), 12, 0)
    assert mc.session_state(sat)[0] is False


def test_after_the_bell_on_a_normal_day_is_closed():
    assert mc.session_state(mc.et_datetime(date(2026, 8, 31), 16, 30))[0] is False
    assert mc.session_state(mc.et_datetime(date(2026, 8, 31), 9, 0))[0] is False


def test_closed_market_stops_the_run_before_any_order(tmp_path, monkeypatch):
    """The whole point: a task firing into a shut market must stand down with a
    ledger record, not send day orders that queue to an unknown opening price."""
    broker = AlpacaCLIBroker(dry_run=True, executor=lambda *a, **k: None)
    broker.get_equity = lambda: 100_000.0
    broker.get_option_chain = StubCLIBroker().get_option_chain
    broker.get_clock = lambda: SHUT_CLOCK
    _stub_run(monkeypatch, broker)
    monkeypatch.setattr(run_mod, "close_checks", lambda *a, **k: [])

    led_path = tmp_path / "l.jsonl"
    rc = run_mod.main(["--dry-run", "--ledger", str(led_path),
                       "--date", "2026-09-07"])
    led = Ledger(led_path)
    kinds = [r["kind"] for r in led.records()]
    assert "market_closed" in kinds
    assert "order_submitting" not in kinds and "order_intent" not in kinds
    assert not led.run_complete("2026-09-07")   # retry, don't burn the day
    assert rc == 0                              # not an error, just shut


def test_clock_outage_falls_back_to_the_calendar_not_to_guessing(monkeypatch):
    """If /v2/clock is unreachable we must not simply assume "open" (orders into
    a shut market) nor blanket-refuse for the week (a flaky endpoint would cost
    the whole contest). Fall back to the deterministic ET calendar."""
    def boom():
        raise TimeoutError("clock unreachable")

    open_, why = run_mod.market_verdict(
        boom, now=mc.et_datetime(date(2026, 8, 31), 10, 0))
    assert open_ is True and "calendar" in why.lower()

    shut, why2 = run_mod.market_verdict(
        boom, now=mc.et_datetime(date(2026, 9, 7), 10, 0))
    assert shut is False and "calendar" in why2.lower()


def test_broker_clock_wins_over_the_calendar(monkeypatch):
    """An unscheduled closure (weather, outage) only the broker knows about."""
    open_, why = run_mod.market_verdict(
        lambda: SHUT_CLOCK, now=mc.et_datetime(date(2026, 8, 31), 10, 0))
    assert open_ is False and "clock" in why.lower()


# ══ (2) money and math ═══════════════════════════════════════════════════

def test_dedup_key_survives_a_quote_move():
    """THE dedup defect. The key hashed the legs including their limit prices,
    so the same trade re-derived from a moved quote produced a DIFFERENT key —
    and gate 3 waved it through. Evidence: 2026-08-25 in the live ledger has
    two SPY order_intent rows, same day, same direction, two dedup keys.

    A re-run after a partially failed day is the design's own retry path, so
    this is not hypothetical: it is how the agent doubles a position.
    """
    legs_a = [{"occ_symbol": LONG_LEG, "side": "buy", "qty": 3, "limit": 4.00},
              {"occ_symbol": SHORT_LEG, "side": "sell", "qty": 3, "limit": 1.20}]
    legs_b = [{"occ_symbol": LONG_LEG, "side": "buy", "qty": 3, "limit": 4.15},
              {"occ_symbol": SHORT_LEG, "side": "sell", "qty": 3, "limit": 1.35}]
    assert dedup_key(RUN_DATE, "SPY", legs_a) == dedup_key(RUN_DATE, "SPY", legs_b)


def test_dedup_key_survives_a_resize():
    """Equity moves between runs, so qty moves too. Still the same day's trade."""
    a = [{"occ_symbol": LONG_LEG, "side": "buy", "qty": 3, "limit": 4.00}]
    b = [{"occ_symbol": LONG_LEG, "side": "buy", "qty": 5, "limit": 4.00}]
    assert dedup_key(RUN_DATE, "SPY", a) == dedup_key(RUN_DATE, "SPY", b)


def test_dedup_key_still_separates_real_differences():
    base = [{"occ_symbol": LONG_LEG, "side": "buy", "qty": 3, "limit": 4.00}]
    other_strike = [{"occ_symbol": SHORT_LEG, "side": "buy", "qty": 3, "limit": 4.0}]
    flipped = [{"occ_symbol": LONG_LEG, "side": "sell", "qty": 3, "limit": 4.0}]
    k = dedup_key(RUN_DATE, "SPY", base)
    assert k != dedup_key(RUN_DATE, "SPY", other_strike)
    assert k != dedup_key(RUN_DATE, "SPY", flipped)
    assert k != dedup_key("2026-09-01", "SPY", base)
    assert k != dedup_key(RUN_DATE, "QQQ", base)


def test_retry_after_a_partial_day_does_not_double_the_position(
        tmp_path, monkeypatch):
    """End to end. Run 1: SPY sends, QQQ 5xx -> day left open (rc=1, by design).
    Run 2 retries with quotes that have moved. SPY must stand down."""
    led_path = tmp_path / "l.jsonl"
    sent: list = []

    class MovingQuoteBroker(StubCLIBroker):
        bump = 0.0
        fail_qqq = True

        def get_option_chain(self, symbol, spot, today):
            if symbol == "QQQ" and self.fail_qqq:
                raise RuntimeError("500 from Alpaca")
            chain = super().get_option_chain(symbol, spot, today)
            for c in chain:                       # the tape moves between runs
                c["bid"] = round(c["bid"] + self.bump, 2)
                c["ask"] = round(c["ask"] + self.bump, 2)
            return chain

        def submit_order(self, symbol, legs, key):
            sent.append(symbol)
            return super().submit_order(symbol, legs, key)

    broker = MovingQuoteBroker(dry_run=True)
    broker.get_clock = lambda: OPEN_CLOCK
    _stub_run(monkeypatch, broker, universe=("SPY", "QQQ"))

    assert run_mod.main(["--dry-run", "--ledger", str(led_path),
                         "--date", RUN_DATE]) == 1
    assert sent == ["SPY"]

    broker.bump, broker.fail_qqq = 0.20, False        # quotes moved, QQQ back
    run_mod.main(["--dry-run", "--ledger", str(led_path), "--date", RUN_DATE])
    assert sent.count("SPY") == 1, "SPY re-sent under a drifted dedup key"
    assert sent.count("QQQ") == 1


def test_negative_worst_case_loss_is_unpriceable_not_free():
    """A credit larger than the spread width is a quote artefact (crossed or
    stale book), not free money. It arrived at the gate as a NEGATIVE max loss,
    which sails under a 5%-of-equity cap and under the red-team's frac check.
    Fail closed: a structure we cannot price honestly is refused."""
    legs = [{"occ_symbol": "SPY260910P00630000", "side": "sell", "qty": 1,
             "limit": 8.00},
            {"occ_symbol": "SPY260910P00625000", "side": "buy", "qty": 1,
             "limit": 1.00}]
    strikes = {"SPY260910P00630000": {"strike": 630.0, "type": "put"},
               "SPY260910P00625000": {"strike": 625.0, "type": "put"}}
    assert estimate_max_loss(legs, strikes) is None
    assert position_size_gate(estimate_max_loss(legs, strikes),
                              100_000.0).allowed is False


def test_credit_spread_max_loss_is_still_correct_when_sane():
    legs = [{"occ_symbol": "SPY260910P00630000", "side": "sell", "qty": 2,
             "limit": 1.50},
            {"occ_symbol": "SPY260910P00625000", "side": "buy", "qty": 2,
             "limit": 0.60}]
    strikes = {"SPY260910P00630000": {"strike": 630.0, "type": "put"},
               "SPY260910P00625000": {"strike": 625.0, "type": "put"}}
    # width 5.00 - credit 0.90 = 4.10 per contract, x100 x2
    assert estimate_max_loss(legs, strikes) == pytest.approx(820.0)


def test_realized_pnl_counts_closed_structures(tmp_path):
    """The -2% daily halt summed records of kind "fill". Nothing in the tree
    has ever written a "fill". The headline halt gate was inert."""
    led = Ledger(tmp_path / "l.jsonl")
    led.append(RUN_DATE, "live", "position_closed", symbol="SPY",
               rule="R3_stop_loss", dry_run=False, pnl=-900.0, why="x")
    led.append(RUN_DATE, "live", "position_closed", symbol="QQQ",
               rule="R3_stop_loss", dry_run=False, pnl=-1400.0, why="x")
    assert led.realized_pnl(RUN_DATE) == pytest.approx(-2300.0)


def test_dry_run_closes_contribute_no_realized_pnl(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    led.append(RUN_DATE, "live", "position_closed", symbol="SPY", rule="R1_time",
               dry_run=True, pnl=-5000.0, why="rehearsal")
    assert led.realized_pnl(RUN_DATE) == 0.0


def test_close_records_carry_the_realized_pnl(sandbox):
    """R2 on the fixture spread: (4.30 - 2.80) x 100 x 2 contracts = +$300."""
    close_checks({"SPY": dict(LONG_SIG)}, ledger=sandbox["ledger"],
                 run_date=RUN_DATE, today=TODAY, positions=POSITIONS,
                 mids=MIDS_TP, executor=sandbox["executor"], dry_run=False)
    closed = [r for r in sandbox["ledger"].records()
              if r["kind"] == "position_closed"]
    assert closed and closed[0]["pnl"] == pytest.approx(300.0)


def test_daily_halt_fires_off_real_closes(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    for sym, pnl in (("SPY", -1200.0), ("QQQ", -900.0)):
        led.append(RUN_DATE, "live", "position_closed", symbol=sym,
                   rule="R3_stop_loss", dry_run=False, pnl=pnl, why="x")
    allowed, results, _ = run_gates(
        legs=[{"occ_symbol": LONG_LEG, "side": "buy", "qty": 1, "limit": 1.0}],
        strikes={LONG_LEG: {"strike": 764.0, "type": "call"}},
        equity=100_000.0, realized_pnl_today=led.realized_pnl(RUN_DATE),
        key="k", already_seen=False)
    assert allowed is False
    assert any(r.gate == "daily_loss_halt" and not r.allowed for r in results)


def test_daily_halt_also_reads_the_accounts_own_day_pnl():
    """Our bookkeeping can be wrong (a close that never got logged, a manual
    trade, an assignment). The account's own equity-vs-last_equity is an
    independent measure of the day's damage; the halt must take the worse of
    the two, not trust only its own ledger."""
    allowed, results, _ = run_gates(
        legs=[{"occ_symbol": LONG_LEG, "side": "buy", "qty": 1, "limit": 1.0}],
        strikes={LONG_LEG: {"strike": 764.0, "type": "call"}},
        equity=100_000.0, realized_pnl_today=0.0,      # ledger says all fine
        key="k", already_seen=False,
        account_day_pnl=-2500.0)                       # the account disagrees
    assert allowed is False
    halt = [r for r in results if r.gate == "daily_loss_halt"][0]
    assert halt.allowed is False and "account" in halt.reason.lower()


def test_account_day_pnl_better_than_the_ledger_does_not_unhalt():
    allowed, results, _ = run_gates(
        legs=[{"occ_symbol": LONG_LEG, "side": "buy", "qty": 1, "limit": 1.0}],
        strikes={LONG_LEG: {"strike": 764.0, "type": "call"}},
        equity=100_000.0, realized_pnl_today=-2500.0,
        key="k", already_seen=False, account_day_pnl=+400.0)
    assert allowed is False


# ══ (3) state corruption ═════════════════════════════════════════════════

def _seed(path: Path, *records: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True) + "\n")


def _row(**kw) -> dict:
    base = {"ts": "2026-08-31T14:00:00+00:00", "run_date": RUN_DATE,
            "book": "live", "kind": "signal"}
    return {**base, **kw}


def test_a_torn_final_line_does_not_brick_the_agent(tmp_path):
    """Killed task / power loss mid-append leaves a partial JSON line. Every
    read went through json.loads on every line, so ONE torn byte-range made
    dedup, the once-per-day guard, the flip guard, the halt gate and the
    dashboard all raise. The agent would not trade again for the week."""
    p = tmp_path / "l.jsonl"
    _seed(p, _row(kind="run_complete"))
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"ts": "2026-08-31T14:00:01+00:00", "run_date": "2026-08-31"')
    led = Ledger(p)
    recs = led.records()
    assert [r["kind"] for r in recs] == ["run_complete", "ledger_torn_tail"]
    assert led.run_complete(RUN_DATE) is True      # the good row still reads
    assert led.torn_tail is not None               # and the damage is visible


def test_a_torn_tail_holding_a_dedup_key_fails_closed(tmp_path):
    """The dangerous torn line is the one that was burning a dedup key. We
    cannot parse it, so we must assume the order went out."""
    p = tmp_path / "l.jsonl"
    _seed(p, _row(kind="signal"))
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"book": "live", "dedup_key": "deadbeefcafe0001", "kind": "or')
    led = Ledger(p)
    assert led.seen_order("deadbeefcafe0001") is True
    assert led.seen_order("0000000000000000") is False


def test_corruption_in_the_middle_is_never_silently_skipped(tmp_path):
    """A torn TAIL is a crash artefact and is survivable. Garbage in the middle
    means the file was rewritten by something, and silently skipping it could
    drop an order record — which is how a duplicate gets sent. Refuse."""
    p = tmp_path / "l.jsonl"
    _seed(p, _row(kind="signal"))
    with open(p, "a", encoding="utf-8") as f:
        f.write("}}} not json at all\n")
    _seed(p, _row(kind="run_complete"))
    with pytest.raises(LedgerCorruption):
        Ledger(p).records()


def test_torn_tail_is_quarantined_not_deleted(tmp_path):
    p = tmp_path / "l.jsonl"
    _seed(p, _row(kind="signal"))
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"kind": "order_sub')
    led = Ledger(p)
    led.records()
    assert led.quarantine_path.exists()
    assert "order_sub" in led.quarantine_path.read_text(encoding="utf-8")


def test_appending_after_a_torn_tail_does_not_glue_two_records(tmp_path):
    p = tmp_path / "l.jsonl"
    _seed(p, _row(kind="signal"))
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"kind": "torn')
    led = Ledger(p)
    led.append(RUN_DATE, "live", "probe")
    kinds = [r["kind"] for r in Ledger(p).records()]
    assert kinds == ["signal", "ledger_torn_tail", "probe"]


# ── the believed book vs. the actual book ────────────────────────────────

@pytest.mark.parametrize("legs,expected", [
    ([{"occ_symbol": LONG_LEG, "qty": 2}, {"occ_symbol": SHORT_LEG, "qty": -2}],
     "long"),                                          # bull call debit
    ([{"occ_symbol": LONG_LEG, "qty": -2}, {"occ_symbol": SHORT_LEG, "qty": 2}],
     "short"),                                         # bear call credit
    ([{"occ_symbol": f"SPY{YMD}P00640000", "qty": 2},
      {"occ_symbol": f"SPY{YMD}P00620000", "qty": -2}], "short"),   # bear put debit
    ([{"occ_symbol": f"SPY{YMD}P00640000", "qty": -2},
      {"occ_symbol": f"SPY{YMD}P00620000", "qty": 2}], "long"),     # bull put credit
    ([{"occ_symbol": LONG_LEG, "qty": 1}], "long"),                 # long call
    ([{"occ_symbol": f"SPY{YMD}P00640000", "qty": 1}], "short"),    # long put
])
def test_direction_is_derivable_from_what_the_broker_actually_holds(legs, expected):
    """Reconciliation needs to name the direction of a position it did not
    open. Purely from strikes and signs — no ledger involved."""
    assert pm.structure_direction([{**l, **pm.parse_occ(l["occ_symbol"])}
                                   for l in legs]) == expected


def test_a_position_the_broker_never_had_is_reconciled_away(sandbox):
    """The 08-25 IWM shape: an order was submitted, logged with a direction,
    and never filled — it expired at the close. Nothing writes position_closed
    for a position that does not exist, so open_direction() answers "long"
    forever and the flip guard freezes that symbol for the whole competition.
    A dry-run rehearsal produces the identical phantom."""
    led = sandbox["ledger"]
    led.append("2026-08-30", "live", "order_intent", symbol="IWM",
               direction="long", status="submitted", dedup_key="k1",
               legs=[], max_loss=500.0)
    assert led.open_direction("IWM") == "long"

    close_checks({}, ledger=led, run_date=RUN_DATE, today=TODAY,
                 positions=[], mids={}, executor=sandbox["executor"])
    assert led.open_direction("IWM") is None
    kinds = [r["kind"] for r in led.records()]
    assert "position_reconciled" in kinds
    assert sandbox["calls"] == []          # reconciling sends no orders


def test_reconciliation_unfreezes_the_symbol_end_to_end(sandbox):
    broker, led = StubCLIBroker(dry_run=True), sandbox["ledger"]
    led.append("2026-08-30", "live", "order_intent", symbol="SPY",
               direction="long", status="submitted", dedup_key="k1",
               legs=[], max_loss=500.0)
    blocked = process("SPY", dict(SHORT_SIG), "live", broker=broker,
                      ledger=led, redteam=StubRedTeam(), run_date=RUN_DATE,
                      today=TODAY)
    assert blocked is None                                  # frozen today

    close_checks({"SPY": dict(SHORT_SIG)}, ledger=led, run_date=RUN_DATE,
                 today=TODAY, positions=[], mids={},
                 executor=sandbox["executor"])
    freed = process("SPY", dict(SHORT_SIG), "live", broker=broker, ledger=led,
                    redteam=StubRedTeam(), run_date="2026-09-01",
                    today=date(2026, 9, 1))
    assert freed is not None


def test_a_broker_position_the_ledger_forgot_is_adopted(sandbox):
    """The mirror image, and the one that costs money: the ledger believes flat
    while the broker is long. Sources include a dry-run rehearsal writing a
    position_closed for a close that never happened, and manual intervention.
    Opening a short here would build the hedged book gate 4 exists to ban."""
    led = sandbox["ledger"]
    led.append("2026-08-30", "live", "order_intent", symbol="SPY",
               direction="long", status="submitted", dedup_key="k1",
               legs=[], max_loss=500.0)
    led.append("2026-08-30", "live", "position_closed", symbol="SPY",
               rule="R2_take_profit", dry_run=True, why="rehearsal")
    assert led.open_direction("SPY") is None                # believed flat

    close_checks({}, ledger=led, run_date=RUN_DATE, today=TODAY,
                 positions=POSITIONS, mids=MIDS_FLAT,
                 executor=sandbox["executor"])
    assert led.open_direction("SPY") == "long"              # broker wins
    assert "position_adopted" in [r["kind"] for r in led.records()]


def test_assigned_stock_is_not_invisible(sandbox):
    """Early assignment on the short leg of a credit spread leaves 100 shares
    per contract in the account. fetch_option_positions() filters to
    asset_class == "us_option", so the stock is invisible to every rule the
    agent has, forever, while the orphaned long leg gets re-evaluated as if it
    were a fresh structure. At minimum it must be seen and shouted about."""
    equity_pos = [{"symbol": "SPY", "asset_class": "us_equity", "qty": "-100",
                   "avg_entry_price": "640.0"}]
    alerts = pm.detect_non_option_positions(equity_pos, ledger=sandbox["ledger"],
                                            run_date=RUN_DATE)
    assert alerts and alerts[0]["symbol"] == "SPY"
    rec = [r for r in sandbox["ledger"].records()
           if r["kind"] == "assignment_suspected"]
    assert rec and rec[0]["qty"] == -100.0


# ── the close path itself ────────────────────────────────────────────────

def test_quote_gap_force_close_is_a_market_order(sandbox):
    """evaluate() decided order_type="market" for the "never hold what we
    cannot see" force-close — and check_positions threw that field away.
    submit_legs always built --type limit, priced from mids that by definition
    include the missing leg, floored to 0.0. A net limit computed with a 0.0
    leg either never fills (we keep the blind position) or fills at a price
    nobody chose."""
    for _ in range(pm.MAX_QUOTE_GAPS):
        close_checks({"SPY": dict(LONG_SIG)}, ledger=sandbox["ledger"],
                     run_date=RUN_DATE, today=TODAY, positions=POSITIONS,
                     mids=MIDS_GAP, executor=sandbox["executor"])
    assert len(sandbox["calls"]) == 1
    assert sandbox["calls"][0]["order_type"] == "market"


def test_a_market_order_carries_no_limit_price(monkeypatch):
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return type("R", (), {"stdout": "{}", "stderr": "", "returncode": 0})()

    monkeypatch.setattr(subprocess, "run", fake_run)
    from gated_agent import cli_executor
    cli_executor.submit_legs(
        [{"occ_symbol": LONG_LEG, "side": "sell", "qty": 2, "limit": 0.0,
          "position_intent": "sell_to_close"},
         {"occ_symbol": SHORT_LEG, "side": "buy", "qty": 2, "limit": 0.0,
          "position_intent": "buy_to_close"}],
        dry_run=True, order_type="market")
    argv = captured["argv"]
    assert "--limit-price" not in argv
    assert argv[argv.index("--type") + 1] == "market"


def test_a_limit_close_never_prices_a_leg_at_zero(sandbox):
    """`mids.get(sym) or 0.0` turns a missing quote into a $0.00 leg. On a
    LIMIT close that is an instruction to accept any price at all."""
    partial = {LONG_LEG: 4.00, SHORT_LEG: None}
    legs = pm.close_legs(POSITIONS, partial)
    assert all(l["limit"] is None or l["limit"] > 0 for l in legs)
    with pytest.raises(ValueError):
        from gated_agent import cli_executor
        cli_executor.submit_legs(
            [{"occ_symbol": LONG_LEG, "side": "sell", "qty": 1, "limit": 0.0}],
            dry_run=True, order_type="limit")


def test_one_bad_structure_does_not_abandon_the_others(sandbox):
    """check_positions had no per-structure isolation: the first raising submit
    aborted the round for every other underlying AND skipped the state and
    close-log writes at the end, so quote-gap counters silently never advanced."""
    qqq = [{"occ_symbol": f"QQQ{YMD}C00560000", "qty": 2, "entry": 4.00},
           {"occ_symbol": f"QQQ{YMD}C00570000", "qty": -2, "entry": 1.20}]
    mids = {**MIDS_TP, f"QQQ{YMD}C00560000": 5.50, f"QQQ{YMD}C00570000": 1.20}

    def half_broken(legs, *, dry_run=True, order_type="limit", **_):
        if legs[0]["occ_symbol"].startswith("QQQ"):
            raise RuntimeError("CLI blew up on QQQ")
        return ExecResult(True, dry_run, {"legs": legs}, "ok")

    recs = pm.check_positions(dry_run=True, today=TODAY,
                              positions=POSITIONS + qqq, mids=mids,
                              executor=half_broken, ledger=sandbox["ledger"],
                              run_date=RUN_DATE)
    by_und = {r["underlying"]: r for r in recs}
    assert by_und["SPY"]["exec_ok"] is True         # SPY was not abandoned
    assert by_und["QQQ"]["exec_ok"] is False
    assert "RuntimeError" in by_und["QQQ"]["error"]
    assert pm.STATE.exists() and pm.LOG.exists()    # round still recorded


def test_close_log_survives_a_raising_structure(sandbox):
    def always_raises(legs, **_):
        raise RuntimeError("nope")

    pm.check_positions(dry_run=True, today=TODAY, positions=POSITIONS,
                       mids=MIDS_TP, executor=always_raises,
                       ledger=sandbox["ledger"], run_date=RUN_DATE)
    rows = [json.loads(l) for l in
            pm.LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert rows and rows[0]["underlying"] == "SPY"


# ══ (4) the red-team loop itself ═════════════════════════════════════════

def test_infrastructure_veto_is_flagged_apart_from_a_judgment_veto(
        monkeypatch, tmp_path):
    """Fail-closed is right per order. But a missing claude binary and a
    considered "this spread is illiquid" veto produce the same ledger shape,
    so a permanently broken red-teamer looks exactly like a cautious one."""
    monkeypatch.setattr(rt, "_ROOT", tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        FileNotFoundError("claude not found")))
    report = McpRedTeam(timeout=1).review(symbol="SPY", dedup_key="k", legs=[],
                                          chain_by_symbol={}, max_loss=10.0,
                                          equity=100_000.0)
    assert report["verdict"] == "veto"
    assert report["infra_failure"] is True

    ok = StubRedTeam().review(symbol="SPY", dedup_key="k", legs=[],
                              chain_by_symbol={}, max_loss=10.0,
                              equity=100_000.0)
    assert ok.get("infra_failure") is False


def test_repeated_infrastructure_failure_raises_a_loud_alarm(tmp_path):
    """Zero trades for a week is the worst possible outcome of a safety
    feature. It must be impossible for that to happen quietly."""
    led = Ledger(tmp_path / "l.jsonl")
    for day in ("2026-08-31", "2026-09-01"):
        led.append(day, "live", "redteam", symbol="SPY", report={
            "verdict": "veto", "infra_failure": True, "veto_reasons": ["boom"]})
    alarm = run_mod.redteam_health(led, "2026-09-01")
    assert alarm is not None and alarm["consecutive_days"] == 2


def test_a_considered_veto_never_raises_the_alarm(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    for day in ("2026-08-31", "2026-09-01"):
        led.append(day, "live", "redteam", symbol="SPY", report={
            "verdict": "veto", "infra_failure": False,
            "veto_reasons": ["exit spread 22% > 10%"]})
    assert run_mod.redteam_health(led, "2026-09-01") is None


def test_the_alarm_reaches_the_log_and_the_exit_code(tmp_path, monkeypatch,
                                                     capsys):
    led_path = tmp_path / "l.jsonl"
    led = Ledger(led_path)
    led.append("2026-08-30", "live", "redteam", symbol="SPY", report={
        "verdict": "veto", "infra_failure": True, "veto_reasons": ["boom"]})

    class Broken(McpRedTeam):
        def review(self, **kw):
            return {"protocol": "redteam.v1", "verdict": "veto",
                    "infra_failure": True, "questions": [],
                    "veto_reasons": ["boom"],
                    "order_dedup_key": kw["dedup_key"], "symbol": kw["symbol"]}

    broker = StubCLIBroker(dry_run=True)
    broker.get_clock = lambda: OPEN_CLOCK
    _stub_run(monkeypatch, broker)
    monkeypatch.setattr(run_mod, "redteam_from_env", Broken)

    rc = run_mod.main(["--dry-run", "--ledger", str(led_path),
                       "--date", RUN_DATE])
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert rc != 0
    assert "RED-TEAM INFRASTRUCTURE" in out.upper()
    assert any(r["kind"] == "redteam_infra_alarm" for r in Ledger(led_path).records())


# ══ (5) judge-facing surfaces ════════════════════════════════════════════

DASH = Path(paths.__file__).resolve().parent / "dashboard.py"


def test_every_dashboard_api_call_is_guarded():
    """One try/except covered /v2/account only. A hiccup on /v2/positions or
    /v2/orders — or an order row with a null field — renders a Python
    traceback on the page the judges are looking at."""
    tree = ast.parse(DASH.read_text(encoding="utf-8"))
    unguarded = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_get"):
            unguarded.append(node.lineno)
    guarded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_get"):
                    guarded.add(sub.lineno)
    assert set(unguarded) <= guarded, (
        f"unguarded _get() on the judge-facing page at lines "
        f"{sorted(set(unguarded) - guarded)}")


def test_dashboard_reads_the_ledger_through_the_tolerant_reader():
    """The dashboard had its own json.loads-per-line reader, so the torn tail
    that bricks the agent also blanks the demo URL."""
    text = DASH.read_text(encoding="utf-8")
    assert "read_jsonl" in text


def test_close_check_payload_can_actually_place_a_live_close():
    """run_daily.cmd documents `set ALPACA_HACKATHON_LIVE=1` inside its own
    setlocal. run_close_check.cmd never mentions it, so following the
    documented go-live procedure gives a close path that raises
    "refusing live order" on every unwind — for the whole week."""
    text = (paths.ROOT / "scripts" / "run_close_check.cmd").read_text(
        encoding="utf-8", errors="replace")
    assert "ALPACA_HACKATHON_LIVE" in text
    assert text.isascii()


def test_scheduler_script_documents_both_tasks_and_a_missed_start():
    """Neither task sets StartWhenAvailable, so a box asleep at 07:00 simply
    does not trade that day and nothing says so."""
    text = (paths.ROOT / "scripts" / "register_task.cmd").read_text(
        encoding="utf-8", errors="replace")
    assert "StartWhenAvailable" in text or "/RI" in text


# ══ (6) the negative-control shadow book ═════════════════════════════════

def test_shadow_book_is_not_frozen_by_its_own_first_trade(sandbox):
    """The signature feature. Live positions get closed (position_closed clears
    the flip guard); shadow positions never do, because position_closed is only
    ever written for the live book. So after its first shadow trade in a symbol
    the coin-flip twin is vetoed on every reverse draw — roughly two days in
    three — and the placebo arm quietly stops being a placebo."""
    led = sandbox["ledger"]
    led.append("2026-08-20", "shadow", "shadow_would_trade", symbol="SPY",
               direction="long", dedup_key="s1", max_loss=400.0,
               legs=[{"occ_symbol": f"SPY{date(2026, 8, 21):%y%m%d}C00640000",
                      "side": "buy", "qty": 1, "limit": 1.0}])
    assert led.open_direction("SPY", book="shadow") == "long"

    run_mod.shadow_exits(led, RUN_DATE, TODAY)      # expiry long since passed
    assert led.open_direction("SPY", book="shadow") is None


def test_shadow_exit_mirrors_r1_not_something_softer(sandbox):
    """The shadow twin must be exited by the SAME rule the live book uses, or
    the comparison is rigged in one direction or the other."""
    led = sandbox["ledger"]
    far = TODAY + timedelta(days=10)
    led.append(RUN_DATE, "shadow", "shadow_would_trade", symbol="QQQ",
               direction="long", dedup_key="s2", max_loss=400.0,
               legs=[{"occ_symbol": f"QQQ{far:%y%m%d}C00560000", "side": "buy",
                      "qty": 1, "limit": 1.0}])
    run_mod.shadow_exits(led, RUN_DATE, TODAY)
    assert led.open_direction("QQQ", book="shadow") == "long"   # DTE 10 > 2

    at_r1 = far - timedelta(days=pm.DTE_CLOSE)      # DTE exactly at the rule
    run_mod.shadow_exits(led, RUN_DATE, at_r1)
    assert led.open_direction("QQQ", book="shadow") is None


def test_the_shadow_book_can_never_reach_the_close_order_path(sandbox):
    """Regression guard on the new exit paths: an exit order for a shadow
    position would be a REAL order. check_positions must only ever see what
    the broker actually holds."""
    led = sandbox["ledger"]
    led.append(RUN_DATE, "shadow", "shadow_would_trade", symbol="SPY",
               direction="long", dedup_key="s3", max_loss=400.0,
               legs=[{"occ_symbol": LONG_LEG, "side": "buy", "qty": 9,
                      "limit": 4.0}])
    close_checks({"SPY": dict(SHORT_SIG)}, ledger=led, run_date=RUN_DATE,
                 today=TODAY, positions=[], mids={},
                 executor=sandbox["executor"])
    run_mod.shadow_exits(led, RUN_DATE, TODAY + timedelta(days=30))
    assert sandbox["calls"] == []
    assert all(r["book"] == "shadow" or r["kind"] != "position_closed"
               for r in led.records() if r.get("symbol") == "SPY"
               and r["kind"] == "position_closed")


def test_shadow_closes_are_never_counted_as_realized_money(sandbox):
    led = sandbox["ledger"]
    led.append(RUN_DATE, "shadow", "position_closed", symbol="SPY",
               rule="R1_time", dry_run=False, pnl=-9999.0, why="shadow")
    assert led.realized_pnl(RUN_DATE) == 0.0
