# Submission package — filled in advance, submitted by a human

Deadline: **Friday 2026-09-04 15:00 UTC = 08:00 PT** — verified 2026-09-01
against the event page's own data (`endAt: 2026-09-04T15:00:00.000Z`;
timeline entry "End of Submissions! Fri Sep 04 2026 17:00 CEST"), at
https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon. That is
one hour after the 07:00 PT run — see the stand-down plan in
ADVERSARIAL-REVIEW.md for why the book should be frozen the evening before.
**Plan: submit the form on Thursday 9/3 evening PT**, not on the deadline
morning; every link is already live, so submitting early costs nothing and
a 60-minute window on 9/4 is no buffer at all.

Two things the event page requires that this package already satisfies:
the Alpaca paper account ID must be on the form and must be a **fresh
account created for the hackathon** (PA32VHBO5AOB was created for it and
switched in on 08-28 — never reuse the dev account), and up to five X /
LinkedIn post links may optionally be attached (none planned).

Nothing in this repo submits anything anywhere; this file is the text to
paste, prepared so submission involves zero writing.

## The form, field by field

The event page's "What to submit" list (read 2026-09-03) has twelve fields
in three groups. Every one is prepared below; nothing needs writing at
submission time.

| Form field | Value |
|---|---|
| **Basic information** | |
| Project title | `Gated Agent` |
| Short description | `An options agent that red-teams its own risk before every order — toy signal, real discipline.` |
| Long description | the block quote under "Suggested form text" below, verbatim |
| Technology & category tags | Alpaca Trading API · Alpaca MCP server · Alpaca CLI · Python · Streamlit · options trading · agents / risk management |
| **Cover image, presentation and write-up** | |
| Cover image | `docs/cover.png` — 1920×1080 title card (upload the file) |
| Video presentation | `https://github.com/Theodore-Liu/gated-agent/blob/main/docs/video.mp4` — 2:58, plays inline on GitHub (raw file: `https://raw.githubusercontent.com/Theodore-Liu/gated-agent/main/docs/video.mp4`, 7.6 MB). Beat sheet + narration: `docs/VIDEO-SCRIPT.md`; what is on screen and why it is real: `docs/MEDIA-BUILD-NOTES.md`. **If the field only accepts a YouTube/Vimeo/Loom URL**, upload `docs/video.mp4` as an unlisted YouTube video and paste that link instead — the file is final, only the host changes. |
| Slide presentation | `https://github.com/Theodore-Liu/gated-agent/blob/main/docs/slides.pdf` — 7 pages; text source `docs/SLIDES.md` (upload the PDF if the field wants a file). |
| One-page write-up | `https://github.com/Theodore-Liu/gated-agent/blob/main/docs/WRITEUP.md` — exactly the three sections the event asks for (AI logic · risk gates · Alpaca infrastructure). Also slides 2–5. |
| **App hosting and repository** | |
| Public GitHub repository | `https://github.com/Theodore-Liu/gated-agent` |
| Demo application platform | Streamlit Community Cloud |
| Application URL | `https://gated-agent-live.streamlit.app` — LIVE since 08-24 on the competition account. **Open it in a browser right before submitting**: Community Cloud hibernates after ~12h without a real viewer session and greets the next visitor with a "Zzzz" wake-up screen (found asleep 09-03 16:10 PT; woken and re-verified rendering equity + positions at 16:14 PT). The hourly `GatedAgentDemoKeepAlive` task now drives a real browser session through Playwright (`scripts/keepalive_demo.py`) and clicks the wake-up button itself; its log line must read `ok: rendered`. `/healthz` returning 200 does **not** mean the app is awake. |
| Alpaca paper trading account ID | `PA32VHBO5AOB` |
| Social posts (optional, up to 5) | none |

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

## Pre-submission checklist (Thursday 9/3 evening PT; re-run the first item on the morning of 9/4)

- [x] Dashboard URL loads and shows the account (not a traceback, not the
      "Zzzz" sleep screen) — 09-03 16:14 PT: rendered equity $103,373 and
      the open legs; keepalive task re-run through Task Scheduler, rc 0,
      `ok: rendered`. **Re-open in a browser right before submitting.**
- [x] `python scripts/verify_account_swap.py` — 09-03 16:05 PT: account
      ACTIVE, options level 3, `PA32VHBO5AOB`, paper endpoint. (Its two
      WARNs — equity ≠ $100k, positions ≠ 0 — are the swap-day checks; after
      seven trading days on this account they are the expected state.)
- [x] Repo pushed: `git status` clean, HEAD == origin/main, README renders.
- [x] Video + slides links world-viewable — 09-03: GitHub blob pages and raw
      files all HTTP 200 without a login (video 7.6 MB, slides 114 KB).
- [x] Book state 09-03 close: NVDA and QQQ closed by R2 take-profit today
      (+$2,229 and +$810 realized, lot-matched, all fills confirmed at the
      broker); AAPL/IWM/SPY spreads riding (all in profit, none at target);
      today's AAPL 9/18 open expired unfilled (reconcile drops it tomorrow).
      Equity $103,373. Live close task continues through expiry.
