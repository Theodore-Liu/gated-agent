# Gated Agent — an options agent that red-teams its own risk before every order

**Team:** Gated Agent · **Hackathon:** Alpaca AI Trading Agents (Aug 28 – Sep 4, 2026) · **Repo:** MIT-licensed, runs offline end-to-end with `pytest` + `python -m gated_agent.run --dry-run`

## AI logic: signal in, red-team veto loop before anything leaves

Most agent demos put the LLM in charge of finding alpha. We invert that. The signal is deliberately a toy from public literature (Faber's 10-month SMA trend rule on SPY/QQQ/IWM), emitted as a four-field dict: `{symbol, direction, strength, spot}`. Deterministic code — never the LLM — maps it to defined-risk option structures: strong conviction becomes a debit spread (delta-targeted legs, ~0.50 buy / ~0.25 sell, moneyness fallback when the feed has no greeks); mild conviction becomes a narrow credit spread (put spread for bullish, call spread for bearish — chosen over cash-secured puts because on a $700+ underlying a CSP can never pass a sane collateral cap).

The AI's role is the **red-team veto loop**: before any order is submitted, an LLM connected to Alpaca's MCP server interrogates the order with three fixed questions — (1) *what exactly is the worst-case loss, computed or guessed?* (2) *what is the greeks exposure, and does it stack?* (3) *what does the exit cost through this spread if we must leave at tomorrow's open?* It answers in a strict `redteam.v1` JSON protocol and holds **veto-only power**: it can kill any order but can never construct, resize, or reprice one. Alongside every live decision, a seeded **random twin signal** runs through the identical pipeline into a shadow book — a built-in placebo arm: if the live book cannot beat its own coin flip, the log proves the signal is noise.

## Risk gates: arithmetic that cannot be talked out of

Every order passes all gates; any veto kills it; anything unpriceable fails closed.

1. **Per-trade max loss** — sizing caps worst-case loss at ≤ 1% of equity (mapper).
2. **Liquidity** — per-leg bid/ask spread ≤ 10% of mid; open interest ≥ 100 when known.
3. **Position cap** — worst-case loss of the order ≤ 5% of equity (independent re-check).
4. **Daily loss halt** — realized PnL ≤ −2% of equity stops all new orders for the day.
5. **Idempotent dedup** — the same (day, symbol, legs) order is never sent twice, keyed off an append-only JSONL decision ledger.
6. **Direction-flip guard** — while a position is open in a symbol, a reverse open is refused until pre-registered exit rules (close N days before expiry, take-profit / stop-loss lines — parameters frozen in config before the run, not improvised) have closed it. Hedged long+short books are banned outright.

Every decision — signal, mapping, gate verdicts, red-team protocol JSON, order intent, shadow twin — is one appended line in the ledger: a complete, replayable audit trail.

## Alpaca infrastructure

- **Data:** `/v2/options/contracts` + `/v1beta1/options/snapshots` (indicative feed) give strikes, quotes, and greeks in one merged chain; equity from `/v2/account`. All read-only.
- **Orders:** the official Alpaca CLI — built for agent/cron sessions — submits each spread as **one atomic multi-leg order** (`--order-class mleg`, net limit price, negative = credit; sign preserved, a bug we caught live). Default mode is the CLI's own `--dry-run`; real submission requires both a `--live` flag and `ALPACA_HACKATHON_LIVE=1`, two independent switches.
- **MCP:** the red-team LLM reads account and market context through Alpaca's MCP server — it observes everything and touches nothing.
- **Environment:** Alpaca paper account; a Windows scheduled task runs the agent each weekday morning; keys live only in `.env` (gitignored), and with no keys the whole system still runs offline on a labeled synthetic chain.

*Toy signal, real discipline. The gates, the veto loop, the negative control, and the audit log are the product.*
