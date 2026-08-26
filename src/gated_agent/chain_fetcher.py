"""Fetch an options chain from Alpaca and shape it for options_mapper.

Clean-room module. Reads keys from environment (ALPACA_API_KEY / ALPACA_SECRET_KEY
or a dotenv file passed explicitly). Read-only: contracts + quotes, no orders.

Data flow:
    /v2/options/contracts        -> strikes, expiries, OI      (trading API)
    /v1beta1/options/snapshots   -> bid/ask quotes             (data API)
    merge on OCC symbol          -> mapper's chain format
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import date, timedelta

TRADING = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"


def _headers() -> dict:
    key = os.environ.get("ALPACA_API_KEY", "")
    sec = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not sec:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_account() -> dict:
    """Raw /v2/account. `equity` sizes the gates; `last_equity` (the previous
    session's close) gives the day's PnL independently of our own bookkeeping,
    which is what the -2% halt cross-checks itself against."""
    return _get(f"{TRADING}/v2/account")


def fetch_equity() -> float:
    """Paper-account equity, for gate sizing. (Integration addition to the
    staging module: same read-only auth/_get plumbing, /v2/account endpoint.)"""
    return float(fetch_account()["equity"])


def fetch_clock() -> dict:
    """Alpaca's own market clock — authoritative for holidays, early closes,
    DST and unscheduled closures. The scheduled tasks fire on a weekday
    calendar that knows none of those things."""
    return _get(f"{TRADING}/v2/clock")


def fetch_spot(symbol: str) -> float:
    d = _get(f"{DATA}/v2/stocks/{symbol}/snapshot")
    trade = d.get("latestTrade") or {}
    px = trade.get("p")
    if not px:
        raise RuntimeError(f"no latest trade for {symbol}")
    return float(px)


def fetch_chain(symbol: str, today: date, min_dte: int = 5, max_dte: int = 16,
                feed: str = "indicative") -> list:
    """Return mapper-format chain: contracts in the DTE window with live quotes."""
    lo = (today + timedelta(days=min_dte)).isoformat()
    hi = (today + timedelta(days=max_dte)).isoformat()

    contracts: list = []
    page_token = None
    while True:
        q = {"underlying_symbols": symbol, "limit": 500,
             "expiration_date_gte": lo, "expiration_date_lte": hi}
        if page_token:
            q["page_token"] = page_token
        d = _get(f"{TRADING}/v2/options/contracts?" + urllib.parse.urlencode(q))
        contracts += d.get("option_contracts") or []
        page_token = d.get("next_page_token")
        if not page_token:
            break

    # snapshots give quote + greeks in one call; batches of <=100 symbols
    snaps: dict = {}
    syms = [c["symbol"] for c in contracts]
    for i in range(0, len(syms), 100):
        batch = ",".join(syms[i:i + 100])
        d = _get(f"{DATA}/v1beta1/options/snapshots?"
                 + urllib.parse.urlencode({"symbols": batch, "feed": feed}))
        snaps.update(d.get("snapshots") or {})

    chain = []
    for c in contracts:
        s = snaps.get(c["symbol"]) or {}
        q = s.get("latestQuote") or {}
        g = s.get("greeks") or {}
        chain.append({
            "symbol": c["symbol"],
            "type": c["type"],
            "strike_price": float(c["strike_price"]),
            "expiration_date": c["expiration_date"],
            "open_interest": (int(c["open_interest"])
                              if c.get("open_interest") is not None else None),
            "bid": float(q.get("bp") or 0),
            "ask": float(q.get("ap") or 0),
            # None when the feed omits greeks (weekends, stale data) --
            # the mapper falls back to moneyness selection then.
            "delta": g.get("delta"),
        })
    return chain


if __name__ == "__main__":
    import sys
    from gated_agent.options_mapper import MapperConfig, map_signal
    # Same defect the 08-25 sweep found in position_manager's standalone
    # entry: every `python -m` entry point must load .env itself, because
    # only run.main() used to. Without this, `python -m gated_agent.
    # chain_fetcher SPY` dies in _headers() on a box that keeps keys in .env.
    from gated_agent.order_cli import load_env
    load_env()

    sym = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    today = date.today()
    spot = fetch_spot(sym)
    chain = fetch_chain(sym, today)
    quoted = [c for c in chain if c["bid"] > 0 and c["ask"] > 0]
    print(f"{sym}: spot={spot}  contracts={len(chain)}  with-quotes={len(quoted)}")

    for direction, strength, label in (("long", 0.9, "strong-long -> call debit spread"),
                                       ("long", 0.5, "mild-long   -> credit put spread"),
                                       ("short", 0.9, "strong-short-> put debit spread"),
                                       ("short", 0.5, "mild-short  -> credit call spread")):
        legs = map_signal({"symbol": sym, "direction": direction,
                           "strength": strength, "spot": spot}, chain, today)
        print(f"\n[{label}]")
        if not legs:
            print("  stand aside (no eligible structure)")
        for leg in legs:
            print(f"  {leg['side']:4} {leg['qty']}x {leg['occ_symbol']}  limit={leg['limit']}")
