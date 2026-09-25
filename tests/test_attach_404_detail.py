#!/usr/bin/env python3
"""When a new session's agent never starts, say what the terminal says.

Found 2026-09-22 on the codex host. Every "new session" produced:

    Attach failed: {"detail":"unknown session_id"}

which is true and useless. The pane was right there, sitting at a shell prompt with the
reason printed in it:

    Error: `npm install -g @openai/codex` failed with status exit status: 243
    npm error path: '/usr/lib/node_modules/@openai'
    npm error The operation was rejected by your operating system.

codex starts by updating itself through `npm install -g`; npm's global prefix was /usr
(root-owned), so it died before writing a thread, no process held that pane, no
`pending-pane-%N` row existed, and attach 404'd. The only way to see any of that was to
ssh in and run `tmux capture-pane`.

So a 404 on a synthetic `pending-pane-%N` id now carries the pane's last lines, and the
browser shows the detail as text instead of as the JSON it arrived in.

    python3 tests/test_attach_404_detail.py      # exit 0 = pass
"""
import asyncio
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


PANE_TAIL = """
npm error code EACCES
npm error   path: '/usr/lib/node_modules/@openai'
npm error The operation was rejected by your operating system.

Error: `npm install -g @openai/codex` failed with status exit status: 243
user@host:~/work/proj$
"""


def main():
    home = tempfile.mkdtemp(prefix="ccweb-404-")
    os.makedirs(os.path.join(home, ".claude"))
    os.environ["HOME"] = home
    os.environ["CC_WEB_TOKEN"] = "t"
    try:
        import cc_web
    except Exception as e:                                  # pragma: no cover
        print("SKIP: cannot import cc_web:", e); return 0

    asked = []

    class Bridge:
        async def get_screen_for(self, pane, max_lines=80, **kw):
            asked.append((pane, max_lines))
            return PANE_TAIL

    class Silent(Bridge):
        async def get_screen_for(self, pane, max_lines=80, **kw):
            asked.append((pane, max_lines))
            return "\n\n   \n"

    class Broken(Bridge):
        async def get_screen_for(self, pane, max_lines=80, **kw):
            raise RuntimeError("bridge is down")

    async def go():
        print("=== a pending-pane id that never came up ===")
        cc_web.bridge = Bridge()
        asked.clear()
        d = await cc_web._attach_404_detail("pending-pane-6")
        # The pane it read is the one the id names — the whole point is that the two
        # halves of the synthetic id agree.
        check("it reads the pane the id names", asked and asked[0][0] == "%6", str(asked))
        check("...and carries the reason the agent died",
              "npm install -g @openai/codex" in d and "EACCES" in d, d[-120:].replace("\n", " | "))
        check("...naming the pane, so the >_ view can be opened on it", "%6" in d, d[:80])
        # The old message is kept as the lead: it is still what happened, and someone
        # searching for it should find this.
        check("...without dropping the original wording", d.startswith("unknown session_id"), d[:40])
        check("...trimmed to the tail, not the whole scrollback",
              len(d.splitlines()) <= 20, str(len(d.splitlines())))

        print("=== a pane with nothing in it ===")
        cc_web.bridge = Silent()
        d2 = await cc_web._attach_404_detail("pending-pane-9")
        check("says the pane is empty rather than showing blank lines",
              "没有任何输出" in d2 and "%9" in d2, d2)

        print("=== a real session id is not a pane ===")
        cc_web.bridge = Bridge()
        asked.clear()
        d3 = await cc_web._attach_404_detail("01a0c2df-f9b7-7e72-b4ff-0ae513f7c2cb")
        check("no pane is read for it", asked == [], str(asked))
        check("...and the answer is the plain one", d3 == "unknown session_id", d3)

        print("=== the bridge itself failing must not replace one error with another ===")
        cc_web.bridge = Broken()
        d4 = await cc_web._attach_404_detail("pending-pane-6")
        check("it falls back to the plain answer", d4 == "unknown session_id", d4)

    asyncio.run(go())

    print("=== the browser shows the detail as text ===")
    idx = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    i = idx.index('const t = await resp.text();')
    blk = idx[i:i + 500]
    # Otherwise the pane's output arrives as one line of JSON with \n escapes in it —
    # technically present, unreadable in an alert().
    check("the attach failure unwraps `detail` before showing it",
          "JSON.parse(t)" in blk and "j.detail" in blk, blk[:80])

    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
