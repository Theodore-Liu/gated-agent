"""The frozen close-rule config: file exists, values match the pre-registered
orchestra spec, and the module actually loads from it (a silently drifted
constant would defeat the pre-registration claim)."""
from __future__ import annotations

import json
from pathlib import Path

from gated_agent import position_manager as pm

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "close_rules.json"

FROZEN = {  # orchestra's 平仓规则定稿 v1 (2026-08-24)
    "dte_close": 2,
    "tp_debit_mult": 1.5,
    "tp_credit_mult": 0.5,
    "sl_debit_mult": 0.5,
    "sl_credit_mult": 2.0,
    "flip_close": True,
    "valuation": "snapshot_mid",
    "max_quote_gaps": 3,
    "check_times": ["open+30min", "close-45min"],
}


def test_config_file_frozen_values():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    for key, want in FROZEN.items():
        assert cfg[key] == want, f"{key} drifted from frozen value"


def test_module_constants_come_from_config():
    assert pm.DTE_CLOSE == FROZEN["dte_close"]
    assert pm.TP_MULT_DEBIT == FROZEN["tp_debit_mult"]
    assert pm.TP_MULT_CREDIT == FROZEN["tp_credit_mult"]
    assert pm.SL_MULT_DEBIT == FROZEN["sl_debit_mult"]
    assert pm.SL_MULT_CREDIT == FROZEN["sl_credit_mult"]
    assert pm.FLIP_CLOSE is FROZEN["flip_close"]
    assert pm.MAX_QUOTE_GAPS == FROZEN["max_quote_gaps"]
    assert pm.CHECK_TIMES == FROZEN["check_times"]


def test_missing_config_falls_back_to_identical_defaults(tmp_path):
    cfg = pm.load_close_config(tmp_path / "nope.json")
    for key, want in FROZEN.items():
        assert cfg[key] == want


def test_underscore_keys_ignored_and_partial_merge(tmp_path):
    p = tmp_path / "partial.json"
    p.write_text(json.dumps({"_comment": "x", "dte_close": 2}),
                 encoding="utf-8")
    cfg = pm.load_close_config(p)
    assert "_comment" not in cfg
    assert cfg["sl_credit_mult"] == 2.0        # merged from frozen defaults
