"""Append-only JSONL ledger: durability, dedup lookup, idempotency flags."""
from __future__ import annotations

from gated_agent.ledger import Ledger


def make(tmp_path):
    return Ledger(tmp_path / "decisions.jsonl")


def test_append_only_accumulates(tmp_path):
    led = make(tmp_path)
    led.append("2026-08-24", "live", "signal", signal={"symbol": "SPY"})
    led.append("2026-08-24", "shadow", "signal", signal={"symbol": "SPY"})
    recs = led.records()
    assert len(recs) == 2
    assert [r["book"] for r in recs] == ["live", "shadow"]
    # file is line-appended, first record untouched by the second write
    lines = (tmp_path / "decisions.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2


def test_seen_order_only_counts_live_book(tmp_path):
    led = make(tmp_path)
    led.append("2026-08-24", "shadow", "order_intent", dedup_key="k1")
    assert not led.seen_order("k1")            # shadow never blocks live
    led.append("2026-08-24", "live", "order_intent", dedup_key="k1")
    assert led.seen_order("k1")


def test_run_complete_flag(tmp_path):
    led = make(tmp_path)
    assert not led.run_complete("2026-08-24")
    led.append("2026-08-24", "live", "run_complete")
    assert led.run_complete("2026-08-24")
    assert not led.run_complete("2026-08-25")


def test_open_direction_tracks_open_and_close(tmp_path):
    led = make(tmp_path)
    assert led.open_direction("SPY") is None
    led.append("2026-08-24", "live", "order_intent", symbol="SPY",
               direction="long", dedup_key="k1")
    assert led.open_direction("SPY") == "long"
    assert led.open_direction("QQQ") is None            # per-symbol
    led.append("2026-08-26", "live", "position_closed", symbol="SPY")
    assert led.open_direction("SPY") is None            # exit rules closed it


def test_open_direction_books_are_isolated(tmp_path):
    led = make(tmp_path)
    led.append("2026-08-24", "shadow", "shadow_would_trade", symbol="SPY",
               direction="short", dedup_key="k1")
    assert led.open_direction("SPY", book="live") is None
    assert led.open_direction("SPY", book="shadow") == "short"


def test_realized_pnl_sums_fills(tmp_path):
    led = make(tmp_path)
    led.append("2026-08-24", "live", "fill", pnl=-1500.0)
    led.append("2026-08-24", "live", "fill", pnl=250.0)
    led.append("2026-08-24", "shadow", "fill", pnl=-9999.0)  # shadow ignored
    assert led.realized_pnl("2026-08-24") == -1250.0
