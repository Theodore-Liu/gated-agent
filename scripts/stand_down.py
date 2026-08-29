"""Disarm the agent after the 2026-09-04 submission. Run by a human, on purpose.

Two stages, because opens and closes must NOT stand down together:

    python scripts/stand_down.py            # stage 1: no new opens
    python scripts/stand_down.py all        # stage 2: closes too (book flat)

Stage 1 (after the 9/4 submission, or the evening of 9/3 to freeze the book
before the judging snapshot): `run_daily.cmd` goes back to `--dry-run` with
the live switch re-commented. `run_close_check.cmd` is left ARMED on purpose —
the account still holds spreads expiring 9/11, and R1 (DTE <= 2) must be able
to really close them around 9/9. An agent that cannot open but can close is
the safe shape; the reverse is not.

Stage 2 (after the account shows ZERO open positions — check with
`scripts/verify_account_swap.py` or the dashboard): `run_close_check.cmd`
goes back to dry-run too. Optionally disable both tasks afterwards
(disable, not delete — the registration and its StartWhenAvailable setting
survive for a possible later run):

    schtasks /Change /TN GatedAgentDaily /DISABLE
    schtasks /Change /TN GatedAgentCloseCheck /DISABLE

This script edits the two payload .cmd files in place with anchored, verified
replacements and prints exactly what changed. It never touches the machine
environment, the ledger, or any order. Commit the result so git history
records when — and that — the agent stood down.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

ENV_LINE = re.compile(r'^set "ALPACA_HACKATHON_LIVE=1"', re.MULTILINE)


def disarm(name: str, live_arg: str, safe_arg: str) -> bool:
    p = SCRIPTS / name
    # Bytes on purpose: text mode would quietly rewrite CRLF as LF, and a
    # .cmd file's line endings are load-bearing for cmd.exe.
    text = orig = p.read_bytes().decode("ascii")    # payloads are pure ASCII
    text = text.replace(live_arg, safe_arg)
    text = ENV_LINE.sub('rem set "ALPACA_HACKATHON_LIVE=1"', text)
    if text == orig:
        print(f"  {name}: already disarmed - nothing to do")
        return False
    if live_arg in text or ENV_LINE.search(text):
        raise SystemExit(f"  {name}: replacement did NOT take - refusing to "
                         f"write a half-disarmed payload. Edit it by hand.")
    p.write_bytes(text.encode("ascii"))
    print(f"  {name}: DISARMED (dry-run, live switch commented)")
    return True


def main() -> int:
    stage = (sys.argv[1] if len(sys.argv) > 1 else "opens").lower()
    if stage not in ("opens", "all"):
        print(__doc__)
        return 2
    print(f"=== gated-agent stand-down: stage {stage!r} ===")
    disarm("run_daily.cmd", "gated_agent.run --live", "gated_agent.run --dry-run")
    if stage == "all":
        disarm("run_close_check.cmd", "gated_agent.position_manager --live",
               "gated_agent.position_manager")
        print("\nStage 2 done. If the book is flat, optionally also:\n"
              "  schtasks /Change /TN GatedAgentDaily /DISABLE\n"
              "  schtasks /Change /TN GatedAgentCloseCheck /DISABLE")
    else:
        print("  run_close_check.cmd: left ARMED on purpose (stage 1) - the "
              "book\n    still needs R1 to close the 9/11 spreads for real "
              "around 9/9.\n\nStage 1 done. After the account shows ZERO open "
              "positions - expected\naround 9/9 once R1 has closed the 9/11 "
              "spreads - run:\n  python scripts/stand_down.py all")
    print("\nCommit the change so the stand-down is on the record:\n"
          "  git add scripts/run_daily.cmd scripts/run_close_check.cmd\n"
          '  git commit -m "ops: stand down after the 9/4 submission"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
