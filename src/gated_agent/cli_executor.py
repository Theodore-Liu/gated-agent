"""Order execution via the official Alpaca CLI (clean-room, deterministic).

Why the CLI and not the SDK: the hackathon requires Alpaca's MCP server or CLI.
The CLI is the right fit for a scheduled, deterministic order path -- Alpaca
describes it as built for agent sessions and cron jobs, it emits structured
JSON, and multi-leg options orders are first-class (--order-class mleg,
<= 4 legs, one atomic submission). Verified live with --dry-run on 2026-08-24.

Safety model:
- dry_run=True (default) uses the CLI's own --dry-run: prints the request body,
  submits nothing. The executor refuses to place real orders unless BOTH
  dry_run=False AND env ALPACA_HACKATHON_LIVE=1 -- so pointing it at the wrong
  account can never fire by accident.
- Single-leg and multi-leg supported; net limit price for spreads is computed
  from the mapper's per-leg limits (positive = debit, negative = credit).
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


def cli_path() -> str:
    """Locate the Alpaca CLI binary.

    Order: $ALPACA_CLI override -> <repo root>/bin/alpaca(.exe) -> bare
    "alpaca" on PATH. The binary itself is NOT committed (see .gitignore);
    drop the official release into bin/ or set ALPACA_CLI.
    """
    override = os.environ.get("ALPACA_CLI")
    if override:
        return override
    root = Path(__file__).resolve().parents[2]        # src/gated_agent -> repo
    exe = root / "bin" / ("alpaca.exe" if os.name == "nt" else "alpaca")
    if exe.exists():
        return str(exe)
    return "alpaca"


@dataclass(frozen=True)
class ExecResult:
    ok: bool
    dry_run: bool
    request: dict | None
    raw: str


def _net_limit(legs: list) -> float:
    """Net price of the structure: buys pay, sells collect.

    Alpaca mleg convention: positive limit = debit you pay, NEGATIVE limit =
    credit you receive. Flooring a credit at +0.01 would give the premium away
    (caught in live dry-run testing 2026-08-24), so the sign must survive.
    """
    net = 0.0
    for leg in legs:
        sign = 1 if leg["side"] == "buy" else -1
        net += sign * float(leg["limit"] or 0.0)
    return round(net, 2)


def _cli_legs(legs: list) -> str:
    out = []
    for leg in legs:
        # Opening legs default to *_to_open; position_manager sets explicit
        # *_to_close intents on its unwind legs (re-synced from staging).
        intent = leg.get("position_intent") or (
            "buy_to_open" if leg["side"] == "buy" else "sell_to_open")
        out.append({
            "symbol": leg["occ_symbol"],
            "ratio_qty": "1",
            "side": leg["side"],
            "position_intent": intent,
        })
    return json.dumps(out)


def _check_priceable(legs: list) -> None:
    """A limit order needs a price for every leg.

    `position_manager.close_legs` used to floor a missing mid to 0.0, which on
    a limit close is an instruction to accept any price at all — the exact
    opposite of what the caller meant. A leg we cannot price does not get a
    made-up one; it gets refused, and the caller either waits for a quote or
    asks for an explicit market order.
    """
    for leg in legs:
        px = leg.get("limit")
        if px is None or float(px) <= 0:
            raise ValueError(
                f"leg {leg['occ_symbol']} has no usable limit price ({px!r}); "
                f"refusing to submit a limit order priced at zero — pass "
                f"order_type='market' if that is really what you want")


def submit_legs(legs: list, *, dry_run: bool = True,
                time_in_force: str = "day", order_type: str = "limit",
                env: dict | None = None) -> ExecResult:
    """Submit mapper output as one order (mleg for spreads, plain for 1 leg).

    `order_type="market"` is the quote-gap force-close path: `evaluate()`
    decides a blind position must go at market, and that decision has to
    survive all the way to the argv. It used to be computed and thrown away,
    so the "never hold a position we cannot see" rule submitted a LIMIT order
    priced off the very quotes that were missing.
    """
    if not legs:
        return ExecResult(True, dry_run, None, "stand aside: no legs")
    if len(legs) > 4:
        raise ValueError("mleg supports at most 4 legs")
    if order_type not in ("limit", "market"):
        raise ValueError(f"unsupported order type {order_type!r}")
    qtys = {leg["qty"] for leg in legs}
    if len(qtys) != 1:
        raise ValueError("all legs must share the same qty (ratio_qty=1 model)")
    qty = qtys.pop()
    if order_type == "limit":
        _check_priceable(legs)

    if not dry_run and os.environ.get("ALPACA_HACKATHON_LIVE") != "1":
        raise RuntimeError("refusing live order: ALPACA_HACKATHON_LIVE != 1")

    price = ([] if order_type == "market" else
             ["--limit-price", f"{float(legs[0]['limit']):.2f}"
              if len(legs) == 1 else f"{_net_limit(legs):.2f}"])
    if len(legs) == 1:
        leg = legs[0]
        args = ["order", "submit", "--symbol", leg["occ_symbol"],
                "--side", leg["side"], "--qty", str(qty),
                "--type", order_type, *price,
                "--time-in-force", time_in_force]
    else:
        args = ["order", "submit", "--order-class", "mleg",
                "--qty", str(qty), "--type", order_type, *price,
                "--time-in-force", time_in_force,
                "--legs", _cli_legs(legs)]
    if dry_run:
        args.append("--dry-run")

    r = subprocess.run([cli_path(), *args], capture_output=True, text=True,
                       env=env or os.environ.copy(), timeout=90)
    raw = (r.stdout or r.stderr).strip()
    req = None
    if r.returncode == 0:
        try:
            req = json.loads(raw)
        except ValueError:
            pass
    return ExecResult(r.returncode == 0, dry_run, req, raw)


def build_command_preview(legs: list, order_type: str = "limit") -> str:
    """For logs/UI: the exact CLI call that would run (dry-run form)."""
    res = []
    if order_type == "market":
        price: list = []
    elif len(legs) == 1:
        price = ["--limit-price", f"{float(legs[0]['limit'] or 0):.2f}"]
    else:
        price = ["--limit-price", f"{_net_limit(legs):.2f}"]
    if len(legs) == 1:
        leg = legs[0]
        res = ["alpaca", "order", "submit", "--symbol", leg["occ_symbol"],
               "--side", leg["side"], "--qty", str(leg["qty"]),
               "--type", order_type, *price]
    elif legs:
        res = ["alpaca", "order", "submit", "--order-class", "mleg",
               "--qty", str(legs[0]["qty"]), "--type", order_type, *price,
               "--legs", _cli_legs(legs)]
    return " ".join(res)
