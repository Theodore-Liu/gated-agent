"""Daily entrypoint: `python -m gated_agent.run --dry-run`

Idempotent per day: a completed run writes a `run_complete` record; running
again the same day is a no-op (use --force to override, dedup still holds).

Pipeline per symbol (identical for live and shadow books; only the live book
may reach the order path):

    close checks (position_manager R1-R4)  -- BEFORE any new open
    signal -> options_mapper -> risk gates -> red-team review -> order intent

Ordering matters for R4/flip coordination: a reverse signal first drives a
close through position_manager (same mleg path), whose `position_closed`
ledger record is exactly what the direction-flip gate reads -- so the reverse
open is admitted only after the conflicting close went through.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone

from . import market_calendar, negctl, position_manager, signals
from .gates import dedup_key, run_gates
from .ledger import Ledger
from .options_mapper import MapperConfig, map_signal
from .order_cli import AlpacaCLIBroker, Broker, broker_from_env, load_env
from .redteam_mcp import RedTeam, StubRedTeam, redteam_from_env

#: Consecutive days of red-team INFRASTRUCTURE failure before the run screams.
#: Fail-closed is right for one order; silently fail-closed for a whole week
#: is a safety feature turning into a zero.
REDTEAM_ALARM_DAYS = 2


def market_verdict(clock_source, now: datetime | None = None
                   ) -> tuple[bool, str]:
    """Is the market open? Broker clock first, built-in ET calendar second.

    Nothing in this project used to ask. Both scheduled tasks fire on a plain
    weekday schedule, so a market holiday (2026-09-07 is the first Monday
    after the contest window) or a 13:00 ET half-day put the agent on a dead
    tape with live keys.

    The fallback matters as much as the check: blanket-refusing whenever
    /v2/clock hiccups would let one flaky endpoint cost the whole contest,
    while assuming "open" would send day orders into a closed market.
    """
    try:
        state = market_calendar.clock_state(clock_source())
    except Exception as e:  # noqa: BLE001
        state = None
        why_clock = f"clock unavailable ({type(e).__name__})"
    else:
        why_clock = "clock returned no usable payload"
    if state is not None:
        return state
    open_, why = market_calendar.session_state(now)
    return open_, f"{why_clock}; fell back to {why}"


def redteam_health(ledger: Ledger, run_date: str,
                   min_days: int = REDTEAM_ALARM_DAYS) -> dict | None:
    """Alarm when the red-teamer has been broken (not merely strict) for
    `min_days` consecutive days with red-team activity.

    An infrastructure veto and a considered veto used to leave the identical
    ledger shape, so a claude CLI that stopped launching on day 2 would have
    produced a week of "RED-TEAM VETO" lines that look like a cautious agent
    doing its job — and zero trades in front of the judges.
    """
    days: dict[str, list[bool]] = {}
    for r in ledger.records():
        if r["kind"] != "redteam" or r["book"] != "live":
            continue
        rep = r.get("report") or {}
        days.setdefault(r["run_date"], []).append(bool(rep.get("infra_failure")))
    recent = sorted(d for d in days if d <= run_date)[-min_days:]
    if len(recent) < min_days:
        return None
    if not all(days[d] and all(days[d]) for d in recent):
        return None
    return {"consecutive_days": len(recent), "days": recent,
            "why": "every red-team pass on these days failed for "
                   "INFRASTRUCTURE reasons (binary, MCP server, timeout), not "
                   "judgement. The agent is fail-closed and therefore not "
                   "trading at all."}


def shadow_exits(ledger: Ledger, run_date: str, today: date) -> list:
    """Apply R1 (DTE <= dte_close) to the negative control's open positions.

    The shadow book is the signature feature and it was quietly dying. Live
    positions get closed by R1-R4, which writes `position_closed` and releases
    the flip guard; shadow positions have no broker and no exit rule, so after
    its first would-trade in a symbol the coin-flip twin was vetoed on every
    reverse draw — roughly two days in three. A placebo arm that cannot take
    the trades the live arm takes is not a placebo arm.

    R1 is the only pre-registered rule computable without quotes, and it is
    applied verbatim so the comparison is not rigged in either direction.
    """
    closed = []
    for symbol in sorted(ledger.open_positions(book="shadow")):
        legs = None
        for r in ledger.records():
            if (r["book"] == "shadow" and r.get("symbol") == symbol
                    and r["kind"] == "shadow_would_trade" and r.get("legs")):
                legs = r["legs"]
        if not legs:
            continue
        expiries = [occ["expiry"] for occ in
                    (position_manager.parse_occ(l["occ_symbol"]) for l in legs)
                    if occ]
        if not expiries:
            continue
        dte = min((e - today).days for e in expiries)
        if dte <= position_manager.DTE_CLOSE:
            closed.append(ledger.append(
                run_date, "shadow", "position_closed", symbol=symbol,
                rule="R1_time", dry_run=False, pnl=None,
                why=f"shadow twin: DTE {dte} <= {position_manager.DTE_CLOSE}, "
                    f"same R1 the live book exits on (no order: the shadow "
                    f"book has no position and no order path)"))
    return closed


def account_day_pnl(broker: Broker) -> float | None:
    """The account's own equity - last_equity, or None if unavailable."""
    getter = getattr(broker, "get_account", None)
    if getter is None:
        return None
    try:
        acct = getter()
        return float(acct["equity"]) - float(acct["last_equity"])
    except Exception:  # noqa: BLE001 -- an optional cross-check, never fatal
        return None


def process(symbol: str, signal: dict, book: str, *, broker: Broker,
            ledger: Ledger, redteam: RedTeam, run_date: str,
            today: date, day_pnl: float | None = None) -> dict | None:
    """Run one signal through mapper -> gates -> red-team. Returns the order
    intent dict if one was approved (live book only), else None."""
    ledger.append(run_date, book, "signal", signal=signal)

    equity = broker.get_equity()
    chain = broker.get_option_chain(symbol, signal["spot"], today)
    legs = map_signal(signal, chain, today, MapperConfig(equity=equity))
    if not legs:
        ledger.append(run_date, book, "stand_aside", symbol=symbol,
                      reason="mapper produced no trade")
        print(f"  [{book}] {symbol}: stand aside "
              f"({signal['direction']}/{signal['strength']:.2f})")
        return None

    chain_by_symbol = {c["symbol"]: c for c in chain}
    strikes = {c["symbol"]: {"strike": float(c["strike_price"]), "type": c["type"]}
               for c in chain}
    key = dedup_key(run_date, symbol, legs)

    allowed, results, max_loss = run_gates(
        legs=legs, strikes=strikes, equity=equity,
        realized_pnl_today=ledger.realized_pnl(run_date), key=key,
        already_seen=(book == "live" and ledger.seen_order(key)),
        direction=signal["direction"],
        open_direction=ledger.open_direction(symbol, book=book),
        account_day_pnl=day_pnl if book == "live" else None,
    )
    ledger.append(run_date, book, "gate_check", symbol=symbol, legs=legs,
                  max_loss=max_loss, dedup_key=key,
                  results=[r.as_dict() for r in results], allowed=allowed)
    if not allowed:
        vetoes = "; ".join(r.reason for r in results if not r.allowed)
        print(f"  [{book}] {symbol}: GATE VETO — {vetoes}")
        return None

    # 08-26 live round: the red team was told to "inspect the LIVE account",
    # so when it reviewed a SHADOW proposal it judged concentration against
    # the LIVE book's positions — both books share one paper account. The
    # shadow book is a negative control and holds nothing, ever; letting it
    # inherit the live book's exposure makes the control more conservative as
    # the live book fills, for reasons unrelated to signal quality. Tell the
    # reviewer whose positions are whose instead of letting it guess.
    report = redteam.review(symbol=symbol, dedup_key=key, legs=legs,
                            chain_by_symbol=chain_by_symbol,
                            max_loss=max_loss, equity=equity, book=book,
                            book_positions=ledger.open_positions(book=book))
    ledger.append(run_date, book, "redteam", report=report)
    if report["verdict"] == "veto":
        print(f"  [{book}] {symbol}: RED-TEAM VETO — "
              f"{'; '.join(report['veto_reasons'])}")
        return None

    if book != "live":
        # shadow book stops here by construction: no order path, ever
        ledger.append(run_date, book, "shadow_would_trade", symbol=symbol,
                      legs=legs, max_loss=max_loss, dedup_key=key,
                      direction=signal["direction"])
        print(f"  [{book}] {symbol}: would trade {len(legs)} leg(s) "
              f"(max loss ${max_loss:,.0f}) — shadow only, not sent")
        return None

    # Burn the dedup key BEFORE the order leaves: if the process dies between
    # "Alpaca has it" and "the receipt is logged" (locked ledger, killed task,
    # power loss), the next run must not send the same order again. A key
    # burned for an order that never went out only costs a skipped trade; a
    # key left unburned costs a duplicate position. Fail in the cheap
    # direction. No `direction` here — that would claim a position the broker
    # has not confirmed; only the receipt row below may open one.
    ledger.append(run_date, book, "order_submitting", symbol=symbol,
                  legs=legs, max_loss=max_loss, dedup_key=key)

    result = broker.submit_order(symbol, legs, key)
    # Broker receipt into the ledger (08-25 live test finding: order_intent
    # rows carried only the command preview — the actual order id / status
    # lived nowhere, breaking "every decision traceable" at the last hop).
    req = result.get("request") or {}
    receipt = {k: req.get(k) for k in ("id", "status", "filled_at",
                                       "filled_avg_price")
               if isinstance(req, dict) and req.get(k) is not None}
    ledger.append(run_date, book, "order_intent", symbol=symbol, legs=legs,
                  max_loss=max_loss, dedup_key=key, status=result["status"],
                  direction=signal["direction"],
                  cli_commands=result["cli_commands"],
                  broker_receipt=receipt or None)
    oid = f" order {str(receipt['id'])[:8]}" if receipt.get("id") else ""
    label = "ORDER REJECTED" if result["status"] == "error" else "ORDER INTENT"
    print(f"  [{book}] {symbol}: {label} ({result['status']}{oid}) — "
          f"max loss ${max_loss:,.0f}")
    for cmd in result["cli_commands"]:
        print("      $ " + (cmd if isinstance(cmd, str) else " ".join(cmd)))
    return result


def close_checks(sigs: dict, *, ledger: Ledger, run_date: str, today: date,
                 dry_run: bool = True, **inject) -> list:
    """Run position_manager's R1-R4 over every open structure BEFORE any new
    open. R4 flips are derived here: today's live signal direction vs. the
    ledger's open direction. Confirmed closes append `position_closed`, which
    is what the direction-flip gate reads — same-day reverse entries are
    admitted only after their conflicting close went through the mleg path."""
    flips: dict = {}
    if position_manager.FLIP_CLOSE:
        for symbol, sig in sigs.items():
            open_dir = ledger.open_direction(symbol, book="live")
            if open_dir and sig["direction"] in ("long", "short") \
                    and sig["direction"] != open_dir:
                flips[symbol] = True
    records = position_manager.check_positions(
        flips=flips, dry_run=dry_run, today=today,
        ledger=ledger, run_date=run_date, **inject)
    for r in records:
        rule = f" ({r['rule']})" if r.get("rule") else ""
        print(f"  [close] {r['underlying']}: {r['action']}{rule} — {r['why']}")
    if not records:
        print("  [close] no open option structures")
    return records


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gated_agent.run")
    ap.add_argument("--dry-run", action="store_true",
                    help="build orders and pass them through the CLI's "
                         "--dry-run; never submit")
    ap.add_argument("--live", action="store_true",
                    help="submit real paper orders — requires Alpaca keys in "
                         ".env AND ALPACA_HACKATHON_LIVE=1 in the environment")
    ap.add_argument("--date", default=None,
                    help="override run date YYYY-MM-DD (default: today)")
    ap.add_argument("--force", action="store_true",
                    help="re-run even if today already completed")
    ap.add_argument("--ledger", default=None, help="ledger JSONL path override")
    ap.add_argument("--ignore-clock", action="store_true",
                    help="run even when the market is closed (manual "
                         "rehearsal only; recorded in the ledger)")
    args = ap.parse_args(argv)

    if not args.dry_run and not args.live:
        print("Pass --dry-run (safe default) or --live (real paper orders; "
              "needs keys + ALPACA_HACKATHON_LIVE=1).", file=sys.stderr)
        return 2
    if args.dry_run and args.live:
        print("--dry-run and --live are mutually exclusive.", file=sys.stderr)
        return 2

    # Every `python -m` entry point loads .env itself (08-25 finding #2).
    # broker_from_env() would do it too, but relying on that made the order
    # implicit — and redteam_from_env() below reads GATED_AGENT_REDTEAM.
    load_env()

    today = date.fromisoformat(args.date) if args.date else date.today()
    run_date = today.isoformat()
    ledger = Ledger(args.ledger) if args.ledger else Ledger()

    if ledger.run_complete(run_date) and not args.force:
        print(f"Run for {run_date} already complete — idempotent skip "
              f"(--force to override).")
        return 0

    broker = broker_from_env(dry_run=not args.live)
    real = isinstance(broker, AlpacaCLIBroker)
    if args.live and not real:
        print("--live needs Alpaca keys in .env (none found) — refusing.",
              file=sys.stderr)
        return 2

    redteam = redteam_from_env()
    rt_name = ("MCP red-team" if not isinstance(redteam, StubRedTeam)
               else "stub red-team")
    mode = "LIVE paper orders" if args.live else "dry run"
    data = "real Alpaca chain" if real else "synthetic chain"
    print(f"gated-agent {mode} — {run_date} "
          f"(equity ${broker.get_equity():,.0f}, {data}, {rt_name})")

    # Is the market actually open? The tasks fire on a weekday calendar that
    # knows nothing about holidays, half-days or unscheduled closures.
    if real:
        now = (datetime.combine(today, datetime.min.time(), timezone.utc)
               .replace(hour=15) if args.date else None)
        is_open, why = market_verdict(broker.get_clock, now=now)
        if not is_open and not args.ignore_clock:
            ledger.append(run_date, "live", "market_closed", reason=why)
            print(f"Market closed — {why}. Nothing evaluated, nothing sent; "
                  f"the day stays open for the next scheduled round.")
            return 0
        if not is_open:
            ledger.append(run_date, "live", "clock_override", reason=why)
            print(f"WARNING: market is closed ({why}) and --ignore-clock was "
                  f"passed. Rehearsal only.", file=sys.stderr)

    day_pnl = account_day_pnl(broker) if real else None

    # Signals come from a third party (yfinance). One symbol's fetch failing
    # must not take the whole round with it — and above all must not prevent
    # the close checks below from running: an unreachable data source is no
    # reason to stop managing money that is already at risk.
    errors = 0
    sigs: dict[str, dict] = {}
    for s in signals.UNIVERSE:
        try:
            sigs[s] = signals.live_signal(s)
        except Exception as e:  # noqa: BLE001
            errors += 1
            ledger.append(run_date, "live", "signal_unavailable", symbol=s,
                          error=f"{type(e).__name__}: {e}")
            print(f"{s}: SIGNAL UNAVAILABLE ({type(e).__name__}) — skipped",
                  file=sys.stderr)

    # Close checks come FIRST: exits (R1-R4, pre-registered config) run before
    # any new open, so a flip closes the old structure before the flip guard
    # is consulted for the reverse entry.
    if real:
        try:
            close_checks(sigs, ledger=ledger, run_date=run_date, today=today,
                         dry_run=not args.live)
        except Exception as e:  # noqa: BLE001 — a failed close round must not
            # kill the run; unclosed positions keep the flip guard engaged.
            errors += 1
            print(f"  [close] check failed ({type(e).__name__}: {e}); "
                  f"flip guard stays engaged for affected symbols")
    else:
        print("  [close] stub broker (no Alpaca keys): no positions to check")

    # The negative control's own exits, by the same pre-registered R1. Runs
    # for every book state, keyless included: the shadow twin has no broker
    # and therefore nothing else can ever release its flip guard.
    for rec in shadow_exits(ledger, run_date, today):
        print(f"  [shadow] {rec['symbol']}: R1 exit — {rec['why']}")

    for symbol in signals.UNIVERSE:
        sig = sigs.get(symbol)
        if sig is None:
            continue
        print(f"{symbol}: {sig['direction']} strength={sig['strength']:.2f} "
              f"spot={sig['spot']:.2f} (10-mo SMA trend)")
        for book, s in (("live", sig),
                        ("shadow", negctl.random_signal(run_date, symbol,
                                                        sig["spot"]))):
            try:
                process(symbol, s, book, broker=broker, ledger=ledger,
                        redteam=redteam, run_date=run_date, today=today,
                        day_pnl=day_pnl)
            except Exception as e:  # noqa: BLE001 — isolate per symbol/book:
                # an Alpaca 500 on symbol 4 must not abandon symbol 5, and the
                # dedup key of anything already sent is burned before the
                # broker call, so retrying the day cannot duplicate an order.
                errors += 1
                ledger.append(run_date, book, "pipeline_error", symbol=symbol,
                              error=f"{type(e).__name__}: {e}")
                print(f"  [{book}] {symbol}: PIPELINE ERROR "
                      f"({type(e).__name__}: {e})", file=sys.stderr)

    # A red-teamer that has been BROKEN (not strict) for days running means
    # zero trades, and fail-closed makes that look identical to caution.
    alarm = redteam_health(ledger, run_date)
    if alarm:
        errors += 1
        ledger.append(run_date, "live", "redteam_infra_alarm", **alarm)
        banner = "=" * 72
        print(f"\n{banner}\n"
              f"ALARM: RED-TEAM INFRASTRUCTURE HAS FAILED "
              f"{alarm['consecutive_days']} DAYS RUNNING "
              f"({', '.join(alarm['days'])}).\n"
              f"Every order is being vetoed by the fail-closed path, so the "
              f"agent is not trading\nat all. This is NOT the gates working "
              f"— it is the red-teamer failing to launch.\nCheck: claude CLI "
              f"on PATH / CLAUDE_BIN in .env, the Alpaca MCP server in "
              f".venv-mcp,\nand logs/daily.log for the exception type.\n"
              f"{banner}\n", file=sys.stderr)

    if errors:
        # No run_complete record: the day is NOT marked done, so the next
        # invocation retries instead of idempotently skipping. Everything that
        # already succeeded is protected by the dedup gate.
        print(f"Completed with {errors} error(s) — day left open for retry. "
              f"Decisions appended to {ledger.path}", file=sys.stderr)
        return 1

    ledger.append(run_date, "live", "run_complete",
                  universe=list(signals.UNIVERSE))
    print(f"Done. Decisions appended to {ledger.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
