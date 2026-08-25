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
from datetime import date, datetime
from pathlib import Path

from .chain_fetcher import DATA, TRADING, _headers
from .cli_executor import submit_legs

_ROOT = Path(__file__).resolve().parents[2]           # src/gated_agent -> repo
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


def fetch_option_positions() -> list:
    """Open option positions -> [{occ_symbol, qty(signed int), entry(float)}]."""
    out = []
    for p in _get(f"{TRADING}/v2/positions"):
        if p.get("asset_class") != "us_option":
            continue
        out.append({"occ_symbol": p["symbol"], "qty": int(float(p["qty"])),
                    "entry": float(p["avg_entry_price"])})
    return out


def fetch_mids(symbols: list) -> dict:
    """OCC symbol -> mid, or None when bid/ask missing (quote gap)."""
    mids: dict = {}
    for i in range(0, len(symbols), 100):
        batch = ",".join(symbols[i:i + 100])
        d = _get(f"{DATA}/v1beta1/options/snapshots?" + urllib.parse.urlencode(
            {"symbols": batch, "feed": "indicative"}))
        for sym, s in (d.get("snapshots") or {}).items():
            q = s.get("latestQuote") or {}
            bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
            mids[sym] = round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else None
    return {s: mids.get(s) for s in symbols}


def group_structures(positions: list) -> dict:
    """Group legs by underlying: one structure per underlying by design
    (the direction-flip gate forbids a second same-underlying entry)."""
    groups: dict = {}
    for p in positions:
        occ = parse_occ(p["occ_symbol"])
        if occ is None:
            continue
        groups.setdefault(occ["underlying"], []).append({**p, **occ})
    return groups


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
        return {**base, "action": "close", "rule": "R1_time", "quote_gaps": 0,
                "order_type": "limit",
                "why": f"DTE {dte} <= {DTE_CLOSE}: avoid pin/assignment week"}
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


def close_legs(legs: list, mids: dict) -> list:
    """Reverse every leg with explicit *_to_close intents, priced at mid.
    cli_executor._net_limit keeps the sign (credit close = negative limit)."""
    out = []
    for leg in legs:
        long = leg["qty"] > 0
        out.append({"occ_symbol": leg["occ_symbol"],
                    "side": "sell" if long else "buy",
                    "position_intent": "sell_to_close" if long else "buy_to_close",
                    "qty": abs(leg["qty"]),
                    "limit": mids.get(leg["occ_symbol"]) or 0.0})
    return out


def _load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def check_positions(flips: dict | None = None, *, dry_run: bool = True,
                    today: date | None = None, positions: list | None = None,
                    mids: dict | None = None, executor=None,
                    ledger=None, run_date: str | None = None) -> list:
    """One monitoring round over every open structure. Returns action records.

    `positions` / `mids` / `executor` are injectable for tests (default: live
    Alpaca fetch + the real mleg CLI path). When a `ledger` is given, every
    executed close also appends a `position_closed` record -- the exact record
    the direction-flip gate reads, closing the R4 loop with gates.py.
    """
    today = today or date.today()
    flips = flips or {}
    submit = executor or submit_legs
    state = _load_state()
    if positions is None:
        positions = fetch_option_positions()
    groups = group_structures(positions)
    all_syms = [l["occ_symbol"] for legs in groups.values() for l in legs]
    if mids is None:
        mids = fetch_mids(all_syms) if all_syms else {}

    records = []
    for und, legs in sorted(groups.items()):
        gaps = int(state.get(und, {}).get("quote_gaps", 0))
        verdict = evaluate(legs, mids, today,
                           flip=und in flips, quote_gaps=gaps)
        rec = {"ts": datetime.now().isoformat(timespec="seconds"),
               "underlying": und, **verdict}
        if verdict["action"] == "close":
            unwind = close_legs(legs, mids)
            res = submit(unwind, dry_run=dry_run)
            rec.update(legs=unwind, exec_ok=res.ok, dry_run=res.dry_run)
            if res.ok and ledger is not None:
                ledger.append(run_date or today.isoformat(), "live",
                              "position_closed", symbol=und,
                              rule=verdict["rule"], dry_run=res.dry_run,
                              why=verdict["why"])
        state[und] = {"quote_gaps": verdict["quote_gaps"]}
        records.append(rec)

    STATE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f)
    with open(LOG, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return records


if __name__ == "__main__":
    import sys
    flips = {}
    for a in sys.argv[1:]:
        if a.startswith("--flip="):
            flips[a.split("=", 1)[1].upper()] = True
    recs = check_positions(flips=flips, dry_run="--live" not in sys.argv)
    if not recs:
        print("no open option structures")
    for r in recs:
        print(f"{r['underlying']:6} {r['action']:5} "
              f"rule={r['rule']} dte={r['dte']} entry={r['entry']} "
              f"value={r['value']} :: {r['why']}")
