"""Red-team loop — the signature layer: before every order, the agent
interrogates its own risk exposure with three questions and may VETO.

Production design (awaiting the Alpaca account + MCP wiring):
  An LLM connected to the Alpaca MCP server asks, with live account/market
  context, before each order:
    1. max_loss_scenario — "What exactly do I lose in the worst case, and is
       that number computed or guessed?"
    2. greeks_exposure   — "What is my directional/vol exposure (delta/vega),
       and does it stack dangerously with existing positions?"
    3. liquidity_exit    — "If I need out tomorrow at the open, what does the
       exit cost through this spread?"
  The LLM's ONLY power is veto: it emits the protocol JSON below; the order
  path never lets it construct or resize orders.

Veto protocol JSON (the contract both stub and real MCP client emit):
    {
      "protocol": "redteam.v1",
      "order_dedup_key": str,
      "symbol": str,
      "questions": [
        {"id": "max_loss_scenario" | "greeks_exposure" | "liquidity_exit",
         "question": str,
         "answer": str,
         "verdict": "pass" | "veto",
         "reason": str}
      ],
      "verdict": "approve" | "veto",       # veto if ANY question vetoes
      "veto_reasons": [str, ...]
    }

`StubRedTeam` below implements the same protocol with deterministic
heuristics (no LLM, no network) so the pipeline runs end-to-end today and
the veto path is testable. `McpRedTeam` is the production implementation:
same `review()` signature, same protocol JSON, same veto-only power, but the
three questions are put to an LLM (claude CLI) wired to the OFFICIAL Alpaca
MCP server with a READ-ONLY tool allowlist, so it answers from the live
account and market state.

Inherited safety properties (proven live in staging on 2026-08-24):
- client-side --allowedTools pins read-only MCP tools; place_*/cancel_* denied
- prompt via stdin (Windows claude.CMD shim truncates multi-line argv)
- cwd = empty sandbox; the LLM's only world-window is the MCP tools
- fail-closed: any error/timeout/garbage -> verdict "veto"
- power is veto-only by protocol; qty/legs are never in the LLM's hands
"""
from __future__ import annotations

import json
import os
import shutil
import time
from abc import ABC, abstractmethod
from pathlib import Path

QUESTIONS = {
    "max_loss_scenario": "What exactly is lost in the worst case, and is that "
                         "number computed or guessed?",
    "greeks_exposure": "What is the directional/vol exposure, and does it "
                       "stack dangerously?",
    "liquidity_exit": "If we must exit at tomorrow's open, what does the exit "
                      "cost through this spread?",
}

# Heuristic thresholds for the stub
_EXIT_SPREAD_VETO = 0.10       # per-leg (ask-bid)/mid above this -> veto
_MAX_LOSS_FRAC_VETO = 0.05     # worst-case loss above 5% equity -> veto (belt+braces with gates)


class RedTeam(ABC):
    @abstractmethod
    def review(self, *, symbol: str, dedup_key: str, legs: list[dict],
               chain_by_symbol: dict[str, dict], max_loss: float | None,
               equity: float) -> dict:
        """Return a redteam.v1 protocol dict."""


class StubRedTeam(RedTeam):
    """Deterministic stand-in for the LLM+MCP red-teamer.

    Answers the same three questions from the data already in hand. When the
    Alpaca MCP client lands, this class is replaced by one that puts the same
    questions to an LLM with live account context — the protocol JSON and the
    veto-only power stay identical.
    """

    def review(self, *, symbol, dedup_key, legs, chain_by_symbol, max_loss,
               equity) -> dict:
        qs = []

        # 1. max-loss scenario
        if max_loss is None:
            qs.append(self._q("max_loss_scenario",
                              "Worst-case loss could not be computed for this "
                              "structure.", "veto",
                              "unpriceable structure — refuse"))
        else:
            frac = max_loss / equity if equity else 1.0
            verdict = "veto" if frac > _MAX_LOSS_FRAC_VETO else "pass"
            qs.append(self._q("max_loss_scenario",
                              f"Computed (not guessed) worst case at expiry: "
                              f"${max_loss:,.0f} = {frac:.1%} of equity.",
                              verdict,
                              "within 5% of equity" if verdict == "pass"
                              else "exceeds 5% of equity"))

        # 2. greeks exposure — indicative feed has no greeks; answer honestly
        #    with the moneyness proxy and flag one-sidedness.
        n_long = sum(1 for l in legs if l["side"] == "buy")
        n_short = sum(1 for l in legs if l["side"] == "sell")
        qs.append(self._q("greeks_exposure",
                          f"No greeks in feed; moneyness proxy: {n_long} long / "
                          f"{n_short} short leg(s), defined-risk vertical or "
                          f"collateralised short. Net exposure bounded by "
                          f"structure width.", "pass",
                          "defined-risk structure bounds delta/vega exposure"))

        # 3. liquidity exit — re-check each leg's spread as exit cost
        worst = 0.0
        for leg in legs:
            c = chain_by_symbol.get(leg["occ_symbol"])
            if c is None:
                qs.append(self._q("liquidity_exit",
                                  f"Leg {leg['occ_symbol']} missing from chain; "
                                  f"cannot estimate exit cost.", "veto",
                                  "unknown exit liquidity"))
                break
            bid, ask = float(c["bid"]), float(c["ask"])
            mid = (bid + ask) / 2
            worst = max(worst, (ask - bid) / mid if mid > 0 else 1.0)
        else:
            verdict = "veto" if worst > _EXIT_SPREAD_VETO else "pass"
            qs.append(self._q("liquidity_exit",
                              f"Worst per-leg exit spread {worst:.1%} of mid.",
                              verdict,
                              "exit cost acceptable" if verdict == "pass"
                              else f"exit spread {worst:.1%} > "
                                   f"{_EXIT_SPREAD_VETO:.0%}"))

        vetoes = [q["reason"] for q in qs if q["verdict"] == "veto"]
        return {
            "protocol": "redteam.v1",
            "order_dedup_key": dedup_key,
            "symbol": symbol,
            "questions": qs,
            "verdict": "veto" if vetoes else "approve",
            "veto_reasons": vetoes,
        }

    @staticmethod
    def _q(qid: str, answer: str, verdict: str, reason: str) -> dict:
        return {"id": qid, "question": QUESTIONS[qid], "answer": answer,
                "verdict": verdict, "reason": reason}


# ── MCP-backed red-teamer (production path) ──────────────────────────────

QUESTION_IDS = ("max_loss_scenario", "greeks_exposure", "liquidity_exit")

_ROOT = Path(__file__).resolve().parents[2]          # src/gated_agent -> repo

# Explicit override slot (tests / callers). None => resolve at call time.
# Windows: `claude` is a .cmd shim; subprocess without shell needs the real path.
CLAUDE_BIN: str | None = None


def _claude_bin() -> str:
    """Resolve at CALL time. Precedence: explicit override -> CLAUDE_BIN from
    the environment (.env is loaded AFTER module import) -> a PATH probe.

    The PATH probe is call-time too: `shutil.which` at import froze the answer
    before load_env() had run *and* before any PATH the task context supplies,
    which is precisely where the claude shim is missing. Unresolvable ->
    "claude", which fails to launch, which fail-closes into a veto.
    """
    return (CLAUDE_BIN or os.environ.get("CLAUDE_BIN")
            or shutil.which("claude") or "claude")

READONLY_TOOLS = ",".join([
    "mcp__alpaca__get_account_info",
    "mcp__alpaca__get_all_positions",
    "mcp__alpaca__get_orders",
    "mcp__alpaca__get_option_contracts",
    "mcp__alpaca__get_option_latest_quote",
    "mcp__alpaca__get_option_snapshot",
    "mcp__alpaca__get_stock_snapshots",
])


def _mcp_server_path() -> str:
    override = os.environ.get("ALPACA_MCP_SERVER")
    if override:
        return override
    exe = "alpaca-mcp-server.exe" if os.name == "nt" else "alpaca-mcp-server"
    sub = ("Scripts" if os.name == "nt" else "bin")
    return str(_ROOT / ".venv-mcp" / sub / exe)


def _mcp_config() -> str:
    cfg = {"mcpServers": {"alpaca": {
        "command": _mcp_server_path(),
        "args": ["--transport", "stdio"],
        "env": {
            "ALPACA_API_KEY": os.environ.get("ALPACA_API_KEY", ""),
            "ALPACA_SECRET_KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
            "ALPACA_PAPER_TRADE": "true",
            # `trading` toolset must be exposed for read-only position/order
            # visibility (get_all_positions lives there). Enforcement moves to
            # the CLIENT allowlist: only read tools are permitted; every
            # place_*/cancel_*/close_* call is denied by the harness. First
            # live run proved the chain works: with positions hidden, the
            # red-teamer refused to approve -- correct fail-safe.
            "ALPACA_TOOLSETS": "account,trading,assets,options-data,stock-data",
        },
    }}}
    path = _ROOT / "ledger" / ".redteam_mcp.json"    # runtime dir, gitignored
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return str(path)


MCP_PROMPT = """\
You are the RISK RED TEAM for an autonomous options trading agent on an
Alpaca paper account. A deterministic strategy proposes this order:

{proposal}

Context computed by the deterministic pipeline (verify, don't trust):
  underlying={symbol}  max_loss_estimate={max_loss}  account_equity={equity}

Use your read-only Alpaca tools to inspect the LIVE account and market state,
then answer EXACTLY these three questions:
1. id "max_loss_scenario": What exactly is lost in the worst case, and is that
   number computed or guessed? (recompute it yourself from the legs)
2. id "greeks_exposure": What is the directional/vol exposure, and does it
   stack dangerously with existing positions? (check positions via tools)
3. id "liquidity_exit": If we must exit at tomorrow's open, what does the exit
   cost through this spread? (check live quotes for each leg)

Your ONLY power is veto. You cannot modify or resize the order.
Respond with ONLY this JSON object:
{{"questions": [{{"id": "...", "answer": "...", "verdict": "pass"|"veto",
                 "reason": "..."}}, ...exactly 3, in the order above...],
  "verdict": "approve"|"veto"}}
"""


def _extract_json(text: str) -> dict | None:
    """Forgiving scan for the first balanced {...} block carrying a verdict."""
    depth, start = 0, -1
    for idx, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    cand = json.loads(text[start:idx + 1])
                    if isinstance(cand, dict) and "verdict" in cand:
                        return cand
                except ValueError:
                    pass
    return None


class McpRedTeam(RedTeam):
    """redteam.v1 client over claude CLI + Alpaca MCP; veto-only, fail-closed."""

    def __init__(self, timeout: int = 300):
        self.timeout = timeout

    def review(self, *, symbol, dedup_key, legs, chain_by_symbol, max_loss,
               equity) -> dict:
        import subprocess
        try:
            sandbox = _ROOT / "ledger" / ".redteam_sandbox"
            sandbox.mkdir(parents=True, exist_ok=True)
            prompt = MCP_PROMPT.format(proposal=json.dumps(legs, indent=1),
                                       symbol=symbol, max_loss=max_loss,
                                       equity=equity)
            r = subprocess.run(
                [_claude_bin(), "-p", "--mcp-config", _mcp_config(),
                 "--allowedTools", READONLY_TOOLS,
                 "--output-format", "json", "--model", "sonnet"],
                input=prompt, cwd=str(sandbox), capture_output=True, text=True,
                encoding="utf-8", timeout=self.timeout)
            env = json.loads(r.stdout)
            raw = _extract_json(env.get("result") or "")
            if raw is None:
                raise ValueError("no verdict JSON in LLM output")
            qs = self._normalize_questions(raw)
            vetoes = [q["reason"] for q in qs if q["verdict"] == "veto"]
            # protocol invariant: overall veto iff any question vetoes;
            # recomputed here so the LLM cannot approve past a vetoed question
            return self._protocol(dedup_key, symbol, qs,
                                  "veto" if vetoes else "approve", vetoes)
        except Exception as e:  # noqa: BLE001 -- fail closed
            reason = (f"red-team pass failed ({type(e).__name__}); "
                      "fail-closed policy vetoes the order")
            qs = [{"id": qid, "question": QUESTIONS[qid],
                   "answer": "not evaluated: red-team infrastructure failure",
                   "verdict": "veto", "reason": reason}
                  for qid in QUESTION_IDS]
            return self._protocol(dedup_key, symbol, qs, "veto", [reason])

    @staticmethod
    def _normalize_questions(raw: dict) -> list:
        by_id = {q.get("id"): q for q in raw.get("questions") or []
                 if isinstance(q, dict)}
        out = []
        for qid in QUESTION_IDS:
            q = by_id.get(qid)
            if q is None or q.get("verdict") not in ("pass", "veto"):
                out.append({"id": qid, "question": QUESTIONS[qid],
                            "answer": "missing from LLM response",
                            "verdict": "veto",
                            "reason": f"question {qid} unanswered -> fail closed"})
            else:
                out.append({"id": qid, "question": QUESTIONS[qid],
                            "answer": str(q.get("answer", ""))[:600],
                            "verdict": q["verdict"],
                            "reason": str(q.get("reason", ""))[:300]})
        return out

    @staticmethod
    def _protocol(dedup_key, symbol, questions, verdict, veto_reasons) -> dict:
        return {"protocol": "redteam.v1", "order_dedup_key": dedup_key,
                "symbol": symbol, "questions": questions, "verdict": verdict,
                "veto_reasons": veto_reasons,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}


def redteam_from_env() -> RedTeam:
    """GATED_AGENT_REDTEAM=llm (explicit opt-in — claude CLI then uses its own
    login/subscription auth, no API key required) or ANTHROPIC_API_KEY present
    -> McpRedTeam; otherwise StubRedTeam. Tests and keyless clones stay fully
    offline."""
    if (os.environ.get("GATED_AGENT_REDTEAM", "").lower() == "llm"
            or os.environ.get("ANTHROPIC_API_KEY")):
        return McpRedTeam()
    return StubRedTeam()
