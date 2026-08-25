"""Toy trend signal from public literature: Faber's 10-month SMA rule.

Reference: Mebane Faber, "A Quantitative Approach to Tactical Asset
Allocation" (Journal of Wealth Management) — the classic rule: be long when
price closes above its 10-month simple moving average, out (here: short)
when below. We approximate 10 months as 210 trading days on daily closes.

This is intentionally a TOY. It exists so the interesting parts of this
project (options mapping, risk gates, red-team veto loop, negative control)
have something realistic to chew on. Nothing here comes from any private
trading system.

Contract (the isolation interface, shared with options_mapper):
    {"symbol": str, "direction": "long"|"short"|"neutral",
     "strength": float 0..1, "spot": float}

strength = |close - SMA| / SMA, normalized so that a 5% distance from the
SMA saturates to 1.0. Within a 0.5% dead band the signal is "neutral"
(whipsaw protection straight from the trend-following literature).
"""
from __future__ import annotations

SMA_DAYS = 210          # ~10 months x 21 trading days
FULL_STRENGTH_PCT = 0.05  # 5% from SMA -> strength 1.0
NEUTRAL_BAND_PCT = 0.005  # within 0.5% of SMA -> neutral

# ETFs for the calm core; two mega-cap single names (top-tier option
# liquidity, no earnings inside the competition window) to demonstrate
# the same gates handle single-stock chains unchanged.
UNIVERSE = ("SPY", "QQQ", "IWM", "AAPL", "NVDA")


def compute_signal(symbol: str, closes: list[float]) -> dict:
    """Pure function: daily closes (oldest -> newest) -> signal contract dict.

    Raises ValueError if there is not enough history for the SMA.
    """
    if len(closes) < SMA_DAYS:
        raise ValueError(
            f"{symbol}: need >= {SMA_DAYS} daily closes for 10-month SMA, "
            f"got {len(closes)}"
        )
    spot = float(closes[-1])
    if spot <= 0:
        raise ValueError(f"{symbol}: non-positive last close {spot!r}")
    sma = sum(float(c) for c in closes[-SMA_DAYS:]) / SMA_DAYS
    dist = (spot - sma) / sma

    if abs(dist) < NEUTRAL_BAND_PCT:
        direction = "neutral"
    elif dist > 0:
        direction = "long"
    else:
        direction = "short"

    strength = min(abs(dist) / FULL_STRENGTH_PCT, 1.0)
    return {
        "symbol": symbol,
        "direction": direction,
        "strength": round(strength, 4),
        "spot": round(spot, 2),
    }


def fetch_closes(symbol: str, lookback_days: int = SMA_DAYS + 30) -> list[float]:
    """Fetch daily closes from yfinance (imported lazily so tests stay offline)."""
    import yfinance as yf

    df = yf.Ticker(symbol).history(period="2y", auto_adjust=True)
    closes = [float(x) for x in df["Close"].dropna().tolist()]
    return closes[-lookback_days:]


def live_signal(symbol: str) -> dict:
    return compute_signal(symbol, fetch_closes(symbol))
