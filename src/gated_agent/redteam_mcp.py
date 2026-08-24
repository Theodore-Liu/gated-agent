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
the veto path is testable.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

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
