# Submission package — filled in advance, submitted by a human

Deadline: **2026-09-04 15:00 UTC = 08:00 PT** (one hour after that morning's
07:00 PT run — see the stand-down plan in ADVERSARIAL-REVIEW.md for why the
book should be frozen the evening before). Nothing in this repo submits
anything anywhere; this file is the text to paste, prepared so the deadline
morning involves zero writing.

## The five items

| Form field | Value |
|---|---|
| Public repo URL | `https://github.com/Theodore-Liu/gated-agent` |
| Demo URL | `https://gated-agent-live.streamlit.app` — LIVE since 08-24; secrets switched to the competition account on 08-28 and verified rendering it (equity ~$100k, the five day-1 spreads). **Re-check it renders on the morning of 9/4 before submitting** (liveness probe: `/healthz` — non-browser clients get a 303 on the main page, that is not an outage). |
| Video link | `https://github.com/Theodore-Liu/gated-agent/blob/main/docs/video.mp4` — 2:58, plays inline on GitHub. Beat sheet + narration: `docs/VIDEO-SCRIPT.md`; what is on screen and why it is real: `docs/MEDIA-BUILD-NOTES.md`. |
| Slides | `https://github.com/Theodore-Liu/gated-agent/blob/main/docs/slides.pdf` — 7 pages; text source `docs/SLIDES.md`. |
| Alpaca paper account | `PA32VHBO5AOB` |

## Suggested form text

**Project name:** Gated Agent

**One-liner:** An options agent that red-teams its own risk before every
order — toy signal, real discipline.

**Description:**

> Most trading-agent demos put the LLM in charge of finding alpha. We
> inverted that. The signal is deliberately a toy from public literature
> (Faber's 10-month SMA on SPY/QQQ/IWM/AAPL/NVDA); all the engineering went
> into discipline: deterministic risk gates that cannot be talked out of
> (worst-case-loss cap, −2% daily halt fed by the worse of our own P&L and
> the account's, structure-keyed idempotent dedup, a direction-flip guard
> reconciled against the broker's actual book every round), an LLM red-teamer
> on Alpaca's MCP server with veto-only power, pre-registered exit rules
> frozen in config before the contest, and a seeded random twin trading a
> shadow book as a built-in placebo arm. Every decision — including every
> mistake we caught — is one line in an append-only ledger, and the
> adversarial reviews that broke the agent before the contest could are
> committed in docs/ADVERSARIAL-REVIEW.md. Orders go out as atomic multi-leg
> spreads through the official Alpaca CLI; the MCP side is read-only by
> client-side allowlist. Account PA32VHBO5AOB ran it unattended from Windows
> scheduled tasks for the whole window.

**Built with:** Alpaca CLI (orders, atomic mleg) · Alpaca MCP server
(read-only LLM red-team) · Alpaca market data (options chains + clock) ·
Python, zero trading frameworks · Streamlit (read-only judge dashboard).

## Pre-submission checklist (morning of 9/4, before 08:00 PT)

- [ ] Dashboard URL loads and shows the account (not a traceback).
- [ ] `python scripts/verify_account_swap.py` — account ACTIVE, the P&L is
      the competition account's.
- [ ] Repo pushed: `git status` clean, README renders on GitHub.
- [ ] Video + slides links are world-viewable (open in an incognito window).
- [ ] Book state as planned: AAPL 9/4 spread already closed by R1 (9/2);
      remaining 9/11 spreads riding under the live close task.
