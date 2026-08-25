"""Order path via the Alpaca CLI — deterministic, no LLM anywhere near it.

Design: the LLM (red-team loop) can only VETO; it can never construct or
mutate an order. Orders are built by pure code and would be submitted through
the Alpaca CLI as an auditable subprocess call.

Two implementations sit behind the `Broker` interface:

  * `StubCLIBroker` — synthesizes an option chain (clearly labeled synthetic)
    and logs the intended command instead of executing it. Used whenever no
    Alpaca keys are configured; keeps tests and the demo fully offline.
  * `AlpacaCLIBroker` — the real path: option chain + equity from Alpaca's
    read-only APIs (chain_fetcher) and order submission through the official
    Alpaca CLI (cli_executor, `--order-class mleg`, atomic spreads). Defaults
    to the CLI's own --dry-run; a real order additionally requires
    ALPACA_HACKATHON_LIVE=1 (enforced in cli_executor — belt and braces).

`broker_from_env()` picks between them: keys in the environment / .env give
the real adapter (still dry-run by default), no keys means stub — so a fresh
clone with no .env behaves exactly as before.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from datetime import date, timedelta
from pathlib import Path

from . import chain_fetcher, cli_executor


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
            "StubCLIBroker never submits. Configure Alpaca keys in .env so "
            "broker_from_env() returns AlpacaCLIBroker instead."
        )


class AlpacaCLIBroker(Broker):
    """Real adapter: Alpaca read-only APIs for data, official Alpaca CLI for
    orders (one atomic mleg submission per spread).

    dry_run=True (default) passes --dry-run to the CLI: full request built and
    echoed by Alpaca's own tooling, nothing submitted. dry_run=False submits
    for real and is additionally hard-gated on ALPACA_HACKATHON_LIVE=1 inside
    cli_executor.submit_legs.

    `executor` is injectable for tests (no subprocess, no binary needed).
    """

    def __init__(self, dry_run: bool = True, executor=None):
        self.dry_run = dry_run
        self._executor = executor or cli_executor.submit_legs
        self.submitted: list[dict] = []   # test hook, same as the stub

    def get_equity(self) -> float:
        return chain_fetcher.fetch_equity()

    def get_option_chain(self, symbol: str, spot: float, today: date) -> list[dict]:
        # spot is unused: the chain window is DTE-based; quotes come live.
        return chain_fetcher.fetch_chain(symbol, today)

    def submit_order(self, symbol: str, legs: list[dict], dedup_key: str) -> dict:
        res = self._executor(legs, dry_run=self.dry_run)
        if not res.ok:
            status = "error"
        elif res.dry_run:
            status = "dry_run"
        else:
            status = "submitted"
        record = {"symbol": symbol, "legs": legs, "dedup_key": dedup_key,
                  "cli_commands": [cli_executor.build_command_preview(legs)],
                  "status": status, "ok": res.ok, "raw": res.raw,
                  "request": res.request}
        self.submitted.append(record)
        return record


# ── environment wiring ───────────────────────────────────────────────────

def load_env(path: str | os.PathLike = ".env") -> dict[str, str]:
    """Minimal .env loader (no extra dependency). KEY=VALUE lines, `#`
    comments; never overwrites variables already set in the environment.
    Accepts both naming schemes and mirrors ALPACA_API_KEY_ID /
    ALPACA_API_SECRET_KEY onto the ALPACA_API_KEY / ALPACA_SECRET_KEY names
    that chain_fetcher reads. Missing file -> no-op (stub mode)."""
    loaded: dict[str, str] = {}
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
                loaded[k] = v
    for src, dst in (("ALPACA_API_KEY_ID", "ALPACA_API_KEY"),
                     ("ALPACA_API_SECRET_KEY", "ALPACA_SECRET_KEY")):
        if os.environ.get(src) and not os.environ.get(dst):
            os.environ[dst] = os.environ[src]
            loaded[dst] = os.environ[src]
    return loaded


def have_alpaca_keys() -> bool:
    return bool(os.environ.get("ALPACA_API_KEY")
                and os.environ.get("ALPACA_SECRET_KEY"))


def broker_from_env(dry_run: bool = True,
                    env_file: str | os.PathLike = ".env") -> Broker:
    """Keys present (env or .env) -> AlpacaCLIBroker; absent -> StubCLIBroker.
    Placeholder values from .env.example are treated as absent."""
    load_env(env_file)
    if have_alpaca_keys() and "your_" not in os.environ["ALPACA_API_KEY"]:
        return AlpacaCLIBroker(dry_run=dry_run)
    return StubCLIBroker(dry_run=True)
