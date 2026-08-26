# Gated Agent — an options agent that red-teams its own risk before every order

**Team:** Gated Agent · **Hackathon:** Alpaca AI Trading Agents (Aug 28 – Sep 4, 2026) · **Repo:** MIT-licensed, runs offline end-to-end with `pytest` + `python -m gated_agent.run --dry-run`

## AI logic: signal in, red-team veto loop before anything leaves

Most agent demos put the LLM in charge of finding alpha. We invert that. The signal is deliberately a toy from public literature (Faber's 10-month SMA trend rule on SPY/QQQ/IWM plus two mega-cap single names, AAPL and NVDA — same gates, ETF and single-stock chains alike), emitted as a four-field dict: `{symbol, direction, strength, spot}`. Deterministic code — never the LLM — maps it to defined-risk option structures: strong conviction becomes a debit spread (delta-targeted legs, ~0.50 buy / ~0.25 sell, moneyness fallback when the feed has no greeks); mild conviction becomes a narrow credit spread (put spread for bullish, call spread for bearish — chosen over cash-secured puts because on a $700+ underlying a CSP can never pass a sane collateral cap).

The AI's role is the **red-team veto loop**: before any order is submitted, an LLM connected to Alpaca's MCP server interrogates the order with three fixed questions — (1) *what exactly is the worst-case loss, computed or guessed?* (2) *what is the greeks exposure, and does it stack?* (3) *what does the exit cost through this spread if we must leave at tomorrow's open?* It answers in a strict `redteam.v1` JSON protocol and holds **veto-only power**: it can kill any order but can never construct, resize, or reprice one. Alongside every live decision, a seeded **random twin signal** runs through the identical pipeline into a shadow book — a built-in placebo arm: if the live book cannot beat its own coin flip, the log proves the signal is noise.

## Risk gates: arithmetic that cannot be talked out of

Every order passes all gates; any veto kills it; anything unpriceable fails closed.

1. **Per-trade max loss** — sizing caps worst-case loss at ≤ 1% of equity (mapper).
2. **Liquidity** — per-leg bid/ask spread ≤ 10% of mid; open interest ≥ 100 when known.
3. **Position cap** — worst-case loss of the order ≤ 5% of equity (independent re-check). A structure that cannot be priced honestly — including one whose quotes imply a *negative* worst case — is refused, not waved through.
4. **Daily loss halt** — ≤ −2% of equity stops all new orders for the day, taking the **worse** of our own realized PnL (booked per closed structure) and the account's own `equity − last_equity`. Two independent measures, so a bug in our bookkeeping can only halt early, never late.
5. **Idempotent dedup** — the same (day, symbol, **structure**) order is never sent twice, keyed off an append-only JSONL decision ledger. Deliberately *not* keyed on price or size: both are re-derived from live quotes and live equity, so hashing them meant the same trade re-proposed twenty minutes later slipped straight past the gate — see [ADVERSARIAL-REVIEW.md](ADVERSARIAL-REVIEW.md) §1.1.
6. **Direction-flip guard** — while a position is open in a symbol, a reverse open is refused until the pre-registered close rules below have closed it (a confirmed close writes a `position_closed` ledger record; the guard reads exactly that). Hedged long+short books are banned outright. The guard's picture of what is open is **reconciled against the broker's actual positions every round** (§2.1): a picture assembled from order *intents* drifts from reality in both directions — an accepted-but-unfilled order freezes a symbol for the entire contest, a forgotten real position lets the agent open against itself — and both leave a log that looks perfectly healthy.
6b. **Market open** — Alpaca's `/v2/clock` first, a deterministic ET calendar (holidays, 13:00 ET half-days, DST, clock-outage fallback) second. Closed → stand down, record it, retry next round rather than burning the day.
7. **Pre-registered close rules R1–R4** — every open structure is re-evaluated **before any new open**, with parameters **frozen in `config/close_rules.json` dated 2026-08-24, before the contest window** — not improvised mid-run:
   - **R1 time gate** — DTE ≤ 2 → close unconditionally (avoid pin/assignment week).
   - **R2 take profit** — debit: value ≥ 1.5× entry (+50%); credit: buy back at ≤ 0.5× the premium received (keep 50%).
   - **R3 stop loss** — debit: value ≤ 0.5× entry (−50%); credit: buy back at ≥ 2.0× the premium received (lose 1× premium).
   - **R4 signal flip** — a reverse signal closes the old structure *first*; only after that close confirms does the flip guard admit the new direction. Same-day ordering is built into the daily run: close checks precede opens.

   Valuation is snapshot mid, the same source as at entry. A leg with no quote skips its structure that round; after 3 consecutive gapped rounds the structure is force-closed at market — *never hold a position we can't see*. Checks run twice per session (open+30min, close−45min), and unwinds go through the **same atomic mleg CLI path** as entries, with explicit `*_to_close` intents and the credit/debit sign preserved.

Every decision — signal, mapping, gate verdicts, red-team protocol JSON, order intent, close-rule verdicts, shadow twin — is one appended line in the ledger: a complete, replayable audit trail.

## Alpaca infrastructure

- **Data:** `/v2/options/contracts` + `/v1beta1/options/snapshots` (indicative feed) give strikes, quotes, and greeks in one merged chain; equity from `/v2/account`. All read-only.
- **Orders:** the official Alpaca CLI — built for agent/cron sessions — submits each spread as **one atomic multi-leg order** (`--order-class mleg`, net limit price, negative = credit; sign preserved, a bug we caught live). Default mode is the CLI's own `--dry-run`; real submission requires both a `--live` flag and `ALPACA_HACKATHON_LIVE=1`, two independent switches.
- **MCP:** the red-team LLM reads account and market context through Alpaca's MCP server behind a client-side read-only tool allowlist (every `place_*`/`cancel_*` call is denied by the harness) — it observes everything and touches nothing. With `GATED_AGENT_REDTEAM=llm` (or `ANTHROPIC_API_KEY`) in `.env` the MCP-backed red-teamer runs; without either, a deterministic stub answers the same three questions in the same protocol, so tests and keyless clones stay offline. Fail-closed either way: any error or garbage output is a veto — but an *infrastructure* failure is tagged as such, and two consecutive days of nothing but those raises a loud alarm, because a red-teamer that has stopped launching looks identical to a cautious one and would otherwise cost a silent week of zero trades.
- **Environment:** Alpaca paper account; a Windows scheduled task runs the agent each weekday morning; keys live only in `.env` (gitignored), and with no keys the whole system still runs offline on a labeled synthetic chain.

*Toy signal, real discipline. The gates, the veto loop, the negative control, and the audit log are the product.*
