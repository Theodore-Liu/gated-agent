"""Snapshot the competition ledger somewhere that actually has copies.

`ledger/` is gitignored, and correctly so: this repository is public and the
ledger carries account-specific detail. But gitignored on this machine means
zero backup -- the git mirror chain only carries what git carries -- and on
2026-08-28 that cost the rehearsal ledger: 166 decisions and 34 closes, deleted
during the account swap with no archive, no git history, and no way back
because the old account's keys had already been overwritten in place.

The competition ledger is now accumulating the evidence a judge will actually
be shown, in a file with exactly the same exposure. So it gets copied, on every
run, to a private location that the existing backup chain already covers.

Not a substitute for the broker: Alpaca keeps the fills. What only lives here is
the reasoning -- gate checks, red-team vetoes, the decision trail -- which is
the part of the submission that cannot be reconstructed from an account
statement.

    python scripts/snapshot_ledger.py            # copy if changed
    python scripts/snapshot_ledger.py --verify   # report only, no writes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "ledger"

# Destination chosen by reading engine_backup.py rather than by guessing.
# `_trading_data_dirs()` enumerates every subdirectory of trading-agent/data/
# by default and excludes only an explicit list plus any name starting with a
# dot -- so this path is picked up automatically and rides the existing
# NAS -> restic chain. The first draft of this script wrote to
# `trading-agent/.hackathon-ledger-backup`, which that function would have
# skipped twice over: wrong level, and a leading dot. A backup that is not
# backed up is worse than none, because it stops anyone looking further.
DEST = Path.home() / "trading-agent" / "data" / "hackathon-ledger"

FILES = ["decisions.jsonl", "close_log.jsonl", ".position_state.json"]

# The 2026-08-25..27 rehearsal ledger is not regenerable: the account's fills
# still exist at the broker, but the gate checks, the stand-asides and the 24
# red-team records only ever lived here, and they are what the submission's
# process narrative is built on. It is a sibling of ledger/ (so not caught by
# the `ledger/` gitignore rule) but it is untracked, which on this machine
# means zero copies.
EXTRA_DIRS = [REPO / "ledger-devtest-20260825-27"]


def digest(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verify", action="store_true",
                    help="report what would happen; write nothing")
    args = ap.parse_args()

    if not LEDGER.is_dir():
        print("FAIL  no ledger/ directory")
        return 1
    DEST.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest = {"snapshot_utc": stamp, "files": {}}
    copied = 0

    for name in FILES:
        src = LEDGER / name
        if not src.exists():
            print(f"skip  {name}: not present")
            continue
        d = digest(src)
        size = src.stat().st_size
        manifest["files"][name] = {"sha256": d, "bytes": size}

        # Keep the newest copy under a stable name, plus a dated one so a
        # truncation cannot quietly overwrite the only good version. The
        # rehearsal ledger was lost to exactly that shape of accident.
        latest = DEST / name
        unchanged = latest.exists() and digest(latest) == d
        print(f"{'same ' if unchanged else 'COPY '} {name:22} {size:>9,} bytes  {d[:12]}")
        if unchanged or args.verify:
            continue
        shutil.copy2(src, latest)
        shutil.copy2(src, DEST / f"{stamp}-{name}")
        copied += 1

    # Irreplaceable one-off archives: copied whole, once, and left alone after.
    for src_dir in EXTRA_DIRS:
        if not src_dir.is_dir():
            continue
        out = DEST / src_dir.name
        for src in sorted(src_dir.iterdir()):
            if not src.is_file():
                continue
            dst = out / src.name
            same = dst.exists() and digest(dst) == digest(src)
            print(f"{'same ' if same else 'COPY '} {src_dir.name}/{src.name:22} "
                  f"{src.stat().st_size:>9,} bytes")
            if same or args.verify:
                continue
            out.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1

    if not args.verify:
        (DEST / "MANIFEST.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"\n{'would copy' if args.verify else 'copied'}: {copied} file(s) -> {DEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
