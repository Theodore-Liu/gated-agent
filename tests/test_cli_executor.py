"""cli_executor: net-limit sign convention, mleg construction, safety gates.
Pure logic — no subprocess, no binary, no network."""
from __future__ import annotations

import json

import pytest

from gated_agent.cli_executor import (
    _cli_legs,
    _net_limit,
    build_command_preview,
    cli_path,
    submit_legs,
)

DEBIT = [  # call debit spread: pay 5.00, collect 2.00 -> net +3.00
    {"occ_symbol": "SPY260904C00640000", "side": "buy", "qty": 2, "limit": 5.00},
    {"occ_symbol": "SPY260904C00655000", "side": "sell", "qty": 2, "limit": 2.00},
]
CREDIT = [  # put credit spread: collect 1.30, pay 1.05 -> net -0.25
    {"occ_symbol": "SPY260904P00625000", "side": "sell", "qty": 1, "limit": 1.30},
    {"occ_symbol": "SPY260904P00620000", "side": "buy", "qty": 1, "limit": 1.05},
]


def test_net_limit_debit_is_positive():
    assert _net_limit(DEBIT) == 3.00


def test_net_limit_credit_is_negative():
    # The bug caught in live dry-run testing 2026-08-24: flooring a credit at
    # +0.01 gives the premium away. The sign must survive.
    assert _net_limit(CREDIT) == -0.25


def test_cli_legs_position_intents():
    legs = json.loads(_cli_legs(DEBIT))
    assert legs[0]["position_intent"] == "buy_to_open"
    assert legs[1]["position_intent"] == "sell_to_open"
    assert all(l["ratio_qty"] == "1" for l in legs)


def test_preview_mleg_carries_negative_credit_limit():
    cmd = build_command_preview(CREDIT)
    assert "--order-class mleg" in cmd
    assert "--limit-price -0.25" in cmd


def test_preview_single_leg_plain_order():
    cmd = build_command_preview(DEBIT[:1])
    assert "--order-class" not in cmd
    assert "--symbol SPY260904C00640000" in cmd


def test_empty_legs_stand_aside():
    res = submit_legs([])
    assert res.ok and res.request is None


def test_more_than_four_legs_rejected():
    with pytest.raises(ValueError):
        submit_legs([dict(DEBIT[0])] * 5)


def test_mismatched_qty_rejected():
    bad = [dict(DEBIT[0]), dict(DEBIT[1], qty=9)]
    with pytest.raises(ValueError):
        submit_legs(bad)


def test_live_refused_without_env_gate(monkeypatch):
    monkeypatch.delenv("ALPACA_HACKATHON_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="ALPACA_HACKATHON_LIVE"):
        submit_legs(DEBIT, dry_run=False)
    monkeypatch.setenv("ALPACA_HACKATHON_LIVE", "0")   # explicit 0 also refuses
    with pytest.raises(RuntimeError):
        submit_legs(DEBIT, dry_run=False)


def test_cli_path_env_override(monkeypatch):
    monkeypatch.setenv("ALPACA_CLI", r"X:\somewhere\alpaca.exe")
    assert cli_path() == r"X:\somewhere\alpaca.exe"
