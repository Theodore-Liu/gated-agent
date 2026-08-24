"""Order path via the Alpaca CLI — deterministic, no LLM anywhere near it.

Design: the LLM (red-team loop) can only VETO; it can never construct or
mutate an order. Orders are built by pure code and would be submitted through
the Alpaca CLI as an auditable subprocess call.

The account does not exist yet, so the CLI is stubbed behind the `Broker`
interface below:

  * `cli_command()` builds the exact argv we intend to run — this is real and
    tested today.
  * `StubCLIBroker` synthesizes an option chain (clearly labeled synthetic;
    swap for `alpaca options chain` / the market-data API once keys exist)
    and, in dry-run mode, logs the intended command instead of executing it.
  * Live submission raises NotImplementedError until the account is wired.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, timedelta


# ── OCC symbology helpers ────────────────────────────────────────────────

def occ_symbol(root: str, expiry: date, opt_type: str, strike: float) -> str:
    """e.g. SPY 2026-09-04 call 640.0 -> SPY260904C00640000"""
    cp = "C" if opt_type == "call" else "P"
    return f"{root}{expiry.strftime('%y%m%d')}{cp}{int(round(strike * 1000)):08d}"


def cli_command(legs: list[dict]) -> list[str]:
    """The exact Alpaca CLI argv we intend to run for these legs (one call
    per leg; day limit orders). Deterministic — same legs, same argv."""
    cmds = []
    for leg in legs:
        cmds.append([
            "alpaca", "orders", "create",
            "--symbol", leg["occ_symbol"],
            "--side", leg["side"],
            "--qty", str(leg["qty"]),
            "--type", "limit",
            "--limit-price", f"{leg['limit']:.2f}",
            "--time-in-force", "day",
        ])
    return cmds


# ── broker interface ─────────────────────────────────────────────────────

class Broker(ABC):
    @abstractmethod
    def get_equity(self) -> float: ...

    @abstractmethod
    def get_option_chain(self, symbol: str, spot: float, today: date) -> list[dict]:
        """Contracts in the shape options_mapper expects (see its docstring)."""

    @abstractmethod
    def submit_order(self, symbol: str, legs: list[dict], dedup_key: str) -> dict: ...


class StubCLIBroker(Broker):
    """Stand-in until the Alpaca paper account exists.

    - equity: fixed paper-account default ($100k).
    - option chain: SYNTHETIC (next weekly expiry, +/-6% strikes, spread ~2%
      of mid, plausible time value). Labeled in every contract so a synthetic
      quote can never be mistaken for a real one downstream.
    - submit_order: dry-run only — returns the intended CLI commands.
    """

    def __init__(self, equity: float = 100_000.0, dry_run: bool = True):
        self.equity = equity
        self.dry_run = dry_run
        self.submitted: list[dict] = []   # test hook: what reached the broker

    def get_equity(self) -> float:
        return self.equity

    def get_option_chain(self, symbol: str, spot: float, today: date) -> list[dict]:
        # next Friday at least 5 days out (matches mapper's 5..16 DTE window)
        exp = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
        if (exp - today).days < 5:
            exp += timedelta(days=7)
        chain = []
        step = max(round(spot * 0.005), 1)          # ~0.5% strike spacing
        for i in range(-12, 13):                    # +/- ~6%
            k = float(round(spot) + i * step)
            # time value: rich ATM, decaying with distance from spot
            tv = max(spot * 0.012 - 0.35 * abs(spot - k), spot * 0.001)
            for typ in ("call", "put"):
                intrinsic = max(spot - k, 0.0) if typ == "call" else max(k - spot, 0.0)
                mid = intrinsic + tv
                half = max(mid * 0.01, 0.02)        # ~2% spread, passes 10% gate
                chain.append({
                    "symbol": occ_symbol(symbol, exp, typ, k),
                    "type": typ,
                    "strike_price": k,
                    "expiration_date": exp.isoformat(),
                    "open_interest": 1000,
                    "bid": round(mid - half, 2),
                    "ask": round(mid + half, 2),
                    "synthetic": True,              # <- honesty flag
                })
        return chain

    def submit_order(self, symbol: str, legs: list[dict], dedup_key: str) -> dict:
        cmds = cli_command(legs)
        record = {"symbol": symbol, "legs": legs, "dedup_key": dedup_key,
                  "cli_commands": cmds}
        if self.dry_run:
            record["status"] = "dry_run"
            self.submitted.append(record)
            return record
        raise NotImplementedError(
            "Live submission awaits the Alpaca paper account: wire real keys "
            "from .env and replace StubCLIBroker with a subprocess runner for "
            "the argv from cli_command()."
        )
