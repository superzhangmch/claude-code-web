#!/usr/bin/env python3
"""End-to-end UI smoke test: a real browser against a real cc_web.

The other tests drive functions. This one drives the PAGE — which is where a whole class
of defect lives that unit tests can't see. Everything it checks here has already caught a
real bug in review:

  * the brief row's shape (a chunk-ownership bug in another view was found the same way)
  * the resume chooser: a source picker that existed but read as a status label, and a
    preview whose format didn't match the list it sits next to
  * the "can't reach the terminal" banner: it named iTerm2 on a tmux host, and the empty
    list underneath still claimed "no live claude tab" — the exact contradiction the
    banner exists to remove

It runs cc_web with the terminal bridge stubbed (a browser can't be given real iTerm2
tabs) on an ephemeral port, with $HOME pointed at a throwaway dir, and drives Firefox
through geckodriver's HTTP API — no selenium dependency. A `mode` file flips the stub
between "healthy" and "wedged" so both stories run against one server.

    python3 tests/test_ui_smoke.py            # exit 0 = pass
    KEEP_SHOTS=1 python3 tests/test_ui_smoke.py    # leave the screenshots behind

Skips (exit 0) when geckodriver or firefox isn't installed.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "ui-smoke-token"

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- the stubbed server ---------------------------------------------------------------
# Runs in its own process (the browser needs a real HTTP server), so the patches have to
# live in a module uvicorn imports rather than being applied from this test.
STUB_APP = '''
import json, os, sys, pathlib
sys.path.insert(0, os.environ["CC_WEB_SRC"])
import cc_web
from cc_web import app  # noqa: F401
import websockets.exceptions as we

HERE = pathlib.Path(os.environ["SMOKE_DIR"])
mode = lambda: (HERE / "mode").read_text().strip()

class Tab:
    def __init__(self, sid, name, wi, ti, pid, cwd):
        self.claude_session_id, self.name = sid, name
        self.window_index, self.tab_index, self.pid, self.cwd = wi, ti, pid, cwd
        self.iterm_session_id = "stub-%d" % pid

TABS = [Tab("aaaaaaaa-1111-2222-3333-444444444444", "\\u2733 cc-web (claude)",   0, 0, 4001, "/tmp/proj-a"),
        Tab("bbbbbbbb-1111-2222-3333-444444444444", "\\u2733 llm-chat (claude)", 0, 1, 4002, "/tmp/proj-b"),
        Tab("cccccccc-1111-2222-3333-444444444444", "\\u2733 tmp (claude)",      1, 0, 4003, "/tmp/proj-c")]

async def _list():
    if mode() == "wedged":
        cc_web.bridge.last_error = cc_web._bridge_reason(we.ConnectionClosedError(None, None))
        return []
    cc_web.bridge.last_error = ""
    return TABS

async def _ensure():
    if mode() == "wedged":
        raise we.ConnectionClosedError(None, None)

async def _ready(timeout=20.0):
    return mode() != "wedged"

def _drop():
    (HERE / "reset_calls").write_text(str(int((HERE / "reset_calls").read_text() or 0) + 1))

async def _list_all():
    if mode() == "wedged":
        return []
    out = [{"iterm_session_id": t.iterm_session_id, "window_index": t.window_index,
            "tab_index": t.tab_index, "name": t.name, "tty": "s%03d" % t.tab_index,
            "is_claude": True, "pid": t.pid} for t in TABS]
    # a plain shell, so the >_ list is exercised with a non-claude row too
    out.append({"iterm_session_id": "stub-shell", "window_index": 1, "tab_index": 1,
                "name": "zsh", "tty": "s099", "is_claude": False, "pid": None})
    return out

cc_web.bridge.list_all_tabs = _list_all
cc_web.bridge.list_claude_tabs = _list
cc_web.bridge.ensure_connected = _ensure
cc_web.bridge.wait_ready = _ready
cc_web.bridge.drop = _drop
cc_web.bridge.last_error = ""
cc_web._claude_session_meta = lambda pid: {
    "sessionId": next((t.claude_session_id for t in TABS if t.pid == pid), ""),
    "startedAt": 1785900000000}

# Record what resume was asked to restore instead of opening terminal tabs.
async def _run_resume(sessions):
    (HERE / "resume_got").write_text(json.dumps(
        {"n": len(sessions), "names": [s.get("name") for s in sessions]}, ensure_ascii=False))
    cc_web._resume_progress.update({"running": False, "done": len(sessions),
                                    "total": len(sessions), "resumed": len(sessions),
                                    "results": [], "current": "", "cancelled": False})
cc_web._run_resume = _run_resume
'''


def seed_home(home, sids):
    """A manual snapshot plus two auto ones (so the chooser has something to choose), and
    a transcript per live tab (so the rows have a real "last used" — that column comes
    from a tail read of the JSONL, which is worth exercising through the page)."""
    cl = os.path.join(home, ".claude")
    import datetime
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)
    for i, sid in enumerate(sids):
        proj = os.path.join(cl, "projects", f"-tmp-proj-{chr(97 + i)}")
        os.makedirs(proj, exist_ok=True)
        with open(os.path.join(proj, sid + ".jsonl"), "w") as f:
            for r in range(3):
                t = (when + datetime.timedelta(minutes=r)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                f.write(json.dumps({"type": "user", "cwd": f"/tmp/proj-{chr(97+i)}", "timestamp": t,
                                    "message": {"role": "user", "content": f"human {r}"}}) + "\n")
                f.write(json.dumps({"type": "assistant", "cwd": f"/tmp/proj-{chr(97+i)}", "timestamp": t,
                                    "message": {"role": "assistant",
                                                "content": [{"type": "text", "text": "reply " + "y" * 200}]}}) + "\n")
    os.makedirs(os.path.join(cl, "cc_web_snapshots"), exist_ok=True)
    # snapshot_every_min=0: the timer would otherwise add entries mid-test
    open(os.path.join(cl, "cc_web.conf"), "w").write(f"token={TOKEN}\nsnapshot_every_min=0\n")
    mk = lambda n, tag, wins=1: [
        {"sid": f"{i:08d}-1111-2222-3333-44444444444{i}", "cwd": f"/tmp/{tag}{i}",
         "name": f"✳ {tag}-{i} (claude)",
         "window_index": i % wins, "tab_index": i // wins} for i in range(n)]
    json.dump({"saved_at": "2026-08-19T09:00:00", "auto": False, "sessions": mk(5, "manualtab")},
              open(os.path.join(cl, "cc_web_session_snapshot.json"), "w"), ensure_ascii=False)
    for stamp, n, wins in (("20260820T100000000", 2, 1), ("20260820T140000000", 4, 2)):
        iso = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}"
        json.dump({"saved_at": iso, "first_seen": iso, "auto": True,
                   "sessions": mk(n, "auto" + stamp[9:13], wins)},
                  open(os.path.join(cl, "cc_web_snapshots", f"auto-{stamp}.json"), "w"),
                  ensure_ascii=False)
    # session titles → the third column of a row ("other_text")
    json.dump({s: {"title": f"会话标题-{i}", "summary": "x"} for i, s in enumerate(sids)},
              open(os.path.join(cl, "cc_web_summaries.json"), "w"), ensure_ascii=False)


# --- the browser ----------------------------------------------------------------------
class Driver:
    """Just enough of the WebDriver protocol, over urllib (no selenium needed)."""

    def __init__(self, port, width=680, height=900):
        self.wd = f"http://127.0.0.1:{port}"
        caps = {"capabilities": {"alwaysMatch": {
            "browserName": "firefox",
            "moz:firefoxOptions": {"args": ["-headless", f"--width={width}", f"--height={height}"]},
            # the resume flow is gated behind confirm(); auto-accept it
            "unhandledPromptBehavior": "accept"}}}
        self.sid = self.call("POST", "/session", caps)["value"]["sessionId"]

    def call(self, method, path, body=None):
        req = urllib.request.Request(
            self.wd + path, method=method,
            data=(json.dumps(body).encode() if body is not None else None),
            headers={"content-type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=120).read().decode())

    def js(self, script):
        return self.call("POST", f"/session/{self.sid}/execute/sync",
                         {"script": script, "args": []})["value"]

    def go(self, url):
        self.call("POST", f"/session/{self.sid}/url", {"url": url})

    def wait(self, expr, tries=80):
        """Poll a JS expression. Also lets the driver dismiss any open dialog."""
        for _ in range(tries):
            try:
                v = self.js("return " + expr)
                if v:
                    return v
            except Exception:
                pass
            time.sleep(0.25)
        try:
            return self.js("return " + expr)
        except Exception:
            return None

    def wait_file(self, path, tries=80):
        for _ in range(tries):
            time.sleep(0.25)
            try: self.js("return 1")      # a command lets WebDriver accept dialogs
            except Exception: pass
            if os.path.exists(path):
                return json.loads(open(path).read())
        return None

    def shot(self, path):
        png = self.call("GET", f"/session/{self.sid}/screenshot")["value"]
        import base64
        open(path, "wb").write(base64.b64decode(png))

    def quit(self):
        try: self.call("DELETE", f"/session/{self.sid}")
        except Exception: pass


def main():
    if not (shutil.which("geckodriver") and shutil.which("firefox")):
        print("SKIP: needs geckodriver + firefox"); return 0
    uvicorn = next((c for c in (os.path.join(ROOT, ".venv/bin/uvicorn"),
                                os.path.expanduser("~/claude-code-web/.venv/bin/uvicorn"))
                    if os.path.exists(c)), shutil.which("uvicorn"))
    if not uvicorn:
        print("SKIP: no uvicorn found"); return 0

    smoke = tempfile.mkdtemp(prefix="ccweb-ui-")
    home = os.path.join(smoke, "home")
    os.makedirs(home)
    sids = ["aaaaaaaa-1111-2222-3333-444444444444", "bbbbbbbb-1111-2222-3333-444444444444",
            "cccccccc-1111-2222-3333-444444444444"]
    seed_home(home, sids)
    open(os.path.join(smoke, "_stub_app.py"), "w").write(STUB_APP)
    open(os.path.join(smoke, "mode"), "w").write("ok")
    open(os.path.join(smoke, "reset_calls"), "w").write("0")
    app_port, wd_port = free_port(), free_port()
    base = f"http://127.0.0.1:{app_port}"

    srv = subprocess.Popen(
        [uvicorn, "_stub_app:app", "--app-dir", smoke, "--host", "127.0.0.1",
         "--port", str(app_port), "--log-level", "warning"],
        env=dict(os.environ, HOME=home, CC_WEB_SRC=ROOT, SMOKE_DIR=smoke),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    gecko = subprocess.Popen(["geckodriver", "--port", str(wd_port)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    drv = None
    try:
        for _ in range(60):
            time.sleep(0.4)
            try:
                urllib.request.urlopen(base + "/api/auth-status", timeout=2).read()
                break
            except Exception:
                continue
        else:
            out = srv.stdout.read(2000) if srv.stdout else ""
            print("  FAIL  the stub server never came up\n" + out); return 1
        for _ in range(40):
            time.sleep(0.25)
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{wd_port}/status", timeout=2).read()
                break
            except Exception:
                continue

        drv = Driver(wd_port)
        drv.go(base)
        drv.js(f"localStorage.setItem('cc_web_token','{TOKEN}');"
               "localStorage.removeItem('cc_web_list_brief');")
        drv.go(base)

        print("=== the session list, brief by default ===")
        n = drv.wait("document.querySelectorAll('#picker-list .brief-row').length")
        check("brief rows appear without touching the toggle", n == 3, f"{n} rows")
        check("...and no full cards", drv.js("return document.querySelectorAll('#picker-list .session-card').length") == 0)
        rows = drv.js("return [...document.querySelectorAll('#picker-list .brief-row')]"
                      ".map(r=>[...r.children].map(c=>c.className+'='+c.textContent).join('|'))")
        check("a row is sid · [tab-name] · session name · last-use",
              all("sw-sid=" in r and "sw-tab=" in r and "sw-sess=" in r and "br-time=" in r
                  for r in rows), rows[0])
        check("...the last-use column is a real timestamp read from the transcript",
              all(len(r.split("br-time=")[1]) >= 5 and "~" not in r.split("br-time=")[1]
                  for r in rows), rows[0].split("br-time=")[-1])
        # The position chip is deliberately back: all three lists use the ⇆ switcher's
        # line, and that includes it.
        check("...with the position chip, like the ⇆ switcher",
              all("sw-wt=" in r for r in rows), rows[0])
        check("...the tab name stripped of iTerm's decorations",
              "✳" not in " ".join(rows) and "(claude)" not in " ".join(rows), rows[0])
        hidden = drv.js("return [getComputedStyle(document.getElementById('picker-quickfilter')).display,"
                        "getComputedStyle(document.getElementById('picker-search')).display]")
        check("brief hides the quick filter and full search", hidden == ["none", "none"], str(hidden))
        drv.shot(os.path.join(smoke, "brief.png"))

        print("=== ▤ full and back ===")
        drv.js("document.getElementById('picker-brief').click()")
        check("full draws cards", drv.wait("document.querySelectorAll('#picker-list .session-card').length") >= 3)
        shown = drv.js("return [getComputedStyle(document.getElementById('picker-quickfilter')).display,"
                       "getComputedStyle(document.getElementById('picker-search')).display]")
        check("...and brings the search chrome back", "none" not in shown, str(shown))
        drv.js("document.getElementById('picker-brief').click()")
        check("back to brief", drv.wait("document.querySelectorAll('#picker-list .brief-row').length") == 3)

        print("=== nothing in the ⚙ menu's button rows is clipped ===")
        # brief/medium/all sit three-across in a 200px menu. With the old ▁▄█ icons and
        # 10px side padding there wasn't room for the words: the menu read "▁ br… ▄ m…
        # █ all". Measured, not eyeballed — a label that says "m…" is not a label.
        clipped = drv.js("""
          const m = document.getElementById('mode-menu');
          const prev = m.style.display; m.style.display = 'block';
          const bad = [...document.querySelectorAll('.switch-menu .scr-cfg-row .sw-item')]
            .filter(b => b.scrollWidth > b.clientWidth + 1)
            .map(b => b.textContent + ' needs ' + b.scrollWidth + 'px in ' + b.clientWidth + 'px');
          m.style.display = prev;
          return bad;
        """)
        check("every label in a menu row fits its button", clipped == [], str(clipped))
        # Each setting is one line: its label and its controls share a row. This menu
        # used to spend a heading line plus a full-width button line on every one of
        # them, and a whole line per speech model.
        rowinfo = drv.js("""
          const m = document.getElementById('mode-menu');
          const prev = m.style.display; m.style.display = 'block';
          const rows = [...m.querySelectorAll('.scr-cfg-row')];
          // "one line" = the row's controls all sit at the same y, AND the row is no
          // taller than one control (i.e. the label isn't stacked above them). The label
          // span's own top differs by a pixel or two from a padded button's, so compare
          // buttons to buttons and use the height for the label.
          const multi = rows.filter(r => {
            const bs = [...r.querySelectorAll('button')].filter(e => e.offsetHeight > 0);
            if (!bs.length) return false;
            const tops = new Set(bs.map(e => Math.round(e.getBoundingClientRect().top)));
            const h = r.getBoundingClientRect().height;
            const bh = bs[0].getBoundingClientRect().height;
            return tops.size > 1 || h > bh + 14;
          }).map(r => r.textContent.trim().replace(/\s+/g, " ").slice(0, 26));
          const res = [rows.length, multi];
          m.style.display = prev;
          return res;
        """)
        check("the ⚙ menu is all label+controls rows", rowinfo[0] >= 6, str(rowinfo[0]))
        check("...and none of them wraps onto a second line", rowinfo[1] == [], str(rowinfo[1]))
        check("...and the ▁▄█ icons are gone from the labels",
              drv.js("return [...document.querySelectorAll('.mode-opt')].map(b=>b.textContent)")
              == ["brief", "medium", "all"],
              str(drv.js("return [...document.querySelectorAll('.mode-opt')].map(b=>b.textContent)")))

        print("=== the >_ tab list uses the same line as the others ===")
        drv.js("document.getElementById('tabs-btn').click()")
        time.sleep(0.6)
        js_rows = ("[...document.querySelectorAll('#tabs-modal .tabs-list .tab-sel')]"
                   ".map(r=>[...r.children].map(c=>c.className+'='+c.textContent).join('|'))")
        rows = drv.wait(js_rows)
        check("it lists every tab, claude or not", len(rows) == 4, f"{len(rows)} rows")
        check("...including the plain shell", any("zsh" in r for r in rows), str(rows[-1]))
        check("...each row is a .sess-line",
              drv.js("return document.querySelectorAll('#tabs-modal .tabs-list .sess-line').length") == 4)
        check("...with the same spans as the brief list (pos · sid · [tab] · name)",
              all("sw-wt=" in r for r in rows)
              and all("sw-tab=" in r for r in rows[:3]), str(rows[0]))
        check("...and no private tl-* classes left", not any("tl-" in r for r in rows))
        check("the position label matches the switcher's form (tN / wXtY, no totals)",
              all(re.match(r"sw-wt=w?\d*t\d+\*?$", r.split("|")[0]) for r in rows),
              str([r.split("|")[0] for r in rows]))
        # On a multi-column desktop layout the entries sit side by side, so the gap
        # between them has to be clearly wider than the gap inside one. At 6px both ways
        # an Attach button was equidistant from its own title and the next tab's — you
        # couldn't tell which tab it would attach.
        gaps = drv.js("""
          const list = document.querySelector('#tabs-modal .tabs-list');
          const row = document.querySelector('#tabs-modal .tab-row');
          const px = v => parseFloat(v) || 0;
          return [px(getComputedStyle(list).columnGap), px(getComputedStyle(row).gap
                  || getComputedStyle(row).columnGap)];
        """)
        check("an entry's own button is much closer to it than the next entry is",
              gaps[0] >= gaps[1] * 3, f"between={gaps[0]}px inside={gaps[1]}px")
        drv.shot(os.path.join(smoke, "tabs_list.png"))

        print("=== the resume chooser ===")
        drv.js("document.getElementById('tabs-snap-resume').click()")
        n = drv.wait("document.querySelectorAll('#snapdlg.show .sd-row').length")
        check("it opens with one row per snapshot", n >= 3, f"{n} rows")
        labels = drv.js("return [...document.querySelectorAll('#snapdlg .sd-row')].map(r=>r.innerText.replace(/\\n/g,' '))")
        check("the manual snapshot is offered and labelled as yours",
              any("手动" in l for l in labels), labels[0])
        check("...and the auto history too, newest first",
              sum(1 for l in labels if "自动" in l) >= 2 and "最近一次" in labels[1], labels[1])
        check("exactly one row is preselected",
              drv.js("return document.querySelectorAll('#snapdlg .sd-row.on').length") == 1)
        check("there is a Cancel button", drv.js("return !!document.getElementById('snapdlg-cancel')"))
        # the 4-session, 2-window auto entry: its preview must show wXtY
        drv.js("[...document.querySelectorAll('#snapdlg .sd-row')]"
               ".find(r=>r.dataset.val.includes('140000')).click()")
        time.sleep(0.9)
        prev = drv.js("return [...document.querySelectorAll('#snapdlg .sd-prev .sd-item')]"
                      ".map(r=>[...r.children].map(c=>c.className+'='+c.textContent).join('|'))")
        check("the preview follows the selection", len(prev) == 4, f"{len(prev)} lines")
        check("...each line is wXtY · sid · [tab-name] · session name",
              all("sw-wt=" in p and "sw-sid=" in p and "sw-tab=" in p for p in prev), prev[0])
        check("...multi-window snapshots show the window too", "sw-wt=w" in prev[0], prev[0])
        check("...names stripped here as well",
              "✳" not in " ".join(prev) and "(claude)" not in " ".join(prev), prev[0])
        drv.shot(os.path.join(smoke, "chooser.png"))

        print("=== Cancel resumes nothing; 开始恢复 uses the row you picked ===")
        drv.js("document.getElementById('snapdlg-cancel').click()")
        time.sleep(0.4)
        check("the dialog closes", not drv.js("return document.getElementById('snapdlg').classList.contains('show')"))
        check("and nothing was resumed", not os.path.exists(os.path.join(smoke, "resume_got")))
        drv.js("document.getElementById('tabs-snap-resume').click()")
        drv.wait("document.querySelectorAll('#snapdlg.show .sd-row').length")
        drv.js("[...document.querySelectorAll('#snapdlg .sd-row')]"
               ".find(r=>r.dataset.val.includes('140000')).click()")
        time.sleep(0.9)
        drv.js("document.getElementById('snapdlg-go').click()")
        got = drv.wait_file(os.path.join(smoke, "resume_got"))
        check("resume got the CHOSEN snapshot, not the preselected one",
              bool(got) and got["n"] == 4, str(got)[:70])
        # The stored name keeps whatever the terminal reported; the server strips the
        # decorations when it re-titles the tab, not in the file.
        check("...its own sessions", bool(got) and "auto1400-0" in got["names"][0],
              str(got and got["names"][:2]))

        print("=== a wedged terminal says so, instead of looking empty ===")
        open(os.path.join(smoke, "mode"), "w").write("wedged")
        drv.go(base)
        shown = drv.wait("getComputedStyle(document.getElementById('picker-bridge')).display !== 'none'")
        banner = drv.js("return document.getElementById('picker-bridge').innerText")
        check("a banner appears", bool(shown) and bool(banner.strip()), banner.replace("\n", " ")[:70])
        term = json.loads(urllib.request.urlopen(
            urllib.request.Request(base + "/api/server-info",
                                   headers={"authorization": "Bearer " + TOKEN}),
            timeout=10).read().decode())["terminal"]
        check(f"...naming THIS host's terminal ({term})", term in banner, banner.replace("\n", " ")[:70])
        other = "tmux" if term == "iTerm2" else "iTerm2"
        check(f"...and never the other one ({other})", other not in banner)
        check("...with a reconnect button", drv.js("return !!document.querySelector('#picker-bridge .pb-act')"))
        empty = drv.js("return document.getElementById('picker-list').innerText")
        check("the empty list does NOT claim there is no tab running",
              "No live" not in empty and "读不到" in empty, empty.replace("\n", " ")[:70])
        drv.shot(os.path.join(smoke, "wedged.png"))
        drv.js("document.querySelector('#picker-bridge .pb-act').click()")
        for _ in range(40):
            time.sleep(0.25)
            try: drv.js("return 1")
            except Exception: pass
            if open(os.path.join(smoke, "reset_calls")).read().strip() != "0":
                break
        check("reconnect reaches the server and drops the cached connection",
              open(os.path.join(smoke, "reset_calls")).read().strip() != "0",
              open(os.path.join(smoke, "reset_calls")).read().strip())
    finally:
        if drv:
            drv.quit()
        gecko.terminate()
        srv.terminate()
        for p in (gecko, srv):
            try: p.wait(timeout=10)
            except Exception: p.kill()
        if os.environ.get("KEEP_SHOTS"):
            print(f"\nscreenshots kept in {smoke}")
        else:
            shutil.rmtree(smoke, ignore_errors=True)

    print(("\nFAILED: " + ", ".join(_fails)) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
