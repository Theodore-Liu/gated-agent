"""Append-only JSONL decision ledger.

Every decision — signal, mapping, gate verdict, red-team verdict, order
intent, negative-control shadow entries — is one JSON line, appended, never
rewritten. The ledger is also the source of truth for idempotency (order
dedup, once-per-day runs) and for the daily-loss halt gate.

Records always carry: ts (UTC ISO), run_date (YYYY-MM-DD), book
("live" | "shadow"), kind, plus kind-specific payload.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .paths import LEDGER_DIR, anchored

# Anchored on the repo root, NOT the CWD. A scheduled task inherits
# %windir%\system32 as its working directory, where a relative "ledger/"
# either raises PermissionError or — on a writable CWD — silently forks a
# SECOND ledger. Every safety property in this project reads this one file:
# dedup, once-per-day idempotency, the direction-flip guard and the daily
# loss halt all return "nothing here" against a fresh fork, which is exactly
# how an agent sends the same order twice.
DEFAULT_PATH = LEDGER_DIR / "decisions.jsonl"

_LOCK_RETRIES = 5          # Windows sharing violations are transient
_LOCK_BACKOFF = 0.2        # seconds, linear


class Ledger:
    def __init__(self, path: str | os.PathLike = DEFAULT_PATH):
        # A caller-supplied relative path is anchored too — the override must
        # not reintroduce the CWD dependency this default exists to remove.
        self.path = anchored(path)

    # ── write ────────────────────────────────────────────────────────────
    def append(self, run_date: str, book: str, kind: str, **payload) -> dict:
        if book not in ("live", "shadow"):
            raise ValueError(f"unknown book {book!r}")
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_date": run_date,
            "book": book,
            "kind": kind,
            **payload,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"
        # Retry a locked ledger (dashboard/editor holding the file on Windows)
        # before giving up: losing this write costs traceability AND dedup.
        for attempt in range(_LOCK_RETRIES):
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
                return rec
            except PermissionError:
                if attempt == _LOCK_RETRIES - 1:
                    raise
                time.sleep(_LOCK_BACKOFF * (attempt + 1))
        return rec  # unreachable; keeps the type checker honest

    # ── read ─────────────────────────────────────────────────────────────
    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def day(self, run_date: str, book: str | None = None) -> list[dict]:
        return [r for r in self.records()
                if r["run_date"] == run_date
                and (book is None or r["book"] == book)]

    #: Kinds that burn a dedup key. `order_submitting` is written BEFORE the
    #: broker call, so a crash between "sent to Alpaca" and "receipt logged"
    #: still blocks a duplicate on the next run — the safe direction to fail.
    ORDER_KINDS = ("order_submitting", "order_intent", "order_submitted")

    def seen_order(self, dedup_key: str) -> bool:
        """Has an order intent with this dedup key already been logged (live book)?"""
        return any(r.get("dedup_key") == dedup_key and r["book"] == "live"
                   for r in self.records()
                   if r["kind"] in self.ORDER_KINDS)

    def open_direction(self, symbol: str, book: str = "live") -> str | None:
        """Direction ("long" | "short") of the currently open position in
        `symbol` for this book, or None.

        Position-state stub until real fills exist (contract v1 flip guard):
        an order intent / submission / shadow-would-trade record carrying a
        `direction` field opens a position; a `position_closed` record for the
        symbol (to be written by the exit rules once live) closes it.

        A REJECTED order (status "error") opens nothing. Counting it would
        engage the flip guard against a position that does not exist and that
        position_manager can therefore never close — the symbol would be
        frozen in one direction for the rest of the competition.
        """
        cur: str | None = None
        for r in self.records():
            if r["book"] != book or r.get("symbol") != symbol:
                continue
            if (r["kind"] in ("order_intent", "order_submitted",
                              "shadow_would_trade")
                    and r.get("direction") and r.get("status") != "error"):
                cur = r["direction"]
            elif r["kind"] == "position_closed":
                cur = None
        return cur

    def run_complete(self, run_date: str) -> bool:
        return any(r["kind"] == "run_complete" and r["run_date"] == run_date
                   for r in self.records())

    def realized_pnl(self, run_date: str) -> float:
        """Sum of realized pnl records for the day (live book).

        Fill/PnL records will be written by the order path once a real
        account exists; until then this is 0.0 and the loss-halt gate is
        exercised by tests.
        """
        return sum(float(r.get("pnl", 0.0))
                   for r in self.day(run_date, book="live")
                   if r["kind"] == "fill")
