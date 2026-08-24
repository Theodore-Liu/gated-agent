"""Signal -> options expression mapper (pure functions, no I/O).

Clean-room module for the Alpaca AI Trading Agents Hackathon.
Zero imports from, and zero knowledge of, any private trading system.

Contract (the isolation interface):
    input  signal  = {"symbol": str, "direction": "long"|"short"|"neutral",
                      "strength": float 0..1, "spot": float}
    input  chain   = list of contract dicts as returned by Alpaca's
                     /v2/options/contracts + quotes merged in:
                     {"symbol": OCC str, "type": "call"|"put",
                      "strike_price": float, "expiration_date": "YYYY-MM-DD",
                      "open_interest": int|None, "bid": float, "ask": float}
    output legs    = list of {"occ_symbol", "side": "buy"|"sell",
                              "qty": int, "limit": float} or [] if no trade.

Design choices (documented for the one-pager):
- Moneyness-based strike selection (ATM buy leg, ~2-3% OTM sell leg) instead of
  delta targets: works without greeks (indicative feed has none) and is easier
  to explain. Effectively delta ~0.5 / ~0.2-0.3 at short expiries.
- Defined-risk structures only (debit/credit spreads, cash-secured puts) --
  paper options level 3 allows them, and max loss is computable at entry,
  which the risk gate needs.
- Every rule is deterministic: same inputs -> same legs. No LLM anywhere here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class MapperConfig:
    # expiry window relative to "today" (days)
    min_dte: int = 5
    max_dte: int = 16
    # sell-leg / short-strike distance from spot
    otm_pct: float = 0.025            # 2.5% out of the money
    # liquidity gates
    max_spread_pct: float = 0.10      # (ask-bid)/mid must be <= 10%
    min_open_interest: int = 100      # applied only when OI is known
    # sizing
    equity: float = 100_000.0
    max_loss_frac: float = 0.01       # per-trade max loss <= 1% of equity
    # One CSP on a $500 underlying ties up $50k. On a $100k account that is
    # half the equity -- deliberate: the income tier holds ONE collateralised
    # position at a time. Lower this if trading cheaper underlyings.
    csp_collateral_frac: float = 0.50 # cash-secured put collateral cap
    # strength tiers
    strong: float = 0.7
    mild: float = 0.3


# ── helpers ──────────────────────────────────────────────────────────────

def _mid(c: dict) -> float:
    return (float(c["bid"]) + float(c["ask"])) / 2.0


def _tick_round(px: float) -> float:
    """Option price ticks: $0.01 under $3.00, $0.05 at or above."""
    if px < 3.0:
        return round(px, 2)
    return round(round(px / 0.05) * 0.05, 2)


def _liquid(c: dict, cfg: MapperConfig) -> bool:
    bid, ask = float(c.get("bid") or 0), float(c.get("ask") or 0)
    if bid <= 0 or ask <= 0 or ask < bid:
        return False
    mid = (bid + ask) / 2
    if mid <= 0 or (ask - bid) / mid > cfg.max_spread_pct:
        return False
    oi = c.get("open_interest")
    if oi is not None and int(oi) < cfg.min_open_interest:
        return False
    return True


def _dte(c: dict, today: date) -> int:
    exp = datetime.strptime(c["expiration_date"], "%Y-%m-%d").date()
    return (exp - today).days


def _eligible(chain: list, opt_type: str, today: date, cfg: MapperConfig) -> list:
    out = [c for c in chain
           if c["type"] == opt_type
           and cfg.min_dte <= _dte(c, today) <= cfg.max_dte
           and _liquid(c, cfg)]
    # nearest expiry first, then by strike
    return sorted(out, key=lambda c: (c["expiration_date"], float(c["strike_price"])))


def _nearest_strike(cands: list, target: float) -> dict | None:
    if not cands:
        return None
    return min(cands, key=lambda c: abs(float(c["strike_price"]) - target))


def _same_expiry(cands: list, expiry: str) -> list:
    return [c for c in cands if c["expiration_date"] == expiry]


# ── structures ───────────────────────────────────────────────────────────

def _debit_spread(chain, opt_type, spot, today, cfg):
    """Buy ATM, sell OTM. For calls OTM is above spot; for puts below."""
    cands = _eligible(chain, opt_type, today, cfg)
    buy = _nearest_strike(cands, spot)
    if buy is None:
        return []
    sign = 1 if opt_type == "call" else -1
    sell_target = spot * (1 + sign * cfg.otm_pct)
    sells = [c for c in _same_expiry(cands, buy["expiration_date"])
             if sign * (float(c["strike_price"]) - float(buy["strike_price"])) > 0]
    sell = _nearest_strike(sells, sell_target)
    if sell is None:
        return []
    debit = _mid(buy) - _mid(sell)
    if debit <= 0:
        return []
    # sizing: max loss of a debit spread = net debit * 100 per contract
    per_contract_loss = debit * 100
    qty = int((cfg.equity * cfg.max_loss_frac) // per_contract_loss)
    if qty < 1:
        return []
    return [
        {"occ_symbol": buy["symbol"], "side": "buy", "qty": qty,
         "limit": _tick_round(_mid(buy))},
        {"occ_symbol": sell["symbol"], "side": "sell", "qty": qty,
         "limit": _tick_round(_mid(sell))},
    ]


def _cash_secured_put(chain, spot, today, cfg):
    """Sell an OTM put, fully collateralised. Mild-bullish income."""
    cands = _eligible(chain, "put", today, cfg)
    target = spot * (1 - cfg.otm_pct)
    otm = [c for c in cands if float(c["strike_price"]) < spot]
    sell = _nearest_strike(otm, target)
    if sell is None:
        return []
    collateral = float(sell["strike_price"]) * 100
    qty = int((cfg.equity * cfg.csp_collateral_frac) // collateral)
    if qty < 1:
        return []
    return [{"occ_symbol": sell["symbol"], "side": "sell", "qty": qty,
             "limit": _tick_round(_mid(sell))}]


def _credit_call_spread(chain, spot, today, cfg):
    """Sell OTM call, buy further-OTM call. Mild-bearish, defined risk."""
    cands = _eligible(chain, "call", today, cfg)
    sell_target = spot * (1 + cfg.otm_pct)
    otm = [c for c in cands if float(c["strike_price"]) > spot]
    sell = _nearest_strike(otm, sell_target)
    if sell is None:
        return []
    buys = [c for c in _same_expiry(otm, sell["expiration_date"])
            if float(c["strike_price"]) > float(sell["strike_price"])]
    buy = _nearest_strike(buys, spot * (1 + 2 * cfg.otm_pct))
    if buy is None:
        return []
    credit = _mid(sell) - _mid(buy)
    if credit <= 0:
        return []
    width = (float(buy["strike_price"]) - float(sell["strike_price"])) * 100
    per_contract_loss = width - credit * 100
    if per_contract_loss <= 0:
        return []
    qty = int((cfg.equity * cfg.max_loss_frac) // per_contract_loss)
    if qty < 1:
        return []
    return [
        {"occ_symbol": sell["symbol"], "side": "sell", "qty": qty,
         "limit": _tick_round(_mid(sell))},
        {"occ_symbol": buy["symbol"], "side": "buy", "qty": qty,
         "limit": _tick_round(_mid(buy))},
    ]


# ── entry point ──────────────────────────────────────────────────────────

def map_signal(signal: dict, chain: list, today: date,
               cfg: MapperConfig = MapperConfig()) -> list:
    """Deterministic signal -> option legs. Empty list = stand aside."""
    direction = signal["direction"]
    strength = float(signal["strength"])
    spot = float(signal["spot"])

    if direction == "neutral" or strength < cfg.mild:
        return []
    if direction == "long":
        if strength >= cfg.strong:
            return _debit_spread(chain, "call", spot, today, cfg)
        return _cash_secured_put(chain, spot, today, cfg)
    if direction == "short":
        if strength >= cfg.strong:
            return _debit_spread(chain, "put", spot, today, cfg)
        return _credit_call_spread(chain, spot, today, cfg)
    raise ValueError(f"unknown direction: {direction!r}")
