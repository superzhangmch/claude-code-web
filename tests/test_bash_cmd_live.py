#!/usr/bin/env python3
"""`!cmd` as the request: the terminal scrolls, and the web must not look asleep.

Typing `!some command` runs it inside claude's own TUI. Two things follow from how that
is recorded, both measured in a real transcript:

  * its two jsonl records are written TOGETHER, when the command FINISHES
    (<bash-input> at …35.741Z, <bash-stdout> at …35.788Z — 47ms apart, with the output
    already in hand). So for the whole run the newest entry is still the previous turn's
    `end_turn`, and "is it idle" answers yes — while output scrolls past.
  * once they land, the newest entry is a plain type=user line. _is_claude_idle walked
    back, hit it, and said "a user message with no answer" → the session read BUSY from
    the moment the command returned until claude's next real turn.

Two fixes, and neither one reads the TUI's own words ("esc to interrupt" is chrome, and
chrome changes between releases — "我们是不需要猜的. 太容易变了. 只需要假设 jsonl 结构稳定"):

  * the idle test skips command bookkeeping, using the same discriminator that already
    labels those lines System instead of You.
  * the live view keeps fetching for 30s after WE send something, renewed by bytes
    moving on screen. That window is the only thing covering a command's RUN, because
    during it the file says nothing at all.

    python3 tests/test_bash_cmd_live.py      # exit 0 = pass
"""
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "static", "index.html")
sys.path.insert(0, ROOT)

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


def main():
    home = tempfile.mkdtemp(prefix="ccweb-bashcmd-")
    os.makedirs(os.path.join(home, ".claude"))
    os.environ["HOME"] = home
    os.environ["CC_WEB_TOKEN"] = "t"
    try:
        import cc_web
    except Exception as e:                                  # pragma: no cover
        print("SKIP: cannot import cc_web:", e); return 0

    def asst(stop="end_turn", text="done"):
        return {"type": "assistant", "message": {"stop_reason": stop,
                                                 "content": [{"type": "text", "text": text}]}}

    def user(text):
        return {"type": "user", "message": {"content": text}}

    print("=== before, during and after a `!cmd` ===")
    base = [user("跑一下那个脚本"), asst()]
    check("after a normal turn: idle", cc_web._is_claude_idle(base) is True)
    # DURING: nothing has been written, so this is still the state the server sees.
    check("...and during the command it is STILL idle, because the file has not moved",
          cc_web._is_claude_idle(base) is True,
          "the 30s window in the client is what covers this, not the jsonl")
    # AFTER: both records land together.
    after = base + [user("<bash-input>ls -la</bash-input>"),
                    user("<bash-stdout>total 8\\ndrwx…</bash-stdout><bash-stderr></bash-stderr>")]
    check("once the command returns, the session is idle again — not stuck busy",
          cc_web._is_claude_idle(after) is True, str(cc_web._is_claude_idle(after)))

    print("=== and a real request still reads as busy ===")
    # The whole point of the idle test. If this ever flips, the page stops showing that
    # claude is working at all.
    check("a typed prompt with no answer yet: busy",
          cc_web._is_claude_idle(base + [user("再跑一次")]) is False)
    check("...a prompt after a `!cmd` too",
          cc_web._is_claude_idle(after + [user("再跑一次")]) is False)
    check("a turn that stopped on a tool call is busy",
          cc_web._is_claude_idle([user("x"), asst(stop="tool_use", text="")]) is False)
    for noise in ("<command-name>/rename</command-name>",
                  "<local-command-stdout>ok</local-command-stdout>",
                  "<task-notification>done</task-notification>"):
        check(f"...and {noise[:18]}… is bookkeeping, not a question",
              cc_web._is_claude_idle(base + [user(noise)]) is True, noise[:40])

    print("=== the client watches for 30s after WE send ===")
    src = open(INDEX, encoding="utf-8").read()
    blk = src[src.index("const HB_GRACE_MS"):][:900]
    check("the window is 30s", "const HB_GRACE_MS = 30000;" in src)
    check("...it restarts the ticker that idle switched off", "syncHeartbeat();" in blk)
    check("...and fetches immediately instead of serving out a backoff",
          "hbNextAt = 0" in blk)
    tick = src[src.index("function hbTick()"):][:1400]
    check("an idle session inside the window keeps fetching",
          "if (claudeIdle && hbInGrace())" in tick
          and tick.index("if (claudeIdle && hbInGrace())") < tick.index("hbFrozen = true"),
          "the grace branch must come BEFORE the freeze")
    fetch = src[src.index("async function hbFetch"):][:3000]
    check("a significant change renews the window",
          "d.changed.length >= POLL_SIGNIF_LINES || d.scroll" in fetch)
    # A spinner is one line a second. Renewing on that would keep every session polling
    # for ever, which is the cost this whole mechanism has to stay under.
    check("...and a 1-2 line tick does NOT (that is a spinner, not output)",
          "POLL_SIGNIF_LINES = 3" in src and "d.changed.length >= POLL_SIGNIF_LINES" in fetch)
    # …and the other edge the window covers: a turn that just ENDED. The screen keeps
    # moving after end_turn (a hook, a background shell, an auto-compact), and none of
    # that reaches the jsonl either. Edge-triggered per session, so merely opening an
    # idle session does not start one.
    poll = src[src.index("claudeIdle = m.claude_idle"):][:900]
    check("going busy → idle starts a watch window too",
          "if (_prevIdle === false && claudeIdle) pokeLive();" in poll)
    check("...keyed per session, and unknown until it has answered once",
          "_prevIdleSid !== sid" in poll and "let _prevIdle = null" in src)

    sends = [m for m in re.findall(r"pokeLive\(\)", src)]
    check("every path that sends something pokes it, plus the idle edge",
          len(sends) >= 5, f"{len(sends)} call sites")
    for fn, why in (("async function sendRaw", "a key / Esc / ⌃C"),
                    ("async function send()", "a typed message"),
                    ("async function postQuickReply", "answering a TUI prompt")):
        i = src.index(fn)
        check(f"...including {why}", "pokeLive()" in src[i:i + 3000], fn)

    print("=== nothing here reads the TUI's own words ===")
    # The rule for this fix: jsonl structure, and bytes moving. Not strings the TUI
    # happens to print today.
    grace = src[src.index("const HB_GRACE_MS"):src.index("function hbTick()")]
    check("the window logic matches no chrome text",
          not re.search(r"esc to interrupt|Working \(|auto mode", grace))

    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
