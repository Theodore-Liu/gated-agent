"""Preflight for the competition account swap. Prints no credential material.

Run it before the swap to record what the rehearsal account looks like, and
again after, to prove the swap actually took effect. It answers the four
questions that decide whether day 1 can trade at all, and it deliberately never
echoes a key, a prefix, a suffix, or a hash of one -- only whether a value is
set, and facts the broker reports back.

    python scripts/verify_account_swap.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REQUIRED_EQUITY = 100_000.0
REQUIRED_OPTIONS_LEVEL = 3


def load_env(path: Path) -> None:
    if not path.exists():
        sys.exit(f"no .env at {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip()


def api(base: str, path: str, key: str, secret: str):
    req = urllib.request.Request(
        base + path,
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    load_env(root / ".env")

    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        print("FAIL  credentials: ALPACA_API_KEY / ALPACA_SECRET_KEY not both set")
        return 1
    print(f"ok    credentials: both set ({len(key)} / {len(secret)} chars, values not shown)")

    paper = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
    base = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
    print(f"ok    endpoint: {'paper' if paper else 'LIVE MONEY'} -> {base}")
    if not paper:
        print("FAIL  ALPACA_PAPER is not true. The hackathon is a paper competition.")
        return 1

    try:
        acct = api(base, "/v2/account", key, secret)
    except urllib.error.HTTPError as exc:
        print(f"FAIL  account: HTTP {exc.code} -- the keys are rejected by this endpoint")
        return 1

    number = acct.get("account_number")
    equity = float(acct.get("equity", 0))
    level = acct.get("options_approved_level")
    status = acct.get("status")

    print(f"ok    account_number: {number}   <- this is what goes on the submission form")
    print(f"{'ok   ' if status == 'ACTIVE' else 'FAIL '} status: {status}")

    # Options level 3 is what multi-leg debit/credit spreads need. A fresh paper
    # account does not necessarily come with it, and the failure mode is an
    # order rejection at the worst possible moment rather than a clear error.
    if level is not None and int(level) >= REQUIRED_OPTIONS_LEVEL:
        print(f"ok    options_approved_level: {level} (spreads need >= {REQUIRED_OPTIONS_LEVEL})")
    else:
        print(f"FAIL  options_approved_level: {level} -- spreads need >= "
              f"{REQUIRED_OPTIONS_LEVEL}. Enable it in the Alpaca dashboard "
              f"before arming anything.")

    if abs(equity - REQUIRED_EQUITY) < 1.0:
        print(f"ok    equity: ${equity:,.2f} (a fresh competition account)")
    else:
        print(f"WARN  equity: ${equity:,.2f}, not ${REQUIRED_EQUITY:,.0f}. "
              f"If this is still the rehearsal account, the swap has not taken effect.")

    positions = api(base, "/v2/positions", key, secret)
    if positions:
        print(f"WARN  open positions: {len(positions)} -- a fresh competition "
              f"account should have none. Reconcile will adopt whatever the "
              f"broker actually holds, so this is not a crash, but it is not a "
              f"clean start either.")
    else:
        print("ok    open positions: 0")

    live = os.environ.get("ALPACA_HACKATHON_LIVE", "0")
    print(f"{'ARMED' if live == '1' else 'safe '} ALPACA_HACKATHON_LIVE={live}"
          f"  ({'live orders permitted' if live == '1' else 'live orders refused by cli_executor'})")

    clock = api(base, "/v2/clock", key, secret)
    print(f"ok    market open: {clock.get('is_open')}   next_close {clock.get('next_close')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
