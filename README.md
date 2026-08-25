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

Dual Alpaca tech, split by power: the **CLI** owns the deterministic order
path, the **MCP server** feeds the veto-only LLM red-teamer. Neither side can
do the other's job.

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
│  chain_fetcher.py    Alpaca contracts + snapshots (quotes+greeks, one call)  │
│                      no keys -> labeled synthetic chain (offline mode)       │
└──────────────────────────────────────┬───────────────────────────────────────┘
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  options_mapper.py   deterministic signal -> defined-risk option legs        │
│  delta-targeted (Δ0.50 buy / Δ0.25 sell), moneyness fallback w/o greeks      │
│  strong: debit spread · mild long: credit PUT spread ·                       │
│  mild short: credit CALL spread   (CSP dropped: unaffordable on $700+ spot)  │
└──────────────────────────────────────┬───────────────────────────────────────┘
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  gates.py            arithmetic, cannot be talked out of                     │
│  [1] worst-case loss <= 5% equity   [2] daily loss halt at -2%               │
│  [3] idempotent dedup (same day+symbol+legs never sent twice)                │
│  [4] direction-flip guard: no reverse open while a position is open;         │
│      hedged long+short books banned (exits belong to the exit rules)         │
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
│  + cli_executor.py        │                  │  reaches the order path —     │
│  official Alpaca CLI,     │                  │  logged as would-trade only   │
│  one atomic mleg order    │                  └───────────────┬───────────────┘
│  per spread; net limit    │                                  │
│  neg = credit; --dry-run  │                                  │
│  default, live needs      │                                  │
│  ALPACA_HACKATHON_LIVE=1  │                                  │
└───────────┬───────────────┘                                  │
            └──────────────────────┬───────────────────────────┘
                                   ▼
                  ┌─────────────────────────────────┐
                  │  ledger.py  append-only JSONL   │
                  │  every decision, both books,    │
                  │  side by side                   │
                  └─────────────────────────────────┘

* red-team LLM+MCP is the one remaining stub — see "What's stubbed" below.
```

**Exit rules are config, not signals.** Positions are closed by pre-registered
deterministic rules — close N days before expiry, take-profit / stop-loss
lines — with parameters frozen in config before the run, never improvised
intraday. The signal only ever opens; a direction flip must wait until the
exit rules have closed the conflicting position (gate 4 enforces this).

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

With Alpaca keys in `.env` the same command switches to the real adapter:
live option chain + equity from Alpaca, orders through the official CLI —
still under the CLI's own `--dry-run`. Real submission requires `--live`
**and** `ALPACA_HACKATHON_LIVE=1` (two independent switches; drop the
[Alpaca CLI](https://github.com/alpacahq/cli/releases) binary into `bin/`
or set `ALPACA_CLI`). `scripts/register_task.cmd` registers the weekday-
morning scheduled task (`GatedAgentDaily`, hidden window, logs to
`logs/daily.log`) — to be run on kickoff day.

## What's real today vs stubbed awaiting the account

| Layer | Status |
|---|---|
| Faber SMA signal (yfinance) | **real, live data** |
| Options mapper (delta-targeted, moneyness fallback) | **real, deterministic, tested** |
| Risk gates (5% cap, -2% halt, dedup, flip guard) | **real, tested** |
| Option chain + equity (`chain_fetcher.py`) | **real code, tested offline** — needs keys in `.env`; without keys: labeled synthetic chain |
| Order submission (`cli_executor.py`, atomic mleg, credit sign verified) | **real code, live dry-run verified 2026-08-24** — needs keys for the account leg |
| Red-team veto protocol (`redteam.v1` JSON) | **real contract**; answers come from a deterministic stub — swaps to LLM + Alpaca MCP |
| Fills / realized PnL / exit-rule execution | not yet — halt gate and flip guard run off the ledger stub, exercised by tests |

## Honesty notes

- The signal layer contains nothing proprietary: it is the 10-month SMA rule
  from Mebane Faber's *A Quantitative Approach to Tactical Asset Allocation*,
  approximated as a 210-trading-day SMA. It is a toy on purpose.
- No keys anywhere in the repo. `.env.example` only; `.env*` is gitignored.
- `src/gated_agent/options_mapper.py`, `chain_fetcher.py`, `cli_executor.py`
  and the mapper tests were written clean-room for this hackathon (orchestra's
  staging modules, live-data validated 2026-08-24) and are integrated with
  minimal edits: package imports, a CLI path resolver, and `fetch_equity()` —
  each marked in-file.

## License

MIT — see [LICENSE](LICENSE).
