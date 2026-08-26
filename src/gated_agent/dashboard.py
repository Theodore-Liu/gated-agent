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
read_jsonl = None
try:
    if str(_ROOT / "src") not in sys.path:
        sys.path.insert(0, str(_ROOT / "src"))
    from gated_agent.ledger import read_jsonl
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
    """Tolerant read, shared with the agent (gated_agent.ledger.read_jsonl).

    The dashboard used to run json.loads over every line with its own reader,
    so the torn final line a killed task leaves behind blanked the demo URL
    with a traceback at the same moment it bricked the agent. Both now agree
    on what a half-written file says.
    """
    try:
        if read_jsonl is not None:
            rows, _torn = read_jsonl(path)
            return rows
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out
    except Exception:  # noqa: BLE001 -- never traceback on the judges' page
        return []


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _section(title: str, path: str):
    """Fetch one endpoint under its own guard. One shared try/except used to
    cover /v2/account only, so any hiccup on positions or orders — or a null
    field in a single row — rendered a Python traceback to the judges."""
    st.subheader(title)
    try:
        return _get(path), None
    except Exception as e:  # noqa: BLE001
        st.warning(f"{title}: Alpaca API unreachable right now ({e}). "
                   f"The agent is unaffected; this page is read-only.")
        return None, e


st.title("Gated Agent — read-only state")
st.caption("Alpaca paper account · auto-refreshes each minute · "
           "no order controls exist on this page")

try:
    acct = _get("/v2/account")
except Exception as e:  # noqa: BLE001
    st.error(f"Account API unreachable: {e}")
    acct = None

if acct:
    equity, last = _num(acct.get("equity")), _num(acct.get("last_equity"))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Equity", f"${equity:,.0f}",
              f"{equity - last:+,.0f} today" if last else None)
    c2.metric("Cash", f"${_num(acct.get('cash')):,.0f}")
    c3.metric("Buying power", f"${_num(acct.get('buying_power')):,.0f}")
    c4.metric("Options level", acct.get("options_trading_level", "?"))

positions, _err = _section("Open positions", "/v2/positions")
if positions:
    st.dataframe([{
        "symbol": p.get("symbol"),
        "asset": p.get("asset_class"),
        "qty": p.get("qty"),
        "avg entry": p.get("avg_entry_price"),
        "market value": f"${_num(p.get('market_value')):,.0f}",
        "unrealized P&L": f"${_num(p.get('unrealized_pl')):,.2f}",
    } for p in positions], width="stretch")
elif positions is not None:
    st.info("No open positions.")

orders, _err = _section("Recent orders", "/v2/orders?status=all&limit=20")
if orders:
    st.dataframe([{
        "submitted": (o.get("submitted_at") or "")[:19],
        "class": o.get("order_class") or "single",
        "legs": len(o.get("legs") or []) or 1,
        "symbol": o.get("symbol") or "(mleg)",
        "side": o.get("side") or "-",
        "qty": o.get("qty"),
        "type": o.get("type"),
        "limit": o.get("limit_price"),
        "status": o.get("status"),
    } for o in orders], width="stretch")
elif orders is not None:
    st.info("No orders yet.")

st.subheader("Close-rule checks (R1–R4, pre-registered)")
close_rows = _jsonl(_ROOT / "ledger" / "close_log.jsonl")
if close_rows:
    st.dataframe([{k: r.get(k) for k in
                   ("ts", "underlying", "action", "rule", "dte",
                    "entry", "value", "kind", "pnl", "why")}
                  for r in close_rows[-30:]], width="stretch")
else:
    st.info("Close log not present in this deployment "
            "(the agent writes ledger/close_log.jsonl at runtime).")

st.subheader("Decision ledger (gates · red-team · orders · shadow twin)")
rows = _jsonl(_ROOT / "ledger" / "decisions.jsonl")
if rows:
    alarms = [r for r in rows[-200:] if r.get("kind") in
              ("redteam_infra_alarm", "assignment_suspected",
               "ledger_torn_tail")]
    for a in alarms[-5:]:
        st.warning(f"{a.get('kind')}: {a.get('why') or a.get('reason') or ''}")
    st.dataframe([{k: json.dumps(r.get(k), ensure_ascii=False, default=str)
                   if isinstance(r.get(k), (dict, list)) else r.get(k)
                   for k in ("ts", "book", "kind", "symbol", "allowed",
                             "max_loss", "status", "pnl")}
                  for r in rows[-40:]], width="stretch")
else:
    st.info("Decision ledger not present in this deployment "
            "(the agent writes ledger/decisions.jsonl at runtime).")
