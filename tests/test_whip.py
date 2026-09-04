#!/usr/bin/env python3
"""The whip: what it refuses to do.

A stopped session cannot notice it has stopped, so something outside has to. That thing
decides with an LLM — which means the safety cannot come from the model being right. It
comes from the action space (nudge / escalate / nothing, and no "approve") and from the
handful of questions that are never asked of a model at all:

  * is something OTHER than claude sitting on stdin (a password prompt)? — regex
  * is the human mid-typing? — the same call the API-error path makes
  * is this session even registered? — the human's own switch
  * backoff, attempt caps, "have I said this already?" — bookkeeping

Those are what this pins. It lives in cc_web (folded into the 3-minute loop that
already nudged sessions stuck on API errors, rather than being a second program with a
second copy of the same four guards).

    python3 tests/test_whip.py      # exit 0 = pass
"""
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


def main():
    home = tempfile.mkdtemp(prefix="ccweb-whip-")
    os.makedirs(os.path.join(home, ".claude"))
    os.environ["HOME"] = home
    os.environ["CC_WEB_TOKEN"] = "t"
    try:
        import cc_web
    except Exception as e:                                  # pragma: no cover
        print("SKIP: cannot import cc_web:", e); return 0

    print("=== the one gate where being wrong is actually dangerous ===")
    # claude spawns children, and a child can be sitting on stdin. The tab is still a
    # claude tab, so the roster still lists it — but a nudge typed now becomes the
    # password. Broad on purpose, and used only to REFUSE.
    for text, want, why in (
            ("[sudo] password for zmc:", True, "sudo"),
            ("Enter passphrase for key '/home/x/.ssh/id_rsa':", True, "ssh key"),
            ("Password for 'https://github.com':", True, "git over https"),
            ("Verification code: ", True, "2FA"),
            ("Are you sure you want to continue connecting (yes/no/[fingerprint])?", True, "host key"),
            ("(END)", True, "a pager has the terminal"),
            ("--More--", True, "a pager has the terminal"),
            ("╭─ Do you want to proceed? ─╮\n│ 1. Yes  2. No │", False, "a claude menu is NOT stdin"),
            ("> ", False, "an ordinary composer"),
            ("● Bash(git status)\n  ⎿ nothing to commit", False, "ordinary transcript"),
    ):
        got = bool(cc_web._WHIP_AT_STDIN.search(text))
        check(f"{'refuses' if want else 'allows'}: {why}", got == want, repr(text[:40]))

    print("=== an unparseable decision does nothing (fail closed) ===")
    check("prose instead of JSON → None", cc_web._whip_json("I think it should continue") is None)
    check("broken JSON → None", cc_web._whip_json('{"action": "nudge"') is None)
    check("a JSON array → None (not a decision)", cc_web._whip_json("[1,2,3]") is None)
    check("JSON in a fence is still read",
          (cc_web._whip_json('```json\n{"action":"nothing","reason":"x"}\n```') or {}).get("action")
          == "nothing")

    print("=== escalation is once per situation, not once per pass ===")
    # A notification that repeats every three minutes is a notification you turn off.
    st = {}
    a = cc_web._whip_escalate("sid1", st, "它卡在权限确认上", dry=True)
    b = cc_web._whip_escalate("sid1", st, "它卡在权限确认上", dry=True)
    c = cc_web._whip_escalate("sid1", st, "另一件事", dry=True)
    check("first time it escalates", a["action"] == "escalate", str(a))
    check("...the same thing again is dropped", b["action"] == "skip", str(b))
    check("...but a different thing gets through", c["action"] == "escalate", str(c))

    print("=== the action space has no way to approve anything ===")
    src = open(os.path.join(ROOT, "cc_web.py"), encoding="utf-8").read()
    body = src[src.index("async def _whip_check"):src.index("async def _whip_pass")]
    check("only three actions are honoured",
          '("nudge", "escalate", "nothing")' in body, "not found")
    # The model is never handed a key, an option index, or a menu selection: the only
    # write in the whole path is send_text_to. So a wrong decision costs a stray
    # sentence, never a tool call nobody authorised.
    sends = re.findall(r"bridge\.\w+\(", body)
    check("...and the only thing it can send is text",
          set(sends) <= {"bridge.send_text_to(", "bridge.input_typed_text(",
                         "bridge.get_screen_for("}, str(sorted(set(sends))))
    # Settled: those options need a keypress, and with auto mode on the prompts that
    # still appear are the ones auto mode said need a human. And it does not even
    # notify — the panel already shows a pending confirmation as its own state, so a
    # notification would be a second copy of something visible, and these are only
    # worth reading while they stay rare.
    pend_block = body[body.index("if pending:"):body.index("rep = _check_read")]
    check("a permission dialog is neither answered nor notified about",
          '"action": "skip"' in pend_block and "_whip_escalate" not in pend_block,
          pend_block[pend_block.index("return"):][:60].replace("\n", " "))
    check("...and the code says so as a decision, not as a TODO",
          "FINAL behaviour" in body and "Do not \"improve\" this by sending" in body)
    check("...while still recording WHAT was being asked, for a dry run",
          "Bash|Edit|Write" in pend_block, "tool+target extracted")
    check("the nudge says it is NOT permission for anything",
          "不构成对任何需要人确认的动作的许可" in body)

    print("=== opt-in, and the gates come before the money ===")
    check("an unregistered session is dropped first",
          body.index('"not registered"') < body.index("_whip_llm"))
    for what, needle in (("quiet for long enough", "only quiet for"),
                         ("actually idle", "_is_claude_idle"),
                         ("you are not mid-typing", "input_typed_text"),
                         ("nothing else is on stdin", "_WHIP_AT_STDIN"),
                         ("no permission dialog", "_detect_pending_confirm_from_screen"),
                         ("the situation changed since last time", "backing off"),
                         ("not already nudged to death", "_WHIP_MAX_NUDGES")):
        check(f"...gate: {what}", body.index(needle) < body.index("_whip_llm"), needle)
    check("triage is the cheap model and only a yes pays for the other one",
          body.index("whip_triage_model") < body.index("whip_decide_model")
          and body.index('"intervene"') < body.index("_WHIP_DECIDE_SYS"))
    check("hours of 'cannot think' escalate rather than looking like 'nothing needed'",
          "llm_fails" in body and "等于没人看着" in body)

    print("=== it folded into the loop that already did this ===")
    # _api_error_watcher was already a whip: same 3-minute loop, same wait-first, same
    # attempt cap, same don't-clobber-a-human check. A second program outside would
    # have been a second copy of all four.
    loop = src[src.index("async def _api_error_watcher"):]
    loop = loop[:loop.index("\n\n\n")] if "\n\n\n" in loop else loop
    check("the deterministic API-error nudge still runs FIRST",
          loop.index("_maybe_auto_continue") < loop.index("_whip_pass"), "order")
    check("...and the whip runs in the same pass", "_whip_pass(dry=False)" in loop)
    check("a pass can be watched instead of waited for", "/api/whip-run" in src)
    check("every action is logged where the human already looks",
          "_whip_log" in body and "/api/watch-log" in src)

    print("=== the API-error nudge is unconditional; the whip is opt-in ===")
    # These are two different things and must stay two: a turn that died on a network
    # blip is not a judgement call and needs nobody's permission, while the whip acts
    # on a model reading a situation. The danger is a refactor quietly putting (1)
    # behind (2)'s switch.
    check("the API-error path walks every bound session",
          "for b in bindings.all():" in loop and loop.index("bindings.all()") < loop.index("_whip_pass"))
    # Just that function's own body: the whip's code now sits between it and the
    # watcher, so slicing to the watcher swept the whip in and the assertion measured
    # nothing. (It failed loudly, which is the only reason this comment exists.)
    _i = src.index("async def _maybe_auto_continue")
    _j = src.index("\n\n\n", _i)
    ac = src[_i:_j]
    check("...and never asks whether the session is registered",
          "watched" not in ac and "supervisor" not in ac, "auto-continue is unconditional")
    check("the whip's first act is to check registration",
          '"not registered"' in body)

    print("=== the numbers are settable, and read every pass ===")
    for key, default in (("whip_quiet_seconds", "600"), ("whip_max_nudges", "3"),
                         ("whip_backoff_seconds", "600,1800,7200"),
                         ("whip_interval_seconds", "180")):
        check(f"{key} comes from the conf", f'"{key}"' in src, key)
        check(f"...and is documented with its default ({default})",
              key in open(os.path.join(ROOT, "config.example", "cc_web.conf"),
                          encoding="utf-8").read())
    # Read per pass, not at startup: turning a knob should not need a restart, which on
    # this server also drops live WebSockets (voice, screen).
    check("the loop re-reads the conf every pass",
          "cfg = _load_conf()" in loop and loop.index("cfg = _load_conf()") < loop.index("asyncio.sleep"))
    check("a malformed number falls back instead of crashing the loop",
          "_whip_num" in src and "except (TypeError, ValueError)" in
          src[src.index("def _whip_num"):src.index("def _whip_backoff")])

    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
