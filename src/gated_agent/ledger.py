"""Append-only JSONL decision ledger.

Every decision — signal, mapping, gate verdict, red-team verdict, order
intent, negative-control shadow entries — is one JSON line, appended, never
rewritten. The ledger is also the source of truth for idempotency (order
dedup, once-per-day runs) and for the daily-loss halt gate.

Records always carry: ts (UTC ISO), run_date (YYYY-MM-DD), book
("live" | "shadow"), kind, plus kind-specific payload.

Two properties added by the 2026-08-26 adversarial review:

* **A torn tail cannot brick the agent.** A killed task or a power loss can
  leave a partial line. Every read used to run `json.loads` over every line,
  so one truncated byte-range made dedup, the once-per-day guard, the flip
  guard, the halt gate and the dashboard all raise at once — the agent would
  not have traded again for the rest of the week. The fragment is now
  quarantined into a sidecar, recorded as a `ledger_torn_tail` row, and
  **fails the dedup gate closed** if it mentions a dedup key (we cannot parse
  it, so we must assume the order it was burning went out).
* **Corruption anywhere else still refuses.** Silently skipping a bad line in
  the middle could drop an order record, which is how a duplicate gets sent.

The ledger models the *believed* book. It is reconciled against the broker's
*actual* positions at the start of every run — see position_manager.reconcile
and the `position_reconciled` / `position_adopted` records below.
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
_FRAGMENT_CAP = 2000       # chars of a torn tail kept in the record


class LedgerCorruption(RuntimeError):
    """A malformed line that is NOT a torn tail.

    A truncated final line is a crash artefact and is survivable. Garbage in
    the middle means something rewrote an append-only file, and silently
    skipping it could drop an order record — which is precisely how a
    duplicate order gets sent. Refuse to read rather than trade on a wrong
    picture of our own book.
    """


def read_jsonl(path, *, tolerate_torn_tail: bool = True
               ) -> tuple[list[dict], str | None]:
    """Parse a JSONL file -> (records, torn_tail_or_None).

    Shared with the Streamlit dashboard so the judge-facing page and the agent
    agree on what the file says — including when a killed task left half a
    line behind. Raises LedgerCorruption for a bad line anywhere but the end.
    """
    p = Path(path)
    if not p.exists():
        return [], None
    lines = p.read_text(encoding="utf-8", errors="replace").split("\n")
    torn = None
    if lines and lines[-1] == "":
        lines.pop()                       # ended with a newline: nothing torn
    elif lines:
        tail = lines.pop()
        try:
            json.loads(tail)
            lines.append(tail)            # complete, merely unterminated
        except ValueError:
            if not tolerate_torn_tail:
                raise LedgerCorruption(f"{p}: truncated final line")
            torn = tail
    out: list[dict] = []
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError as e:
            raise LedgerCorruption(
                f"{p}: line {n} is not valid JSON ({e}); refusing to trade on "
                f"a partial view of the ledger") from e
    return out, torn


class Ledger:
    def __init__(self, path: str | os.PathLike = DEFAULT_PATH):
        # A caller-supplied relative path is anchored too — the override must
        # not reintroduce the CWD dependency this default exists to remove.
        self.path = anchored(path)
        self.torn_tail: str | None = None
        self._healing = False

    @property
    def quarantine_path(self) -> Path:
        return self.path.with_name(self.path.name + ".torn")

    # ── crash repair ─────────────────────────────────────────────────────
    def _heal(self) -> None:
        """Move a torn final line out of the way, permanently and visibly.

        Quarantined to a sidecar (nothing is destroyed), truncated out of the
        ledger (so the next append cannot glue itself onto the fragment) and
        recorded as a `ledger_torn_tail` row — which is what keeps the dedup
        gate failing closed for that key after the process restarts.
        """
        if self._healing or not self.path.exists() or not self.path.stat().st_size:
            return
        with open(self.path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            if f.read(1) == b"\n":
                return                    # clean file: the common case
        _, torn = read_jsonl(self.path)
        if not torn:
            # Complete JSON that merely lacks its newline: terminate it.
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("\n")
            return
        self.torn_tail = torn
        with open(self.quarantine_path, "a", encoding="utf-8") as f:
            f.write(f"# torn at {datetime.now(timezone.utc).isoformat()}\n")
            f.write(torn + "\n")
        raw = self.path.read_bytes()
        keep = raw[:len(raw) - len(torn.encode("utf-8"))]
        self.path.write_bytes(keep)
        self._healing = True
        try:
            self.append(datetime.now(timezone.utc).date().isoformat(), "live",
                        "ledger_torn_tail", fragment=torn[:_FRAGMENT_CAP],
                        quarantined_to=str(self.quarantine_path))
        finally:
            self._healing = False

    # ── write ────────────────────────────────────────────────────────────
    def append(self, run_date: str, book: str, kind: str, **payload) -> dict:
        if book not in ("live", "shadow"):
            raise ValueError(f"unknown book {book!r}")
        self._heal()
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
        self._heal()
        recs, torn = read_jsonl(self.path)
        if torn:                                  # healing raced with a writer
            self.torn_tail = torn
        return recs

    def day(self, run_date: str, book: str | None = None) -> list[dict]:
        return [r for r in self.records()
                if r["run_date"] == run_date
                and (book is None or r["book"] == book)]

    def torn_fragments(self) -> list[str]:
        """Every unparseable fragment we have ever recovered, plus any the
        current instance is holding. Consulted by the dedup gate."""
        out = [r["fragment"] for r in self.records()
               if r["kind"] == "ledger_torn_tail" and r.get("fragment")]
        if self.torn_tail:
            out.append(self.torn_tail)
        return out

    #: Kinds that burn a dedup key. `order_submitting` is written BEFORE the
    #: broker call, so a crash between "sent to Alpaca" and "receipt logged"
    #: still blocks a duplicate on the next run — the safe direction to fail.
    ORDER_KINDS = ("order_submitting", "order_intent", "order_submitted")

    #: Statuses that mean the order never reached the market, so it opened
    #: nothing. Deliberately a DENY-list: an unrecognised status is treated as
    #: a real order, because the cost of wrongly believing a position is open
    #: is a skipped trade, while the cost of wrongly believing it is closed is
    #: a hedged book — the exact thing gate 4 exists to prevent.
    NON_OPENING_STATUSES = ("error", "dry_run", "rejected", "canceled",
                            "cancelled", "expired")

    def seen_order(self, dedup_key: str) -> bool:
        """Has an order with this dedup key already been logged (live book)?

        Fails CLOSED against a torn tail that mentions the key: we cannot
        parse the row that was burning it, so we must assume the order went
        out. A key wrongly burned costs one skipped trade; a key wrongly free
        costs a duplicate position.
        """
        if any(r.get("dedup_key") == dedup_key and r["book"] == "live"
               for r in self.records() if r["kind"] in self.ORDER_KINDS):
            return True
        return any(dedup_key in frag for frag in self.torn_fragments())

    def open_direction(self, symbol: str, book: str = "live") -> str | None:
        """Direction ("long" | "short") of the currently open position in
        `symbol` for this book, or None.

        Opened by: an order the broker accepted, a shadow would-trade record,
        or a `position_adopted` record written by reconciliation when the
        broker turns out to hold something the ledger had forgotten.

        Closed by: a confirmed `position_closed` (exit rules) or a
        `position_reconciled` (the broker does not have it — expiry,
        assignment, an order that never filled, or a dry-run rehearsal).

        A REJECTED order (status "error") and a DRY-RUN rehearsal open
        nothing. Counting either would engage the flip guard against a
        position that does not exist and that position_manager can therefore
        never close — the symbol would be frozen for the rest of the week.
        The 08-26 review found the dry-run half of that: every `--dry-run`
        smoke test on a box WITH keys wrote an order_intent carrying a
        direction, so rehearsing the pipeline froze all five symbols.
        """
        cur: str | None = None
        for r in self.records():
            if r["book"] != book or r.get("symbol") != symbol:
                continue
            kind = r["kind"]
            if kind in ("order_intent", "order_submitted"):
                if (r.get("direction")
                        and r.get("status") not in self.NON_OPENING_STATUSES):
                    cur = r["direction"]
            elif kind in ("shadow_would_trade", "position_adopted"):
                if r.get("direction"):
                    cur = r["direction"]
            elif kind in ("position_closed", "position_reconciled"):
                cur = None
        return cur

    def open_positions(self, book: str = "live") -> dict[str, str]:
        """symbol -> direction for every position this book believes is open."""
        symbols = {r["symbol"] for r in self.records()
                   if r["book"] == book and r.get("symbol")}
        out = {}
        for sym in sorted(symbols):
            d = self.open_direction(sym, book=book)
            if d:
                out[sym] = d
        return out

    def run_complete(self, run_date: str) -> bool:
        return any(r["kind"] == "run_complete" and r["run_date"] == run_date
                   for r in self.records())

    #: Kinds carrying realized money. `fill` is reserved for a future
    #: per-fill feed; `position_closed` is what the exit rules write today.
    PNL_KINDS = ("fill", "position_closed")

    def realized_pnl(self, run_date: str) -> float:
        """Realized PnL booked today on the LIVE book, in dollars.

        Only real closes count: a dry-run rehearsal and the shadow twin both
        move imaginary money, and feeding either into the -2% halt would
        either freeze a healthy account or hide a bleeding one.
        """
        return sum(float(r.get("pnl") or 0.0)
                   for r in self.day(run_date, book="live")
                   if r["kind"] in self.PNL_KINDS and not r.get("dry_run"))
