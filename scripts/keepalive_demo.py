"""Keep the judge-facing Streamlit demo awake, and wake it if it has dozed off.

Community Cloud hibernates an app after ~12h without a *viewer session*
(a browser that ran the app's JS and held the websocket). The previous
keepalive (headless ``chrome.exe --screenshot``) snapshotted the page before
any JS ran -- 152 hourly runs, every screenshot a blank white 2.7 KB PNG,
and the app was found asleep on 09-03 evening anyway. A page hit is not a
view.

This one drives a real browser through Playwright (already in the venv for
make_media.py), waits for the app's own markup to render, holds the session
for a few seconds, and -- if it lands on the "Zzzz" sleep page -- clicks the
wake-up button and waits for the app to come back. It exits non-zero unless
the dashboard actually rendered, so the scheduled task's last-run code is
meaningful. Pure read-only against the demo; nothing touches the broker.

Usage: python scripts/keepalive_demo.py [--url URL] [--hold SECONDS] [--png PATH]
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

URL = "https://gated-agent-live.streamlit.app/"
# Text that only the rendered dashboard contains (from app.py's title).
APP_MARKER = "Gated Agent"
SLEEP_MARKER = "gone to sleep"
WAKE_BUTTON = "get this app back up"


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=URL)
    ap.add_argument("--hold", type=float, default=8.0, help="seconds to keep the session open once rendered")
    ap.add_argument("--png", default=None, help="optional screenshot path (evidence for the log)")
    ap.add_argument("--render-timeout", type=float, default=120.0, help="seconds to wait for the app to render")
    a = ap.parse_args()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1200, "height": 800})
        try:
            page.goto(a.url, wait_until="domcontentloaded", timeout=60_000)
            body = page.locator("body")
            # Give the page a moment to show either the app shell or the sleep screen.
            page.wait_for_timeout(3_000)
            text = body.inner_text()
            woke = False
            if SLEEP_MARKER in text:
                log("app was ASLEEP -- clicking wake-up")
                page.get_by_role("button", name=WAKE_BUTTON, exact=False).first.click()
                woke = True
            # The app may render in the top document or (after a wake-up, and on
            # the Community Cloud wrapper) inside an iframe -- poll every frame.
            deadline = time.time() + a.render_timeout
            rendered = False
            while time.time() < deadline and not rendered:
                for fr in page.frames:
                    try:
                        if fr.get_by_text(APP_MARKER, exact=False).first.is_visible():
                            rendered = True
                            break
                    except Exception:
                        continue
                if not rendered:
                    page.wait_for_timeout(2_000)
            if not rendered:
                snippet = " || ".join(
                    fr.locator("body").inner_text()[:120].replace("\n", " | ")
                    for fr in page.frames if fr.url)
                log(f"FAIL: app did not render within {a.render_timeout:.0f}s; frames say: {snippet}")
                if a.png:
                    page.screenshot(path=a.png)
                return 2
            # Hold the websocket session open so the platform counts a view.
            page.wait_for_timeout(int(a.hold * 1000))
            if a.png:
                page.screenshot(path=a.png)
            log(f"ok: rendered{' after wake' if woke else ''}; session held {a.hold:.0f}s")
            return 0
        finally:
            browser.close()


if __name__ == "__main__":
    t0 = time.time()
    rc = main()
    log(f"exit {rc} ({time.time() - t0:.1f}s)")
    sys.exit(rc)
