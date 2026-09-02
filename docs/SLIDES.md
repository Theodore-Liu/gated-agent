# Gated Agent — slides (7)

> One slide per WRITEUP section plus open/close. Render to PDF or paste into
> any deck tool; text is final, layout is free. Submission form takes a link —
> the GitHub-rendered version of this file works as-is.

---

## 1 · Gated Agent

**An options paper-trading agent whose every order must survive its own red team.**

lablab.ai × Alpaca AI Trading Agents Hackathon · team *Gated Agent*
Public repo `Theodore-Liu/gated-agent` · live demo `gated-agent-live.streamlit.app`
Alpaca competition paper account `PA32VHBO5AOB` — every position a defined-risk option spread.

---

## 2 · The inversion — the LLM as gatekeeper, not trader

Most trading-agent demos put the LLM in charge of finding alpha.
**We put it in charge of saying no.**

- Signal: deliberately a textbook toy — Faber 10-month SMA on
  SPY / QQQ / IWM / AAPL / NVDA — a publicly documented rule, deliberately
  not presented as novel alpha.
- Deterministic code maps signal → defined-risk spread (Δ-targeted legs).
- The LLM's only power is **veto**. It cannot size, price, or place anything.
- The bet: the hard engineering problem here is risk discipline, not alpha.

---

## 3 · The deterministic risk layer — arithmetic that cannot be talked out of

Six gates, before the LLM ever sees the order:

worst-case loss ≤ 1% per trade · 5% position cap · −2% daily halt (blocks new opens; exits still run)
idempotent dedup · direction-flip guard (no hedged books) · liquidity floor

Exits are **pre-registered**: R1–R4 frozen in `config/close_rules.json`
*before* the contest window — git history proves the rules predate the runs.

---

## 4 · The red team

Before each order, an LLM wired **read-only** into Alpaca's official MCP
server answers three questions from live account + market data:

1. What exactly is lost in the worst case — computed or guessed?
2. Does this stack dangerously with *this book's* existing positions?
3. What does the exit cost through this spread at the next open?

Real rehearsal veto: per-order math fine at <1% max loss — vetoed anyway for
stacking a third correlated spread sharing a strike with an open position.
**Verdicts aggregate deterministically: any single failed question triggers
an automatic veto, which the model cannot override.**

---

## 5 · Every decision traceable

- Append-only JSON Lines ledger: signal → gates → red-team transcript →
  order intent; submitted orders carry the **broker receipt**; reconciled
  programmatically against the account every round.
- **Negative control**: a seeded random signal runs the identical pipeline,
  shadow-only, structurally unable to reach the order path.
- Torn-tail quarantine, crash-safe dedup, market-holiday awareness —
  it ran the contest week unattended.

---

## 6 · We attacked it before the market could

Two adversarial reviews, pre-contest (`docs/ADVERSARIAL-REVIEW.md`):

**19 real defects** — a dedup gate that didn't dedup, a dead loss-halt, a
close rule that wasn't unconditional, a negative control that had been silently
inheriting the live book (found and fixed) — **each fixed with a regression test that fails on the
pre-fix tree.** Tests: 110 → 219.

> Trust is not the absence of bugs.
> It is evidence that known failure modes stay fixed.

---

## 7 · Verified results

- Discipline as the product: pre-registration, negative controls,
  veto-only LLM power, receipt-level traceability.
- Unattended live operation on the competition account from day 1 —
  every spread submitted in the contest window filled (10/10, verified
  against the account), its broker receipt in the book.
- Everything in this deck is verifiable in the public repo's git history.

*Built with Alpaca Trading API + official MCP server + CLI (mleg).*
