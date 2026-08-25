"""McpRedTeam: drop-in compatibility with StubRedTeam, fail-closed behavior,
protocol normalization, and the env-based selection switch. No network, no
claude binary — failures ARE the tested path."""
from __future__ import annotations

import inspect

from gated_agent import redteam_mcp as rt
from gated_agent.redteam_mcp import (
    McpRedTeam,
    QUESTION_IDS,
    StubRedTeam,
    _extract_json,
    redteam_from_env,
)


def test_review_signature_matches_stub():
    """Drop-in requirement: same keyword-only parameters as StubRedTeam."""
    stub = inspect.signature(StubRedTeam.review)
    mcp = inspect.signature(McpRedTeam.review)
    assert list(stub.parameters) == list(mcp.parameters)
    for name, p in stub.parameters.items():
        assert mcp.parameters[name].kind == p.kind


def test_fail_closed_when_cli_missing(monkeypatch, tmp_path):
    """No claude binary -> infrastructure failure -> full veto, valid protocol."""
    monkeypatch.setattr(rt, "CLAUDE_BIN",
                        str(tmp_path / "definitely-not-claude.exe"))
    monkeypatch.setattr(rt, "_ROOT", tmp_path)   # sandbox/mcp-config in tmp
    out = McpRedTeam(timeout=5).review(
        symbol="SPY", dedup_key="k1", legs=[], chain_by_symbol={},
        max_loss=100.0, equity=100_000.0)
    assert out["protocol"] == "redteam.v1"
    assert out["verdict"] == "veto"
    assert out["order_dedup_key"] == "k1"
    assert [q["id"] for q in out["questions"]] == list(QUESTION_IDS)
    assert all(q["verdict"] == "veto" for q in out["questions"])
    assert out["veto_reasons"]


def test_extract_json_finds_verdict_amid_prose():
    text = 'Sure! Here it is:\n```json\n{"verdict": "approve", "questions": []}\n``` hope that helps'
    assert _extract_json(text) == {"verdict": "approve", "questions": []}


def test_extract_json_skips_non_verdict_objects():
    text = '{"foo": 1} then {"verdict": "veto"}'
    assert _extract_json(text) == {"verdict": "veto"}


def test_extract_json_none_on_garbage():
    assert _extract_json("no json here { broken") is None


def test_normalize_missing_question_vetoes():
    raw = {"verdict": "approve",
           "questions": [{"id": "max_loss_scenario", "answer": "ok",
                          "verdict": "pass", "reason": "fine"}]}
    qs = McpRedTeam._normalize_questions(raw)
    assert [q["id"] for q in qs] == list(QUESTION_IDS)
    assert qs[0]["verdict"] == "pass"
    assert qs[1]["verdict"] == qs[2]["verdict"] == "veto"


def test_normalize_bad_verdict_vetoes():
    raw = {"verdict": "approve",
           "questions": [{"id": qid, "answer": "a",
                          "verdict": "maybe", "reason": "?"}
                         for qid in QUESTION_IDS]}
    qs = McpRedTeam._normalize_questions(raw)
    assert all(q["verdict"] == "veto" for q in qs)


def test_llm_cannot_approve_past_vetoed_question(monkeypatch, tmp_path):
    """Overall verdict is recomputed from per-question verdicts: an LLM saying
    approve while one question vetoes still yields veto."""
    class FakeResult:
        stdout = ('{"result": "{\\"verdict\\": \\"approve\\", \\"questions\\": '
                  '[{\\"id\\": \\"max_loss_scenario\\", \\"answer\\": \\"a\\", '
                  '\\"verdict\\": \\"veto\\", \\"reason\\": \\"too big\\"}, '
                  '{\\"id\\": \\"greeks_exposure\\", \\"answer\\": \\"b\\", '
                  '\\"verdict\\": \\"pass\\", \\"reason\\": \\"ok\\"}, '
                  '{\\"id\\": \\"liquidity_exit\\", \\"answer\\": \\"c\\", '
                  '\\"verdict\\": \\"pass\\", \\"reason\\": \\"ok\\"}]}"}')

    import subprocess
    monkeypatch.setattr(rt, "_ROOT", tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeResult())
    out = McpRedTeam().review(symbol="SPY", dedup_key="k2", legs=[],
                              chain_by_symbol={}, max_loss=1.0, equity=1e5)
    assert out["verdict"] == "veto"
    assert out["veto_reasons"] == ["too big"]
    assert out["questions"][0]["verdict"] == "veto"


def test_redteam_from_env_switch(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(redteam_from_env(), StubRedTeam)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert isinstance(redteam_from_env(), McpRedTeam)


def test_readonly_allowlist_has_no_order_tools():
    tools = rt.READONLY_TOOLS.split(",")
    assert tools, "allowlist must not be empty"
    for t in tools:
        assert t.startswith("mcp__alpaca__get_"), t   # read-only by prefix
        for banned in ("place", "cancel", "close", "submit", "create"):
            assert banned not in t
