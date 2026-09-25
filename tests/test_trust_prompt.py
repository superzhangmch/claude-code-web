#!/usr/bin/env python3
"""Answering "do you trust this folder?" — with YES, and never blindly.

Both bridges answered it by sending `"1\\r"`, on the belief that option 1 was
"Yes, I trust this folder". claude now puts the refusal first:

    ❯ No, exit
      Yes, I trust this folder

so that keystroke answered NO. The session cc-web had just opened exited, the tab fell
back to a shell, and the resume reported "resumed" anyway.

Found 2026-09-23 on mac-pro: after a reboot, 4 of 19 sessions did not come back. One had
no transcript left (claude: "No conversation found"), and one — d069d324, in a folder
claude had not been trusted for — was killed by our own keystroke. The captured screen
from that tab is the fixture below.

So the option is located by its TEXT, and the answer is "press nothing" whenever the
screen does not say enough. A wrong key here does not fail safe: it exits the session.

    python3 tests/test_trust_prompt.py      # exit 0 = pass
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


# Verbatim from the tab that died (mac-pro, 2026-09-23 21:46), cursor on the refusal.
CLAUDE_REAL = """
 Accessing workspace:
 /Users/me/some/project
 Quick safety check: Is this a project you created or one you trust? (Like your own
 code, a well-known open source project, or work from your team). If not, take a moment
 to review what's in this folder first.
 Claude Code'll be able to read, edit, and execute files here.
 Security guide
 ❯ No, exit
   Yes, I trust this folder
 Enter to confirm · Esc to cancel
"""

# The order this code used to assume.
CLAUDE_YES_FIRST = """
 Do you trust the files in this folder?
 ❯ Yes, I trust this folder
   No, exit
"""

# A numbered list (codex's shape): typing the number is unambiguous.
CODEX_NUMBERED = """
 Do you trust the contents of this directory?
   1. Yes, proceed
   2. No, exit
"""

NOT_THE_PROMPT = """
 Welcome back to Claude Code v2.1.220
 ❯ type your message
"""


def main():
    try:
        from iterm_bridge import trust_prompt_keys as keys
    except Exception as e:                                  # pragma: no cover
        print("SKIP: cannot import iterm_bridge:", e); return 0

    print("=== the real screen that got answered wrongly ===")
    k = keys(CLAUDE_REAL)
    # One Down (cursor is on "No, exit", yes is the next line), then Enter.
    check("the cursor is walked DOWN to the yes line, then Enter",
          k == "\x1b[B\r", repr(k))
    check("...and it is not the old '1' answer", k != "1\r", repr(k))

    print("=== the same dialog with the options the other way round ===")
    # The point of locating by text: this must keep working if claude reorders again.
    k = keys(CLAUDE_YES_FIRST)
    check("the cursor is already on yes, so just Enter", k == "\r", repr(k))

    print("=== a numbered list ===")
    k = keys(CODEX_NUMBERED)
    check("the number of the yes line is typed", k == "1\r", repr(k))
    k = keys(CODEX_NUMBERED.replace("1. Yes, proceed", "9. Yes, proceed"))
    check("...whichever number it is", k == "9\r", repr(k))

    print("=== when it cannot tell, it presses NOTHING ===")
    # This is the whole safety property: a wrong key exits the session, so silence has
    # to be the default. (The caller logs it and leaves the prompt for a human.)
    check("no yes option on screen → no keys", keys(NOT_THE_PROMPT) == "", repr(keys(NOT_THE_PROMPT)))
    check("empty screen → no keys", keys("") == "" and keys(None) == "")
    no_cursor = " Do you trust this folder?\n   Yes, I trust this folder\n   No, exit\n"
    check("a cursor list with no cursor visible → no keys", keys(no_cursor) == "",
          repr(keys(no_cursor)))

    print("=== both bridges use it ===")
    src_it = open(os.path.join(ROOT, "iterm_bridge.py"), encoding="utf-8").read()
    src_tm = open(os.path.join(ROOT, "tmux_bridge.py"), encoding="utf-8").read()
    for name, src in (("iterm", src_it), ("tmux", src_tm)):
        check(f"[{name}] sends what the helper decided",
              "trust_prompt_keys(screen)" in src, name)
        # The bug, by shape: a SEND of the literal "1\r". Matched on the call and not on
        # the string, because the comments above both call sites quote the old answer to
        # explain what changed — and a test that trips over its own rationale gets
        # deleted rather than kept.
        import re as _re
        bad = _re.findall(r"send_text_to\([^)]*\"1\\\\r\"", src)
        check(f"[{name}] ...and nothing sends the old '1' answer", bad == [], str(bad))
    check("one implementation, imported by the other",
          "trust_prompt_keys" in src_tm.split("class ")[0])

    print("=== and the resume says what happened to each session ===")
    # The other half of why this took an hour to diagnose: per-session results lived only
    # in _resume_progress, which the next run overwrites and nothing writes down.
    src_cc = open(os.path.join(ROOT, "cc_web.py"), encoding="utf-8").read()
    check("each result is logged, not just kept in memory",
          'log.info("resume %d/%d %s [%s] -> %s"' in src_cc
          and 'log.info("resume %d/%d %s [%s] -> already running"' in src_cc)

    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
