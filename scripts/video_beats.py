"""The demo video as a beat table: one narration sentence = one visual.

Each beat is what the viewer hears (`text`), what is on screen while they hear
it (`visual`), and the phrase that stays on the lower third (`lt`). Frames are
cut on sentence boundaries — hard cuts, no crossfades — so the picture always
shows the thing the voice is talking about, with that thing highlighted and
the rest dimmed. make_media.py renders, voices and assembles this table and
also regenerates docs/VIDEO-SCRIPT.md from it.

Every visual is a real artifact: a source file in this repo, a captured log
block, a ledger record, or a screenshot of the live pages. Nothing is mocked.

Visual kinds
  term  {src, lines=(a,b)?, focus=[(a,b) | "substring", ...], title, cap}
        src = a media-work/*.txt capture; `lines` (optional) is the 1-based
        SOURCE line window shown; each focus item is a (a,b) source-line range
        or a substring that marks every line containing it. Non-focus lines
        are dimmed.
  crop  {src, y, box=(x,y,w,h)?}   1920x1080 crop of a webshot at offset y;
        box (in crop coordinates) is outlined and everything else dimmed.
  fit   {src, part='top'|'bottom'|'full'}   the mermaid render, fitted.
  card  {lines=[...]}   a plain closing card (repo + dashboard URLs).
"""
from __future__ import annotations

LIVE = "live competition account · PA32VHBO5AOB"
REHEARSAL = "rehearsal account (dev) · 2026-08-25 → 08-27"

T_LOG = "logs\\daily.log — scheduled task GatedAgentDaily · 07:00 PT · 2026-08-31"
T_VETO = "red-team vetoes — ledger-devtest-20260825-27/decisions.jsonl"
T_QQQ = "the QQQ veto — per-order math passed, the book did not"
T_LEDGER = "ledger\\decisions.jsonl — append-only, one line per decision"

BEATS: list[dict] = [
    # ── shot 1: the one-liner ─────────────────────────────────────────────
    dict(id="b01", shot=1,
         text="This is Gated Agent: an options paper-trading agent whose every "
              "order has to survive its own red team.",
         lt="every order must survive its own red team",
         visual=dict(kind="crop", src="readme_full.png", y=60)),
    dict(id="b02", shot=1,
         text="The signal is deliberately a textbook toy — Faber's ten-month "
              "moving average.",
         lt="signal = 10-month moving average · on purpose",
         visual=dict(kind="term", src="shot2.txt", lines=(1, 12),
                     focus=[(7, 7), (11, 11)], title=T_LOG, cap=LIVE)),
    dict(id="b03", shot=1,
         text="All the engineering effort lives between that signal and broker "
              "submission.",
         lt="signal → mapper → gates → red team → broker",
         visual=dict(kind="fit", src="mermaid.png", part="full")),

    # ── shot 2: a live run ────────────────────────────────────────────────
    dict(id="b04", shot=2,
         text="Every day, one pass.",
         lt="one scheduled run per trading day · 07:00 PT",
         visual=dict(kind="term", src="shot2.txt", lines=(1, 12),
                     focus=[(1, 1)], title=T_LOG, cap=LIVE)),
    dict(id="b05", shot=2,
         text="First, it checks the pre-registered exits: take profit, stop "
              "loss, and a mandatory time-based exit before expiration week.",
         lt="exit rules R1–R3 · frozen in config before the contest",
         visual=dict(kind="term", src="close_rules.txt", lines=(1, 14),
                     focus=["R1 time gate", "R2 take profit", "R3 stop loss"],
                     title="src/gated_agent/position_manager.py")),
    dict(id="b06", shot=2,
         text="They are rules, not judgement calls: on September first the "
              "stop-loss closed I W M for a loss, and wrote down why.",
         lt="position_closed · R3_stop_loss · pnl −828",
         visual=dict(kind="term", src="position_closed.txt",
                     focus=['"pnl"', '"rule"', '"why"'],
                     title="ledger\\decisions.jsonl — position_closed",
                     cap=LIVE)),
    dict(id="b07", shot=2,
         text="Then, per symbol: deterministic code maps the signal to a "
              "defined-risk spread.",
         lt="signal → bull call spread · worst case $678",
         visual=dict(kind="term", src="shot2.txt", lines=(7, 18),
                     focus=[(7, 9)], title=T_LOG, cap=LIVE)),
    dict(id="b08", shot=2,
         text="Deterministic risk gates — arithmetic, no LLM — check the "
              "worst-case loss against a five-percent cap, a two-percent "
              "daily-loss halt, duplicate orders, direction conflicts, and "
              "liquidity.",
         lt="gates.py — arithmetic that cannot be talked out of",
         visual=dict(kind="term", src="gates.txt", lines=(1, 17),
                     focus=[(4, 13)], title="src/gated_agent/gates.py")),
    dict(id="b09", shot=2,
         text="The halt is not decorative: on September first it fired, and "
              "all five of that day's orders were refused.",
         lt="daily_loss_halt · allowed: false · 5 of 5 orders refused · 2026-09-01",
         visual=dict(kind="term", src="gate_check.txt", lines=(1, 31),
                     focus=['"allowed": false', "daily_loss_halt",
                            "halt floor"],
                     title="ledger\\decisions.jsonl — gate_check", cap=LIVE)),
    dict(id="b10", shot=2,
         text="Only after every gate passes does an LLM — connected read-only "
              "to Alpaca's MCP server — get the one power it has: veto.",
         lt="LLM red team · read-only MCP · its only power is veto",
         visual=dict(kind="term", src="protocol.txt",
                     focus=["An LLM connected", "context, before each order",
                            "ONLY power is veto", "path never lets it"],
                     title="src/gated_agent/redteam_mcp.py")),

    # ── shot 3: the red team saying no ────────────────────────────────────
    dict(id="b11", shot=3,
         text="This is a real veto from rehearsal week.",
         lt="read-only dump of the rehearsal ledger",
         visual=dict(kind="term", src="shot3.txt", lines=(1, 14),
                     focus=[(1, 4)], title=T_VETO, cap=REHEARSAL)),
    dict(id="b12", shot=3,
         text="The spread passed the per-trade loss check at under one percent.",
         lt="max_loss_scenario → pass",
         visual=dict(kind="term", src="shot3.txt", lines=(15, 28),
                     focus=[(21, 25)], title=T_VETO, cap=REHEARSAL)),
    dict(id="b13", shot=3,
         text="The review vetoed it anyway: it would have added a third "
              "correlated bullish spread, reusing a strike already in the book.",
         lt="verdict: veto — a third correlated bull spread, same 712 strike",
         visual=dict(kind="term", src="shot3_qqq.txt", lines=(1, 16),
                     focus=[(5, 8)], title=T_QQQ, cap=REHEARSAL)),
    dict(id="b14", shot=3,
         text="Portfolio concentration risk that per-trade loss math does not "
              "capture.",
         lt="greeks_exposure → veto · the per-order check cannot see the book",
         visual=dict(kind="term", src="shot3_qqq.txt", lines=(1, 16),
                     focus=[(11, 14)], title=T_QQQ, cap=REHEARSAL)),
    dict(id="b15", shot=3,
         text="The review used account and market data fetched over MCP at "
              "decision time.",
         lt="live greeks + open positions, fetched over Alpaca MCP",
         visual=dict(kind="term", src="shot3_qqq.txt", lines=(1, 16),
                     focus=[(10, 10)], title=T_QQQ, cap=REHEARSAL)),
    dict(id="b16", shot=3,
         text="And the protocol derives the final decision from the recorded "
              "verdicts: if the model marks any question as failed, the order "
              "is vetoed.",
         lt="redteam.v1 · any failed question ⇒ veto",
         visual=dict(kind="term", src="protocol.txt",
                     focus=['"verdict": "pass" | "veto"',
                            "veto if ANY question", '"veto_reasons"'],
                     title="src/gated_agent/redteam_mcp.py — protocol "
                           "redteam.v1")),

    # ── shot 4: the ledger ────────────────────────────────────────────────
    dict(id="b17", shot=4,
         text="Every decision lands in an append-only JSON Lines ledger: the "
              "signal, the gate results, the red-team transcript, and the "
              "order intent.",
         lt="ledger/decisions.jsonl · append-only",
         visual=dict(kind="term", src="shot4.txt", lines=(1, 15),
                     focus=[(4, 9)], title=T_LEDGER, cap=LIVE)),
    dict(id="b18", shot=4,
         text="Submitted orders also record the broker's own receipt,",
         lt="broker_receipt.id — Alpaca's order id, not ours",
         visual=dict(kind="term", src="shot4_record.txt",
                     focus=['"broker_receipt"', '"id"', '"status"', '"symbol"',
                            '"submitted"'],
                     title="one order_intent record — with the broker's own "
                           "receipt", cap=LIVE)),
    dict(id="b19", shot=4,
         text="and the ledger is reconciled programmatically against the "
              "account each round.",
         lt="position_reconciled · believed book vs. the broker's book",
         visual=dict(kind="term", src="reconciled.txt",
                     focus=['"kind"', '"was"', '"why"'],
                     title="ledger\\decisions.jsonl — position_reconciled",
                     cap=LIVE)),
    dict(id="b20", shot=4,
         text="Beside it runs a negative control: a seeded random signal "
              "through the same pipeline, shadow-only and hard-blocked from "
              "ever submitting — a baseline for what the same gates do to "
              "random choices.",
         lt="negative control · shadow only · never submits",
         visual=dict(kind="term", src="shot2.txt", lines=(7, 26),
                     focus=[(10, 10), (14, 14), (18, 18), (22, 22), (26, 26)],
                     title=T_LOG, cap=LIVE)),

    # ── shot 5: what judges see ───────────────────────────────────────────
    dict(id="b21", shot=5,
         text="This is the public dashboard — the page judges see. It shows "
              "the live competition paper account.",
         lt="gated-agent-live.streamlit.app · read-only",
         visual=dict(kind="crop", src="dashboard_full.png", y=90)),
    dict(id="b22", shot=5,
         text="It started with one hundred thousand dollars, and every "
              "position is a defined-risk spread.",
         lt="$100,000 start · every position a defined-risk spread",
         visual=dict(kind="crop", src="dashboard_full.png", y="second")),
    dict(id="b23", shot=5,
         text="Beyond returns, we optimized for something rarer — and this "
              "file is the part we are proudest of: two adversarial reviews "
              "that broke the agent before the market could,",
         lt="docs/ADVERSARIAL-REVIEW.md",
         visual=dict(kind="crop", src="advrev_full.png", y=0)),
    dict(id="b24", shot=5,
         text="nineteen defects found by attacking our own system — and then "
              "a post-mortem of the two the market found anyway on day three. "
              "Every one fixed with a regression test.",
         lt="19 + 2 defects · every one pinned by a test · 227 tests",
         visual=dict(kind="crop", src="advrev_market.png", y=0)),
    dict(id="b25", shot=5,
         text="Trust is not the absence of bugs. It is evidence that known "
              "failure modes stay fixed.",
         lt="",
         visual=dict(kind="card", lines=[
             "gated-agent",
             "github.com/Theodore-Liu/gated-agent",
             "gated-agent-live.streamlit.app",
             "",
             "Trust is not the absence of bugs.",
             "It is evidence that known failure modes stay fixed."])),
]

SHOT_TITLES = {1: "the one-liner", 2: "a live run",
               3: "the red team saying no", 4: "the ledger",
               5: "what judges see"}
