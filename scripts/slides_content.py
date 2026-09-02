# -*- coding: utf-8 -*-
"""The submission deck as HTML — text is docs/SLIDES.md verbatim; this file
owns layout only. System fonts only: the PDF renders identically offline.
Printed by scripts/make_media.py via headless Chrome (@page 13.333in x 7.5in;
each .slide is 1280x720 css px == the same size at 96dpi)."""

CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
@page { size: 13.333in 7.5in; margin: 0; }
html, body { background:#e8e6e1; }
body { font-family:'Segoe UI','Segoe UI Variable',system-ui,sans-serif;
       color:#1c212b; }
.slide { width:1280px; height:720px; background:#fbfaf8; overflow:hidden;
         position:relative; page-break-after:always; padding:64px 76px;
         display:flex; flex-direction:column; }
.slide:last-child { page-break-after:auto; }
code, .mono { font-family:Consolas,'Cascadia Mono',monospace; }

.kick { font-size:15px; letter-spacing:.14em; text-transform:uppercase;
        color:#b3282d; font-weight:600; margin-bottom:10px; }
h1 { font-size:64px; font-weight:650; letter-spacing:-.015em; }
h2 { font-size:40px; font-weight:650; letter-spacing:-.01em;
     margin-bottom:26px; }
.rule { width:64px; height:4px; background:#b3282d; border-radius:2px;
        margin:22px 0 26px; }
.foot { position:absolute; left:76px; right:76px; bottom:30px;
        display:flex; justify-content:space-between; font-size:13.5px;
        color:#8a8577; }
.pageno { color:#b3282d; font-weight:600; }

.big { font-size:27px; line-height:1.45; }
.lede { font-size:24px; line-height:1.5; color:#3a4150; }
strong { font-weight:650; color:#14181f; }
ul { list-style:none; }
li { font-size:22px; line-height:1.5; margin-bottom:16px;
     padding-left:30px; position:relative; }
li::before { content:''; position:absolute; left:2px; top:13px; width:9px;
             height:9px; background:#b3282d; border-radius:2px; }
.chip { display:inline-block; background:#f1efe9; border:1px solid #ddd8cc;
        border-radius:8px; padding:10px 18px; margin:0 10px 12px 0;
        font-size:20px; color:#2a303b; }
.panel { background:#f4f2ec; border-left:5px solid #b3282d; padding:22px 28px;
         border-radius:0 10px 10px 0; font-size:21px; line-height:1.5; }
.quote { font-size:26px; line-height:1.5; font-style:italic; color:#2a303b;
         border-left:5px solid #b3282d; padding-left:26px; }
.meta { font-size:20px; line-height:2.0; color:#3a4150; }
.meta code { background:#f1efe9; padding:2px 9px; border-radius:6px;
             font-size:18px; }
.num { color:#b3282d; font-weight:650; font-variant-numeric:tabular-nums; }
.qrow { display:flex; gap:18px; margin-bottom:14px; align-items:flex-start; }
.qn { font-size:26px; font-weight:650; color:#b3282d; min-width:34px;
      font-variant-numeric:tabular-nums; }
.qt { font-size:22px; line-height:1.45; padding-top:2px; }
.stat { font-size:96px; font-weight:650; color:#b3282d;
        letter-spacing:-.02em; line-height:1; }
.statlbl { font-size:24px; color:#3a4150; margin-top:6px; }
"""


def _foot(n: int) -> str:
    return (f"<div class='foot'><span>Gated Agent — lablab.ai × Alpaca "
            f"AI Trading Agents Hackathon</span>"
            f"<span class='pageno'>{n} / 7</span></div>")


S1 = f"""
<div class="slide" style="justify-content:center">
  <div class="kick">lablab.ai × Alpaca AI Trading Agents Hackathon · team Gated Agent</div>
  <h1>Gated Agent</h1>
  <div class="rule"></div>
  <div class="big" style="max-width:900px"><strong>An options agent whose every
  order must survive its own red team.</strong></div>
  <div class="meta" style="margin-top:40px">
    Public repo <code>Theodore-Liu/gated-agent</code> ·
    live demo <code>gated-agent-live.streamlit.app</code><br>
    Competition account <code>PA32VHBO5AOB</code> — every trade a
    defined-risk option spread.
  </div>
  {_foot(1)}
</div>"""

S2 = f"""
<div class="slide">
  <div class="kick">the premise</div>
  <h2>The inversion</h2>
  <div class="lede" style="margin-bottom:34px">Most trading-agent demos put
  the LLM in charge of finding alpha.<br>
  <strong style="font-size:28px">We put it in charge of saying no.</strong></div>
  <ul>
    <li>Signal: deliberately a textbook toy — Faber 10-month SMA on
        <span class="mono">SPY / QQQ / IWM / AAPL / NVDA</span>.
        Public literature, four fields.</li>
    <li>Deterministic code maps signal → defined-risk spread
        (Δ-targeted legs).</li>
    <li>The LLM's only power is <strong>veto</strong>. It cannot size, price,
        or place anything.</li>
  </ul>
  {_foot(2)}
</div>"""

S3 = f"""
<div class="slide">
  <div class="kick">deterministic layer</div>
  <h2>Arithmetic that cannot be talked out of</h2>
  <div class="lede" style="margin-bottom:24px">Six gates, before the LLM ever
  sees the order:</div>
  <div style="margin-bottom:36px">
    <span class="chip">worst-case loss ≤ 1% per trade</span>
    <span class="chip">5% position cap</span>
    <span class="chip">−2% daily halt</span><br>
    <span class="chip">idempotent dedup</span>
    <span class="chip">direction-flip guard (no hedged books)</span>
    <span class="chip">liquidity floor</span>
  </div>
  <div class="panel">Exits are <strong>pre-registered</strong>: R1–R4 frozen in
  <code>config/close_rules.json</code> <em>before</em> the contest window —
  git history proves the rules predate the runs.</div>
  {_foot(3)}
</div>"""

S4 = f"""
<div class="slide">
  <div class="kick">the veto layer</div>
  <h2>The red team</h2>
  <div class="lede" style="margin-bottom:22px">Before each order, an LLM wired
  <strong>read-only</strong> into Alpaca's official MCP server answers three
  questions from live account + market data:</div>
  <div class="qrow"><div class="qn">1</div><div class="qt">What exactly is lost
    in the worst case — computed or guessed?</div></div>
  <div class="qrow"><div class="qn">2</div><div class="qt">Does this stack
    dangerously with <em>this book's</em> existing positions?</div></div>
  <div class="qrow"><div class="qn">3</div><div class="qt">What does the exit
    cost through this spread tomorrow?</div></div>
  <div class="panel" style="margin-top:20px">Real rehearsal veto: per-order
  math fine at &lt;1% max loss — vetoed anyway for stacking a third correlated
  spread sharing a strike with an open position.
  <strong>The protocol recomputes the verdict from per-question verdicts:
  the model cannot approve past its own failed question.</strong></div>
  {_foot(4)}
</div>"""

S5 = f"""
<div class="slide">
  <div class="kick">audit layer</div>
  <h2>Every decision traceable</h2>
  <ul style="margin-top:10px">
    <li>Append-only JSONL ledger: signal → gates → red-team transcript →
        order intent → <strong>broker receipt</strong>, reconciled against
        the account every round.</li>
    <li><strong>Negative control</strong>: a seeded random signal runs the
        identical pipeline, shadow-only, structurally unable to reach the
        order path.</li>
    <li>Torn-tail quarantine, crash-safe dedup, market-holiday awareness —
        it ran the contest week unattended.</li>
  </ul>
  {_foot(5)}
</div>"""

S6 = f"""
<div class="slide">
  <div class="kick">what we are proudest of</div>
  <h2>We attacked it before the market could</h2>
  <div style="display:flex; gap:56px; align-items:flex-start">
    <div style="min-width:250px">
      <div class="stat">19</div>
      <div class="statlbl">real defects</div>
      <div class="statlbl" style="margin-top:18px">Tests:
        <span class="num">110 → 219</span></div>
    </div>
    <div>
      <div class="lede" style="font-size:22px; margin-bottom:22px">Two
      adversarial reviews, pre-contest
      (<code style="font-size:19px">docs/ADVERSARIAL-REVIEW.md</code>) — a
      dedup gate that didn't dedup, a dead loss-halt, a close rule that
      wasn't unconditional, a negative control silently inheriting the live
      book — <strong>each fixed with a regression test that fails on the
      pre-fix tree.</strong></div>
      <div class="quote">A trustworthy agent isn't one that never had bugs.<br>
      It's one that hunts its own.</div>
    </div>
  </div>
  {_foot(6)}
</div>"""

S7 = f"""
<div class="slide">
  <div class="kick">closing</div>
  <h2>What we'd claim</h2>
  <ul style="margin-top:8px">
    <li>Discipline as the product: pre-registration, negative controls,
        veto-only LLM power, receipt-level traceability.</li>
    <li>Unattended live operation on the competition account from day 1,
        every order a filled multi-leg spread with its receipt in the
        book.</li>
    <li>Everything in this deck is verifiable in the public repo's
        git history.</li>
  </ul>
  <div style="margin-top:auto; font-size:19px; color:#8a8577;
              font-style:italic">Built with Alpaca Trading API + official MCP
  server + CLI (<span class="mono">mleg</span>).</div>
  {_foot(7)}
</div>"""

SLIDES_HTML = (f"<!doctype html><meta charset='utf-8'>"
               f"<title>Gated Agent — slides</title>"
               f"<style>{CSS}</style>{S1}{S2}{S3}{S4}{S5}{S6}{S7}")
