"""Read-only Streamlit dashboard for the hackathon agent.

Satisfies the mandatory demo-URL requirement (Streamlit is one of the three
allowed platforms) without exposing any strategy logic: it only RENDERS state
that the account already shows publicly to its owner -- equity, open
positions, recent orders, and the agent's decision / close-rule logs.

Deploy (Streamlit Community Cloud):
  main file path  src/gated_agent/dashboard.py
  secrets         ALPACA_API_KEY / ALPACA_SECRET_KEY  (paper account)
Nothing here can place orders. See README "Dashboard" for the full note.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
import urllib.request

import streamlit as st

TRADING = "https://paper-api.alpaca.markets"
_ROOT = Path(__file__).resolve().parents[2]           # src/gated_agent -> repo

# Streamlit executes this file as a top-level script, not as a package module,
# so relative imports are unavailable and the package may not be on sys.path.
# Without this the dashboard was the one entry point that never loaded .env:
# it worked on Streamlit Cloud (secrets.toml) and reported "Account API
# unreachable" on the very box that runs the agent, where .env IS the source
# of truth. Same family as the 08-25 findings.
try:
    if str(_ROOT / "src") not in sys.path:
        sys.path.insert(0, str(_ROOT / "src"))
    from gated_agent.order_cli import load_env
    load_env(_ROOT / ".env")          # absent on Cloud -> no-op, secrets win
except Exception:  # noqa: BLE001 -- a dashboard must render without the package
    pass

st.set_page_config(page_title="Gated Agent — Live State", layout="wide")


def _secret(name: str) -> str:
    # Touching st.secrets raises when no secrets.toml exists (it does not fall
    # back like dict.get), which turned the env-var path into dead code and
    # made every non-Cloud run report "Account API unreachable".
    try:
        value = st.secrets.get(name)
    except Exception:  # noqa: BLE001 -- any secrets-backend failure -> env
        value = None
    return value or os.environ.get(name, "")


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID": _secret("ALPACA_API_KEY"),
        "APCA-API-SECRET-KEY": _secret("ALPACA_SECRET_KEY"),
    }


@st.cache_data(ttl=60)
def _get(path: str):
    req = urllib.request.Request(TRADING + path, headers=_headers())
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


st.title("Gated Agent — read-only state")
st.caption("Alpaca paper account · auto-refreshes each minute · "
           "no order controls exist on this page")

try:
    acct = _get("/v2/account")
except Exception as e:  # noqa: BLE001
    st.error(f"Account API unreachable: {e}")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Equity", f"${float(acct['equity']):,.0f}",
          f"{float(acct['equity']) - float(acct['last_equity']):+,.0f} today")
c2.metric("Cash", f"${float(acct['cash']):,.0f}")
c3.metric("Buying power", f"${float(acct['buying_power']):,.0f}")
c4.metric("Options level", acct.get("options_trading_level", "?"))

st.subheader("Open positions")
positions = _get("/v2/positions")
if positions:
    st.dataframe([{
        "symbol": p["symbol"],
        "qty": p["qty"],
        "avg entry": p["avg_entry_price"],
        "market value": f"${float(p['market_value']):,.0f}",
        "unrealized P&L": f"${float(p['unrealized_pl']):,.2f}",
    } for p in positions], width="stretch")
else:
    st.info("No open positions.")

st.subheader("Recent orders")
orders = _get("/v2/orders?status=all&limit=20")
if orders:
    st.dataframe([{
        "submitted": o["submitted_at"][:19],
        "class": o.get("order_class") or "single",
        "legs": len(o.get("legs") or []) or 1,
        "symbol": o.get("symbol") or "(mleg)",
        "side": o.get("side") or "-",
        "qty": o.get("qty"),
        "limit": o.get("limit_price"),
        "status": o["status"],
    } for o in orders], width="stretch")
else:
    st.info("No orders yet.")

st.subheader("Close-rule checks (R1–R4, pre-registered)")
close_rows = _jsonl(_ROOT / "ledger" / "close_log.jsonl")
if close_rows:
    st.dataframe([{k: r.get(k) for k in
                   ("ts", "underlying", "action", "rule", "dte",
                    "entry", "value", "kind", "why")}
                  for r in close_rows[-30:]], width="stretch")
else:
    st.info("Close log not present in this deployment "
            "(the agent writes ledger/close_log.jsonl at runtime).")

st.subheader("Decision ledger (gates · red-team · orders · shadow twin)")
rows = _jsonl(_ROOT / "ledger" / "decisions.jsonl")
if rows:
    st.dataframe([{k: json.dumps(r.get(k), ensure_ascii=False, default=str)
                   if isinstance(r.get(k), (dict, list)) else r.get(k)
                   for k in ("ts", "book", "kind", "symbol", "allowed",
                             "max_loss", "status")}
                  for r in rows[-40:]], width="stretch")
else:
    st.info("Decision ledger not present in this deployment "
            "(the agent writes ledger/decisions.jsonl at runtime).")
