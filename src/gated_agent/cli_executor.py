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
        net += sign * float(leg["limit"])
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


def submit_legs(legs: list, *, dry_run: bool = True,
                time_in_force: str = "day", env: dict | None = None) -> ExecResult:
    """Submit mapper output as one order (mleg for spreads, plain for 1 leg)."""
    if not legs:
        return ExecResult(True, dry_run, None, "stand aside: no legs")
    if len(legs) > 4:
        raise ValueError("mleg supports at most 4 legs")
    qtys = {leg["qty"] for leg in legs}
    if len(qtys) != 1:
        raise ValueError("all legs must share the same qty (ratio_qty=1 model)")
    qty = qtys.pop()

    if not dry_run and os.environ.get("ALPACA_HACKATHON_LIVE") != "1":
        raise RuntimeError("refusing live order: ALPACA_HACKATHON_LIVE != 1")

    if len(legs) == 1:
        leg = legs[0]
        args = ["order", "submit", "--symbol", leg["occ_symbol"],
                "--side", leg["side"], "--qty", str(qty),
                "--type", "limit", "--limit-price", f"{float(leg['limit']):.2f}",
                "--time-in-force", time_in_force]
    else:
        args = ["order", "submit", "--order-class", "mleg",
                "--qty", str(qty), "--type", "limit",
                "--limit-price", f"{_net_limit(legs):.2f}",
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


def build_command_preview(legs: list) -> str:
    """For logs/UI: the exact CLI call that would run (dry-run form)."""
    res = []
    if len(legs) == 1:
        leg = legs[0]
        res = ["alpaca", "order", "submit", "--symbol", leg["occ_symbol"],
               "--side", leg["side"], "--qty", str(leg["qty"]),
               "--type", "limit", "--limit-price", f"{float(leg['limit']):.2f}"]
    elif legs:
        res = ["alpaca", "order", "submit", "--order-class", "mleg",
               "--qty", str(legs[0]["qty"]), "--type", "limit",
               "--limit-price", f"{_net_limit(legs):.2f}",
               "--legs", _cli_legs(legs)]
    return " ".join(res)
