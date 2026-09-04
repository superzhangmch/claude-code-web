#!/usr/bin/env python3
"""One number for "these exact bytes".

Prints a sha256 over every source file that decides what the suite is testing and what
a deploy would ship. `tests/run_all.sh` stamps it on a green run; the deploy refuses to
ship a tree whose fingerprint has no stamp. That turns "run the suite before deploying"
from a sentence in a notes box into something that can actually stop you — which is the
whole point, because on 2026-09-04 it did not stop me: the suite was red, the commands
were queued behind it, and four hosts got the code anyway.

Both sides compute it HERE rather than each having its own idea of what counts, because
a gate whose two halves disagree is a gate that opens.

    tests/tree_fingerprint.py            # the hash
    tests/tree_fingerprint.py --list     # what went into it, with per-file hashes

Covers code, the SPA, the skills' scripts, and the tests themselves — a suite result
belongs to the tests that produced it, so editing a test invalidates the stamp on
purpose. Deliberately NOT docs/notes/README: a gate that makes you re-run 20 tests to
fix a typo in a comment is a gate you will start bypassing.
"""
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PATTERNS = (
    "*.py",                       # cc_web.py and the bridges/helpers beside it
    "static/index.html",
    "static/*.webmanifest",
    "static/vendor/*.js",
    "skills/**/*.py",
    "skills/**/*.sh",
    "skills/**/ccsid",
    "skills/**/codexsid",
    "tests/*.py",
    "tests/*.sh",
)
SKIP_PARTS = {"__pycache__", ".venv", "node_modules", ".git"}


def files():
    seen = set()
    for pat in PATTERNS:
        for p in sorted(ROOT.glob(pat)):
            if not p.is_file() or set(p.parts) & SKIP_PARTS:
                continue
            rel = p.relative_to(ROOT).as_posix()
            if rel not in seen:
                seen.add(rel)
                yield rel, p


def fingerprint():
    h = hashlib.sha256()
    out = []
    for rel, p in files():
        d = hashlib.sha256(p.read_bytes()).hexdigest()
        h.update(rel.encode() + b"\0" + d.encode() + b"\0")
        out.append((rel, d))
    return h.hexdigest(), out


def main() -> int:
    fp, listing = fingerprint()
    if "--list" in sys.argv:
        for rel, d in listing:
            print(f"{d[:12]}  {rel}")
        print(f"\n{len(listing)} files")
    print(fp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
