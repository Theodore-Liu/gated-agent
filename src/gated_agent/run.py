"""Daily entrypoint: `python -m gated_agent.run --dry-run`

Idempotent per day: a completed run writes a `run_complete` record; running
again the same day is a no-op (use --force to override, dedup still holds).

Pipeline per symbol (identical for live and shadow books; only the live book
may reach the order path):

    signal -> options_mapper -> risk gates -> red-team review -> order intent
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from . import negctl, signals
from .gates import dedup_key, run_gates
from .ledger import Ledger
from .options_mapper import MapperConfig, map_signal
from .order_cli import AlpacaCLIBroker, Broker, broker_from_env
from .redteam_mcp import RedTeam, StubRedTeam


def process(symbol: str, signal: dict, book: str, *, broker: Broker,
            ledger: Ledger, redteam: RedTeam, run_date: str,
            today: date) -> dict | None:
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
    )
    ledger.append(run_date, book, "gate_check", symbol=symbol, legs=legs,
                  max_loss=max_loss, dedup_key=key,
                  results=[r.as_dict() for r in results], allowed=allowed)
    if not allowed:
        vetoes = "; ".join(r.reason for r in results if not r.allowed)
        print(f"  [{book}] {symbol}: GATE VETO — {vetoes}")
        return None

    report = redteam.review(symbol=symbol, dedup_key=key, legs=legs,
                            chain_by_symbol=chain_by_symbol,
                            max_loss=max_loss, equity=equity)
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

    result = broker.submit_order(symbol, legs, key)
    ledger.append(run_date, book, "order_intent", symbol=symbol, legs=legs,
                  max_loss=max_loss, dedup_key=key, status=result["status"],
                  direction=signal["direction"],
                  cli_commands=result["cli_commands"])
    print(f"  [{book}] {symbol}: ORDER INTENT ({result['status']}) — "
          f"max loss ${max_loss:,.0f}")
    for cmd in result["cli_commands"]:
        print("      $ " + (cmd if isinstance(cmd, str) else " ".join(cmd)))
    return result


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
    args = ap.parse_args(argv)

    if not args.dry_run and not args.live:
        print("Pass --dry-run (safe default) or --live (real paper orders; "
              "needs keys + ALPACA_HACKATHON_LIVE=1).", file=sys.stderr)
        return 2
    if args.dry_run and args.live:
        print("--dry-run and --live are mutually exclusive.", file=sys.stderr)
        return 2

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

    redteam = StubRedTeam()
    mode = "LIVE paper orders" if args.live else "dry run"
    data = "real Alpaca chain" if real else "synthetic chain"
    print(f"gated-agent {mode} — {run_date} "
          f"(equity ${broker.get_equity():,.0f}, {data}, stub red-team)")
    for symbol in signals.UNIVERSE:
        sig = signals.live_signal(symbol)          # live yfinance closes
        print(f"{symbol}: {sig['direction']} strength={sig['strength']:.2f} "
              f"spot={sig['spot']:.2f} (10-mo SMA trend)")
        process(symbol, sig, "live", broker=broker, ledger=ledger,
                redteam=redteam, run_date=run_date, today=today)
        shadow = negctl.random_signal(run_date, symbol, sig["spot"])
        process(symbol, shadow, "shadow", broker=broker, ledger=ledger,
                redteam=redteam, run_date=run_date, today=today)

    ledger.append(run_date, "live", "run_complete",
                  universe=list(signals.UNIVERSE))
    print(f"Done. Decisions appended to {ledger.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
