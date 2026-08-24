# gated-agent

**An agent that red-teams its own options risk exposure before every order.**

Built for the Alpaca AI Trading Agents Hackathon (Aug 28 – Sep 4, 2026). MIT licensed.

## The idea

Most trading-agent demos put the LLM in charge of finding alpha. We think that's
backwards. Here the signal is deliberately a **toy from public literature** —
Faber's 10-month SMA trend rule on SPY/QQQ/IWM — and all the engineering goes
into the part that actually keeps accounts alive: **discipline**.

> **Toy signal, real discipline.** The signal is honest about being simple.
> The gates, the red-team veto loop, the negative control, and the append-only
> audit log are the product.

Two design rules follow from that:

1. **The LLM can only veto, never construct.** Orders are built and submitted by
   deterministic code (Alpaca CLI path). The LLM red-teamer interrogates each
   order — worst-case loss, greeks exposure, liquidity exit — and can kill it.
   It can never size it, price it, or invent a new one.
2. **Every live decision has a random twin.** A seeded random signal runs through
   the *identical* pipeline into a shadow book, logged side by side. If the live
   book can't beat its own coin-flip twin, the signal is noise and the log proves
   it — a built-in placebo arm.

## Architecture

```
                ┌──────────────────────────────────────────────────────┐
                │                 python -m gated_agent.run            │
                │              (daily, idempotent per day)             │
                └──────────────────────────────────────────────────────┘
                                        │
        ┌───────────────────────────────┼──────────────────────────────┐
        ▼                                                              ▼
┌───────────────────┐                                        ┌───────────────────┐
│  signals.py       │   toy: Faber 10-mo SMA on              │  negctl.py        │
│  SPY/QQQ/IWM      │   daily closes (yfinance)              │  seeded RANDOM    │
│  {symbol,direction│                                        │  signal, same     │
│   strength,spot}  │                                        │  contract shape   │
└─────────┬─────────┘                                        └─────────┬─────────┘
          │                    IDENTICAL PIPELINE                      │
          ▼                                                            ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  options_mapper.py   deterministic signal -> defined-risk option legs        │
│  (strong: debit spreads · mild long: CSP · mild short: credit call spread)   │
└──────────────────────────────────────┬───────────────────────────────────────┘
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  gates.py            arithmetic, cannot be talked out of                     │
│  [1] worst-case loss <= 5% equity   [2] daily loss halt at -2%               │
│  [3] idempotent dedup (same day+symbol+legs never sent twice)                │
└──────────────────────────────────────┬───────────────────────────────────────┘
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  redteam_mcp.py      LLM via Alpaca MCP*, VETO-ONLY, 3 questions pre-order:  │
│  1. max-loss scenario?  2. greeks exposure?  3. liquidity exit cost?         │
│  emits redteam.v1 protocol JSON  →  approve | veto                           │
└───────────┬──────────────────────────────────────────────┬───────────────────┘
   live book│                                              │shadow book
            ▼                                              ▼
┌───────────────────────────┐                  ┌───────────────────────────────┐
│  order_cli.py             │                  │  STOP. shadow book never      │
│  Alpaca CLI* submission   │                  │  reaches the order path —     │
│  deterministic, no LLM    │                  │  logged as would-trade only   │
└───────────┬───────────────┘                  └───────────────┬───────────────┘
            └──────────────────────┬───────────────────────────┘
                                   ▼
                  ┌─────────────────────────────────┐
                  │  ledger.py  append-only JSONL   │
                  │  every decision, both books,    │
                  │  side by side                   │
                  └─────────────────────────────────┘

* stubbed until the Alpaca account exists — see "What's stubbed" below.
```

## Signal contract (the isolation interface)

```python
{"symbol": "SPY", "direction": "long" | "short" | "neutral",
 "strength": 0.0..1.0,   # |close - SMA| / SMA, saturating at 5% distance
 "spot": 640.25}
```

Everything downstream depends only on this dict. Swap in any signal you like;
the discipline layers don't care.

## Run it (today, no account, no keys)

```bash
pip install -e ".[dev]"
python -m gated_agent.run --dry-run     # live yfinance closes -> intended orders
pytest                                  # offline unit + pipeline tests
```

Dry run prints each signal, gate verdicts, red-team verdicts, and the exact
Alpaca CLI commands it *would* run, and appends everything to
`ledger/decisions.jsonl`. Running it twice on the same day is a no-op
(idempotent per day; `--force` re-runs, order dedup still holds).

## What's real today vs stubbed awaiting the account

| Layer | Status |
|---|---|
| Faber SMA signal (yfinance) | **real, live data** |
| Options mapper (signal → defined-risk legs) | **real, deterministic, tested** |
| Risk gates (5% cap, -2% halt, dedup) | **real, tested** |
| Red-team veto protocol (`redteam.v1` JSON) | **real contract**; answers come from a deterministic stub — swaps to LLM + Alpaca MCP |
| Option chain | **synthetic** (labeled `"synthetic": true`) — swaps to Alpaca options data |
| Order submission | **dry-run stub** printing exact CLI argv — swaps to subprocess once keys exist |
| Fills / realized PnL | not yet — halt gate is exercised by tests until then |

## Honesty notes

- The signal layer contains nothing proprietary: it is the 10-month SMA rule
  from Mebane Faber's *A Quantitative Approach to Tactical Asset Allocation*,
  approximated as a 210-trading-day SMA. It is a toy on purpose.
- No keys anywhere in the repo. `.env.example` only; `.env*` is gitignored.
- `src/gated_agent/options_mapper.py` and its tests were written clean-room for
  this hackathon (see module docstring) and are preserved verbatim.

## License

MIT — see [LICENSE](LICENSE).
