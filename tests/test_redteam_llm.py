"""McpRedTeam: drop-in compatibility with StubRedTeam, fail-closed behavior,
protocol normalization, and the env-based selection switch. No network, no
claude binary — failures ARE the tested path."""
from __future__ import annotations

import inspect
import json

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
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
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
    monkeypatch.delenv("GATED_AGENT_REDTEAM", raising=False)
    assert isinstance(redteam_from_env(), StubRedTeam)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert isinstance(redteam_from_env(), McpRedTeam)


def test_redteam_from_env_explicit_llm_needs_no_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GATED_AGENT_REDTEAM", "llm")
    assert isinstance(redteam_from_env(), McpRedTeam)
    monkeypatch.setenv("GATED_AGENT_REDTEAM", "stub")
    assert isinstance(redteam_from_env(), StubRedTeam)


def test_readonly_allowlist_has_no_order_tools():
    tools = rt.READONLY_TOOLS.split(",")
    assert tools, "allowlist must not be empty"
    for t in tools:
        assert t.startswith("mcp__alpaca__get_"), t   # read-only by prefix
        for banned in ("place", "cancel", "close", "submit", "create"):
            assert banned not in t


# ── 08-26: the negative control must not inherit the live book's positions ──

def test_prompt_scopes_positions_to_the_book_under_review():
    """Observed live on 2026-08-26: reviewing a SHADOW proposal, the LLM
    vetoed it citing "two existing same-direction SPY/QQQ spreads" — those
    were the LIVE book's. Both books share one paper account, so an old
    prompt saying "inspect the LIVE account" made the control tighten as the
    live book filled, for reasons unrelated to signal quality."""
    assert "BOOK UNDER REVIEW" in rt.MCP_PROMPT
    assert "{book_positions}" in rt.MCP_PROMPT
    assert "NOT yours" in rt.MCP_PROMPT
    assert "get_all_positions" in rt.MCP_PROMPT, (
        "the prompt must name the tool whose output is NOT to be used for "
        "concentration, or the instruction is easy to read past")


def test_shadow_book_is_told_it_holds_nothing(monkeypatch, tmp_path):
    """The shadow book's prompt must state an empty position set explicitly —
    an absent field reads as 'unknown', and unknown invites a tool call."""
    captured = {}

    class _Res:
        returncode = 0
        stdout = json.dumps({"result": json.dumps({
            "questions": [{"id": i, "answer": "a", "verdict": "pass",
                           "reason": "r"} for i in rt.QUESTION_IDS],
            "verdict": "approve"})})
        stderr = ""

    def fake_run(cmd, **kw):
        captured["prompt"] = kw.get("input", "")
        return _Res()

    monkeypatch.setattr(rt, "_ROOT", tmp_path)
    monkeypatch.setattr("subprocess.run", fake_run)
    out = McpRedTeam(timeout=5).review(
        symbol="SPY", dedup_key="k", legs=[], chain_by_symbol={},
        max_loss=100.0, equity=100_000.0, book="shadow", book_positions={})
    assert out["verdict"] == "approve"
    assert "BOOK UNDER REVIEW: shadow" in captured["prompt"]
    assert "holds no option positions" in captured["prompt"]


def test_live_book_positions_are_passed_through(monkeypatch, tmp_path):
    captured = {}

    class _Res:
        returncode = 0
        stdout = json.dumps({"result": json.dumps({
            "questions": [{"id": i, "answer": "a", "verdict": "pass",
                           "reason": "r"} for i in rt.QUESTION_IDS],
            "verdict": "approve"})})
        stderr = ""

    monkeypatch.setattr(rt, "_ROOT", tmp_path)
    monkeypatch.setattr("subprocess.run",
                        lambda cmd, **kw: (captured.__setitem__(
                            "prompt", kw.get("input", "")), _Res())[1])
    McpRedTeam(timeout=5).review(
        symbol="QQQ", dedup_key="k", legs=[], chain_by_symbol={},
        max_loss=100.0, equity=100_000.0, book="live",
        book_positions={"QQQ": "long", "SPY": "long"})
    assert "BOOK UNDER REVIEW: live" in captured["prompt"]
    assert "QQQ" in captured["prompt"] and "long" in captured["prompt"]


def test_stub_accepts_the_same_book_arguments():
    """Signature parity is what lets the stub stand in offline — a red team
    the pipeline cannot call is not a fallback."""
    out = StubRedTeam().review(symbol="SPY", dedup_key="k", legs=[],
                               chain_by_symbol={}, max_loss=10.0,
                               equity=100_000.0, book="shadow",
                               book_positions={})
    assert out["protocol"] == "redteam.v1"
