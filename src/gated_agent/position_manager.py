"""Deterministic closing rules R1-R4 for open option structures.

Pre-registered rules (frozen 2026-08-24 as the "平仓规则定稿 v1" bus message;
parameters live in config/close_rules.json so judges can see the rules were
written BEFORE the contest ran):

  R1 time gate    DTE <= 2                     -> close unconditionally
  R2 take profit  debit:  value >= 1.5x entry  -> close   (+50%)
                  credit: buyback <= 0.5x credit-> close   (keep 50% premium)
  R3 stop loss    debit:  value <= 0.5x entry  -> close   (-50%)
                  credit: buyback >= 2.0x credit-> close   (lose 1x premium)
  R4 signal flip  reverse signal arrives        -> close old structure first
                  (the flip guard in gates.py blocks the reverse entry until
                   this close is confirmed -- no hedged books)

Valuation: snapshot mid, same source as entry. A leg with no mid this round
-> skip the whole structure this round; after max_quote_gaps consecutive
gapped rounds -> force-close at market (never hold a position we can't see).

Signed-value convention (makes R2/R3 one comparison each):
  V = sum(+mid for long legs, -mid for short legs)   per structure unit
  E = same sum over entry prices.  Debit: E > 0, profit when V grows.
  Credit: E < 0 (premium received), profit when V rises toward 0.
  -> TP:  debit V >= 1.5E   | credit V >= 0.5E     (signed, both "V >= k*E")
  -> SL:  debit V <= 0.5E   | credit V <= 2.0E

Coordination with the daily run: `python -m gated_agent.run` calls
`check_positions()` BEFORE any new opens. A confirmed close appends a
`position_closed` record to the decision ledger, which is exactly what the
direction-flip gate reads -- so a reverse entry is admitted only after the
close it depends on has gone through the same mleg order path.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from .chain_fetcher import DATA, TRADING, _headers
from .cli_executor import submit_legs
from .paths import ROOT

_ROOT = ROOT                     # repo root, never the CWD (see paths.py)
CONFIG_PATH = _ROOT / "config" / "close_rules.json"
STATE = _ROOT / "ledger" / ".position_state.json"     # runtime, gitignored
LOG = _ROOT / "ledger" / "close_log.jsonl"

_FROZEN_DEFAULTS = {                    # mirror of config/close_rules.json --
    "dte_close": 2,                     # a missing file must not soften rules
    "tp_debit_mult": 1.5,
    "tp_credit_mult": 0.5,
    "sl_debit_mult": 0.5,
    "sl_credit_mult": 2.0,
    "flip_close": True,
    "valuation": "snapshot_mid",
    "max_quote_gaps": 3,
    "check_times": ["open+30min", "close-45min"],
    "close_cross_frac": 1.0,       # execution, added 08-26 (see below)
}


def load_close_config(path: os.PathLike | str = CONFIG_PATH) -> dict:
    """Frozen close-rule parameters; fall back to the identical built-in
    defaults if the config file is absent (fresh clone still fails closed)."""
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return dict(_FROZEN_DEFAULTS)
    return {**_FROZEN_DEFAULTS, **{k: v for k, v in cfg.items()
                                   if not k.startswith("_")}}


CLOSE_CONFIG = load_close_config()
DTE_CLOSE = CLOSE_CONFIG["dte_close"]                 # R1
TP_MULT_DEBIT = CLOSE_CONFIG["tp_debit_mult"]         # R2
TP_MULT_CREDIT = CLOSE_CONFIG["tp_credit_mult"]
SL_MULT_DEBIT = CLOSE_CONFIG["sl_debit_mult"]         # R3
SL_MULT_CREDIT = CLOSE_CONFIG["sl_credit_mult"]
FLIP_CLOSE = CLOSE_CONFIG["flip_close"]               # R4 enabled?
MAX_QUOTE_GAPS = CLOSE_CONFIG["max_quote_gaps"]       # force-close threshold
CHECK_TIMES = CLOSE_CONFIG["check_times"]             # scheduler contract
# EXECUTION, not a rule. The frozen R1-R4 thresholds above decide WHETHER
# to close; this decides at what price the resulting order is sent, and
# it was a latent defect: mid-priced closes rest instead of filling.
# Added 2026-08-26 after a live close asked 4.00 credit against an
# executable 3.82. 1.0 = price at the executable side, 0.0 = old mid.
CLOSE_CROSS_FRAC = float(CLOSE_CONFIG.get("close_cross_frac", 1.0))

_OCC = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


def parse_occ(symbol: str) -> dict | None:
    m = _OCC.match(symbol)
    if not m:
        return None
    und, ymd, cp, strike = m.groups()
    return {"underlying": und,
            "expiry": datetime.strptime(ymd, "%y%m%d").date(),
            "type": "call" if cp == "C" else "put",
            "strike": int(strike) / 1000.0}


def _get(url: str) -> dict | list:
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_all_positions() -> list:
    return list(_get(f"{TRADING}/v2/positions"))


def fetch_option_positions(raw: list | None = None) -> list:
    """Open option positions -> [{occ_symbol, qty(signed int), entry(float)}]."""
    out = []
    for p in (fetch_all_positions() if raw is None else raw):
        if p.get("asset_class") != "us_option":
            continue
        out.append({"occ_symbol": p["symbol"], "qty": int(float(p["qty"])),
                    "entry": float(p["avg_entry_price"])})
    return out


def detect_non_option_positions(raw: list, *, ledger=None,
                                run_date: str | None = None) -> list:
    """Shout about anything in the account that is not an option.

    Early assignment on the short leg of a credit spread leaves 100 shares per
    contract sitting in the account. Every rule this agent has looks only at
    `asset_class == "us_option"`, so that stock is invisible to R1-R4, to the
    flip guard and to the position-size gate — forever — while the orphaned
    long leg gets re-evaluated as if it were a fresh structure. Liquidating it
    automatically is a bigger decision than a review should take unilaterally;
    making it impossible to MISS is not.
    """
    alerts = []
    for p in raw or []:
        if p.get("asset_class") == "us_option":
            continue
        try:
            qty = float(p.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        alert = {"symbol": p.get("symbol"), "asset_class": p.get("asset_class"),
                 "qty": qty, "avg_entry_price": p.get("avg_entry_price")}
        alerts.append(alert)
        if ledger is not None:
            ledger.append(run_date or date.today().isoformat(), "live",
                          "assignment_suspected", **alert,
                          why="non-option position in the account: early "
                              "assignment or manual intervention. No exit rule "
                              "in this agent can see or manage it.")
    return alerts


def structure_direction(legs: list) -> str | None:
    """Directional sense of a position, from strikes and signs alone.

    Reconciliation has to be able to name the direction of a structure it did
    not open (a fill that outlived its ledger row, a manual trade, a rehearsal
    that turned real). Purely arithmetic — no ledger, no signal.

        long  = bull call debit, bull put credit, long call, short put
        short = bear call credit, bear put debit, long put, short call
    """
    legs = [l for l in legs if l.get("qty")]
    if not legs:
        return None
    if len(legs) == 1:
        (leg,) = legs
        bullish = (leg["type"] == "call") == (leg["qty"] > 0)
        return "long" if bullish else "short"
    longs = [l for l in legs if l["qty"] > 0]
    shorts = [l for l in legs if l["qty"] < 0]
    if len(longs) != 1 or len(shorts) != 1:
        return None
    (lo,), (sh,) = longs, shorts
    if lo["type"] != sh["type"]:
        return None
    if lo["type"] == "call":
        return "long" if lo["strike"] < sh["strike"] else "short"
    return "short" if lo["strike"] > sh["strike"] else "long"


def reconcile(ledger, run_date: str, actual: dict) -> list:
    """Re-sync the ledger's BELIEVED book to the broker's ACTUAL positions.

    The flip guard, and therefore the ban on hedged books, is only as good as
    the ledger's picture of what is open. That picture is built from order
    *intents*, and intents drift from reality in both directions:

      * believed open, actually flat — an order that was accepted and never
        filled (the 08-25 IWM order expired unfilled), a position that expired
        worthless, one that was assigned away, or a `--dry-run` rehearsal.
        Nothing ever writes `position_closed` for a position the broker does
        not have, so the symbol stays frozen in one direction for the whole
        competition and no log line says why.
      * believed flat, actually open — a dry-run close that wrote
        `position_closed` for an unwind that never happened, or manual
        intervention. Opening the reverse here builds exactly the hedged book
        gate 4 exists to ban.

    `actual` maps underlying -> direction (from structure_direction). Runs at
    the start of every round, before any exit rule and long before any open.
    """
    records = []
    believed = ledger.open_positions(book="live")
    for symbol, direction in sorted(believed.items()):
        if symbol not in actual:
            records.append(ledger.append(
                run_date, "live", "position_reconciled", symbol=symbol,
                was=direction, why="ledger believed this position was open; "
                                   "the broker has no option position in it "
                                   "(unfilled, expired, assigned, or a "
                                   "dry-run rehearsal)"))
    for symbol, direction in sorted(actual.items()):
        if believed.get(symbol) != direction:
            records.append(ledger.append(
                run_date, "live", "position_adopted", symbol=symbol,
                direction=direction, believed=believed.get(symbol),
                why="the broker holds this structure; the ledger did not "
                    "believe it was open (or had it in the other direction). "
                    "The account is the source of truth."))
    return records


#: Per-leg book, kept alongside the mid so a close can be priced where it
#: actually trades. `mids` stays the valuation input (frozen config says
#: valuation = snapshot_mid) — this is the EXECUTION side, a different thing
#: that happened to reuse the same number until 2026-08-26.
QUOTES: dict = {}


def fetch_mids(symbols: list) -> dict:
    """OCC symbol -> mid, or None when bid/ask missing (quote gap).

    Also records each leg's bid/ask in `QUOTES`: valuation wants the mid, but
    an ORDER priced at mid rests instead of filling (measured 2026-08-26 — a
    close asked 4.00 credit while the executable credit was 3.82).
    """
    mids: dict = {}
    for i in range(0, len(symbols), 100):
        batch = ",".join(symbols[i:i + 100])
        d = _get(f"{DATA}/v1beta1/options/snapshots?" + urllib.parse.urlencode(
            {"symbols": batch, "feed": "indicative"}))
        for sym, s in (d.get("snapshots") or {}).items():
            q = s.get("latestQuote") or {}
            bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
            if bid > 0 and ask > 0:
                mids[sym] = round((bid + ask) / 2, 2)
                QUOTES[sym] = {"bid": bid, "ask": ask}
            else:
                mids[sym] = None
    return {s: mids.get(s) for s in symbols}


def group_structures(positions: list) -> dict:
    """Group legs into structures, keyed by underlying.

    08-26 live round: this grouped by underlying ALONE, on the stated premise
    that the flip gate forbids a second same-underlying entry. It does not —
    it forbids a second entry in the OPPOSITE direction. A same-direction
    re-entry is legal and happened the same morning, leaving SPY with four
    legs across two expiries. `structure_direction` needs exactly one long
    and one short, returned None for the four-leg blob, reconciliation read
    "the broker does not have SPY", and RELEASED the flip guard on a position
    the account was actually holding — the precise state gate 4 exists to
    prevent. Legs are grouped per (underlying, expiry) now; the returned dict
    stays keyed by underlying, with every leg of that underlying present, so
    valuation and unwinding are unchanged.
    """
    groups: dict = {}
    for p in positions:
        occ = parse_occ(p["occ_symbol"])
        if occ is None:
            continue
        groups.setdefault(occ["underlying"], []).append({**p, **occ})
    return groups


def substructures(legs: list) -> list:
    """Split one underlying's legs into per-expiry structures."""
    by_exp: dict = {}
    for leg in legs:
        by_exp.setdefault(leg["expiry"], []).append(leg)
    return [by_exp[k] for k in sorted(by_exp)]


def book_direction(legs: list) -> str | None:
    """Direction of an underlying's whole book: the shared direction of its
    per-expiry structures, or None when they genuinely disagree.

    Never returns None merely because there is more than one structure — that
    conflation is what released the guard on a live position.
    """
    dirs = {d for sub in substructures(legs)
            if (d := structure_direction(sub)) is not None}
    if len(dirs) == 1:
        return dirs.pop()
    return None


def _signed_value(legs: list, price_of) -> float | None:
    """V or E per structure unit; None if any leg has no price."""
    total = 0.0
    for leg in legs:
        px = price_of(leg)
        if px is None:
            return None
        total += (1 if leg["qty"] > 0 else -1) * float(px)
    return round(total, 4)


def evaluate(legs: list, mids: dict, today: date,
             flip: bool = False, quote_gaps: int = 0) -> dict:
    """Apply R1-R4 to one structure. Pure function -> testable without APIs."""
    entry = _signed_value(legs, lambda l: l["entry"])
    value = _signed_value(legs, lambda l: mids.get(l["occ_symbol"]))
    dte = min((l["expiry"] - today).days for l in legs)
    kind = "debit" if entry is not None and entry > 0 else "credit"
    base = {"entry": entry, "value": value, "dte": dte, "kind": kind}

    if value is None:
        # 08-29 endgame review: this branch used to run BEFORE R1, so a leg
        # with no bid — the NORMAL state of a far-OTM short leg in its final
        # two days — deferred the "close unconditionally" rule behind the
        # 3-round gap counter. With two rounds a day that could push the exit
        # to expiry afternoon. R1 is frozen as unconditional; an unpriceable
        # structure inside the R1 window goes out at market NOW, which is the
        # same escalation R1 already uses when it can price the book.
        if dte <= DTE_CLOSE:
            return {**base, "action": "close", "rule": "R1_time",
                    "order_type": "market", "quote_gaps": 0,
                    "why": f"DTE {dte} <= {DTE_CLOSE} and a leg has no quote: "
                           f"R1 is unconditional and does not wait out the "
                           f"quote-gap counter this close to expiry (market "
                           f"order)"}
        gaps = quote_gaps + 1
        if gaps >= MAX_QUOTE_GAPS:
            return {**base, "action": "close", "rule": "quote_gap",
                    "order_type": "market", "quote_gaps": gaps,
                    "why": f"{gaps} consecutive rounds without a full quote; "
                           "never hold a position we cannot see"}
        return {**base, "action": "skip", "rule": "quote_gap",
                "quote_gaps": gaps,
                "why": f"missing mid on some leg (round {gaps}/{MAX_QUOTE_GAPS})"}

    if dte <= DTE_CLOSE:                                        # R1
        # 08-26 live round: a close priced at snapshot mid did not fill —
        # mid was 3.93 while the immediately executable credit was 3.82, so
        # the order sat. For R2/R3/R4 that is fine, they can wait a round.
        # R1 exists precisely to GUARANTEE we are out before pin/assignment
        # week, and a limit that never fills defeats the entire rule: the
        # position rides into expiry, which is the one outcome R1 forbids.
        # Urgency-tiered: the rule that must complete crosses the spread.
        return {**base, "action": "close", "rule": "R1_time", "quote_gaps": 0,
                "order_type": "market",
                "why": f"DTE {dte} <= {DTE_CLOSE}: avoid pin/assignment week "
                       f"(market order — this close must complete, not rest)"}
    if entry:                                                   # R2 / R3
        tp = entry * (TP_MULT_DEBIT if kind == "debit" else TP_MULT_CREDIT)
        sl = entry * (SL_MULT_DEBIT if kind == "debit" else SL_MULT_CREDIT)
        if value >= tp:
            return {**base, "action": "close", "rule": "R2_take_profit",
                    "order_type": "limit", "quote_gaps": 0,
                    "why": f"value {value} >= target {round(tp, 4)} ({kind})"}
        if value <= sl:
            return {**base, "action": "close", "rule": "R3_stop_loss",
                    "order_type": "limit", "quote_gaps": 0,
                    "why": f"value {value} <= stop {round(sl, 4)} ({kind})"}
    if flip and FLIP_CLOSE:                                     # R4
        return {**base, "action": "close", "rule": "R4_signal_flip",
                "order_type": "limit", "quote_gaps": 0,
                "why": "reverse signal: unwind before the flip guard admits "
                       "the new direction"}
    return {**base, "action": "hold", "rule": None, "quote_gaps": 0,
            "why": "no rule triggered"}


def close_legs(legs: list, mids: dict, quotes: dict | None = None) -> list:
    """Reverse every leg with explicit *_to_close intents, priced at mid.
    cli_executor._net_limit keeps the sign (credit close = negative limit).

    A leg with no mid gets `limit: None`, NOT 0.0. Flooring to zero used to
    turn "we cannot see this leg" into "accept any price for it" on a limit
    order; cli_executor now refuses such a leg outright, and the only path
    that legitimately has no price is the quote-gap force-close, which goes
    out as an explicit market order.
    """
    quotes = quotes or {}
    out = []
    for leg in legs:
        long = leg["qty"] > 0
        mid = mids.get(leg["occ_symbol"])
        # Price where the leg actually trades, not at the mid. Selling a long
        # leg fills at the bid; buying back a short fills at the ask. Pricing
        # both at mid asks the market for a better-than-market print, and the
        # order rests — which is merely slow for R2/R3/R4 and outright breaks
        # R1, whose whole purpose is to be OUT before assignment week.
        # CLOSE_CROSS_FRAC=1.0 crosses fully; 0.0 restores the old mid pricing.
        q = QUOTES.get(leg["occ_symbol"]) or quotes.get(leg["occ_symbol"]) or {}
        px = mid
        if (mid or 0) > 0 and q.get("bid") and q.get("ask"):
            target = q["bid"] if long else q["ask"]
            px = round(mid + (target - mid) * CLOSE_CROSS_FRAC, 2)
        out.append({"occ_symbol": leg["occ_symbol"],
                    "side": "sell" if long else "buy",
                    "position_intent": "sell_to_close" if long else "buy_to_close",
                    "qty": abs(leg["qty"]),
                    "limit": px if (px or 0) > 0 else None})
    return out


def structure_pnl(legs: list, entry: float | None, value: float | None
                  ) -> float | None:
    """Realized dollars on a closed structure: (V - E) x 100 x contracts.

    The -2% daily halt summed ledger records of kind "fill" — and nothing in
    this project has ever written a "fill". The halt was inert. This is the
    number that makes it real.
    """
    if entry is None or value is None or not legs:
        return None
    units = min(abs(int(l["qty"])) for l in legs)
    return round((value - entry) * 100 * units, 2)


def live_switch_armed() -> bool:
    """Is the second live switch set? Read at CALL time, never at import —
    .env is parsed after this module loads (the 08-25 CLAUDE_BIN family)."""
    return os.environ.get("ALPACA_HACKATHON_LIVE") == "1"


def _load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def check_positions(flips: dict | None = None, *, dry_run: bool = True,
                    today: date | None = None, positions: list | None = None,
                    mids: dict | None = None, executor=None,
                    ledger=None, run_date: str | None = None,
                    raw_positions: list | None = None) -> list:
    """One monitoring round over every open structure. Returns action records.

    `positions` / `mids` / `executor` are injectable for tests (default: live
    Alpaca fetch + the real mleg CLI path). When a `ledger` is given, the round
    also (a) reconciles the ledger's believed book against these positions
    before evaluating anything, and (b) appends a `position_closed` record for
    every executed close -- the exact record the direction-flip gate reads,
    closing the R4 loop with gates.py.

    Every structure is isolated. Before, one raising submit aborted the round
    for every OTHER underlying and skipped the state and close-log writes at
    the end, so the quote-gap counters silently never advanced and the
    judge-facing close log had no row for a round that definitely happened.
    """
    today = today or date.today()
    flips = flips or {}
    submit = executor or submit_legs
    state = _load_state()
    if positions is None:
        raw_positions = fetch_all_positions() if raw_positions is None else raw_positions
        positions = fetch_option_positions(raw_positions)
    groups = group_structures(positions)
    all_syms = [l["occ_symbol"] for legs in groups.values() for l in legs]
    if mids is None:
        mids = fetch_mids(all_syms) if all_syms else {}

    if ledger is not None:
        if raw_positions is not None:
            detect_non_option_positions(raw_positions, ledger=ledger,
                                        run_date=run_date)
        # book_direction, not structure_direction: an underlying may hold
        # several same-direction structures (two expiries), and calling that
        # "no position" would release the flip guard on a live book.
        actual = {und: d for und, legs in groups.items()
                  if (d := book_direction(legs))}
        # An underlying we hold but cannot read the direction of is NOT flat.
        # Reconciliation may only retire a belief when the broker truly has
        # nothing; anything else is a loud unknown, never a silent release.
        unreadable = [und for und, legs in groups.items()
                      if legs and book_direction(legs) is None]
        for und in unreadable:
            ledger.append(run_date or today.isoformat(), "live",
                          "position_direction_unknown", symbol=und,
                          why="legs are held but their direction cannot be "
                              "inferred (mixed directions, or a shape no rule "
                              "models). The flip guard stays as it is: an "
                              "unreadable book is not an empty one.")
        reconcile(ledger, run_date or today.isoformat(), actual)

    records = []
    for und, legs in sorted(groups.items()):
        gaps = int(state.get(und, {}).get("quote_gaps", 0))
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "underlying": und}
        try:
            verdict = evaluate(legs, mids, today,
                               flip=und in flips, quote_gaps=gaps)
            rec.update(verdict)
            if verdict["action"] == "close":
                unwind = close_legs(legs, mids)
                order_type = verdict.get("order_type", "limit")
                res = submit(unwind, dry_run=dry_run, order_type=order_type)
                pnl = structure_pnl(legs, verdict["entry"], verdict["value"])
                rec.update(legs=unwind, exec_ok=res.ok, dry_run=res.dry_run,
                           pnl=pnl)
                if res.ok and ledger is not None:
                    ledger.append(run_date or today.isoformat(), "live",
                                  "position_closed", symbol=und,
                                  rule=verdict["rule"], dry_run=res.dry_run,
                                  pnl=pnl, why=verdict["why"])
            state[und] = {"quote_gaps": verdict["quote_gaps"]}
        except Exception as e:                       # noqa: BLE001 -- isolate
            # Fail loudly for THIS structure and carry on with the others. A
            # position we could not act on keeps its gap counter, so the
            # force-close threshold is not silently reset by the failure.
            rec.setdefault("action", "error")
            rec.update(exec_ok=False, error=f"{type(e).__name__}: {e}",
                       rule=rec.get("rule"), dte=rec.get("dte"),
                       entry=rec.get("entry"), value=rec.get("value"))
            state[und] = {"quote_gaps": gaps}
            if ledger is not None:
                ledger.append(run_date or today.isoformat(), "live",
                              "close_check_error", symbol=und,
                              error=f"{type(e).__name__}: {e}")
        records.append(rec)

    # `finally`-grade: the round is recorded even when every structure failed.
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return records


if __name__ == "__main__":
    import sys
    # 08-25 live-test R2 finding: the standalone entry (the afternoon
    # scheduled task's payload) never loaded .env — keys were only loaded
    # by run.main(), so this path fail-crashed on a box that relies on .env.
    # 08-26 follow-up: load_env() itself resolved ".env" against the CWD, so
    # under the scheduled task (CWD = %windir%\system32) this fix did not
    # actually hold. It is repo-anchored now; see paths.py.
    from .order_cli import have_alpaca_keys, load_env
    load_env()
    if not have_alpaca_keys():
        # Fail loudly and early rather than as a traceback out of _headers()
        # several HTTP calls deep. No positions are touched.
        print("ALPACA_API_KEY / ALPACA_SECRET_KEY not set (looked in the "
              "environment and in the repo-root .env). Nothing checked.",
              file=sys.stderr)
        raise SystemExit(2)
    # 08-26 adversarial review: this task fires weekdays 12:15 PT with no idea
    # whether the market is open. On a holiday or after a 13:00 ET half-day
    # close it would price unwinds off a dead tape.
    from . import market_calendar
    from .chain_fetcher import fetch_clock
    live = "--live" in sys.argv
    if "--ignore-clock" not in sys.argv:
        try:
            state = market_calendar.clock_state(fetch_clock())
        except Exception as e:  # noqa: BLE001
            state = None
            print(f"clock unavailable ({type(e).__name__}); using the built-in "
                  f"ET calendar", file=sys.stderr)
        if state is None:
            state = market_calendar.session_state()
        if not state[0]:
            print(f"market closed — {state[1]}. Nothing checked, nothing sent.")
            raise SystemExit(0)
    if live and not live_switch_armed():
        # Fail here rather than inside every unwind: submit_legs raises, and a
        # raise per structure would have been logged as a close-check error
        # all week while positions quietly never closed.
        print("--live requires ALPACA_HACKATHON_LIVE=1 in the environment "
              "(scripts/run_close_check.cmd sets it). Nothing checked.",
              file=sys.stderr)
        raise SystemExit(2)

    flips = {}
    for a in sys.argv[1:]:
        if a.startswith("--flip="):
            flips[a.split("=", 1)[1].upper()] = True
    # 08-26 live round: this entry called check_positions WITHOUT a ledger, so
    # a close it executed at the broker was never recorded. Measured: the R4
    # unwind filled, NVDA left the account — and the flip guard still believed
    # NVDA was open, because `position_closed` is what releases it. This entry
    # IS the afternoon scheduled task's payload, so every close it ever made
    # would have locked its symbol out for the rest of the contest while the
    # account sat flat. The daily run passed a ledger; this path never did.
    from .ledger import Ledger
    recs = check_positions(flips=flips, dry_run=not live, ledger=Ledger())
    if not recs:
        print("no open option structures")
    for r in recs:
        print(f"{r['underlying']:6} {r['action']:5} "
              f"rule={r['rule']} dte={r['dte']} entry={r['entry']} "
              f"value={r['value']} :: {r['why']}")
