"""Negative control — the signature move.

Every day, alongside the real signal, a RANDOM signal for the same symbol is
pushed through the *identical* pipeline (mapper -> gates -> red-team) and
logged to a shadow book, side by side with the live book. If the live book
cannot beat its own random twin over time, the signal is noise and we say so.
This is the same logic as a placebo arm: the discipline layers see both
books; only the live book can ever reach the order path.

Determinism: the random draw is seeded by (run_date, symbol), so re-running
the same day reproduces the same shadow signal — the ledger stays consistent
with idempotent re-runs.
"""
from __future__ import annotations

import hashlib
import random

DIRECTIONS = ("long", "short", "neutral")


def random_signal(run_date: str, symbol: str, spot: float) -> dict:
    """Random signal in the exact same contract shape as signals.compute_signal.

    Uses the real spot (the shadow book trades the same market, just with a
    coin-flip brain). Seeded by (run_date, symbol): reproducible per day.
    """
    seed = int.from_bytes(
        hashlib.sha256(f"negctl|{run_date}|{symbol}".encode()).digest()[:8],
        "big",
    )
    rng = random.Random(seed)
    return {
        "symbol": symbol,
        "direction": rng.choice(DIRECTIONS),
        "strength": round(rng.random(), 4),
        "spot": spot,
    }
