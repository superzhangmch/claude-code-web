#!/usr/bin/env python3
"""The suite gate: the fingerprint, and the stamp that vouches for it.

"Run the full suite before deploying" was a sentence in a notes box, and on
2026-09-04 it did not stop me: the suite was RED, the deploy commands were queued
behind it in one batch, and four hosts got the code. The gate that replaces the
sentence is in two halves — tests/tree_fingerprint.py (what bytes are these?) and
tests/run_all.sh (stamp them when green) — with the refusal itself in the deploy
script, which is machine-local and not in this repo.

This pins the halves that ARE here. A gate whose two sides disagree about what counts
is a gate that opens.

    python3 tests/test_deploy_gate.py      # exit 0 = pass
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FP = os.path.join(ROOT, "tests", "tree_fingerprint.py")
RUNNER = os.path.join(ROOT, "tests", "run_all.sh")

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


def fp_of(root):
    """Runs the copy's OWN script: tree_fingerprint.py takes its root from __file__,
    not from the cwd (correct — the deploy invokes $SRC/tests/tree_fingerprint.py and
    means that tree), so pointing the original at a copy would just re-measure the
    original. Which it did, and these assertions caught it."""
    r = subprocess.run([sys.executable, os.path.join(root, "tests", "tree_fingerprint.py")],
                       capture_output=True, text=True,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    return r.stdout.strip().splitlines()[-1] if r.returncode == 0 else ""


def main():
    print("=== the fingerprint is stable, and it moves when the shipped bytes do ===")
    a, b = fp_of(ROOT), fp_of(ROOT)
    check("same tree, same number", a and a == b, a[:16])
    check("...and it is a sha256", bool(re.fullmatch(r"[0-9a-f]{64}", a or "")), (a or "")[:20])

    # A copy of the tree, so nothing here can disturb the real one.
    tmp = tempfile.mkdtemp(prefix="ccweb-fp-")
    work = os.path.join(tmp, "src")
    shutil.copytree(ROOT, work, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc"))
    base = fp_of(work)
    check("a copy of the tree fingerprints the same", base == a, f"{base[:12]} vs {a[:12]}")

    def touch(rel, text="\n# gate probe\n"):
        p = os.path.join(work, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(text)
        return fp_of(work)

    def restore():
        shutil.rmtree(work); shutil.copytree(ROOT, work, symlinks=True,
                                             ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc"))

    for rel, why in (("cc_web.py", "the server"),
                     ("static/index.html", "the SPA"),
                     ("skills/self-check/selfcheck.py", "a skill's script"),
                     ("tests/test_session_memo.py", "a test")):
        got = touch(rel)
        check(f"one byte into {why} invalidates it ({rel})", got != base, f"{got[:12]}")
        restore()

    # A suite result belongs to the tests that produced it, so a NEW test counts too —
    # otherwise adding a failing test and shipping anyway would pass the gate.
    got = touch("tests/test_zz_probe.py", "import sys\nsys.exit(1)\n")
    check("a new test file counts", got != base, got[:12])
    restore()

    # ...but a gate that makes you re-run twenty tests to fix a typo in prose is a gate
    # you start bypassing. Docs are deliberately out.
    for rel in ("README.md", "notes/ai-se-control.md", "skills/README.md"):
        got = touch(rel, "\n<!-- gate probe -->\n")
        check(f"prose does not ({rel})", got == base, got[:12])
        restore()

    print("=== the stamp is written only when the suite is green ===")
    src = open(RUNNER, encoding="utf-8").read()
    check("the runner writes a stamp", ".suite-stamp" in src)
    stamp_block = src[src.index("STAMP="):]
    check("...guarded by the failure flag", re.search(r"if \[ \$fail -eq 0 \]", stamp_block) is not None)
    check("...and it stores the FINGERPRINT, not just the commit",
          "tree_fingerprint.py" in stamp_block and '"fingerprint"' in stamp_block)
    # This deploys the working tree, uncommitted changes included, so a commit hash
    # says nothing about what actually ships. The stamp records what does.
    check("...plus the head and whether the tree was dirty, for reading later",
          '"head"' in stamp_block and '"dirty"' in stamp_block)
    # A red run must REMOVE it: a stale green stamp vouching for a red tree is worse
    # than no gate, because it looks like a gate.
    check("a red run deletes it rather than leaving yesterday's green",
          re.search(r"else\s*\n\s*rm -f \"\$STAMP\"", stamp_block) is not None,
          stamp_block[stamp_block.find("else"):][:40].replace("\n", " "))
    check("the stamp is not committed (it is state about one working tree)",
          ".suite-stamp" in open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read())

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
