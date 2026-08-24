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
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = Path("ledger") / "decisions.jsonl"


class Ledger:
    def __init__(self, path: str | os.PathLike = DEFAULT_PATH):
        self.path = Path(path)

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
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        return rec

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

    def seen_order(self, dedup_key: str) -> bool:
        """Has an order intent with this dedup key already been logged (live book)?"""
        return any(r.get("dedup_key") == dedup_key and r["book"] == "live"
                   for r in self.records()
                   if r["kind"] in ("order_intent", "order_submitted"))

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
