"""Deterministic risk gates. Every order passes through all of them; any veto
kills the order. Fail-closed: a structure the gates cannot price is vetoed.

Gates (agreed architecture, contract v1 with the signal side):
  1. Position size  — worst-case loss of the order <= 5% of equity.
  2. Daily loss halt — if today's realized PnL <= -2% of equity, no new orders.
  3. Idempotent dedup — the same (day, symbol, legs) order is never sent twice.
  4. Direction flip — while a position in a symbol is open, no order in the
     opposite direction is allowed. Exits belong to deterministic exit rules
     (DTE / take-profit / stop-loss, pre-registered config), not to a reverse
     signal — so a flip must wait until the exit rules have closed the
     conflicting position. This also bans hedged (long+short same symbol)
     books outright.

No LLM anywhere in this file. The red-team loop is a *separate*, additional
veto layer; these arithmetic gates run first and cannot be talked out of.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

MAX_POSITION_FRAC = 0.05   # worst-case loss per order <= 5% equity
DAILY_LOSS_HALT_FRAC = 0.02  # stop trading for the day at -2% equity


@dataclass(frozen=True)
class GateResult:
    gate: str
    allowed: bool
    reason: str

    def as_dict(self) -> dict:
        return {"gate": self.gate, "allowed": self.allowed, "reason": self.reason}


# ── worst-case loss of a leg structure ───────────────────────────────────

def estimate_max_loss(legs: list[dict], strikes: dict[str, dict]) -> float | None:
    """Worst-case dollar loss at expiry for the structures the mapper emits.

    `strikes` maps occ_symbol -> {"strike": float, "type": "call"|"put"}.
    Returns None for anything it cannot price (caller must fail closed).
    """
    if not legs:
        return 0.0
    try:
        for leg in legs:
            if leg["occ_symbol"] not in strikes:
                return None

        if len(legs) == 1:
            (leg,) = legs
            meta = strikes[leg["occ_symbol"]]
            prem = float(leg["limit"]) * 100 * leg["qty"]
            if leg["side"] == "buy":
                return prem                                   # long option: premium
            if meta["type"] == "put":                         # cash-secured put
                return meta["strike"] * 100 * leg["qty"] - prem
            return None                                       # naked short call: refuse

        if len(legs) == 2:
            buy = next((l for l in legs if l["side"] == "buy"), None)
            sell = next((l for l in legs if l["side"] == "sell"), None)
            if buy is None or sell is None or buy["qty"] != sell["qty"]:
                return None
            b, s = strikes[buy["occ_symbol"]], strikes[sell["occ_symbol"]]
            if b["type"] != s["type"]:
                return None
            qty = buy["qty"]
            net = float(buy["limit"]) - float(sell["limit"])
            if net >= 0:                                      # debit spread
                return net * 100 * qty
            width = abs(b["strike"] - s["strike"])            # credit spread
            return (width * 100 - (-net) * 100) * qty

        return None
    except (KeyError, TypeError, ValueError):
        return None


# ── individual gates ─────────────────────────────────────────────────────

def position_size_gate(max_loss: float | None, equity: float,
                       frac: float = MAX_POSITION_FRAC) -> GateResult:
    cap = equity * frac
    if max_loss is None:
        return GateResult("position_size", False,
                          "cannot price worst-case loss; failing closed")
    if max_loss > cap:
        return GateResult("position_size", False,
                          f"worst-case loss ${max_loss:,.0f} > "
                          f"{frac:.0%} of equity (${cap:,.0f})")
    return GateResult("position_size", True,
                      f"worst-case loss ${max_loss:,.0f} <= ${cap:,.0f}")


def daily_loss_halt_gate(realized_pnl_today: float, equity: float,
                         frac: float = DAILY_LOSS_HALT_FRAC) -> GateResult:
    floor = -equity * frac
    if realized_pnl_today <= floor:
        return GateResult("daily_loss_halt", False,
                          f"realized PnL ${realized_pnl_today:,.0f} <= "
                          f"halt floor ${floor:,.0f}; trading halted for the day")
    return GateResult("daily_loss_halt", True,
                      f"realized PnL ${realized_pnl_today:,.0f} above halt floor")


def dedup_key(run_date: str, symbol: str, legs: list[dict]) -> str:
    """Stable key for one intended order: same day + symbol + legs => same key."""
    canon = json.dumps({"run_date": run_date, "symbol": symbol, "legs": legs},
                       sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def direction_flip_gate(direction: str,
                        open_direction: str | None) -> GateResult:
    """Contract v1: never open against an existing position in the same symbol.

    `open_direction` is the direction of the currently open position for this
    symbol (from the ledger's fills stub until real fills exist), or None.
    Same-direction re-entry is left to the dedup/sizing gates; a *reverse*
    open is vetoed until the exit rules have closed the position.
    """
    if open_direction is not None and direction in ("long", "short") \
            and direction != open_direction:
        return GateResult(
            "direction_flip", False,
            f"open {open_direction} position conflicts with new {direction} "
            f"order; exit rules must close it first (no hedged positions)")
    return GateResult("direction_flip", True, "no conflicting open position")


def dedup_gate(key: str, already_seen: bool) -> GateResult:
    if already_seen:
        return GateResult("dedup", False,
                          f"order {key} already logged today; idempotent skip")
    return GateResult("dedup", True, f"order {key} not seen before")


# ── combined ─────────────────────────────────────────────────────────────

def run_gates(*, legs: list[dict], strikes: dict[str, dict], equity: float,
              realized_pnl_today: float, key: str, already_seen: bool,
              direction: str = "neutral", open_direction: str | None = None,
              ) -> tuple[bool, list[GateResult], float | None]:
    """Run all gates. Returns (allowed, results, max_loss)."""
    max_loss = estimate_max_loss(legs, strikes)
    results = [
        daily_loss_halt_gate(realized_pnl_today, equity),
        position_size_gate(max_loss, equity),
        dedup_gate(key, already_seen),
        direction_flip_gate(direction, open_direction),
    ]
    return all(r.allowed for r in results), results, max_loss
