"""One anchor for every on-disk artifact — the repo root, never the CWD.

Why this module exists (2026-08-26 pre-kickoff sweep). The agent runs
unattended from a Windows scheduled task, and `schtasks /Create /TR ...`
cannot set a working directory: the task inherits `%windir%\\system32`. Any
path the code resolves against the CWD therefore means something different
at 07:00 unattended than it did in the shell where it was tested:

  * `Path("ledger")/"decisions.jsonl"` -> System32\\ledger\\... (PermissionError,
    or worse, a *second* ledger on a writable box — which silently resets
    dedup, once-per-day idempotency, the direction-flip guard and the
    daily-loss halt, because all four read the ledger).
  * `.env` -> System32\\.env (absent) -> no keys -> the run silently degrades
    to the synthetic-chain stub broker.

Both are the same failure family as the three found in live-fire testing on
08-25: *works when run by hand, fails in the context it will actually run in.*
Anchoring on `Path(__file__)` makes the answer identical in both contexts.

The scripts also `cd /d` to the root (belt and braces) — but the code must
not depend on them having done so.
"""
from __future__ import annotations

from pathlib import Path

# src/gated_agent/paths.py -> src/gated_agent -> src -> repo root
ROOT = Path(__file__).resolve().parents[2]

LEDGER_DIR = ROOT / "ledger"
LOGS_DIR = ROOT / "logs"
CONFIG_DIR = ROOT / "config"
DOTENV = ROOT / ".env"


def anchored(path: str | Path) -> Path:
    """Absolute paths pass through; relative ones resolve against the repo
    root rather than whatever directory the process happens to be in."""
    p = Path(path)
    return p if p.is_absolute() else ROOT / p
