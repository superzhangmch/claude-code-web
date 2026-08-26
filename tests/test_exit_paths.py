#!/usr/bin/env python3
"""Ending a tab: one server path, four UI triggers, and the claude/plain-shell split.

Four things in the UI end a tab — typing `/exit` in the composer, the ⚙ menu's
⏏, the tab list's ⏏, a picker card's Close — and they must all mean the same
thing. They didn't: the tab list's key-row button was a generic
`data-cmd="exit"`, i.e. it typed the six characters `exit` at whatever tab was
selected. In a claude tab that text goes into claude's COMPOSER as a prompt (and
the row that closes tabs then closes nothing while claude answers a question
about exiting); in a plain shell the old `/exit` was command-not-found. So the
distinction is real in both directions, and it is not "do we have a session id" —
a tab can be running claude while unbound, having never been attached.

What is pinned:

  1. a bound claude tab: detach -> `/exit` -> job count -> `exit`;
  2. a plain shell tab: NO `/exit`, but still job-counted before `exit`;
  3. an UNBOUND claude tab (no sid, send_exit=true) still gets its `/exit` — the
     case that infers wrongly from the session id;
  4. background jobs still veto the close, on both kinds of tab;
  5. all four UI triggers go through the one shared client function, and the tab
     list's exit no longer types `exit` as text;
  6. `/exit` typed in the composer is intercepted only when it's the WHOLE
     message, and lands you back on the main page.

    python3 tests/test_exit_paths.py       # exit 0 = pass
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


class StubBridge:
    """Records what got typed at the tab, and answers the job-count probe."""

    def __init__(self, njobs=0):
        self.sent = []
        self.njobs = njobs

    async def ensure_connected(self):
        pass

    async def send_text_to(self, iterm_id, text):
        self.sent.append((iterm_id, text))

    async def get_screen_for(self, iterm_id, **kw):
        # Echo of the command line, then its output — the sentinel must match only
        # the output line (that's why the real probe uses `=$(...)` vs `=<digits>`).
        return (f"$ echo CCWEB_JOBS=$(jobs | wc -l)\n"
                f"CCWEB_JOBS={self.njobs}\n$ ")


async def main():
    os.environ.setdefault("CC_WEB_TOKEN", "t")
    import cc_web

    P = cc_web.CloseTabPayload

    async def close(bridge, **kw):
        real = cc_web.bridge
        cc_web.bridge = bridge
        try:
            return await cc_web.post_close_tab(P(**kw))
        finally:
            cc_web.bridge = real

    def typed(b):
        return [t for _, t in b.sent]

    print("=== a bound claude tab: /exit first, then the shell's exit ===")
    b = StubBridge()
    detached = []
    real_remove = cc_web.bindings.remove_session
    cc_web.bindings.remove_session = lambda s: detached.append(s)
    try:
        r = await close(b, claude_session_id="sid-1", iterm_session_id="x1")
    finally:
        cc_web.bindings.remove_session = real_remove
    check("the binding is dropped", detached == ["sid-1"], str(detached))
    check("claude got /exit", typed(b)[0] == "/exit\r", str(typed(b)[:1]))
    check("...before the job probe", "CCWEB_JOBS" in typed(b)[1], str(typed(b)[1:2]))
    check("...and the shell's exit closes the tab", typed(b)[-1] == "exit\r", str(typed(b)))
    check("reported closed", r.get("tab_closed") is True and r.get("claude_exited") is True,
          str(r))
    check("it went to the right tab", {i for i, _ in b.sent} == {"x1"}, str(b.sent))

    print("=== a plain shell tab: no /exit (it would be command-not-found) ===")
    b = StubBridge()
    r = await close(b, iterm_session_id="x2", send_exit=False)
    check("no /exit was typed", "/exit\r" not in typed(b), str(typed(b)))
    check("but the job count IS still checked — a plain tab can have jobs too",
          any("CCWEB_JOBS" in t for t in typed(b)), str(typed(b)))
    check("the tab is closed with exit", typed(b)[-1] == "exit\r", str(typed(b)))
    check("reported as not-a-claude-exit",
          r.get("tab_closed") is True and r.get("claude_exited") is False, str(r))
    check("nothing claimed to be detached (there was no binding)",
          r.get("detached") is False, str(r))

    print("=== an UNBOUND claude tab still needs its /exit ===")
    # The case that breaks if send_exit is inferred from the session id: claude is
    # running in there, we were just never attached to it. Typing `exit` at it would
    # land in claude's composer as a prompt and the tab would never close.
    b = StubBridge()
    r = await close(b, iterm_session_id="x3", send_exit=True)
    check("claude got /exit even with no session id", typed(b)[0] == "/exit\r", str(typed(b)))
    check("...and the tab still closed", typed(b)[-1] == "exit\r", str(typed(b)))

    print("=== background jobs veto the close, on either kind of tab ===")
    for kind, kw in (("claude", dict(claude_session_id="sid-4", iterm_session_id="x4")),
                     ("plain", dict(iterm_session_id="x5", send_exit=False))):
        b = StubBridge(njobs=2)
        r = await close(b, **kw)
        check(f"{kind} tab with 2 jobs is left open", r.get("tab_closed") is False, str(r))
        check(f"...and no exit was sent ({kind})",
              typed(b).count("exit\r") == 0, str(typed(b)))
        check(f"...the reason names the jobs ({kind})",
              r.get("jobs") == 2 and "background job" in (r.get("detail") or ""), str(r))

    print("=== an unreadable job count also leaves it open ===")
    class Blind(StubBridge):
        async def get_screen_for(self, iterm_id, **kw):
            return "no marker here"
    b = Blind()
    r = await close(b, claude_session_id="sid-6", iterm_session_id="x6")
    check("no count -> no close", r.get("tab_closed") is False and "exit\r" not in typed(b),
          str((r, typed(b))))

    print("=== no tab to close ===")
    b = StubBridge()
    r = await close(b, claude_session_id="sid-7")
    check("detach-only, nothing typed", r.get("tab_closed") is False and not b.sent, str(r))

    # ---------------------------------------------------------------- client side
    print("=== all four triggers share one client path ===")
    html = Path(ROOT, "static", "index.html").read_text(encoding="utf-8")

    check("the tab list's exit is NOT a literal `exit` command any more "
          "(it would be typed into claude as a prompt)",
          'data-cmd="exit"' not in html, "found data-cmd=\"exit\"")
    check("it's the shared exit path instead", 'data-exit=""' in html)
    check("the key row routes data-exit through exitTabFromList",
          re.search(r"dataset\.exit != null[\s\S]{0,400}?exitTabFromList", html) is not None)
    check("there is exactly ONE exit control in the tab list, not one per row",
          html.count("exitTabFromList(") == 2 and "tab-exit" not in html,
          f'{html.count("exitTabFromList(")} references')
    check("...and exitTabFromList passes the tab's OWN kind, not the presence of a sid",
          re.search(r"async function exitTabFromList[\s\S]{0,400}?isClaude: !!t\.is_claude",
                    html) is not None)
    # Every trigger ends at exitAndCloseTab; count the call sites so a fifth copy of
    # the flow can't be added quietly.
    sites = len(re.findall(r"(?<!function )exitAndCloseTab\(", html))
    check("four call sites: composer /exit, ⚙ menu, tab list key row, picker card",
          sites == 4, f"{sites} call sites")

    check("the composer's /exit and the ⚙ menu both land on the main page",
          len(re.findall(r"exitToPicker\(", html)) >= 3,
          str(len(re.findall(r"exitToPicker\(", html))))
    check("...and that helper really goes to the picker",
          re.search(r"function exitToPicker[\s\S]{0,200}?showPicker\(\)", html) is not None)

    print("=== /exit is intercepted only when it IS the message ===")
    # Drive the real regex out of the file rather than retyping it here — a test that
    # re-declares the pattern passes even after the pattern in the page changes.
    m = re.search(r"if \((/\^\\/\([a-z|]+\)\$/i)\.test\(text\.trim\(\)\)", html)
    check("found the guard in send()", m is not None)
    node = shutil.which("node")
    if m and not node:
        print("  skip  node not installed — regex behaviour unchecked")
    elif m:
        with tempfile.TemporaryDirectory() as td:
            js = Path(td, "t.js")
            js.write_text(
                "const re = %s;\n"
                "const cases = ['/exit', '/quit', ' /exit ', '/EXIT', '/exit now',"
                " 'run /exit for me', '/exits', 'exit', '', '/exit\\n/exit'];\n"
                "console.log(JSON.stringify(cases.map(c => re.test(c.trim()))));\n"
                % m.group(1), encoding="utf-8")
            out = subprocess.run([node, str(js)], capture_output=True, text=True)
        got = out.stdout.strip()
        want = "[true,true,true,true,false,false,false,false,false,false]"
        check("bare /exit and /quit intercepted; '/exit now', 'run /exit for me' "
              "and a bare 'exit' are just text", got == want, f"{got} err={out.stderr[:200]}")
    check("the interception is skipped when files are attached "
          "(those would be silently dropped)",
          re.search(r"test\(text\.trim\(\)\) && !pendingUploads\.length", html) is not None)

    print(("\nFAILED: " + ", ".join(_fails)) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
