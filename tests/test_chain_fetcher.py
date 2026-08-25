"""chain_fetcher: auth guard, pagination, snapshot merge into mapper format.
Network is monkeypatched out — every test is offline."""
from __future__ import annotations

import pytest

from gated_agent import chain_fetcher


def contract(sym, typ="call", strike=640.0, exp="2026-09-04", oi=500):
    return {"symbol": sym, "type": typ, "strike_price": str(strike),
            "expiration_date": exp, "open_interest": oi}


def fake_get(pages):
    """Return a _get replacement serving canned responses and logging URLs."""
    calls = []

    def _get(url):
        calls.append(url)
        return pages.pop(0)

    return _get, calls


def test_headers_raise_without_keys(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="not set"):
        chain_fetcher._headers()


def test_fetch_chain_merges_quotes_and_greeks(monkeypatch):
    from datetime import date
    pages = [
        {"option_contracts": [contract("A"), contract("B", typ="put", oi=None)],
         "next_page_token": None},
        {"snapshots": {
            "A": {"latestQuote": {"bp": 1.10, "ap": 1.20},
                  "greeks": {"delta": 0.48}},
            # B has a quote but no greeks (weekend/stale feed)
            "B": {"latestQuote": {"bp": 0.55, "ap": 0.60}},
        }},
    ]
    get, _ = fake_get(pages)
    monkeypatch.setattr(chain_fetcher, "_get", get)
    chain = chain_fetcher.fetch_chain("SPY", date(2026, 8, 28))
    a, b = chain
    assert a["bid"] == 1.10 and a["ask"] == 1.20 and a["delta"] == 0.48
    assert a["strike_price"] == 640.0                 # str -> float
    assert b["delta"] is None                         # mapper falls back
    assert b["open_interest"] is None                 # unknown OI preserved


def test_fetch_chain_follows_pagination(monkeypatch):
    from datetime import date
    pages = [
        {"option_contracts": [contract("A")], "next_page_token": "t1"},
        {"option_contracts": [contract("B")], "next_page_token": None},
        {"snapshots": {}},
    ]
    get, calls = fake_get(pages)
    monkeypatch.setattr(chain_fetcher, "_get", get)
    chain = chain_fetcher.fetch_chain("SPY", date(2026, 8, 28))
    assert [c["symbol"] for c in chain] == ["A", "B"]
    assert "page_token=t1" in calls[1]


def test_snapshot_requests_batch_by_100(monkeypatch):
    from datetime import date
    many = [contract(f"C{i}") for i in range(150)]
    pages = [
        {"option_contracts": many, "next_page_token": None},
        {"snapshots": {}},        # batch 1 (100 symbols)
        {"snapshots": {}},        # batch 2 (50 symbols)
    ]
    get, calls = fake_get(pages)
    monkeypatch.setattr(chain_fetcher, "_get", get)
    chain_fetcher.fetch_chain("SPY", date(2026, 8, 28))
    snap_calls = [c for c in calls if "snapshots" in c]
    assert len(snap_calls) == 2


def test_fetch_equity(monkeypatch):
    monkeypatch.setattr(chain_fetcher, "_get",
                        lambda url: {"equity": "100234.56"})
    assert chain_fetcher.fetch_equity() == 100234.56


def test_fetch_spot_missing_trade_raises(monkeypatch):
    monkeypatch.setattr(chain_fetcher, "_get", lambda url: {})
    with pytest.raises(RuntimeError, match="no latest trade"):
        chain_fetcher.fetch_spot("SPY")
