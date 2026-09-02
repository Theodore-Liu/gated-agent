# Media build notes — video + slides

Built locally by `scripts/make_media.py` (re-runnable: `python
scripts/make_media.py` rebuilds every stage; pass stage names — `text web
frames tts video script slides verify` — to rebuild selectively). No orders
were placed, no live switch was touched, and the live ledger was never
written to — every capture is read-only.

## Deliverables

| Artifact | Verified |
|---|---|
| `docs/video.mp4` | 177.6 s (≤ 3:00) · 1920x1080 · H.264 + AAC · 7.6 MB |
| `docs/slides.pdf` | 7 pages · 16:9 · system fonts only, renders offline |
| `docs/VIDEO-SCRIPT.md` | generated from `scripts/video_beats.py` (stage `script`) |

## How the video is put together

`scripts/video_beats.py` is the single source of truth: 25 beats, one
narration sentence each, with the visual that sentence is about. Each beat
is voiced separately (local Kokoro TTS, voice `af_heart`, speed 1.0) and
rendered as one 1920x1080 frame; the video is those frames cut hard on the
sentence boundaries (0.25 s lead / 0.35 s tail per beat, +0.5 s at shot
ends), so the picture always shows the thing the voice is describing. On
terminal-style frames the lines being talked about are highlighted and the
rest dimmed; every frame carries a lower-third key phrase, and frames from
account data are badged `live competition account · PA32VHBO5AOB` or
`rehearsal account (dev) · 2026-08-25 → 08-27`.

Loudness is normalised once over the whole programme (loudnorm I=−18,
true peak −1.5 dB). Pronunciation respellings (`LLM → "L L M"`, `MCP →
"M C P"`, `QQQ → "Q Q Q"`, `mleg → "em-leg"`) are applied to the TTS input
only; the script file shows the words as written.

## Authenticity ledger — what is on screen

Nothing is mocked. Every visual is one of:

- **Source files of this repo**, shown verbatim: the module docstrings of
  `gates.py` (the four arithmetic gates), `position_manager.py` (exit rules
  R1–R4) and `redteam_mcp.py` (the three questions and the `redteam.v1`
  protocol).
- **The 2026-08-31 live competition round**, verbatim from `logs/daily.log`
  (scheduled task `GatedAgentDaily`, 07:00 PT): close checks, per-symbol
  signal → order intent → submitted `alpaca` command, and the shadow
  negative-control lines beside each one.
- **Single live-ledger records** from `ledger/decisions.jsonl`, pretty-
  printed and otherwise unaltered: the 2026-09-01 `gate_check` refused by
  `daily_loss_halt` (all five orders that day were), the 2026-09-01
  `position_closed` (IWM, R3 stop-loss, pnl −828), an `order_intent` with
  its `broker_receipt` (`cli_commands` omitted for legibility), and a
  `position_reconciled` record.
- **The rehearsal-week QQQ veto** from `ledger-devtest-20260825-27/
  decisions.jsonl` (the exact read-only dump command is on screen).
- **Screenshots**: the public GitHub README, the README's own Mermaid
  diagram rendered locally from the identical source (GitHub's Mermaid
  iframe does not render under headless Chrome), the dashboard page
  (same code, same competition account as `gated-agent-live.streamlit.app`),
  and `docs/ADVERSARIAL-REVIEW.md` rendered locally (its title page and the
  2026-09-01 "What the market found" section).

## Slides

`docs/SLIDES.md` text used verbatim; layout only (16:9 pages, Segoe UI +
Consolas, single crimson accent). Built from `scripts/slides_content.py`
via headless-Chrome print-to-PDF; pypdf-verified 7 pages.

## Pipeline stages

`text` (capture real terminal text and ledger records) → `web` (README /
dashboard / review-doc / Mermaid screenshots) → `frames` (one PNG per beat
+ a contact sheet for review) → `tts` (one Kokoro WAV per beat) → `video`
(per-beat still+voice segments → concat) → `script` (regenerate
VIDEO-SCRIPT.md) → `slides` (HTML → PDF) → `verify` (ffprobe asserts).
Intermediates live in gitignored `media-work/`.
