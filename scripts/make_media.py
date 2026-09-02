"""Build the hackathon submission media — docs/video.mp4 + docs/slides.pdf.

The video is driven by scripts/video_beats.py: one narration sentence = one
visual (a "beat"). Each beat is voiced separately (Kokoro TTS), rendered as
one 1920x1080 frame with the spoken-about lines highlighted and the rest
dimmed, plus a lower-third key phrase, and cut hard on the sentence boundary.
So the picture always shows what the voice is talking about — the first build
showed 3–4 evenly-spaced frames per 30-second shot, and the visuals looked
unrelated to the narration.

Everything on screen is a REAL artifact: source files of this repo, the
2026-08-31 live round from logs/daily.log, ledger records (live and
rehearsal), and screenshots of the live README / dashboard / review doc.
Pronunciation respellings (LLM -> "L L M") are applied to the TTS INPUT ONLY;
docs/VIDEO-SCRIPT.md is regenerated from the beat table (stage `script`).

Nothing here runs the trading pipeline, touches the live ledger, or talks to
the broker. All reads.

Usage:
    python scripts/make_media.py                 # all stages
    python scripts/make_media.py text web frames tts video script slides verify
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import wave
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "media-work"
FRAMES = WORK / "frames"
AUDIO = WORK / "audio"
SEGS = WORK / "segments"
DOCS = ROOT / "docs"

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
KOKORO_MODEL = r"E:/AI/models/kokoro/kokoro-v1.0.onnx"
KOKORO_VOICES = r"E:/AI/models/kokoro/voices-v1.0.bin"
VOICE = "af_heart"          # english_coach.py's known-good US-English voice
TTS_SPEED = 1.0             # raise slightly if total would exceed 3:00

REPO_URL = "https://github.com/Theodore-Liu/gated-agent"
ADVREV_URL = REPO_URL + "/blob/main/docs/ADVERSARIAL-REVIEW.md"
DASH_URL = "https://gated-agent-live.streamlit.app"

# pronunciation fixes for the TTS input only — the script file is untouched
RESPELL = {"LLM": "L L M", "MCP": "M C P", "mleg": "em-leg", "QQQ": "Q Q Q"}

LEAD, TAIL = 0.25, 0.35     # silence before/after each beat's narration
SHOT_GAP = 0.5              # extra hold on the last beat of a shot

sys.path.insert(0, str(Path(__file__).resolve().parent))
from video_beats import BEATS, SHOT_TITLES, LIVE, REHEARSAL  # noqa: E402

REHEARSAL_CAP = REHEARSAL
LIVE_CAP = LIVE


# --------------------------------------------------------------------------
# narration: the beat table is the single source of truth
# --------------------------------------------------------------------------
def narration() -> list[str]:
    """Per-shot narration (beats joined) — kept for the slides/notes tools."""
    out = []
    for s in range(1, 6):
        out.append(" ".join(b["text"] for b in BEATS if b["shot"] == s))
    return out


def tts_text(t: str) -> str:
    for k, v in RESPELL.items():
        t = re.sub(rf"\b{re.escape(k)}\b", v, t)
    return t


# --------------------------------------------------------------------------
# stage: text — capture the real terminal text for shots 2/3/4
# --------------------------------------------------------------------------
SHOT3_CODE = (
    "import json;\n"
    "[print(json.dumps(r['report'], indent=1)[:900]) for r in\n"
    " map(json.loads, open('ledger-devtest-20260825-27/decisions.jsonl'))\n"
    " if r.get('kind')=='redteam' and r['report'].get('verdict')=='veto']"
)
SHOT4_CODE = (
    "import json;\n"
    "rows=[json.loads(l) for l in open('ledger/decisions.jsonl')];\n"
    "[print(r['kind'], r.get('symbol',''), (r.get('broker_receipt') or {})"
    ".get('id','')[:8]) for r in rows[-12:]]"
)


def run_readonly(code: str) -> str:
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, timeout=60,
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"capture command failed:\n{r.stderr}")
    return r.stdout


def stage_text() -> None:
    WORK.mkdir(exist_ok=True)
    # shot 2: today's real scheduled live round from logs/daily.log
    log = (ROOT / "logs" / "daily.log").read_text(encoding="utf-8",
                                                  errors="replace")
    blocks = re.split(r"^=====.*$", log, flags=re.M)
    headers = re.findall(r"^=====.*$", log, flags=re.M)
    block = None
    for h, b in zip(headers, blocks[1:]):
        if "2026/08/31" in h:
            block = b.strip()
    assert block and "LIVE paper orders" in block, "08-31 live round not found"
    (WORK / "shot2.txt").write_text(block + "\n", encoding="utf-8")

    # shot 3: the exact veto-dump command from the script (read-only)
    out3 = run_readonly(SHOT3_CODE)
    cmd3 = 'python -c "' + SHOT3_CODE.replace("\n", "\n  ") + '"'
    (WORK / "shot3.txt").write_text(f"$ {cmd3}\n{out3}", encoding="utf-8")
    # zoomed QQQ veto: the full report record, pretty-printed
    qqq = None
    for line in (ROOT / "ledger-devtest-20260825-27" /
                 "decisions.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if (r.get("kind") == "redteam" and r.get("book") == "live"
                and r["report"].get("verdict") == "veto"
                and r["report"].get("symbol") == "QQQ"):
            qqq = r
    assert qqq, "QQQ live veto not found in rehearsal ledger"
    rep = qqq["report"]
    stack_q = next((q for q in rep.get("questions", [])
                    if "book" in q.get("question", "").lower()
                    or "stack" in str(q).lower()
                    or q.get("verdict") == "fail"), None)
    zoom = {"run_date": qqq.get("run_date"), "book": qqq.get("book"),
            "symbol": rep.get("symbol", "QQQ"),
            "verdict": rep["verdict"],
            "veto_reasons": rep["veto_reasons"]}
    if stack_q:
        zoom["failed_question"] = stack_q
    (WORK / "shot3_qqq.txt").write_text(
        json.dumps(zoom, indent=1)[:2200] + "\n", encoding="utf-8")

    # shot 4: the exact ledger-tail command from the script (read-only)
    out4 = run_readonly(SHOT4_CODE)
    cmd4 = 'python -c "' + SHOT4_CODE.replace("\n", "\n  ") + '"'
    (WORK / "shot4.txt").write_text(f"$ {cmd4}\n{out4}", encoding="utf-8")
    # plus one full real order_intent record with its broker receipt
    intent = None
    for line in (ROOT / "ledger" / "decisions.jsonl").read_text(
            encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("kind") == "order_intent" and r.get("broker_receipt"):
            intent = r
    assert intent, "no order_intent with broker_receipt in live ledger"
    intent.pop("cli_commands", None)  # huge; the receipt is the point
    (WORK / "shot4_record.txt").write_text(
        json.dumps(intent, indent=1), encoding="utf-8")

    # ---- beat-level captures: source docstrings and single ledger records
    src = ROOT / "src" / "gated_agent"

    def head(path: Path, a: int, b: int) -> str:
        return "\n".join(path.read_text(encoding="utf-8").splitlines()[a - 1:b])

    (WORK / "close_rules.txt").write_text(
        head(src / "position_manager.py", 1, 14) + "\n", encoding="utf-8")
    (WORK / "gates.txt").write_text(
        head(src / "gates.py", 1, 17) + "\n", encoding="utf-8")
    (WORK / "protocol.txt").write_text(
        head(src / "redteam_mcp.py", 5, 30) + "\n", encoding="utf-8")

    live = [json.loads(l) for l in (ROOT / "ledger" / "decisions.jsonl")
            .read_text(encoding="utf-8").splitlines()]

    def last(pred) -> dict:
        rows = [r for r in live if pred(r)]
        assert rows, "ledger record not found"
        return rows[-1]

    def compact(rec: dict) -> str:
        """indent=1 JSON with the `legs` list on one line per leg — a leg is
        four fields, and five lines each would push the point off screen."""
        rec = dict(rec)
        legs = rec.pop("legs", None)
        txt = json.dumps(rec, indent=1)
        if legs is not None:
            leg_lines = ",\n".join("  " + json.dumps(l) for l in legs)
            txt = txt[:-2] + f',\n "legs": [\n{leg_lines}\n ]\n}}'
        return txt + "\n"

    (WORK / "gate_check.txt").write_text(compact(last(
        lambda r: r.get("kind") == "gate_check" and r.get("book") == "live"
        and not r.get("allowed", True))), encoding="utf-8")
    (WORK / "position_closed.txt").write_text(compact(last(
        lambda r: r.get("kind") == "position_closed"
        and r.get("book") == "live")), encoding="utf-8")
    (WORK / "reconciled.txt").write_text(compact(last(
        lambda r: r.get("kind") == "position_reconciled"
        and r.get("book") == "live")), encoding="utf-8")
    print("text: captures written to", WORK)


# --------------------------------------------------------------------------
# stage: web — headless-Chrome screenshots of the live pages
# --------------------------------------------------------------------------
def chrome_shot(url: str, out: Path, w: int, h: int, dsf: float = 1.5,
                budget_ms: int = 20000) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
           "--force-device-scale-factor=%g" % dsf,
           f"--window-size={w},{h}",
           f"--virtual-time-budget={budget_ms}",
           f"--screenshot={out}", url]
    subprocess.run(cmd, capture_output=True, timeout=180, check=True)
    assert out.exists() and out.stat().st_size > 20000, f"screenshot thin: {out}"


def render_doc_page(md_path: Path, out: Path, title: str = "") -> None:
    """Minimal, deterministic local render of a markdown doc for framing.

    Headings and bold only — enough to read on camera; everything else stays
    as-is in a readable serif column. Single paint, no network, no ghosting.
    """
    import html as _html
    import re as _re
    text = md_path.read_text(encoding="utf-8")
    lines_out = []
    for ln in text.splitlines():
        esc = _html.escape(ln)
        esc = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
        m = _re.match(r"^(#{1,3}) (.*)$", esc)
        if m:
            lvl = len(m.group(1))
            lines_out.append(f"<h{lvl}>{m.group(2)}</h{lvl}>")
        elif esc.strip() == "":
            lines_out.append("<div class='sp'></div>")
        else:
            lines_out.append(f"<p>{esc}</p>")
    page = f"""<!doctype html><meta charset='utf-8'><style>
    body{{background:#ffffff;color:#1f2328;margin:0;padding:48px 72px;
         font:17px/1.55 'Segoe UI',system-ui,sans-serif;max-width:1100px}}
    h1{{font-size:30px;border-bottom:2px solid #d0d7de;padding-bottom:8px}}
    h2{{font-size:24px;margin-top:28px}} h3{{font-size:19px;margin-top:20px}}
    p{{margin:4px 0}} .sp{{height:10px}}
    .bar{{background:#24292f;color:#e6edf3;font:13px Consolas,monospace;
         padding:8px 14px;margin:-48px -72px 28px}}
    </style><div class='bar'>{_html.escape(title)}</div>
    {''.join(lines_out)}"""
    tmp = out.with_suffix(".html")
    tmp.write_text(page, encoding="utf-8")
    chrome_shot(tmp.as_uri(), out, 1280, 3000, budget_ms=15000)


def render_advrev_market(out: Path) -> None:
    """The 09-01 'What the market found' section, rendered on its own page:
    the full review is ~700 lines, far taller than one capture, so the day-3
    post-mortem the narration points at would otherwise never be on screen."""
    md = ROOT / "docs" / "ADVERSARIAL-REVIEW.md"
    text = md.read_text(encoding="utf-8")
    i = text.index("# What the market found")
    tmp = WORK / "advrev_market.md"
    tmp.write_text(text[i:], encoding="utf-8")
    render_doc_page(tmp, out,
                    title="docs/ADVERSARIAL-REVIEW.md — day 3: what the "
                          "market found (2026-09-01)")


def render_mermaid(out: Path) -> None:
    """Render the README's own mermaid block locally (GitHub's iframe does
    not render under headless Chrome — 'Unable to render rich display')."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"```mermaid\n(.*?)```", readme, flags=re.S)
    assert m, "mermaid block not found in README.md"
    html = (
        "<meta charset='utf-8'><style>body{background:#0d1117;margin:0;"
        "display:flex;justify-content:center;padding:24px 0}</style>"
        f"<pre class='mermaid'>\n{escape(m.group(1))}\n</pre>"
        "<script src='https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/"
        "mermaid.min.js'></script>"
        "<script>mermaid.initialize({startOnLoad:true,theme:'dark',"
        "flowchart:{useMaxWidth:false}});</script>")
    src = WORK / "mermaid.html"
    src.write_text(html, encoding="utf-8")
    chrome_shot(src.as_uri(), out, 1600, 2400, dsf=1.0, budget_ms=20000)


def _pw_streamlit_shot(url: str, out: Path, wake: bool,
                       settle_s: int = 18) -> bool:
    """Screenshot a Streamlit page with real wall-clock waits (headless
    Chrome's --virtual-time-budget captures the loading skeleton instead).
    If `wake`, clicks through Streamlit Cloud's 'gone to sleep' page first."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={"width": 1280, "height": 2000},
                         device_scale_factor=1.5)
        try:
            pg.goto(url, timeout=60000, wait_until="domcontentloaded")
            if wake:
                btn = pg.get_by_text("Yes, get this app back up")
                if btn.count():
                    print("web: cloud app asleep — waking it ...")
                    btn.first.click()
                    pg.wait_for_timeout(90000)   # cold boot
            pg.wait_for_timeout(settle_s * 1000)
            body = pg.inner_text("body")
            ok = "$" in body and "sleep" not in body.lower()
            pg.screenshot(path=str(out))
            return ok
        finally:
            br.close()


def capture_dashboard(out: Path) -> None:
    """Judge-facing Streamlit page. Try the cloud URL first (waking it if it
    sleeps — that also leaves it awake for the judges); fall back to the same
    page served locally (same code, same account, real .env). The local
    fallback starts one streamlit process and kills exactly that PID."""
    try:
        if _pw_streamlit_shot(DASH_URL, out, wake=True):
            print("web: dashboard captured from", DASH_URL)
            return
        print("web: cloud dashboard did not render — using local fallback")
    except Exception as e:  # noqa: BLE001
        print(f"web: cloud dashboard failed ({type(e).__name__}) — "
              f"using local fallback")
    import time
    import urllib.request
    py = WORK / "venv-dash" / "Scripts" / "python.exe"
    proc = subprocess.Popen(
        [str(py), "-m", "streamlit", "run",
         str(ROOT / "src" / "gated_agent" / "dashboard.py"),
         "--server.port", "8599", "--server.headless", "true",
         "--browser.gatherUsageStats", "false"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen("http://localhost:8599", timeout=2)
                break
            except Exception:  # noqa: BLE001
                time.sleep(1)
        assert _pw_streamlit_shot("http://localhost:8599", out, wake=False), \
            "local dashboard did not render either"
        print("web: dashboard captured from local streamlit")
    finally:
        proc.kill()  # this PID only — never anything else


def stage_web() -> None:
    shots = WORK / "webshots"
    shots.mkdir(parents=True, exist_ok=True)
    # window 1280 wide at dsf 1.5 -> 1920-px-wide PNG with legible text
    chrome_shot(REPO_URL, shots / "readme_full.png", 1280, 3400,
                budget_ms=30000)
    # ADVERSARIAL-REVIEW is rendered LOCALLY, not screenshotted from the
    # GitHub blob page: GitHub's progressive markdown render double-paints
    # under a headless virtual-time budget, which produced ghosted overlapping
    # text in the first build (QC-rejected frame). Same reasoning as
    # render_mermaid — depend on our own renderer, not their page timing.
    render_doc_page(ROOT / "docs" / "ADVERSARIAL-REVIEW.md",
                    shots / "advrev_full.png",
                    title="docs/ADVERSARIAL-REVIEW.md — the reviews that "
                          "broke the agent before the market could")
    render_advrev_market(shots / "advrev_market.png")
    render_mermaid(shots / "mermaid.png")
    capture_dashboard(shots / "dashboard_full.png")
    print("web: screenshots in", shots)


def crop_slice(src: Path, out: Path, y: int) -> None:
    """One 1920x1080 crop of a tall 1920-wide screenshot at offset y."""
    subprocess.run([FFMPEG, "-y", "-i", str(src),
                    "-vf", f"crop=1920:1080:0:{y}", str(out)],
                   capture_output=True, timeout=60, check=True)


# --------------------------------------------------------------------------
# stage: frames — one 1920x1080 PNG per beat
# --------------------------------------------------------------------------
TERM_CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
body { width:1920px; height:1080px; background:#0b0f14; overflow:hidden;
       font-family:Consolas,'Cascadia Mono',monospace; }
.bar { height:56px; background:#151b23; display:flex; align-items:center;
       padding:0 28px; border-bottom:1px solid #232b36;
       font-family:'Segoe UI',sans-serif; }
.bar .title { color:#8b98a9; font-size:22px; }
.term { padding:26px 44px 0 44px; color:#c9d4e0; font-size:%(fs)dpx;
        line-height:1.42; }
.ln  { white-space:pre-wrap; word-break:break-all; padding:0 14px;
       margin-left:-14px; border-left:5px solid transparent; }
.ln.focus { background:rgba(255,216,102,0.10); border-left-color:#ffd866;
            color:#eef3f8; }
.ln.dimline { opacity:0.36; }
.cap { position:absolute; right:28px; bottom:22px; background:#3a2f10;
       color:#e8c060; border:1px solid #6b551d; border-radius:8px;
       padding:8px 18px; font-size:21px;
       font-family:'Segoe UI',sans-serif; }
.cap.live { background:#0f2a18; color:#6fd08c; border-color:#1e5a35; }
.veto  { color:#ff7b72; font-weight:bold; }
.ok    { color:#7ee787; }
.cmd   { color:#79c0ff; }
.dim   { color:#616e7f; }
.hl    { color:#ffd866; }
"""


def colorize(line: str) -> str:
    e = escape(line)
    if "VETO" in line or '"veto"' in line:
        return f'<span class="veto">{e}</span>'
    if "ORDER INTENT" in line or "would trade" in line:
        return f'<span class="ok">{e}</span>'
    if line.lstrip().startswith(("$", ">")):
        return f'<span class="cmd">{e}</span>'
    if line.lstrip().startswith(("[close]", "[shadow]")) or "hold —" in line:
        return f'<span class="dim">{e}</span>'
    if "veto_reasons" in line or "verdict" in line:
        return f'<span class="hl">{e}</span>'
    return e


def _chrome_png(html: str, out: Path) -> None:
    tmp = out.with_suffix(".html")
    tmp.write_text(html, encoding="utf-8")
    subprocess.run([CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
                    "--window-size=1920,1080", f"--screenshot={out}",
                    tmp.as_uri()], capture_output=True, timeout=120,
                   check=True)


def resolve_focus(src_lines: list[str], focus) -> set[int]:
    """Focus items -> set of 1-based source line numbers."""
    hit: set[int] = set()
    for f in focus or []:
        if isinstance(f, str):
            found = [i for i, l in enumerate(src_lines, 1) if f in l]
            assert found, f"focus marker not found: {f!r}"
            hit.update(found)
        else:
            a, b = f
            hit.update(range(a, b + 1))
    return hit


def term_frame(src_lines: list[str], out: Path, title: str, *,
               window=None, focus=None, caption: str = "",
               cap_class: str = "") -> None:
    """Render a window of `src_lines` (1-based inclusive) with focus lines
    highlighted and everything else dimmed. Font size and wrap width follow
    the number of DISPLAYED lines so every frame fills the screen."""
    import textwrap
    a, b = window or (1, len(src_lines))
    shown = list(range(a, min(b, len(src_lines)) + 1))
    fset = resolve_focus(src_lines, focus)
    assert not focus or (fset & set(shown)), f"focus outside window: {out.name}"
    # largest font whose wrapped rows still fit above the lower third
    # (56 bar + 26 pad + ~110 lower third -> ~880 px of usable height)
    for fs in range(36, 17, -1):
        wrap = int(1800 / (fs * 0.55))
        rows = []
        for i in shown:
            cls = "focus" if i in fset else ("dimline" if fset else "")
            parts = textwrap.wrap(src_lines[i - 1], wrap,
                                  drop_whitespace=False,
                                  subsequent_indent="  ") or [""]
            for part in parts:
                rows.append(f'<div class="ln {cls}">{colorize(part)}</div>')
        if len(rows) * fs * 1.42 <= 900:
            break
    else:
        raise AssertionError(f"{out.name}: {len(rows)} rows overflow at 18px")
    cap = (f'<div class="cap {cap_class}">{escape(caption)}</div>'
           if caption else "")
    html = (f"<meta charset='utf-8'><style>{TERM_CSS % {'fs': fs}}</style>"
            f"<div class='bar'><span class='title'>{escape(title)}</span></div>"
            f"<div class='term'>{''.join(rows)}</div>{cap}")
    _chrome_png(html, out)


def fit_frame(img, out: Path, bg=(11, 15, 20), reserve_bottom=0) -> None:
    """Scale a PIL image to fit 1920x(1080-reserve_bottom), centred in that
    band on dark — `reserve_bottom` keeps the lower third off the picture."""
    from PIL import Image
    avail = 1080 - reserve_bottom
    s = min(1920 / img.width, avail / img.height)
    im = img.resize((round(img.width * s), round(img.height * s)),
                    Image.LANCZOS)
    canvas = Image.new("RGB", (1920, 1080), bg)
    canvas.paste(im, ((1920 - im.width) // 2, (avail - im.height) // 2))
    canvas.save(out)


def card_frame(lines: list[str], out: Path) -> None:
    body = []
    for i, l in enumerate(lines):
        cls = "h" if i == 0 else "u" if "." in l and " " not in l else "q"
        body.append(f"<div class='{cls}'>{escape(l)}</div>" if l
                    else "<div class='sp'></div>")
    html = ("<meta charset='utf-8'><style>*{margin:0;padding:0}"
            "body{width:1920px;height:1080px;background:#0b0f14;display:flex;"
            "flex-direction:column;justify-content:center;align-items:center;"
            "font-family:'Segoe UI',sans-serif;color:#c9d4e0}"
            ".h{font-size:72px;font-weight:600;color:#fff;margin-bottom:18px}"
            ".u{font-size:34px;color:#79c0ff;font-family:Consolas,monospace;"
            "margin:4px 0}.sp{height:44px}"
            ".q{font-size:40px;color:#ffd866;margin:6px 0}</style>"
            + "".join(body))
    _chrome_png(html, out)


def lower_third(png: Path, text: str, box=None) -> None:
    """Post-process a frame in place: optional highlight box (everything else
    dimmed), then the lower-third key phrase. Same look on every frame kind."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(png).convert("RGBA")
    if box:
        x, y, w, h = box
        shade = Image.new("RGBA", im.size, (0, 0, 0, 150))
        cut = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        shade.paste(cut, (x, y))
        im = Image.alpha_composite(im, shade)
        ImageDraw.Draw(im).rectangle([x, y, x + w, y + h],
                                     outline=(255, 216, 102, 255), width=5)
    if text:
        font = ImageFont.truetype(r"C:\Windows\Fonts\segoeuib.ttf", 34)
        d = ImageDraw.Draw(im)
        tw = d.textlength(text, font=font)
        x0, y0 = 44, 1080 - 40 - 66
        bar = Image.new("RGBA", im.size, (0, 0, 0, 0))
        bd = ImageDraw.Draw(bar)
        bd.rounded_rectangle([x0, y0, x0 + tw + 60, y0 + 66], radius=8,
                             fill=(11, 15, 20, 225))
        bd.rectangle([x0, y0, x0 + 8, y0 + 66], fill=(255, 216, 102, 255))
        im = Image.alpha_composite(im, bar)
        ImageDraw.Draw(im).text((x0 + 30, y0 + 13), text, font=font,
                                fill=(245, 247, 250, 255))
    im.convert("RGB").save(png)


def crop_y(src: Path, spec, img_h: int) -> int:
    """Named crop offsets that depend on the captured page's height."""
    if isinstance(spec, int):
        return max(0, min(spec, img_h - 1080))
    if spec == "second":                       # dashboard: positions table
        return max(0, min(1270, img_h - 1080))
    raise ValueError(spec)


def stage_frames() -> None:
    from PIL import Image
    FRAMES.mkdir(parents=True, exist_ok=True)
    for old in FRAMES.glob("*"):        # stale frames must not leak into
        old.unlink()                    # the video on a re-run
    shots = WORK / "webshots"
    mer = None
    for b in BEATS:
        v = b["visual"]
        out = FRAMES / f"{b['id']}.png"
        box = None
        if v["kind"] == "term":
            src_lines = (WORK / v["src"]).read_text(
                encoding="utf-8").splitlines()
            cap = v.get("cap", "")
            term_frame(src_lines, out, v["title"], window=v.get("lines"),
                       focus=v.get("focus"), caption=cap,
                       cap_class="live" if cap == LIVE else "")
        elif v["kind"] == "crop":
            src = shots / v["src"]
            h = Image.open(src).height
            crop_slice(src, out, crop_y(src, v["y"], h))
            box = v.get("box")
        elif v["kind"] == "fit":
            if mer is None:
                mer = Image.open(shots / v["src"])
                bbox = mer.convert("L").point(
                    lambda p: 255 if p > 30 else 0).getbbox()
                mer = mer.crop((0, 0, mer.width,
                                min(bbox[3] + 20, mer.height)))
            hh = mer.height
            part = {"top": (0, int(hh * 0.56)),
                    "bottom": (int(hh * 0.44), hh),
                    "full": (0, hh)}[v["part"]]
            fit_frame(mer.crop((0, part[0], mer.width, part[1])), out,
                      reserve_bottom=120 if b["lt"] else 0)
        elif v["kind"] == "card":
            card_frame(v["lines"], out)
        else:
            raise ValueError(v["kind"])
        lower_third(out, b["lt"], box)
    print("frames:", len(list(FRAMES.glob("*.png"))), "PNGs in", FRAMES)
    # contact sheet for review: 5 columns
    pngs = sorted(FRAMES.glob("b*.png"))
    cols, tw, th = 5, 384, 216
    rows_n = (len(pngs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows_n * th), (0, 0, 0))
    for i, p in enumerate(pngs):
        sheet.paste(Image.open(p).resize((tw, th)),
                    ((i % cols) * tw, (i // cols) * th))
    sheet.save(WORK / "contact_sheet.png")
    print("frames: contact sheet ->", WORK / "contact_sheet.png")


# --------------------------------------------------------------------------
# stage: tts — Kokoro, one WAV per beat
# --------------------------------------------------------------------------
def stage_tts() -> None:
    AUDIO.mkdir(parents=True, exist_ok=True)
    for old in AUDIO.glob("*.wav"):
        old.unlink()
    from kokoro_onnx import Kokoro
    import soundfile as sf
    k = Kokoro(KOKORO_MODEL, KOKORO_VOICES)
    durs = {}
    for b in BEATS:
        samples, sr = k.create(tts_text(b["text"]), voice=VOICE,
                               speed=TTS_SPEED, lang="en-us")
        out = AUDIO / f"{b['id']}.wav"
        sf.write(str(out), samples, sr)
        durs[b["id"]] = round(len(samples) / sr, 2)
    (AUDIO / "durations.json").write_text(json.dumps(durs, indent=1))
    total = sum(durs.values())
    pads = len(BEATS) * (LEAD + TAIL) + 5 * SHOT_GAP
    print(f"tts: {len(durs)} beats, narration {total:.1f}s + pads {pads:.1f}s "
          f"= {total + pads:.1f}s")
    if total + pads > 178:
        print("WARNING: over 3:00 — raise TTS_SPEED or trim beats",
              file=sys.stderr)


# --------------------------------------------------------------------------
# stage: video — one still+voice segment per beat, hard cuts, concat
# --------------------------------------------------------------------------
def wav_dur(p: Path) -> float:
    with wave.open(str(p)) as w:
        return w.getnframes() / w.getframerate()


def build_segment(frame: Path, wav: Path, out: Path, tail: float) -> float:
    total = LEAD + wav_dur(wav) + tail
    norm = ("scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x0b0f14,"
            "setsar=1,fps=30,format=yuv420p")
    fc = (f"[0:v]{norm}[v];"
          f"[1:a]adelay={int(LEAD * 1000)}:all=1,apad,aresample=48000[a]")
    cmd = [FFMPEG, "-y", "-loop", "1", "-t", f"{total + 0.05:.3f}",
           "-i", str(frame), "-i", str(wav), "-filter_complex", fc,
           "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}",
           "-c:v", "libx264", "-preset", "medium", "-crf", "23",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
           "-ar", "48000", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg segment failed:\n{r.stderr[-2000:]}")
    return total


def stage_video() -> None:
    SEGS.mkdir(parents=True, exist_ok=True)
    for old in SEGS.glob("*.mp4"):
        old.unlink()
    total, lst, timeline = 0.0, [], []
    for i, b in enumerate(BEATS):
        last_of_shot = (i + 1 == len(BEATS)
                        or BEATS[i + 1]["shot"] != b["shot"])
        tail = TAIL + (SHOT_GAP if last_of_shot else 0.0)
        seg = SEGS / f"{b['id']}.mp4"
        t = build_segment(FRAMES / f"{b['id']}.png", AUDIO / f"{b['id']}.wav",
                          seg, tail)
        timeline.append({"id": b["id"], "shot": b["shot"],
                         "start": round(total, 2), "dur": round(t, 2)})
        total += t
        lst.append(seg)
    (WORK / "timeline.json").write_text(json.dumps(timeline, indent=1))
    concat = WORK / "concat.txt"
    concat.write_text("".join(f"file '{p.as_posix()}'\n" for p in lst))
    out = DOCS / "video.mp4"
    # video stream copied; audio loudness-normalised ONCE over the whole
    # programme (per-clip loudnorm on 3-second clips pumps between beats)
    r = subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i",
                        str(concat), "-c:v", "copy",
                        "-af", "loudnorm=I=-18:TP=-1.5:LRA=11",
                        "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                        "-movflags", "+faststart", str(out)],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"concat failed:\n{r.stderr[-2000:]}")
    print(f"video: {out} — {len(BEATS)} beats, {total:.1f}s")


# --------------------------------------------------------------------------
# stage: script — regenerate docs/VIDEO-SCRIPT.md from the beat table
# --------------------------------------------------------------------------
def _fmt_t(s: float) -> str:
    return f"{int(s // 60)}:{int(s % 60):02d}"


def stage_script() -> None:
    tl_path = WORK / "timeline.json"
    tl = ({t["id"]: t for t in json.loads(tl_path.read_text())}
          if tl_path.exists() else {})
    out = ["# Demo video — beat sheet and narration (≤3:00)", "",
           "> Generated by `python scripts/make_media.py script` from",
           "> `scripts/video_beats.py` — one narration sentence = one visual.",
           "> Every artifact on screen is real: a source file of this repo, the",
           "> 2026-08-31 live round from `logs/daily.log`, a ledger record, or a",
           "> screenshot of the live README / dashboard / review doc. Nothing is",
           "> mocked for the camera. Narration is read verbatim by Kokoro TTS.",
           ""]
    for s in range(1, 6):
        beats = [b for b in BEATS if b["shot"] == s]
        span = ""
        if tl:
            t0 = tl[beats[0]["id"]]["start"]
            t1 = tl[beats[-1]["id"]]["start"] + tl[beats[-1]["id"]]["dur"]
            span = f" ({_fmt_t(t0)}–{_fmt_t(t1)})"
        out += [f"## Shot {s} — {SHOT_TITLES[s]}{span}", "",
                "| t | narration | on screen | lower third |",
                "|---|---|---|---|"]
        for b in beats:
            v = b["visual"]
            if v["kind"] == "term":
                where = f"`{v['src']}` → **{v['title']}**"
                if v.get("focus"):
                    where += " — highlighted: " + ", ".join(
                        f"`{f}`" if isinstance(f, str)
                        else f"lines {f[0]}–{f[1]}" for f in v["focus"])
            elif v["kind"] == "crop":
                where = f"screenshot `{v['src']}` (crop @ {v['y']})"
            elif v["kind"] == "fit":
                where = f"README mermaid diagram ({v['part']})"
            else:
                where = "closing card"
            t = _fmt_t(tl[b["id"]]["start"]) if tl else ""
            where = where.replace("|", "\\|")
            out.append(f"| {t} | {b['text']} | {where} | {b['lt'] or '—'} |")
        out.append("")
        out += ["> \"" + " ".join(b["text"] for b in beats) + "\"", ""]
    (DOCS / "VIDEO-SCRIPT.md").write_text("\n".join(out), encoding="utf-8")
    print("script:", DOCS / "VIDEO-SCRIPT.md")


# --------------------------------------------------------------------------
# stage: slides — HTML deck from docs/SLIDES.md content -> docs/slides.pdf
# --------------------------------------------------------------------------
def stage_slides() -> None:
    html = build_slides_html()
    src = WORK / "slides.html"
    src.write_text(html, encoding="utf-8")
    out = DOCS / "slides.pdf"
    r = subprocess.run([CHROME, "--headless", "--disable-gpu",
                        "--no-pdf-header-footer",
                        f"--print-to-pdf={out}", src.as_uri()],
                       capture_output=True, text=True, timeout=180)
    assert out.exists(), f"pdf not produced:\n{r.stderr[-1500:]}"
    from pypdf import PdfReader
    n = len(PdfReader(str(out)).pages)
    print(f"slides: {out} — {n} pages")
    assert n == 7, f"expected 7 pages, got {n}"
    # slide-1 layout check PNG (13.333in x 7.5in == 1280x720 css px)
    subprocess.run([CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
                    "--window-size=1280,720", "--force-device-scale-factor=1.5",
                    f"--screenshot={WORK / 'slide1.png'}", src.as_uri()],
                   capture_output=True, timeout=120, check=True)
    print("slides: layout check ->", WORK / "slide1.png")


def build_slides_html() -> str:
    from slides_content import SLIDES_HTML  # noqa
    return SLIDES_HTML


# --------------------------------------------------------------------------
# stage: verify — ffprobe the final video, thumbnails for review
# --------------------------------------------------------------------------
def stage_verify() -> None:
    out = DOCS / "video.mp4"
    r = subprocess.run([FFPROBE, "-v", "quiet", "-print_format", "json",
                        "-show_format", "-show_streams", str(out)],
                       capture_output=True, text=True, timeout=60, check=True)
    info = json.loads(r.stdout)
    dur = float(info["format"]["duration"])
    size = int(info["format"]["size"])
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    print(f"verify: {dur:.1f}s  {v['width']}x{v['height']} "
          f"{v['codec_name']}+{a['codec_name']}  {size / 1e6:.1f} MB")
    assert dur <= 180.5, "video exceeds 3:00"
    assert (v["width"], v["height"]) == (1920, 1080)
    assert size < 50_000_000, "video exceeds 50MB"
    for i, t in enumerate([dur * 0.15, dur * 0.5, dur * 0.85]):
        subprocess.run([FFMPEG, "-y", "-ss", f"{t:.1f}", "-i", str(out),
                        "-frames:v", "1", str(WORK / f"thumb{i}.png")],
                       capture_output=True, timeout=60, check=True)
    print("verify: thumbnails ->", WORK)


STAGES = {"text": stage_text, "web": stage_web, "frames": stage_frames,
          "tts": stage_tts, "video": stage_video, "script": stage_script,
          "slides": stage_slides, "verify": stage_verify}

if __name__ == "__main__":
    which = sys.argv[1:] or list(STAGES)
    for name in which:
        print(f"== {name} ==")
        STAGES[name]()
