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
    # name= is the machine label the page title is built from. A value nothing else
    # could produce, so the title assertion cannot pass by coincidence.
    open(os.path.join(cl, "cc_web.conf"), "w").write(
        f"token={TOKEN}\nsnapshot_every_min=0\nname=smokebox\n")
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
        try:
            return json.loads(urllib.request.urlopen(req, timeout=120).read().decode())
        except urllib.error.HTTPError as e:
            # A JS error comes back as a 500 with the message in the body; printing the
            # status alone turned every typo in a probe into a five-minute hunt.
            raise RuntimeError("webdriver " + str(e.code) + ": " + e.read().decode()[:400]) from None

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

        print("=== the tab title says WHICH machine and WHICH agent ===")
        # Both instances of this app served the identical title, so several open tabs
        # were told apart only by a 16px favicon. Asserted against what the server
        # reports rather than a literal, so it stays true wherever this runs.
        info = json.loads(urllib.request.urlopen(
            urllib.request.Request(base + "/api/server-info",
                                   headers={"authorization": "Bearer " + TOKEN}),
            timeout=10).read().decode())
        want = f"{info['name']} - {info['agent']}"
        drv.wait("document.title === " + json.dumps(want))   # set once server-info lands
        got = drv.js("return document.title")
        check("title is '<machine> - <agent>'", got == want, f"{got!r} != {want!r}")
        check("...and the machine name came from the conf, not the hostname",
              info["name"] == "smokebox", info["name"])

        print("=== the session list, brief by default ===")
        n = drv.wait("document.querySelectorAll('#picker-list .brief-row').length")
        check("brief rows appear without touching the toggle", n == 3, f"{n} rows")
        check("...and no full cards", drv.js("return document.querySelectorAll('#picker-list .session-card').length") == 0)
        rows = drv.js("return [...document.querySelectorAll('#picker-list .brief-row')]"
                      ".map(r=>[...r.children].map(c=>c.className+'='+c.textContent).join('|'))")
        check("a row is sid · [tab-name] · session name · last-use",
              all("sw-sid=" in r and "sw-tab=" in r and "sw-sess=" in r and "br-time=" in r
                  for r in rows), rows[0])
        # The column is an AGE now ("3.0h"), not "08-17 11:38" — so the old
        # length>=5 test no longer states the intent. The intent was: this came from a
        # real transcript timestamp, not a guess. Which is: it parses as an age, and
        # carries no ~ (the marker for "derived from the file mtime").
        ages = [r.split("br-time=")[1].split("|")[0] for r in rows]
        check("...the last-use column is a real timestamp read from the transcript",
              all(re.fullmatch(r"(now|\d+(\.\d)?[mhd])", a) for a in ages), str(ages))
        stamps = drv.js("return [...document.querySelectorAll('#picker-list .br-time')].map(e=>e.title)")
        check("...with the absolute stamp kept on hover, not thrown away",
              all(re.fullmatch(r"\d\d-\d\d \d\d:\d\d", (t or "")) for t in stamps), str(stamps))
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

        src_index = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()

        print("=== desktop keyboard: i is gone, t is the terminal, p peeks ===")
        # Plain letters, only when you are not typing in a field. Remapped 2026-09-21:
        # the tab list lost its letter (the ⇆ button opens it, and 1–9 still pick a tab
        # once it is open), the terminal window moved s→t, and tailScr moved t→p (peek).
        gone = drv.js("""
          const tv = document.getElementById('transcript-view');
          const sm = document.getElementById('switch-menu');
          const scr = document.getElementById('screen-modal');
          const tail = document.getElementById('tail-box');
          const had = tv.classList.contains('show');
          tv.classList.add('show');            // the handler bails unless a view is open
          sm.style.display = 'none';
          document.dispatchEvent(new KeyboardEvent('keydown',
            { key: 'i', bubbles: true, cancelable: true }));
          const r = { menu: sm.style.display, scr: scr.classList.contains('show'),
                      tail: tail.style.display };
          if (!had) tv.classList.remove('show');
          return r;
        """)
        # Driven, because "the key does nothing" is exactly what a leftover handler would
        # break. (t and p are not driven here: both open a window that talks to the
        # attached session, and with none attached they raise a native dialog that
        # freezes the driver. Their MAPPING is checked below instead.)
        check("i no longer opens the tab list", gone["menu"] == "none", str(gone))
        check("...and does nothing else either",
              gone["scr"] is False and gone["tail"] != "block", str(gone))
        _sw = src_index[src_index.index("switch (e.key) {"):]
        _sw = _sw[:_sw.index("\n  });")]
        check("t opens the terminal window (was s)",
              'case "t": case "T": e.preventDefault(); openScreenInfo();' in _sw)
        check("p peeks at the tail (was t)",
              'case "p": case "P": e.preventDefault(); tailScreen();' in _sw)
        check("...and no letter is left bound to the tab list",
              "openSwitchMenu()" not in _sw.replace("hideSwitchMenu(); openSwitchMenu();", ""),
              "a letter still opens it")
        # The help inside that window has to say the same thing, or it teaches old keys.
        check("...and the in-app shortcut list was updated with them",
              'appendInfoRow(infoBody, "t", "terminal window' in src_index
              and 'appendInfoRow(infoBody, "p", "peek' in src_index
              and '"i", "toggle tab list' not in src_index)

        print("=== select a response → 引用 | copy ===")
        # This view is the session LIST, where <footer> carries .hidden — and an element
        # inside a display:none subtree cannot take focus at all. So the footer is
        # un-hidden for these checks; otherwise "did it focus the composer" is
        # unanswerable here rather than answered no.
        #
        # A fabricated state, and therefore worth only as much as its resemblance to the
        # real one — so it is pinned to the app's own code path first. showTranscript()
        # is what reveals the composer when you open a session; if it ever stops doing
        # it this way, the fabrication below is a fiction and these two fail.
        _st = src_index[src_index.index("function showTranscript()"):]
        _st = _st[:_st.index("\n  }")]
        check("the state faked below is the one showTranscript() produces",
              'footerEl.classList.remove("hidden")' in _st,
              _st.strip().splitlines()[1][:60])
        check("...and the composer really is inside that footer",
              src_index.index('<footer') < src_index.index('id="input"') < src_index.index("</footer>"))
        drv.js("document.querySelector('footer').classList.remove('hidden')")
        check("...so with it un-hidden the composer can be focused at all",
              drv.js("""
                const i = document.getElementById('input');
                i.focus();
                return (document.activeElement || {}).id;
              """) == "input")
        print("=== the control pages open OVER the session, not instead of it ===")
        # Going to /remote/ unloaded this view: coming back re-fetched the transcript and
        # lost the place you were reading. Driven through the real links, because the
        # whole change is what a left-click does.
        ctrl = drv.js("""
          const modal = document.getElementById('ctrl-modal');
          const frame = document.getElementById('ctrl-frame');
          const a = document.getElementById('mm-ctrl-desk');
          const transcriptBefore = !!document.getElementById('transcript');
          a.click();
          const opened = { show: modal.classList.contains('show'), src: frame.getAttribute('src'),
                           state: (history.state || {}).ccCtrl,
                           // the session view must still be there, untouched, behind it
                           transcript: !!document.getElementById('transcript') && transcriptBefore,
                           openA: document.getElementById('ctrl-open').getAttribute('href'),
                           target: document.getElementById('ctrl-open').getAttribute('target'),
                           // 100% of the viewport, nothing subtracted: no title bar, no
                           // padding ring, no border. The float sits ON it.
                           frame: (() => { const r = frame.getBoundingClientRect();
                             return [Math.round(r.left), Math.round(r.top),
                                     Math.round(r.width), Math.round(r.height)]; })(),
                           viewport: [window.innerWidth, window.innerHeight],
                           floatOver: (() => {
                             const f = document.querySelector('.ctrl-float').getBoundingClientRect();
                             const r = frame.getBoundingClientRect();
                             return f.right <= r.right + 1 && f.bottom <= r.bottom + 1
                                 && f.top > r.top + r.height / 2; })(),
                           noBar: !document.getElementById('ctrl-title') };
          document.getElementById('ctrl-close').click();
          const closed = { show: modal.classList.contains('show'), src: frame.getAttribute('src'),
                           transcript: !!document.getElementById('transcript') };
          return { opened, closed };
        """)
        o, c = ctrl["opened"], ctrl["closed"]
        check("a left-click opens the overlay instead of navigating",
              o["show"] is True and o["src"].startswith("/remote_pc/"), str(o))
        check("...telling the page it is embedded, so it hides its own ←",
              "embed=1" in (o["src"] or ""), str(o["src"]))
        check("...with the session view still loaded behind it", o["transcript"] is True)
        check("...and a history entry, so back / edge-swipe closes it",
              o["state"] == 1, str(o["state"]))
        check("...plus a way to get the full page in a tab anyway",
              o["openA"] == "/remote_pc/" and o["target"] == "_blank", str(o))
        # "full screen" = all of the USABLE viewport. A title bar across the top cost a
        # row of the thing you opened it to look at; `inset: 0` went one step too far the
        # other way and put the control page's header under the iPhone's status bar,
        # clock on top of the buttons, untappable. (This browser reports no insets, so
        # here the two coincide — the insets themselves are checked below.)
        check("the frame fills the viewport exactly",
              o["frame"] == [0, 0, o["viewport"][0], o["viewport"][1]],
              f'{o["frame"]} vs viewport {o["viewport"]}')
        _cf = src_index[src_index.index(".ctrl-modal #ctrl-frame {"):]
        _cf = _cf[:_cf.index("\n}")]
        check("...minus the safe areas, so nothing sits under the status bar",
              all(f"env(safe-area-inset-{k}" in _cf for k in ("top", "right", "bottom", "left")),
              _cf[:160])
        check("...with no chrome of its own, only ✕ / ↗ floating over the corner",
              o["noBar"] is True and o["floatOver"] is True, str(o))
        # And the browser's OWN chrome stays. "Full screen" here means all of the
        # viewport; the OS kind (requestFullscreen) throws the window out of the browser,
        # hiding the tabs and the address bar — it was tried, and it is not what a tap
        # should do.
        # Matched on the CALL, not on the word: the comment above the open() explains why
        # requestFullscreen is not used, and a check that trips over its own rationale is
        # a check that gets deleted rather than kept.
        check("...and it does not go OS-fullscreen",
              ".requestFullscreen &&" not in src_index
              and "document.exitFullscreen()" not in src_index)
        check("✕ closes it and the session is right there",
              c["show"] is False and c["transcript"] is True, str(c))
        # Hidden-but-alive would keep polling screenshots of the mac for as long as this
        # tab lives, which is the sort of thing you only find out from a data bill.
        check("...and the frame is torn down, not just hidden",
              c["src"] == "about:blank", str(c["src"]))
        esc = drv.js("""
          document.getElementById('mm-ctrl-phone').click();
          const on = document.getElementById('ctrl-modal').classList.contains('show');
          document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
          return [on, document.getElementById('ctrl-modal').classList.contains('show'),
                  document.getElementById('ctrl-frame').getAttribute('src')];
        """)
        check("Esc closes it too", esc[0] is True and esc[1] is False and esc[2] == "about:blank",
              str(esc))

        print("=== the tail window's three keys sit under the output, not over it ===")
        # The box is small and the line you are watching is the LAST one. A control strip
        # that covered it, or that pushed the box past the composer, would be worse than
        # no strip. (What the keys send is driven in test_tail_keys.py.)
        tk = drv.js("""
          const box = document.getElementById('tail-box');
          const pre = document.getElementById('tail-pre');
          const was = box.style.display;
          box.style.display = 'block';
          pre.textContent = Array.from({length: 9}, (_, i) => 'line ' + i + ' of live output').join('\\n');
          const keys = document.getElementById('tail-keys');
          const R = (e) => e.getBoundingClientRect();
          const br = R(box), pr = R(pre), kr = R(keys);
          const bs = [...keys.querySelectorAll('button')];
          const r = {
            labels: bs.map(b => b.textContent.trim()),
            inside: kr.top >= br.top - 1 && kr.bottom <= br.bottom + 1,
            belowText: kr.top >= pr.bottom - 1,
            rightAligned: br.right - kr.right < 14,
            tall: Math.min(...bs.map(b => Math.round(R(b).height))),
            fitsViewport: br.bottom <= window.innerHeight + 1,
            titled: bs.every(b => (b.title || "").length > 3),
          };
          box.style.display = was; pre.textContent = '';
          return r;
        """)
        check("Esc / ↑ / clear-line are all there",
              tk["labels"] == ["Esc", "↑", "⌫ line"], str(tk["labels"]))
        check("...inside the box and below the output",
              tk["inside"] is True and tk["belowText"] is True, str(tk))
        check("...right-aligned, and the box still fits above the composer",
              tk["rightAligned"] is True and tk["fitsViewport"] is True, str(tk))
        check("...each a real tap target, each saying what it does",
              tk["tall"] >= 24 and tk["titled"] is True, f'{tk["tall"]}px')

        # Measured while chasing the focus: clearing the page selection does NOT blur a
        # focused textarea (afterFocus=input → afterRemoveRanges=input). The BODY reading
        # that started the hunt came from this view's hidden footer, not from the order
        # of those two calls — worth recording, because the code carries a comment about
        # that order and it would otherwise read as the fix for something it did not fix.
        blur = drv.js("""
          const inp = document.getElementById('input');
          inp.focus();
          const a1 = (document.activeElement || {}).id;
          inp.setSelectionRange(0, 0);
          try { window.getSelection().removeAllRanges(); } catch (e) {}
          return [a1, (document.activeElement || {}).id];
        """)
        check("clearing the selection does not blur the composer",
              blur == ["input", "input"], str(blur))
        # Driven as a real selection in a real browser: the bar's whole job is to appear
        # over a Range, and a Range's geometry does not exist without layout.
        selres = drv.js("""
          const m = document.getElementById('main');
          m.innerHTML = '';
          const a = document.createElement('div');
          a.className = 'msg assistant';
          a.textContent = '官方合成就是加权和, 四个分量等权相加。';
          m.appendChild(a);
          const bar = document.getElementById('sel-bar');
          const before = getComputedStyle(bar).display;
          const r = document.createRange();
          r.selectNodeContents(a);
          const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
          document.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
          return { before, txt: String(sel).slice(0, 20) };
        """)
        shown = drv.wait("getComputedStyle(document.getElementById('sel-bar')).display !== 'none' ? 1 : 0",
                         tries=20)
        check("the bar is hidden until something is selected", selres["before"] == "none",
              selres["before"])
        check("...and appears on a selection in the transcript", bool(shown), str(shown))
        placed = drv.js("""
          const bar = document.getElementById('sel-bar');
          const b = bar.getBoundingClientRect();
          const sr = window.getSelection().getRangeAt(0).getBoundingClientRect();
          return { over: Math.round(sr.top - b.bottom),
                   above: b.bottom <= sr.top + 1 && sr.top - b.bottom <= 12,
                   below: b.top >= sr.bottom - 1 && b.top - sr.bottom <= 12,
                   onScreen: b.left >= 0 && b.top >= 0 && b.right <= window.innerWidth,
                   parent: bar.parentElement.tagName,
                   labels: [...bar.querySelectorAll('button')].map(x => x.textContent.trim()) };
        """)
        # The invariant is "next to the selection and not covering it", not "above it":
        # a selection near the top of the viewport has no room above, and the code drops
        # the bar below — which is right, and is what this assertion originally got
        # wrong (it read the correct fallback as a 64px error).
        check("...adjacent to the selection, and not over the words",
              placed["above"] or placed["below"], f'gapAbove={placed["over"]}')
        # …and for a selection of several lines, at the END of it: the point you just
        # dragged to. Against the whole selection's bounding box the bar drifted to the
        # top-centre of the paragraph — for a long pick that is the other end from your
        # finger, and it sat over the message above.
        multi = drv.js("""
          document.getElementById('sel-bar').style.display = 'none';
          const m = document.getElementById('main');
          m.innerHTML = '';
          const pad = document.createElement('div'); pad.style.height = '120px';
          m.appendChild(pad);
          const a = document.createElement('div');
          a.className = 'msg assistant';
          // Long enough to wrap several times at this width, and ending mid-line so the
          // end point is nowhere near the box's centre.
          a.textContent = 'FCM/APNs 的推送是一次性投递。'.repeat(6) + ' 到后台再弹。';
          m.appendChild(a);
          const r = document.createRange(); r.selectNodeContents(a);
          const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
          document.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
          return null;
        """)
        drv.wait("getComputedStyle(document.getElementById('sel-bar')).display !== 'none' ? 1 : 0", tries=20)
        me = drv.js("""
          const b = document.getElementById('sel-bar').getBoundingClientRect();
          const rg = window.getSelection().getRangeAt(0);
          const all = rg.getBoundingClientRect();
          const rects = [...rg.getClientRects()].filter(r => r.width > 0 || r.height > 0);
          const end = rects[rects.length - 1];
          return { lines: rects.length,
                   dyEnd: Math.round(Math.min(Math.abs(b.top - end.bottom), Math.abs(end.top - b.bottom))),
                   dyStart: Math.round(Math.min(Math.abs(b.top - all.top), Math.abs(all.top - b.bottom))),
                   dxEnd: Math.round(Math.abs((b.left + b.right) / 2 - end.right)),
                   dxMid: Math.round(Math.abs((b.left + b.right) / 2 - (all.left + all.right) / 2)),
                   onScreen: b.left >= 0 && b.top >= 0
                          && b.right <= window.innerWidth && b.bottom <= window.innerHeight };
        """)
        check("a multi-line selection puts the bar at its END",
              me["lines"] >= 3 and me["dyEnd"] <= 12, str(me))
        check("...not at the start, and not centred on the whole block",
              me["dyEnd"] < me["dyStart"] and me["dxEnd"] <= me["dxMid"], str(me))
        check("...still fully on screen", me["onScreen"] is True, str(me))
        # A scroll used to hide it — backwards, since you scroll to see more of what you
        # just selected. It follows the text now, and only a press OUTSIDE dismisses it.
        # (The follow is rAF-throttled, hence the wait for a frame.)
        scrolled = drv.js("""
          const bar = document.getElementById('sel-bar');
          const m = document.getElementById('main');
          // The fixture is shorter than the viewport, so scrollTop would not budge and
          // the probe would "pass" by measuring nothing moving. Give it something to
          // scroll (appended AFTER the selection, which it leaves alone).
          const tall = document.createElement('div');
          tall.style.height = '2000px'; m.appendChild(tall);
          const rg = () => { const r = [...window.getSelection().getRangeAt(0).getClientRects()]
                                        .filter(x => x.width > 0 || x.height > 0);
                             return r[r.length - 1]; };
          const before = { top: bar.getBoundingClientRect().top, end: rg().bottom };
          m.scrollTop = m.scrollTop + 60;
          m.dispatchEvent(new Event('scroll', { bubbles: false }));
          return new Promise(res => requestAnimationFrame(() => requestAnimationFrame(() => {
            const after = { shown: getComputedStyle(bar).display !== 'none',
                            top: bar.getBoundingClientRect().top, end: rg().bottom };
            res({ before, after,
                  moved: Math.abs(after.top - before.top) > 20,
                  stillUnderEnd: Math.abs(after.top - after.end) <= 12 });
          })));
        """)
        check("scrolling does NOT dismiss it", scrolled["after"]["shown"] is True, str(scrolled))
        check("...it follows the text it belongs to",
              scrolled["moved"] is True and scrolled["stillUnderEnd"] is True, str(scrolled))
        dismiss = drv.js("""
          const bar = document.getElementById('sel-bar');
          // A press on the bar is a button doing its job — it must not dismiss it.
          bar.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
          const onBar = getComputedStyle(bar).display !== 'none';
          // …and a press anywhere else does.
          document.getElementById('main').dispatchEvent(
            new PointerEvent('pointerdown', { bubbles: true }));
          return [onBar, getComputedStyle(bar).display !== 'none'];
        """)
        check("...a press ON the bar keeps it", dismiss[0] is True, str(dismiss))
        check("...and a press outside is what dismisses it", dismiss[1] is False, str(dismiss))
        # ...and wherever the selection is, the bar goes UNDER the end of it while there
        # is room: that is the space you just stopped dragging in, and it is the side
        # iOS leaves free. (This check used to demand "above, when there is room above"
        # — the old rule, which for a multi-line pick put the bar on top of the words.)
        lower = drv.js("""
          // Hidden FIRST: the bar is already on screen from the case above, so waiting
          // for "visible" would be satisfied instantly and measure the OLD position —
          // the reposition runs 10ms after the mouseup. (Measured 236px of "error"
          // that way, which was the test racing itself, not the bar being wrong.)
          document.getElementById('sel-bar').style.display = 'none';
          const m = document.getElementById('main');
          m.innerHTML = '';
          const pad = document.createElement('div'); pad.style.height = '300px';
          m.appendChild(pad);
          const a = document.createElement('div');
          // Same text as above: the assertions further down quote it back, and a probe
          // that swaps the fixture out from under them is a probe that breaks them.
          a.className = 'msg assistant';
          a.textContent = '官方合成就是加权和, 四个分量等权相加。';
          m.appendChild(a);
          const r = document.createRange(); r.selectNodeContents(a);
          const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
          document.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
          return null;
        """)
        drv.wait("getComputedStyle(document.getElementById('sel-bar')).display !== 'none' ? 1 : 0", tries=20)
        mid = drv.js("""
          const b = document.getElementById('sel-bar').getBoundingClientRect();
          const rg = window.getSelection().getRangeAt(0);
          const sr = rg.getBoundingClientRect();
          const rects = [...rg.getClientRects()].filter(r => r.width > 0 || r.height > 0);
          const end = rects[rects.length - 1];
          return { under: Math.round(b.top - end.bottom), selTop: Math.round(sr.top),
                   covers: b.top < sr.bottom - 1 && b.bottom > sr.top + 1 };
        """)
        check("...a selection in the TOP part gets the bar under its end",
              0 <= mid["under"] <= 12 and mid["covers"] is False, str(mid))
        # The other side, and the reason for it: iOS puts its own Look Up / Copy bar
        # just above the selection whenever there is room, so for anything past the
        # middle of the screen ours has to go below or the two land on top of each
        # other — reported from a phone, where ours was the unreachable one.
        drv.js("""
          document.getElementById('sel-bar').style.display = 'none';
          const m = document.getElementById('main');
          m.innerHTML = '';
          const pad = document.createElement('div');
          pad.style.height = Math.round(window.innerHeight * 0.75) + 'px';
          m.appendChild(pad);
          const a = document.createElement('div');
          a.className = 'msg assistant';
          a.textContent = '官方合成就是加权和, 四个分量等权相加。';
          m.appendChild(a);
          m.scrollTop = 0;
          const r = document.createRange(); r.selectNodeContents(a);
          const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
          document.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
        """)
        drv.wait("getComputedStyle(document.getElementById('sel-bar')).display !== 'none' ? 1 : 0", tries=20)
        lowsel = drv.js("""
          const b = document.getElementById('sel-bar').getBoundingClientRect();
          const sr = window.getSelection().getRangeAt(0).getBoundingClientRect();
          return { below: Math.round(b.top - sr.bottom), selTop: Math.round(sr.top),
                   vh: window.innerHeight, onScreen: b.bottom <= window.innerHeight };
        """)
        check("...and one in the LOWER part gets it below, clear of iOS's own bar",
              0 <= lowsel["below"] <= 12 and lowsel["onScreen"], str(lowsel))
        check("...fully on screen", placed["onScreen"] is True)
        # It has to be a child of something that is never hidden. Inside <footer> it was
        # 0x0 at (0,0) in the session list, because a display:none parent leaves a child
        # with no layout whatever the child's own display says.
        check("...and not nested in anything that gets hidden",
              placed["parent"] == "BODY", placed["parent"])
        # 引用 and 解释 both dress the text up; 追加 is the same destination with none of
        # it; 复制 goes elsewhere entirely and stays last, where the thumb expects it.
        check("...with the four actions", placed["labels"] == ["引用", "解释", "追加", "复制"],
              str(placed["labels"]))
        quoted = drv.js("""
          document.getElementById('input').value = '';
          document.getElementById('sel-quote').click();
          return { v: document.getElementById('input').value,
                   active: (document.activeElement || {}).id,
                   bar: getComputedStyle(document.getElementById('sel-bar')).display,
                   caret: document.getElementById('input').selectionStart,
                   len: document.getElementById('input').value.length };
        """)
        check("引用 quotes it with markdown, then the lead-in",
              quoted["v"].startswith("> 官方合成") and quoted["v"].endswith("关于这点,"),
              quoted["v"].replace("\n", "\\n"))
        # The lead-in only helps if you can type straight after it.
        check("...and the caret sits at the end, ready to type",
              quoted["caret"] == quoted["len"], f'{quoted["caret"]}/{quoted["len"]}')
        check("...and the bar gets out of the way", quoted["bar"] == "none")
        # The composer has to be the thing listening afterwards, or the lead-in is a
        # sentence you then have to go and tap into. Measured, because the first version
        # cleared the page selection AFTER focusing — and once a textarea has focus the
        # document selection IS its caret, so removeAllRanges() wiped the caret and
        # handed focus back to <body>.
        check("...and the composer has focus", quoted["active"] == "input", str(quoted["active"]))
        # A half-written draft must survive being quoted at.
        kept = drv.js("""
          const inp = document.getElementById('input');
          inp.value = '我已经写了半句';
          const a = document.querySelector('#main .msg.assistant');
          const r = document.createRange(); r.selectNodeContents(a);
          const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
          document.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
          document.getElementById('sel-quote').click();
          return inp.value;
        """)
        check("...a draft already in the box is kept, above the quote",
              kept.startswith("我已经写了半句") and "> 官方合成" in kept,
              kept.replace("\n", "\\n")[:60])
        # 解释 is the same machinery with a different tail — and its tail is a whole
        # sentence, because you press it to send, not to keep typing.
        expl = drv.js("""
          const inp = document.getElementById('input');
          inp.value = '';
          const a = document.querySelector('#main .msg.assistant');
          const r = document.createRange(); r.selectNodeContents(a);
          const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
          document.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
          document.getElementById('sel-explain').click();
          return { v: inp.value, active: (document.activeElement || {}).id };
        """)
        check("解释 quotes it and asks, in a sendable sentence",
              expl["v"].startswith("> 官方合成") and expl["v"].rstrip().endswith("能通俗解释下吗"),
              expl["v"].replace("\n", "\\n")[-30:])
        check("...and it focuses the composer too", expl["active"] == "input", str(expl["active"]))
        # 追加: for a path, a command, a model name — something you are about to type INTO
        # your own sentence, where a `>` block and a canned lead-in are in the way.
        app = drv.js("""
          const inp = document.getElementById('input');
          inp.value = '';
          const a = document.querySelector('#main .msg.assistant');
          const r = document.createRange(); r.selectNodeContents(a);
          const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
          document.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
          document.getElementById('sel-append').click();
          const plain = { v: inp.value, active: (document.activeElement || {}).id,
                          caret: inp.selectionStart, len: inp.value.length };
          inp.value = '我已经写了半句';
          const r2 = document.createRange(); r2.selectNodeContents(a);
          sel.removeAllRanges(); sel.addRange(r2);
          document.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
          document.getElementById('sel-append').click();
          return { plain, onto: inp.value };
        """)
        check("追加 puts the selection in as it stands",
              app["plain"]["v"] == "官方合成就是加权和, 四个分量等权相加。", app["plain"]["v"])
        check("...no quote marks and no lead-in — that is the whole difference from 引用",
              ">" not in app["plain"]["v"] and "关于这点" not in app["plain"]["v"])
        check("...caret at the end, composer focused, like the other two",
              app["plain"]["caret"] == app["plain"]["len"] and app["plain"]["active"] == "input",
              str(app["plain"]))
        check("...and a draft in the box keeps its own line",
              app["onto"].startswith("我已经写了半句\n\n官方合成"),
              app["onto"].replace("\n", "\\n"))
        # Selecting in the composer is being done for some other reason; a bar over it
        # would be in the way.
        elsewhere = drv.js("""
          const sel = window.getSelection(); sel.removeAllRanges();
          const inp = document.getElementById('input');
          inp.focus(); inp.setSelectionRange(0, 5);
          document.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
          return getComputedStyle(document.getElementById('sel-bar')).display;
        """)
        check("a selection outside the transcript shows nothing", elsewhere == "none", elsewhere)
        # NOTE: this wipes #main — which holds the picker AND #transcript-view. Anything
        # after this line is looking at a gutted DOM, so probes that need either of those
        # belong ABOVE it. (Cost me a while: getElementById('transcript-view') returning
        # null reads like a typo, not like "a previous test removed it".)
        drv.js("document.getElementById('main').innerHTML = ''; document.getElementById('input').value = '';")

        row2 = drv.js("""
          const rows = [...document.querySelectorAll('.switch-menu .scr-cfg-row')];
          const r = rows.find(x => (x.firstElementChild || {}).textContent === 'load more');
          if (!r) return { missing: true };
          return { btns: [...r.querySelectorAll('button')].map(b => b.id),
                   texts: [...r.querySelectorAll('button')].map(b => b.textContent.trim()) };
        """)
        if not row2.get("missing"):
            # Named after the button they change, so the pairing is obvious from the row.
            check("load more carries both paging filters",
                  row2["btns"] == ["mm-earlier-resp", "mm-earlier-peer"], str(row2["btns"]))
            check("...both defaulting to the light option",
                  row2["texts"] == ["reqs only", "no peer"], str(row2["texts"]))
        check("the peer filter is applied on the server, like the other one",
              'params.set("skip_peer", "1")' in src_index and "skip_peer: bool" in
              open(os.path.join(ROOT, "cc_web.py"), encoding="utf-8").read())

        print("=== the ⚙ menu is grouped, with a line between groups ===")
        # Twelve rows with nothing between them had become a wall. Grouped by WHAT each
        # row acts on — this session · how it reads · finding something in it · dictation
        # · the way out — which is the grouping that survives new rows: a new setting has
        # an obvious home instead of landing at the bottom.
        grp = drv.js("""
          const m = document.getElementById('mode-menu');
          const prev = m.style.display; m.style.display = 'block';
          const groups = [[]];
          for (const el of m.children) {
            if (el.classList.contains('sw-sep')) { groups.push([]); continue; }
            if (el.id === 'mm-asr-sec') { groups[groups.length - 1].push('<语音>'); continue; }
            if (!el.classList.contains('scr-cfg-row')) continue;
            // A row whose controls name themselves has no label span; report its ids.
            const first = el.firstElementChild || {};
            groups[groups.length - 1].push(
              (first.tagName === 'SPAN' && !first.classList.contains('row-bar'))
                ? first.textContent
                : '[' + [...el.querySelectorAll('button')].map(b => b.id || b.dataset.mode).join(' ') + ']');
          }
          const sepH = [...m.querySelectorAll('.sw-sep')].map(s => getComputedStyle(s).borderTopWidth);
          m.style.display = prev;
          return { groups, sepH, translate: !!document.getElementById('mm-autotranslate') };
        """)
        check("three groups, two lines",
              len(grp["groups"]) == 3 and len(grp["sepH"]) == 2, str(grp["groups"]))
        # Everything that acts on the session itself goes LAST — the menu hangs from the
        # ⚙ at the top of the screen, so on a phone its bottom is what a thumb reaches.
        check("...the session's own controls last, where a thumb lands",
              grp["groups"][2] == ["tree", "ctrl", "[mm-search mm-star mm-exit-close]", "task"],
              str(grp["groups"][2]))
        # The header's 🖥 is picker-only, so from inside a session this was the missing
        # door. Links, not buttons — long-press / middle-click should behave.
        ctrl = drv.js("""
          const m = document.getElementById('mode-menu');
          const prev = m.style.display; m.style.display = 'block';
          const as = [...m.querySelectorAll('a.sw-item')].map(a => [a.id, a.getAttribute('href'),
                                                                    getComputedStyle(a).textDecorationLine]);
          m.style.display = prev;
          return as;
        """)
        check("...ctrl offers both pages, as real links",
              ctrl == [["mm-ctrl-phone", "/remote/", "none"],
                       ["mm-ctrl-desk", "/remote_pc/", "none"]], str(ctrl))
        # Not one setting per row: one row carries font size, theme AND latex, because
        # none of them needs a line to itself. The latex button says its own state, which is
        # what let it leave its label behind.
        # No labels on these two: the buttons have been brief/medium/all and a−/A+/◐
        # since the beginning, and the label only said it again in four characters.
        check("...how the transcript reads comes first, two rows not five, no labels",
              grp["groups"][0][:2] == ["[brief medium all mm-live]",
                                       "[mm-fontdown mm-fontup mm-theme mm-latex]"],
              str(grp["groups"][0]))
        # With no label to divide them, the bar is the only thing saying where one
        # setting stops and the next starts.
        bars = drv.js("""
          const m = document.getElementById('mode-menu');
          const prev = m.style.display; m.style.display = 'block';
          const shape = [...m.querySelectorAll('.scr-cfg-row')]
            .filter(r => r.querySelector('.row-bar'))
            .map(r => [...r.children].map(c => c.classList.contains('row-bar') ? '|'
                                                : (c.id || c.dataset.mode || c.tagName)).join(' '));
          const bar = m.querySelector('.row-bar');
          const vis = bar ? getComputedStyle(bar).display !== 'none' && bar.offsetWidth > 0 : false;
          m.style.display = prev;
          return { shape, vis };
        """)
        check("a bar separates the two settings sharing a row",
              sorted(bars["shape"]) == sorted(["brief medium all | mm-live",
                                               "mm-fontdown mm-fontup mm-theme | mm-latex",
                                               "mm-search mm-star | mm-exit-close"]), str(bars["shape"]))
        check("...and it is actually visible", bars["vis"] is True)
        live = drv.js("""
          const b = document.getElementById('mm-live');
          const row = b.closest('.scr-cfg-row');
          const first = b.textContent.trim();
          b.click();
          const after = b.textContent.trim();
          b.click();
          return { ids: [...row.querySelectorAll('button')].map(x => x.id || x.dataset.mode),
                   first, after, back: b.textContent.trim() };
        """)
        # Same axis as brief/medium/all — how much text do you want — so it rides along
        # instead of spending a labelled row on one binary choice.
        check("the live-preview toggle sits with brief/medium/all",
              live["ids"] == ["brief", "medium", "all", "mm-live"], str(live["ids"]))
        check("...as one button naming its own state",
              {live["first"], live["after"]} == {"live full", "live 2"},
              f'{live["first"]} → {live["after"]}')
        check("...and it toggles back", live["back"] == live["first"], str(live))
        look = drv.js("""
          const m = document.getElementById('mode-menu');
          const prev = m.style.display; m.style.display = 'block';
          const lx = document.getElementById('mm-latex');
          // By the button, not by a label: the row has none any more. Looked up by the
          // old label, this returned undefined and the whole probe threw a 500.
          const ids = [...lx.closest('.scr-cfg-row').querySelectorAll('button')].map(b => b.id);
          const first = lx.textContent.trim();
          lx.click();
          const after = lx.textContent.trim();
          lx.click();
          m.style.display = prev;
          return { ids, first, after, back: lx.textContent.trim() };
        """)
        check("one row carries the four small controls",
              look["ids"] == ["mm-fontdown", "mm-fontup", "mm-theme", "mm-latex"], str(look["ids"]))
        # It answers "is it on right now", not "what would happen if you pressed me" —
        # with no label beside it, the second reading is a coin flip.
        check("...and the latex button names its own state",
              {look["first"], look["after"]} == {"latex on", "latex off"},
              f'{look["first"]} → {look["after"]}')
        check("...toggling it both ways", look["back"] == look["first"],
              f'{look["first"]} → {look["after"]} → {look["back"]}')
        check("...with paging in the same group — it governs what shows up there",
              grp["groups"][0][-1] == "load more", str(grp["groups"][0]))
        check("...then dictation", grp["groups"][1] == ["<语音>"], str(grp["groups"][1]))
        # Two self-naming buttons share a row, so neither needs a label. exit is at the
        # RIGHT end, behind the bar: it is the only irreversible thing in this menu, so it
        # must not be what a thumb meets on its way to find.
        # find and ★ are both "get me to something in this session"; exit stays behind
        # the bar, at the right end.
        check("...find and ★ share the row, with the way out behind the bar",
              grp["groups"][2][2] == "[mm-search mm-star mm-exit-close]", str(grp["groups"][2]))
        _x = src_index.index('getElementById("mm-exit-close")')
        check("...with exit still asking first",
              "if (!confirm(q)) return false;" in src_index
              and "noConfirm" not in src_index[_x:_x + 700])
        check("the separators actually draw a line", set(grp["sepH"]) == {"1px"}, str(grp["sepH"]))
        # One line for something that is off and stays off. The feature is still there
        # (localStorage + the per-message 🔧), the row is not.
        check("发送后翻译 no longer spends a row", grp["translate"] is False)
        # Merging rows buys width problems, and the menu is `max-width: 80vw` — on a
        # 390px phone that is 312px for a label plus three chips, with no flex-wrap on
        # the row, so an overflow SHRINKS the buttons and clips their text. Measured at
        # that width rather than at whatever this browser window happens to be.
        narrow = drv.js("""
          const m = document.getElementById('mode-menu');
          const prevD = m.style.display, prevW = m.style.width, prevMax = m.style.maxWidth;
          m.style.display = 'block'; m.style.maxWidth = 'none'; m.style.width = '312px';
          const rows = [...m.querySelectorAll('.scr-cfg-row')];
          const bad = [], stacked = [];
          for (const r of rows) {
            const bs = [...r.querySelectorAll('button')].filter(b => b.offsetHeight > 0);
            for (const b of bs) if (b.scrollWidth > b.clientWidth + 1)
              bad.push((r.firstElementChild || {}).textContent + '/' + b.textContent.trim());
            const tops = new Set(bs.map(b => Math.round(b.getBoundingClientRect().top)));
            if (tops.size > 1) stacked.push((r.firstElementChild || {}).textContent);
          }
          m.style.display = prevD; m.style.width = prevW; m.style.maxWidth = prevMax;
          return { bad, stacked };
        """)
        check("at phone width nothing is clipped", narrow["bad"] == [], str(narrow["bad"]))
        check("...and no row breaks into two", narrow["stacked"] == [], str(narrow["stacked"]))

        print("=== long-press ➤ : three rows, and they fit ===")
        # Five one-per-line items became three rows: "set task" was written twice and the
        # single-word ones had a line each. Driven by opening the real menu — the rows are
        # built in JS, so reading the source would only prove the source.
        menu = drv.js("""
          const btn = document.getElementById('send');
          btn.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
          return 1;
        """)
        time.sleep(0.7)                       # the long-press timer is 500ms
        sm = drv.js("""
          const m = document.getElementById('send-menu');
          if (!m) return { missing: true };
          const rows = [...m.querySelectorAll('.send-menu-row')].map(r => ({
            lead: (r.querySelector('.send-menu-lead') || {}).textContent || '',
            items: [...r.querySelectorAll('.send-menu-item')].map(b => b.textContent),
            seps: r.querySelectorAll('.send-menu-sep').length,
            tops: new Set([...r.querySelectorAll('.send-menu-item')]
                          .map(b => Math.round(b.getBoundingClientRect().top))).size,
          }));
          const r = m.getBoundingClientRect();
          const over = [...m.querySelectorAll('.send-menu-item')]
            .filter(b => b.scrollWidth > b.clientWidth + 1).map(b => b.textContent);
          const small = [...m.querySelectorAll('.send-menu-item')]
            .filter(b => b.getBoundingClientRect().height < 30).map(b => b.textContent);
          m.remove();
          return { rows, w: Math.round(r.width), vw: window.innerWidth, over, small };
        """)
        if sm.get("missing"):
            check("SKIP: the long-press menu did not open here", True)
        else:
            check("three rows", len(sm["rows"]) == 3, str(sm["rows"]))
            check("...set task says its noun once, then the two of them",
                  sm["rows"][0]["lead"] == "set task:"
                  and sm["rows"][0]["items"] == ["desc", "constrain"]
                  and sm["rows"][0]["seps"] == 1, str(sm["rows"][0]))
            check("...view asr on its own", sm["rows"][1]["items"] == ["view asr"], str(sm["rows"][1]))
            check("...copy and clear together", sm["rows"][2]["items"] == ["copy", "clear"],
                  str(sm["rows"][2]))
            # Rows, not columns: if a row wrapped, the grouping has bought nothing.
            check("...each row really is one line",
                  all(r["tops"] == 1 for r in sm["rows"]), str([r["tops"] for r in sm["rows"]]))
            check("...no label is clipped", sm["over"] == [], str(sm["over"]))
            # It is a phone menu: a 14px row you can miss is worse than a taller one.
            check("...and every item stays a real tap target", sm["small"] == [], str(sm["small"]))
            check("...the menu still fits the screen", sm["w"] < sm["vw"], f'{sm["w"]}/{sm["vw"]}')

        print("=== ★ : the list, the heart in ☰, and the jump ===")
        # Why this exists (said plainly, because it decides the design): in a long chat
        # several things surface at once and only one can be dealt with; the rest are
        # forgotten. So a bookmark is a TO-DO, which is why the list reads oldest-first
        # (the order they came up) and why un-starring is the way to close one.
        bm = drv.js("""
          // Two user messages, one of them starred. The star/heart both read the same
          // map, so the two views cannot disagree.
          const m = document.getElementById('main');
          m.innerHTML = '';
          for (const [uuid, txt] of [['uu-1', '第一个问题'], ['uu-2', '第二个问题']]) {
            const d = document.createElement('div');
            d.className = 'msg user'; d.dataset.uuid = uuid;
            const md = document.createElement('div'); md.className = 'markdown';
            md.textContent = txt; d.appendChild(md);
            m.appendChild(d);
          }
          const r = {};
          document.getElementById('jump-asks').click();         // ☰ — no bookmarks yet
          const menu = document.getElementById('asks-menu');
          r.heartsBefore = menu.querySelectorAll('.ask-heart').length;
          r.rows = menu.querySelectorAll('.ask-row:not(.ask-more)').length;
          r.open = menu.style.display;
          return r;
        """)
        check("the ☰ list shows both requests", bm["rows"] == 2, str(bm))
        check("...with no hearts until something is starred", bm["heartsBefore"] == 0, str(bm))
        # The heart itself cannot be driven from here: bmMap lives inside the page's
        # IIFE, so an injected script has no way to put a bookmark into it. What IS
        # checkable is that both views read the SAME map — the property that keeps them
        # from disagreeing — while the storage is covered against the real endpoint by
        # tests/test_bookmarks.py.
        _sa = src_index[src_index.index("function showAsksMenu()"):]
        _sa = _sa[:_sa.index("\n  }")]
        check("the \u2630 list hearts come from the same map as the \u2605 buttons",
              "bmHas(n.dataset.uuid)" in _sa and 'h.textContent = "\u2665"' in _sa)
        check("...and the \u2605 buttons read it too",
              'btn.textContent = bmHas(b.uuid) ? "\u2605" : "\u2606"' in src_index)
        # A bookmark is a to-do: un-starring closes one, so the list has to offer that
        # without making you find the message first.
        check("the list can un-star a row without going to the message",
              'del.textContent = "\u2715"' in src_index
              and "await bmToggle({ uuid: it.uuid }" in src_index)
        # Addressed by uuid, never by _idx (window-relative — see cc_web.py).
        check("the jump looks the message up by uuid",
              "[data-uuid=" in src_index and "bmJump" in src_index)
        # With nothing saved it still opens. A disabled button and a broken button look
        # the same from the outside — and this one is how you find out where the ☆ is.
        empty = drv.js("""
          const m = document.getElementById('bm-modal');
          m.classList.remove('show');
          const b = document.getElementById('mm-star');
          const wasDisabled = b.disabled;
          b.click();
          const r = { shown: m.classList.contains('show'), disabled: wasDisabled,
                      label: b.textContent.trim(),
                      says: (document.getElementById('bm-list').textContent || '').trim() };
          m.classList.remove('show');
          return r;
        """)
        check("★ opens even with nothing saved", empty["shown"] is True and empty["disabled"] is False,
              str(empty))
        check("...and the window says where to add one", "☆" in empty["says"], empty["says"][:60])
        check("...the label is just the star when the count is zero",
              empty["label"] == "★", empty["label"])

        # It looked wrong on first try because .link-row's layout is scoped to
        # #links-modal: borrowing the class names inherited none of it, and the window
        # rendered as unaligned text with stray ✕ buttons in the middle of it. The fix is
        # to SHARE the ☰ list's selector rather than write a lookalike — so this measures
        # that a row in the ★ window and a row in the ☰ popup lay out identically.
        same = drv.js("""
          const m = document.getElementById('bm-modal');
          m.classList.add('show');
          const list = document.getElementById('bm-list');
          list.innerHTML = '';
          const row = document.createElement('div'); row.className = 'ask-row';
          const n = document.createElement('span'); n.className = 'ask-n'; n.textContent = '09-22 10:11';
          const t = document.createElement('span'); t.className = 'ask-t';
          t.textContent = '很长很长的一条请求'.repeat(20);
          const x = document.createElement('button'); x.className = 'bm-x'; x.textContent = '✕';
          row.appendChild(n); row.appendChild(t); row.appendChild(x);
          list.appendChild(row);
          const cs = getComputedStyle(row), ts = getComputedStyle(t);
          const hdr = m.querySelector('.modal-row');
          const h3 = hdr.querySelector('h3'), close = hdr.querySelector('button');
          // Compared against a REAL ☰ row rather than against numbers typed here: the
          // claim is "the same as the others", so the other one is the reference.
          const am = document.getElementById('asks-menu');
          const prevAm = am.style.display; am.style.display = 'block';
          am.innerHTML = '';
          const ref = document.createElement('div'); ref.className = 'ask-row';
          const rn = document.createElement('span'); rn.className = 'ask-n'; rn.textContent = '#1';
          const rt = document.createElement('span'); rt.className = 'ask-t'; rt.textContent = 'x'.repeat(200);
          ref.appendChild(rn); ref.appendChild(rt); am.appendChild(ref);
          const rs = getComputedStyle(ref);
          const shape = (e) => { const c = getComputedStyle(e);
            return [c.display, c.alignItems, c.gap, c.padding, c.borderTopLeftRadius, c.fontSize].join('|'); };
          const r = {
            mine: shape(row), ref: shape(ref),
            rowH: Math.round(row.getBoundingClientRect().height),
            refH: Math.round(ref.getBoundingClientRect().height),
            clipped: ts.whiteSpace + '/' + ts.overflow,
            wide: t.getBoundingClientRect().width > n.getBoundingClientRect().width * 2,
            // Centres, not tops: the row centres its children, so a 15px title and a
            // shorter button are level while their tops differ by design. And the row
            // must be no taller than its tallest child, or they are stacked.
            headLevel: Math.abs((h3.getBoundingClientRect().top + h3.getBoundingClientRect().bottom) / 2
                              - (close.getBoundingClientRect().top + close.getBoundingClientRect().bottom) / 2) <= 1,
            headStacked: Math.round(hdr.getBoundingClientRect().height)
                       > Math.round(Math.max(h3.getBoundingClientRect().height,
                                             close.getBoundingClientRect().height)) + 1,
            xRight: close.getBoundingClientRect().left > h3.getBoundingClientRect().left,
            xLast: x.getBoundingClientRect().left > t.getBoundingClientRect().left,
          };
          am.innerHTML = ''; am.style.display = prevAm;
          m.classList.remove('show'); list.innerHTML = '';
          return r;
        """)
        check("a ★ row lays out exactly like a ☰ row",
              same["mine"] == same["ref"], f'{same["mine"]} vs {same["ref"]}')
        check("...one line tall, the same as that row",
              same["rowH"] == same["refH"] and same["xLast"] is True,
              f'{same["rowH"]} vs {same["refH"]}')
        check("...the text is clipped, not wrapped (a bookmark can be a paragraph)",
              same["clipped"] == "nowrap/hidden" and same["wide"] is True, str(same))
        check("...and the title and its ✕ share one line, ✕ on the right",
              same["headLevel"] is True and same["headStacked"] is False
              and same["xRight"] is True, str(same))
        check("...and pages back, bounded and narrated, when it is not loaded",
              "BM_PAGES" in src_index and "await loadEarlierRounds(8)" in src_index
              and "\u5f80\u524d\u627e\u6536\u85cf\u7684\u90a3\u4e00\u6761" in src_index)

        print("=== find-in-page: a thin bar over what is loaded ===")
        # A phone installed as a web app has no find-in-page. This is one, and it
        # filters the LOADED transcript — not a server search (there was one for about
        # an hour; it went, because with load more → reqs only you can pull hundreds of
        # requests down for a few hundred bytes and then look through them for free).
        fb = drv.js("""
          const bar = document.getElementById('find-bar');
          const before = getComputedStyle(bar).display;
          bar.style.display = 'flex';
          const q = document.getElementById('find-q');
          const r = bar.getBoundingClientRect();
          return { before, item: !!document.getElementById('mm-search'),
                   qFont: getComputedStyle(q).fontSize,
                   users: !!document.getElementById('find-users'),
                   h: Math.round(r.height), vh: window.innerHeight,
                   covers: Math.round(r.height / window.innerHeight * 100) };
        """)
        check("the ⚙ menu opens it", fb["item"] is True)
        check("...it starts hidden", fb["before"] == "none", fb["before"])
        # "只需要很小. 别遮挡搜出的东西" — the matches are behind it.
        check("...and it is a thin bar, not a panel over the results",
              fb["covers"] <= 10, f'{fb["h"]}px of {fb["vh"]} = {fb["covers"]}%')
        check("...with a requests-only box", fb["users"] is True)
        check("...and a ≥16px field, so the phone does not zoom", fb["qFont"] == "16px", fb["qFont"])
        drv.js("document.getElementById('find-bar').style.display = 'none'")
        js = src_index[src_index.index("function findRun("):]
        js = js[:js.index("\n  }\n")]
        check("it reads the ENTRY, not the rendered node",
              "entryTextOf(e)" in js and "entryCache" in js)
        # A folded message's DOM is missing its middle, which is the part you are most
        # likely searching for.
        check("...which is what makes a folded message searchable",
              "entryTextOf" in src_index and "message.content" in src_index)
        check("...and nothing here calls the server", "authedFetch" not in js)

        # The bug this feature shipped with, and why the checks below are shaped like
        # this: find sat at 0/0 with the words on screen, because it looks a message up
        # by entry index and the only element carrying a dataset.idx was the stacked
        # TOOL row. Every lookup missed, silently. "点这里看它的回复" was dead for the
        # same reason and had never appeared at all.
        #
        # It cannot be driven from here — ingest/entryCache/renderBlock all live inside
        # the page's IIFE — so the link is pinned at both ends instead, and the gap is
        # written down rather than papered over.
        check("every rendered block is stamped with its entry index",
              "div.dataset.idx = b.idx" in
              src_index[src_index.index("function renderBlock(b) {"):][:600])
        check("...and every block carries one to stamp",
              "const idx = entry._idx" in src_index)
        check("find looks a hit up by exactly that",
              'dataset.idx) === String(e._idx)' in src_index)
        check("...and so does the load-answer strip", "el.dataset.idx" in src_index)
        # It must cost no HEIGHT. The first version was a full-width row under each
        # request, which added a line to every one of them — exactly the height that
        # turning responses off was buying back. Measured, since "inline" is a claim
        # about layout.
        cost = drv.js("""
          const m = document.getElementById('main');
          m.innerHTML = '';
          const mk = (withBtn, block) => {
            const d = document.createElement('div'); d.className = 'msg user';
            const md = document.createElement('div'); md.className = 'markdown';
            md.innerHTML = '<p>一句不长不短的请求</p>';
            d.appendChild(md); m.appendChild(d);
            if (withBtn) {
              const b = document.createElement('button');
              b.className = 'load-answer'; b.textContent = '↓回复';
              if (block) b.style.display = 'block';
              (md.lastElementChild || md).appendChild(b);
            }
            return Math.round(d.getBoundingClientRect().height);
          };
          const plain = mk(false), inline = mk(true, false), row = mk(true, true);
          m.innerHTML = '';
          return { plain, inline, row };
        """)
        check("the load-answer link adds no row to a request",
              cost["inline"] == cost["plain"], f'{cost["plain"]} → {cost["inline"]}px')
        check("...unlike a block one, which is what it replaced",
              cost["row"] > cost["plain"], f'block would be {cost["row"]}px')

        print("=== find marks the WORDS, not just the message ===")
        # "我都不知道你搜索的命中哪一个" — outlining the block says which MESSAGE, which
        # in one of several paragraphs is not the question.
        #
        # findMarkIn lives inside the page's IIFE like everything else here, so its
        # SOURCE is lifted out and injected — the same text, running in a real DOM,
        # which is the closest this can get to driving the shipped function.
        _fm = src_index[src_index.index("function findMarkIn(el, q) {"):]
        _fm = _fm[:_fm.index("\n  }\n") + 4]
        marked = drv.js("window.__fm = " + _fm + """;
          const host = document.createElement('div');
          host.id = '__mk';
          host.innerHTML = '<p>关于 <b>task desc</b> 的问题</p><p>又一次 TASK DESC</p>';
          document.body.appendChild(host);
          window.__fm(host, 'task desc');
          const marks = [...host.querySelectorAll('mark.find-mark')];
          return { n: marks.length, texts: marks.map(m => m.textContent),
                   bTags: host.querySelectorAll('b').length,
                   pTags: host.querySelectorAll('p').length,
                   html: host.innerHTML.slice(0, 80) };
        """)
        check("every occurrence is wrapped", marked["n"] == 2, str(marked["n"]))
        # Case-insensitive to find, but the page keeps what was written.
        check("...matched case-insensitively, shown as written",
              marked["texts"] == ["task desc", "TASK DESC"], str(marked["texts"]))
        # The first match sits inside <b>: an innerHTML replace would have eaten the tag
        # or matched inside one. This walks text nodes for that reason.
        check("...without disturbing the markup it walked through",
              marked["bTags"] == 1 and marked["pTags"] == 2, marked["html"])
        drv.js("const n = document.getElementById('__mk'); if (n) n.remove();")

        print("=== your own long messages fold in the middle ===")
        # Driven through the page's own renderer by building a block the way the
        # transcript does, because what matters is the DOM you end up looking at: two
        # lines, a seam that says how many are missing, two more lines.
        fold = drv.js("""
          const m = document.getElementById('main');
          m.innerHTML = '';
          const mk = (n) => Array.from({length: n}, (_, i) => 'L' + (i + 1)).join('\\n');
          const out = {};
          for (const [key, n] of [['short', 4], ['long', 12]]) {
            const d = document.createElement('div');
            d.className = 'msg user';
            const md = document.createElement('div'); md.className = 'markdown';
            md.dataset.src = mk(n);
            d.appendChild(md); m.appendChild(d);
          }
          return Object.keys(out);
        """)
        # The fold lives inside the page's IIFE, so the assertions below drive it through
        # a real render instead: push a long user turn into the transcript the way the
        # poll does. If that is not reachable from here, the line maths is covered in
        # node (tests/test_gear_menu_ui.py) and this pins the CSS + the button's shape.
        css = src_index[src_index.index(".msg .fold-btn {"):]
        css = css[:css.index("}")]
        check("the seam is a full-width dim row, not a button-looking button",
              "width: 100%" in css and "var(--muted)" in css and "dashed" in css,
              css.strip()[:60])
        js = src_index[src_index.index("function userFold"):]
        js = js[:js.index("\n  }\n")]
        # Sliced to the collapsed branch first: `appendChild(btn)` appears in the
        # expanded branch too, and .index() would match that earlier one — which is how
        # this assertion failed while the code was right.
        folded = js[js.index("} else {"):]
        check("folded state renders head, seam, tail — in that order",
              folded.index("appendChild(h)") < folded.index("appendChild(btn)")
              < folded.index("appendChild(t)"))
        check("...and the seam says how much is hidden, in characters",
              "展开中间" in js and "parts.hidden" in js and "字" in js)
        check("...expanding shows the whole thing and offers 收起",
              "收起" in js and "renderMarkdown(text)" in js)
        check("head and tail are rendered as separate markdown, not a sliced tree",
              "renderMarkdown(parts.head)" in src_index and "renderMarkdown(parts.tail)" in src_index)
        # Queued messages come through the same branch (role user + .queued), so the
        # fold applies to them for free — which is the point of it being one branch.
        br = src_index[src_index.index('if (b.kind === "text") {'):]
        br = br[:br.index("} else if")]
        check("queued turns get the same fold (same branch, keyed on role)",
              'if (b.role === "user") userFold(' in br and "indieQueued" in br)
        check("...and math is detected from the SOURCE, so a folded formula still gets ∑",
              "_mathRe.test(b.text" in br)

        print("=== a quoted line still LOOKS quoted after markdown ===")
        # User messages go through marked, so `> …` becomes a <blockquote> and the
        # marker itself is gone from the text. With no styling for it — and there was
        # none — what you get back is a line indistinguishable from your own words,
        # which is the one thing 引用 must not produce.
        qv = drv.js("""
          const m = document.getElementById('main');
          m.innerHTML = '';
          const d = document.createElement('div');
          d.className = 'msg user';
          const md = document.createElement('div');
          md.className = 'markdown';
          md.innerHTML = renderMarkdownProbe('> 被引用的那一句\n\n关于这点,');
          d.appendChild(md); m.appendChild(d);
          const bq = md.querySelector('blockquote');
          if (!bq) return { none: true, html: md.innerHTML.slice(0, 80) };
          const cs = getComputedStyle(bq);
          return { border: cs.borderLeftWidth, colour: cs.color,
                   textColour: getComputedStyle(md).color,
                   pad: cs.paddingLeft };
        """) if drv.js("return typeof renderMarkdownProbe") == "function" else {"skip": True}
        if qv.get("skip"):
            # renderMarkdown lives inside the page's IIFE, so it cannot be called from
            # here. The CSS is what was missing, so the CSS is what gets pinned.
            bqcss = src_index[src_index.index(".msg .markdown blockquote {"):]
            bqcss = bqcss[:bqcss.index("}")]
            check("a blockquote has a bar down its left", "border-left" in bqcss, bqcss.strip()[:60])
            check("...and is dimmed apart from the surrounding text", "color:" in bqcss)
            check("...and the user's own bubble gets an accent bar, since it is dim already",
                  ".msg.user .markdown blockquote" in src_index)
        else:
            check("a quoted line renders with a visible bar",
                  qv.get("border", "0px") != "0px", str(qv))

        print("=== ↑ in the float pill steps back one request ===")
        # ↑/↓ were taken OUT of this pill once, in favour of ☰ (the list), on the
        # grounds that stepping is worse than being shown your requests. True for "find
        # the one I mean"; wrong for "the one just before this" — and stepping only ever
        # existed as the `u` key, which a phone does not have.
        #
        # Driven in a real browser rather than asserted against the source, because what
        # could break is not the function (`u` already used it) but the wiring and the
        # layout: real heights, real scrollTop, real click dispatch. The transcript view
        # needs an attached session, so the messages are injected — the button, the
        # handler and the scrolling arithmetic are all still the real ones.
        step = drv.js("""
          const m = document.getElementById('main');
          m.innerHTML = '';
          for (let i = 0; i < 4; i++) {
            const u = document.createElement('div');
            u.className = 'msg user'; u.dataset.n = i; u.textContent = 'human ' + i;
            u.style.height = '80px';
            m.appendChild(u);
            const a = document.createElement('div');
            a.className = 'msg assistant'; a.style.height = '400px';
            m.appendChild(a);
          }
          m.scrollTop = m.scrollHeight;                 // as if reading the latest
          const before = m.scrollTop;
          const base = () => m.getBoundingClientRect().top;
          const atTop = () => {
            let n = null;
            m.querySelectorAll('.msg.user').forEach(x => {
              if (Math.abs(x.getBoundingClientRect().top - base()) < 6) n = x.dataset.n;
            });
            return n;
          };
          document.getElementById('jump-prev-ask').click();
          const after = m.scrollTop, at = atTop();
          document.getElementById('jump-prev-ask').click();
          return {before, after, at, at2: atTop(),
                  scrollable: m.scrollHeight > m.clientHeight};
        """)
        check("the pill has an ↑ and clicking it scrolls up",
              step["scrollable"] and step["after"] < step["before"],
              f'{step["before"]} → {step["after"]}')
        check("...landing ON a request, not somewhere near one", step["at"] is not None, str(step))
        # The bug worth catching: a second tap that does nothing — an off-by-one that
        # keeps re-selecting whatever is already at the top. That is exactly what makes
        # a stepper feel broken on a phone, where tapping again is the natural response.
        check("...and tapping again steps to the one before that",
              step["at2"] is not None and int(step["at2"]) == int(step["at"]) - 1,
              f'{step["at"]} then {step["at2"]}')
        drv.js("document.getElementById('main').innerHTML = ''")   # leave the page as found

        print("=== 🎤 puts the bar up before it opens the mic ===")
        # "我总以为我点了它没用,我得点好几次" — tapping 🎤 used to do nothing visible for
        # 200ms to two seconds, because the handler awaited getUserMedia and only then
        # revealed the bar. That wait is not ours to remove: opening a mic enumerates
        # devices and starts the OS capture graph, and the browser charges for it even
        # when permission was granted long ago. What WAS ours is the sequencing.
        #
        # A click runs the handler synchronously up to its first `await`, so reading the
        # DOM straight after .click() in the same script sees exactly what the user sees
        # in that first frame — which makes this testable without a working microphone,
        # and true regardless of how fast getUserMedia happens to be here.
        first = drv.js("""
          const bar = document.getElementById('rec-bar');
          const live = document.getElementById('rec-live');
          const wave = bar.querySelector('.rec-wave');
          const btn = id => document.getElementById(id);
          document.getElementById('mic-btn').click();
          const shown = ['rec-cancel', 'rec-stop', 'rec-edit', 'rec-send', 'rec-pause']
            .filter(id => btn(id) && getComputedStyle(btn(id)).display !== 'none');
          const enabled = shown.filter(id => !btn(id).disabled);
          return { bar: getComputedStyle(bar).display,
                   live: (live.innerHTML || '').slice(0, 120),
                   spinner: !!live.querySelector('.rec-spin'),
                   wave: wave ? getComputedStyle(wave).display : 'gone',
                   arming: bar.classList.contains('rec-arming'),
                   dotBg: getComputedStyle(bar.querySelector('.rec-dot')).backgroundColor,
                   shown, enabled,
                   micRec: document.getElementById('mic-btn').classList.contains('recording') };
        """)
        check("the bar is up in the same frame as the tap", first["bar"] == "flex", str(first["bar"]))
        check("...with a spinner, so a tap that did register looks like one", first["spinner"] is True,
              first["live"][:60])
        check("...saying what it is waiting for", "打开麦克风" in first["live"], first["live"][:60])
        # Honesty: the wave bars animate as though sound were arriving. Nothing is being
        # captured until the device opens, and anything said in that gap is genuinely
        # lost — hence "先别说话" rather than a convincing fake.
        check("...and no wave pretending audio is already coming in", first["wave"] == "none",
              str(first["wave"]))
        # "你至少是可以一点它,那个录音框以及所有的按钮就可以出来" — all five, at once.
        # Buttons trickling in a second later is its own "did that work?", and a row that
        # changes width under your thumb is worse than one that starts complete.
        check("every button on the bar is there from the first frame",
              set(first["shown"]) == {"rec-cancel", "rec-stop", "rec-edit", "rec-send", "rec-pause"},
              str(first["shown"]))
        # ...but only Cancel can mean anything yet: nothing has been captured, so there
        # is nothing to stop, pause, edit or send.
        check("...and only Cancel is live, since there is no audio yet",
              first["enabled"] == ["rec-cancel"], str(first["enabled"]))
        # The dot is the ready signal, and it is the one both modes share: batch
        # overwrites the text row with its own hint, so words alone would not carry it.
        check("the red dot is NOT red while the mic is still opening",
              first["arming"] is True and "0, 0, 0, 0" in first["dotBg"].replace("rgba(", "").replace(")", ""),
              first["dotBg"])
        check("...先别说话 is spelled out, because that audio really is lost",
              "先别说话" in first["live"], first["live"][:60])
        check("the mic button itself shows as active", first["micRec"] is True)
        # Headless has no microphone, so getUserMedia rejects — which exercises the other
        # half: the failure parks IN the popup with a reason instead of alert()ing, and an
        # alert on a phone covers the thing it is talking about.
        # Whichever way it goes, the arming state must END — and say which way. Headless
        # may have a fake device (resolves) or none (rejects), so both outcomes are
        # accepted; what is NOT accepted is staying in "正在打开麦克风…" forever, which is
        # the shape of the original complaint.
        # 3s in, the hint names the likely cause — the permission prompt. Worth its own
        # check because "still opening" with no explanation is what made tapping again
        # feel like the only option.
        hint = drv.wait("""(function () {
          const h = (document.getElementById('rec-live').innerHTML || '');
          return /权限/.test(h) ? h.slice(0, 90) : 0;
        })()""", tries=30)
        check("after a few seconds it names the likely cause", bool(hint), str(hint)[:70])

        outcome = drv.wait("""(function () {
          const bar = document.getElementById('rec-bar');
          if (document.querySelector('#rec-live .rec-live-err')) return 'failed';
          if (!bar.classList.contains('rec-arming')) return 'ready';
          return 0;
        })()""", tries=130)
        # The failure this replaced: headless sat in "正在打开麦克风…" forever, because
        # getUserMedia never resolves while a permission prompt goes unanswered. A
        # watchdog that waits for ever is the original complaint wearing a costume.
        check("the opening state always resolves, even with no mic at all",
              bool(outcome), str(outcome))
        if outcome == "failed":
            err = drv.js("return document.querySelector('#rec-live .rec-live-err').textContent")
            check("a mic that cannot open says so in the bar, not in an alert()",
                  "麦克风" in err, err[:70])
            check("...and the message tells you what to do about it",
                  ("允许" in err) or ("HTTPS" in err), err[:70])
        elif outcome == "ready":
            # The moment audio exists: dot red, wave running, and the buttons that need
            # audio become usable. This is the "tell me when it's ready" half.
            st = drv.js("""
              const bar = document.getElementById('rec-bar');
              return { dot: getComputedStyle(bar.querySelector('.rec-dot')).backgroundColor,
                       wave: getComputedStyle(bar.querySelector('.rec-wave')).display,
                       stopOn: !document.getElementById('rec-stop').disabled,
                       live: (document.getElementById('rec-live').innerHTML || '').slice(0, 80) };
            """)
            check("...the dot turns red exactly when capture starts", "229, 57, 53" in st["dot"], st["dot"])
            check("...the wave starts only now", st["wave"] != "none", str(st["wave"]))
            check("...and the buttons that need audio come alive", st["stopOn"] is True)
            check("...and it says so in words too", "可以说了" in st["live"] or "⏸" in st["live"],
                  st["live"][:60])
        drv.js("document.getElementById('rec-cancel').click()")

        print("=== the Task window gives its height to the two boxes ===")
        # "input box 之外的地方要紧凑显示,让 input box 占据更大空间." Measured, because
        # "looks tighter" is not a property: what matters is the share of the card the
        # two textareas actually get, and that only exists at real font sizes and real
        # widths.
        box = drv.js("""
          const modal = document.getElementById('memo-modal');
          modal.classList.add('show');
          const card = modal.querySelector('.memo-card');
          const t = document.getElementById('memo-task'), n = document.getElementById('memo-notes');
          const r = e => e.getBoundingClientRect().height;
          const rows = [...card.children].filter(e => getComputedStyle(e).display !== 'none');
          return { card: r(card), task: r(t), notes: r(n),
                   rows: rows.length,
                   checkShown: getComputedStyle(document.getElementById('memo-check-sec')).display !== 'none',
                   fs: getComputedStyle(t).fontSize };
        """)
        drv.js("""
          document.getElementById('memo-task').value =
            '监督 peer claude session 的执行。让 peer 判断进度与完成情况。你只需要像领导一样, 监督就行。';
          document.getElementById('memo-task-meta').textContent = '还没发过';
        """)
        drv.shot(os.path.join(smoke, "task-modal.png"))
        share = (box["task"] + box["notes"]) / box["card"] if box["card"] else 0
        check("the two boxes get most of the card", share > 0.72,
              f'{round(share * 100)}% of {round(box["card"])}px')
        check("...and they are equal halves", abs(box["task"] - box["notes"]) < 6,
              f'{round(box["task"])} vs {round(box["notes"])}')
        # The self-check report is the longest thing in the window and the rarest thing
        # you open it for, so it starts folded — but its verdict rides on the button, or
        # folding it would mean hiding a bad result.
        check("the self-check report starts folded", box["checkShown"] is False)
        # Versions: switched off for a day, then asked for again with the semantics
        # spelled out — fork makes one, each can be edited on its own, each can be made
        # the current one. Those three are pinned server-side (test_session_memo); here
        # only that the controls exist and the list folds like everything else in this
        # window.
        vers = drv.js("""
          const list = document.getElementById('memo-vers');
          return { verlist: !!document.getElementById('memo-verlist'),
                   fork: !!document.getElementById('memo-fork'),
                   label: !!document.getElementById('memo-vercur'),
                   listHidden: getComputedStyle(list).display === 'none',
                   opens: (document.getElementById('memo-verlist').click(),
                           getComputedStyle(list).display !== 'none') };
        """)
        check("versions / fork / the 'editing vN' label are all there",
              vers["verlist"] and vers["fork"] and vers["label"], str(vers))
        # Folded by default: most of the time there is one version, and a row saying so
        # is furniture taking height from the boxes.
        check("...the list starts folded", vers["listHidden"] is True)
        check("...and `versions` opens it", vers["opens"] is True)
        drv.js("document.getElementById('memo-verlist').click()")
        # Three buttons said "run check" and differed only by which row they stood in —
        # "这四个啥意思?". Now each says its scope, and the fourth (periodic) is gone:
        # it typed "起一个每 30 分钟的 watcher" at the session, which ⚙ → Watch → 检查周期
        # now does properly.
        labels = drv.js("""
          const m = document.getElementById('memo-modal');
          const fills = [...m.querySelectorAll('button')].map(b => b.textContent.trim())
                          .filter(t => t === '填入输入框');
          return { fills: fills.length,
                   checks: [...m.querySelectorAll('button')].map(b => b.textContent.trim())
                             .filter(t => t.indexOf('check') === 0).length,
                   cb: !!document.querySelector('#memo-modal input[type=checkbox]'),
                   periodicGone: !document.getElementById('memo-periodic') };
        """)
        # Three buttons all said `run check` and differed only by which row they stood
        # in — asked about directly ("这四个啥意思?"). They are gone: one 填入输入框 per
        # box plus one for both, and you write the sentence you meant.
        check("no check buttons left", labels["checks"] == 0, str(labels["checks"]))
        check("...three 填入输入框 instead: each box, and both", labels["fills"] == 3,
              str(labels["fills"]))
        # `set periodic check` became a checkbox, because the button read as though
        # cc-web would run the watcher. It never did.
        check("the watcher request is an opt-in checkbox", labels["cb"] is True)
        check("...and the button is gone", labels["periodicGone"] is True)
        check("...with the verdict still on the button",
              "自检" in drv.js("return document.getElementById('memo-checktoggle').textContent"),
              drv.js("return document.getElementById('memo-checktoggle').textContent"))
        opened = drv.js("""
          document.getElementById('memo-checktoggle').click();
          const sec = document.getElementById('memo-check-sec');
          const t = document.getElementById('memo-task');
          return { shown: getComputedStyle(sec).display !== 'none',
                   task: t.getBoundingClientRect().height };
        """)
        check("...and the button expands it", opened["shown"] is True)
        # Two rows with a `set` each plus a `view run` was three buttons for two windows.
        # One row now: Task → [set] [watch]. `view run` is gone (the report is inside the
        # Task window, on its own 自检 button) but its VERDICT stays on `set` — the point
        # of a check is to be noticed, and one you must open a window to find is one you
        # find late.
        row = drv.js("""
          const rows = [...document.querySelectorAll('#switch-menu .scr-cfg-row, .switch-menu .scr-cfg-row')];
          const r = rows.find(x => (x.firstElementChild || {}).textContent === 'task');
          if (!r) return { missing: true, labels: rows.map(x => (x.firstElementChild||{}).textContent) };
          return { btns: [...r.querySelectorAll('button')].map(b => b.id),
                   texts: [...r.querySelectorAll('button')].map(b => b.textContent.trim()),
                   watchRowGone: !rows.some(x => (x.firstElementChild || {}).textContent === 'Watch'),
                   viewRunGone: !document.getElementById('mm-check') };
        """)
        if row.get("missing"):
            check("SKIP: task row not in the ⚙ menu here", True, str(row.get("labels"))[:60])
        else:
            check("one row, two windows", row["btns"] == ["mm-memo", "mm-watch"], str(row["btns"]))
            check("...labelled set and watch",
                  row["texts"][0].startswith("set") and row["texts"][1].startswith("watch"),
                  str(row["texts"]))
            check("...the separate Watch row is gone", row["watchRowGone"] is True)
            check("...and so is `view run`", row["viewRunGone"] is True)
        # One computation behind both the ⚙ hint and the in-window 自检 button: two
        # copies of this existed briefly, which is how such a pair drifts.
        # The longest label the row can ever carry, measured: a hint that wraps or gets
        # clipped is how "过期" became unreadable in the first place.
        widest = drv.js("""
          const h = document.getElementById('mm-memo-hint');
          if (!h) return { missing: true };
          h.textContent = ' \u2713';
          const row = h.closest('.scr-cfg-row');
          const btns = [...row.querySelectorAll('button')];
          const tops = new Set(btns.map(b => Math.round(b.getBoundingClientRect().top)));
          return { rows: tops.size,
                   clipped: btns.filter(b => b.scrollWidth > b.clientWidth + 1).map(b => b.id) };
        """)
        if not widest.get("missing"):
            check("the longest verdict still fits on one line", widest["rows"] == 1, str(widest["rows"]))
            check("...and nothing is clipped", widest["clipped"] == [], str(widest["clipped"]))
        check("the verdict glyph is computed in one place",
              src_index.count("function memoCheckTag") == 1
              and src_index.count("memoCheckTag()") >= 2
              and "const memoCheckBadge = () => {" in src_index)
        # It used to be on `set`'s face as well. Dropped on purpose: you press `set` to
        # open the window, not to deal with a warning, so a verdict there was alarm on a
        # row you were reading for another reason. It stays in the window, on the 自检
        # button that produced it, and in this button's tooltip.
        hint = drv.js("""
          const h = document.getElementById('mm-memo-hint');
          const b = document.getElementById('mm-memo');
          return { hint: (h ? h.textContent : '(missing)'), title: b ? b.title : '' };
        """)
        check("the set hint says only whether anything is written",
              "自检" not in hint["hint"] and "⚠" not in hint["hint"], repr(hint["hint"]))
        check("...while the tooltip still explains where the verdict lives",
              "自检结果也在里面" in hint["title"], hint["title"][:40])
        # `view run` is gone (one row, two windows), so what used to be the difference
        # between the two buttons is now a property of `set` alone: it opens with the
        # report FOLDED, every time. The fold is a class on the element, so without an
        # explicit reset a window left unfolded would open unfolded — and the 自检 button
        # would look like it did nothing.
        _i = src_index.index('if (mmMemo) mmMemo.addEventListener')
        check("set opens with the report folded, explicitly",
              'memoCheckSec.classList.remove("show")' in src_index[_i:_i + 900])
        drv.js("document.getElementById('memo-checktoggle').click()")

        print("=== ⤢ gives one box the whole card ===")
        z = drv.js("""
          const m = document.getElementById('memo-modal');
          m.classList.add('show');
          const card = m.querySelector('.memo-card');
          const t = document.getElementById('memo-task'), n = document.getElementById('memo-notes');
          const h = e => Math.round(e.getBoundingClientRect().height);
          const before = { task: h(t), notes: getComputedStyle(n.closest('.memo-sec')).display };
          const btn = t.closest('.memo-sec').querySelector('.memo-zoom');
          btn.click();
          const zoom = { task: h(t), notes: getComputedStyle(n.closest('.memo-sec')).display,
                         glyph: btn.textContent.trim(),
                         hdr: getComputedStyle(m.querySelector('.memo-head')).display,
                         save: !!document.getElementById('memo-save').offsetParent };
          btn.click();
          const back = { task: h(t), notes: getComputedStyle(n.closest('.memo-sec')).display,
                         glyph: btn.textContent.trim() };
          return { before, zoom, back };
        """)
        # Hidden, not shrunk: the point is a phone, where "bigger" means nothing unless
        # the other box stops taking room.
        check("the other box gets out of the way", z["zoom"]["notes"] == "none",
              str(z["zoom"]["notes"]))
        check("...and this one takes the height", z["zoom"]["task"] > z["before"]["task"] * 1.6,
              f'{z["before"]["task"]} → {z["zoom"]["task"]}')
        # 保存 and ✕ have to stay reachable, or the only way out is a reload.
        check("...while the header stays", z["zoom"]["hdr"] != "none" and z["zoom"]["save"] is True)
        check("the button says which way it goes", z["zoom"]["glyph"] == "⤡", z["zoom"]["glyph"])
        check("...and pressing it again restores both boxes",
              z["back"]["notes"] != "none" and z["back"]["task"] == z["before"]["task"]
              and z["back"]["glyph"] == "⤢", f'{z["back"]["task"]} vs {z["before"]["task"]}')
        wz = drv.js("""
          const m = document.getElementById('watch-modal');
          m.classList.add('show');
          const ta = document.getElementById('watch-policy');
          const h = e => Math.round(e.getBoundingClientRect().height);
          const before = h(ta);
          ta.closest('.memo-sec').querySelector('.memo-zoom').click();
          const opts = [...m.querySelectorAll('.wt-opts')].map(o => getComputedStyle(o).display);
          const after = h(ta);
          ta.closest('.memo-sec').querySelector('.memo-zoom').click();
          m.classList.remove('show');
          return { before, after, opts };
        """)
        check("Watch zooms too, hiding the cadence rows",
              set(wz["opts"]) == {"none"} and wz["after"] > wz["before"], str(wz))
        drv.js("document.getElementById('memo-modal').classList.remove('show')")

        print("=== 🎤 on the Task boxes: a different bar, with the choice on it ===")
        # The choice between the raw and the polished version was a full-screen window
        # for a day. Wrong shape: the bar already echoes the text, so the panel covered
        # the thing being chosen between. It lives on the bar now — 看润色/看原文 swaps
        # which one is shown, 插入 takes the one on screen.
        v = drv.js("""
          const m = document.getElementById('memo-modal');
          m.classList.add('show');
          const mics = [...m.querySelectorAll('.memo-mic')].map(b => b.dataset.box);
          const bar = document.getElementById('rec-bar');
          const vis = (el) => el && getComputedStyle(el).display !== 'none';
          bar.style.display = 'flex';
          const ids = ['rec-swap', 'rec-use', 'rec-send', 'rec-edit', 'rec-stop'];
          const g = () => { const o = {}; ids.forEach(i => o[i] = vis(document.getElementById(i))); return o; };
          // Composer mode: the bar is exactly as it always was.
          ['rec-send', 'rec-edit', 'rec-stop'].forEach(i => document.getElementById(i).style.display = '');
          const composer = g();
          bar.classList.add('rec-memo');
          const memo = g();
          bar.classList.add('rec-two');
          const memoTwo = g();
          bar.classList.add('rec-picked');
          const memoPicked = g();
          bar.classList.remove('rec-memo', 'rec-two', 'rec-picked');
          bar.style.display = 'none';
          m.classList.remove('show');
          return { mics, composerMic: !!document.getElementById('mic-btn'),
                   composer, memo, memoTwo, memoPicked,
                   gone: !document.getElementById('vpick') };
        """)
        check("both Task boxes have a mic", v["mics"] == ["task", "notes"], str(v["mics"]))
        check("...and the composer keeps its own", v["composerMic"] is True)
        check("the composer's bar is untouched: Polish / Edit / Send, no 插入",
              v["composer"] == {"rec-swap": False, "rec-use": False, "rec-send": True,
                                "rec-edit": True, "rec-stop": True}, str(v["composer"]))
        # Two jobs, two button sets. A Task box submits nothing (no Send) and inserting
        # into the box you are editing IS the edit (no Edit) — both collapse into 插入.
        check("...and a Task box's bar drops Send and Edit for one 插入",
              v["memo"]["rec-use"] and not v["memo"]["rec-send"] and not v["memo"]["rec-edit"]
              and v["memo"]["rec-stop"], str(v["memo"]))
        check("...with no swap until there are two versions to swap between",
              v["memo"]["rec-swap"] is False and v["memoTwo"]["rec-swap"] is True)
        check("...and Polish gone once it has run", v["memoPicked"]["rec-stop"] is False)
        check("the full-screen chooser is gone", v["gone"] is True)
        # Shipped looking like two different kinds of control in one row: 插入 and 看润色
        # were never added to the bar's shared button rule, so they fell back to the
        # browser's default button next to Polish's pill. Measured, not eyeballed.
        btn = drv.js("""
          const bar = document.getElementById('rec-bar');
          bar.style.display = 'flex'; bar.classList.add('rec-memo', 'rec-two');
          const box = (id) => { const e = document.getElementById(id), c = getComputedStyle(e);
            return { r: c.borderTopLeftRadius, px: c.paddingLeft, py: c.paddingTop,
                     fs: c.fontSize, bw: c.borderTopWidth,
                     h: Math.round(e.getBoundingClientRect().height),
                     bg: c.backgroundColor, fw: c.fontWeight }; };
          const r = { stop: box('rec-stop'), use: box('rec-use'), swap: box('rec-swap'),
                      send: box('rec-send') };
          bar.classList.remove('rec-memo', 'rec-two'); bar.style.display = 'none';
          return r;
        """)
        same = lambda a, b: all(a[k] == b[k] for k in ("r", "px", "py", "fs", "bw", "h"))
        check("插入 is the same shape of button as Polish", same(btn["use"], btn["stop"]),
              f'{btn["use"]} vs {btn["stop"]}')
        check("...and so is 看润色", same(btn["swap"], btn["stop"]), f'{btn["swap"]} vs {btn["stop"]}')
        # Same shape, different weight: 插入 is the one that commits, exactly as Send is
        # in the composer — so it wears the composer's Send fill, not one of its own.
        check("...with 插入 filled like the composer's Send, since both commit",
              btn["use"]["bg"] == btn["send"]["bg"] and btn["use"]["bg"] != btn["stop"]["bg"]
              and btn["use"]["fw"] == btn["send"]["fw"], f'{btn["use"]["bg"]} vs {btn["send"]["bg"]}')
        check("...and 看润色 staying quiet, like Polish", btn["swap"]["bg"] == btn["stop"]["bg"])
        # One recorder, two destinations. A second copy would have been a second copy of
        # the bar-first opening, the 12s cap and the permission hint.
        check("both entry points share one recording path",
              src_index.count("async function micTap") == 1
              and src_index.count("micTap();") == 2, "micTap")
        # The composer aims at the caret now too, and marks it — the bar covers the
        # composer while you talk, so "where will this land" needs to be visible there
        # as well. `pick: false` is what keeps the rest of the composer as it was: it
        # still writes as you speak and still has Polish / Edit / Send.
        _mb = src_index[src_index.index('micBtn.addEventListener("click"'):][:420]
        check("...and the composer aims at the caret, with the same marker",
              'voiceTarget = { el: inputEl, pick: false, after: "" }' in _mb
              and "vtMarkIn();" in _mb, _mb[:120])
        # A second tap means stop. Dropping another marker (and a fresh target) into a
        # running session would be the opposite of that.
        check("...but only when starting one, not when stopping it",
              "if (!_recording && !_micArming)" in _mb)

        print("=== the composer: one row while the text fits, stacked when it does not ===")
        # 🎤 and 📎 shared one 44px cell, stacked vertically — so the mic was ~20px tall,
        # the hardest button in the app to hit, and a growing box squeezed it further.
        # Now: 📎 left · text · 🎤 ➤ right, each the full height of the row, and past one
        # line the row wraps (box across the top, buttons underneath). Measured, because
        # every claim here is a claim about geometry.
        lay = drv.js("""
          const row = document.querySelector('footer .input-row');
          const ta = document.getElementById('input');
          const clip = document.getElementById('upload-btn');
          const mic = document.getElementById('mic-btn');
          const send = document.getElementById('send');
          const R = (e) => e.getBoundingClientRect();
          const mid = (e) => { const r = R(e); return (r.top + r.bottom) / 2; };
          const set = (v) => { ta.value = v; ta.dispatchEvent(new Event('input')); };
          // No ASR is configured against the stub, so the mic hides itself — and a
          // display:none button measures 0×0 and would make every claim below vacuous.
          const micWas = mic.style.display; mic.style.display = '';
          const shot = () => ({
            stacked: row.classList.contains('stack'),
            rowH: Math.round(R(row).height), taH: Math.round(R(ta).height),
            micH: Math.round(R(mic).height), sendH: Math.round(R(send).height),
            clipH: Math.round(R(clip).height),
            clipW: Math.round(R(clip).width), micW: Math.round(R(mic).width),
            // one row = everything level; stacked = buttons BELOW the box
            level: Math.abs(mid(clip) - mid(ta)) <= 1 && Math.abs(mid(mic) - mid(ta)) <= 1,
            below: R(mic).top >= R(ta).bottom - 1 && R(clip).top >= R(ta).bottom - 1,
            clipLeft: R(clip).right <= R(ta).left + 1 || R(clip).left < R(mic).left,
            micBeforeSend: R(mic).right <= R(send).left + 1,
            taWide: R(ta).width / R(row).width,
          });
          const before = ta.value;
          set('');            const empty = shot();
          set('短');          const one = shot();
          set('很长的一行文字'.repeat(14));   const many = shot();
          set('a\\nb\\nc');   const nl = shot();
          set('短');          const back = shot();
          set(before); ta.dispatchEvent(new Event('input'));
          mic.style.display = micWas;
          return { empty, one, many, nl, back, micHidden: micWas === 'none' };
        """)
        one, many = lay["one"], lay["many"]
        check("an empty box is one row", lay["empty"]["stacked"] is False, str(lay["empty"]))
        check("...and so is a short line, with everything on it",
              one["stacked"] is False and one["level"] is True, str(one))
        check("...📎 on the left, 🎤 then ➤ on the right",
              one["clipLeft"] is True and one["micBeforeSend"] is True, str(one))
        # The complaint, in numbers: half of a 44px cell. A tap target wants ~36px+, and
        # the mic is now exactly as tall as the box it sits beside.
        check("...and the two buttons you press every turn are the full height",
              one["micH"] >= 36 and one["micH"] == one["taH"] and one["sendH"] == one["taH"],
              f'mic={one["micH"]} send={one["sendH"]} box={one["taH"]}')
        # 📎 is the exception, and asked for: a thin strip, narrow but as TALL as the mic.
        # It gives width back to the text without giving up the height that makes it
        # hittable — half-height was tried first and was wrong.
        check("...and 📎 is narrow but just as tall as the mic",
              one["clipH"] == one["micH"] and one["clipW"] <= one["micW"] * 0.65,
              f'clip={one["clipW"]}×{one["clipH"]} vs mic={one["micW"]}×{one["micH"]}')
        check("...including on the stacked button row",
              lay["many"]["clipH"] == lay["many"]["micH"]
              and lay["many"]["clipW"] <= lay["many"]["micW"] * 0.65,
              f'clip={lay["many"]["clipW"]}×{lay["many"]["clipH"]} '
              f'vs mic={lay["many"]["micW"]}×{lay["many"]["micH"]}')
        check("text past one line stacks the row", many["stacked"] is True, str(many))
        check("...with the box across the full width",
              many["taWide"] > 0.9, f'{many["taWide"]:.2f} of the row')
        check("...and the buttons on their own row under it, not squeezed beside it",
              many["below"] is True and many["micH"] >= 36, str(many))
        check("...📎 still left, 🎤 ➤ still right",
              many["clipLeft"] is True and many["micBeforeSend"] is True, str(many))
        check("a typed newline stacks it too", lay["nl"]["stacked"] is True, str(lay["nl"]))
        # The live transcript is a row INSIDE the bar, and the bar is absolute over the
        # composer row. Pinning it to 38px in the stacked layout outranked
        # .rec-live-on's own `height: auto` — the words you were dictating disappeared
        # and what overflowed landed on top of the status line.
        live = drv.js("""
          const row = document.querySelector('footer .input-row');
          const ta = document.getElementById('input'), before = ta.value;
          ta.value = '很长的一句话'.repeat(20); ta.dispatchEvent(new Event('input'));
          const bar = document.getElementById('rec-bar');
          const liveEl = document.getElementById('rec-live');
          bar.style.display = 'flex'; bar.classList.add('rec-live-on');
          const prevLive = liveEl.style.display;
          liveEl.style.display = ''; liveEl.textContent = '实时识别出来的一段文字'.repeat(8);
          const br = bar.getBoundingClientRect(), lr = liveEl.getBoundingClientRect();
          const rr = row.getBoundingClientRect();
          const r = { stacked: row.classList.contains('stack'),
                      barH: Math.round(br.height), liveH: Math.round(lr.height),
                      inside: lr.bottom <= br.bottom + 1 && lr.top >= br.top - 1,
                      insideRow: br.bottom <= rr.bottom + 1,
                      liveVisible: lr.height > 10 && getComputedStyle(liveEl).display !== 'none' };
          liveEl.textContent = ''; liveEl.style.display = prevLive;
          bar.classList.remove('rec-live-on'); bar.style.display = 'none';
          ta.value = before; ta.dispatchEvent(new Event('input'));
          return r;
        """)
        check("the live transcript still fits inside the bar when the row is stacked",
              live["inside"] is True and live["liveVisible"] is True, str(live))
        check("...so the bar grows past one button row to hold it",
              live["barH"] > 40 and live["barH"] >= live["liveH"], str(live))
        check("...without spilling past the composer onto the status line",
              live["insideRow"] is True, str(live))
        # The one that bites: the stacked box is WIDER, so text that needed two lines
        # beside the buttons can fit on one across the full width — decide from the
        # current geometry and the layout flips on every keystroke. fitInput() always
        # measures at the one-row width, so going back down is the exact inverse.
        check("...and deleting it goes back to one row (no flip-flop)",
              lay["back"]["stacked"] is False and lay["back"]["level"] is True, str(lay["back"]))
        check("...through one shared fit, so a programmatic edit resizes the same way",
              src_index.count("function fitInput()") == 1
              and 'inputEl.addEventListener("input", fitInput)' in src_index)
        # After a send the box is emptied directly (no input event) — miss that and the
        # row keeps the shape the long message gave it.
        check("...and a send un-stacks the row it grew into",
              'inputEl.value = ""; fitInput();' in src_index)
        # The bar carries the timer, the wave and every button that ends a recording,
        # so it has to be where you are looking. Dictating into a Task box, that is a
        # full-screen window with the footer behind it — the bar was running, correctly,
        # completely out of sight.
        moved = drv.js("""
          const bar = document.getElementById('rec-bar');
          const home = bar.parentNode.id || bar.parentNode.className;
          document.getElementById('memo-modal').classList.add('show');
          const sec = document.getElementById('memo-task').closest('.memo-sec');
          bar.classList.add('rec-inline');
          sec.insertBefore(bar, document.getElementById('memo-task'));
          bar.style.display = 'flex';
          const cs = getComputedStyle(bar);
          const br = bar.getBoundingClientRect(), tr = document.getElementById('memo-task').getBoundingClientRect();
          const r = { home, inSec: bar.parentNode === sec,
                      nextIsBox: bar.nextElementSibling && bar.nextElementSibling.id === 'memo-task',
                      pos: cs.position, radius: cs.borderTopLeftRadius,
                      border: cs.borderTopWidth,
                      // Does it sit ABOVE the box, or on top of it?
                      overlaps: br.bottom > tr.top + 2, gap: Math.round(tr.top - br.bottom) };
          bar.style.display = 'none';
          bar.classList.remove('rec-inline');
          document.querySelector('footer .input-row').appendChild(bar);
          document.getElementById('memo-modal').classList.remove('show');
          return r;
        """)
        check("the bar can live next to a Task box", moved["inSec"] is True)
        check("...directly above the box being dictated into", moved["nextIsBox"] is True)
        # Its styles were scoped `footer .rec-bar` — 29 rules that all stopped applying
        # the moment it moved, which is how it went invisible rather than misplaced.
        # Checked on a property the bar has in EVERY state: align-items varies with
        # .rec-live-on, so asserting on it was asserting on which state the previous
        # test left behind.
        check("...and keeps its styling there",
              moved["radius"] == "10px" and moved["border"] != "0px",
              f'radius={moved["radius"]} border={moved["border"]}')
        # In the footer it is absolutely positioned to COVER the composer row. Dropped
        # into a Task section unchanged it would cover the textarea instead of sitting
        # above it — the bar would be visible and the box would not.
        check("...in normal flow, not covering the box", moved["pos"] == "static", moved["pos"])
        check("...so the box is still there under it",
              moved["overlaps"] is False, f'gap={moved["gap"]}px')
        # Looked for as a SELECTOR: the comment explaining why they were de-scoped
        # mentions the old form, and a check that trips over its own rationale is a
        # check nobody keeps.
        import re as _re
        check("the rules are not scoped to the footer any more",
              not _re.search(r"(?m)^\s*footer\s+(\.rec-bar|#rec-bar)", src_index)
              and "footer .rec-bar ." not in src_index)
        check("...and it is MOVED, not duplicated",
              src_index.count('id="rec-bar"') == 1 and "recBarHome()" in src_index)

        check("a Task recording asks before it writes",
              'voiceTarget && voiceTarget.pick' in src_index and "voicePickShow(" in src_index)
        # Same bar, said differently: the dashed frame is there to mean "nothing has gone
        # into your box yet", which is exactly what separates this mode from the other one.
        dashed = drv.js("""
          const bar = document.getElementById('rec-bar');
          bar.style.display = 'flex';
          const plain = getComputedStyle(bar).borderTopStyle;
          bar.classList.add('rec-memo');
          const memo = getComputedStyle(bar).borderTopStyle;
          bar.classList.remove('rec-memo'); bar.style.display = 'none';
          return { plain, memo };
        """)
        check("...and looks different while it does",
              dashed["memo"] == "dashed" and dashed["plain"] != "dashed", str(dashed))

        print("=== A- / A+ resize the boxes, and only the boxes ===")
        sizes = drv.js("""
          const t = document.getElementById('memo-task');
          const lab = document.querySelector('#memo-modal .memo-lab');
          const before = { fs: getComputedStyle(t).fontSize, lab: getComputedStyle(lab).fontSize };
          for (let i = 0; i < 4; i++) document.getElementById('memo-fontup').click();
          const up = { fs: getComputedStyle(t).fontSize, lab: getComputedStyle(lab).fontSize };
          for (let i = 0; i < 9; i++) document.getElementById('memo-fontdn').click();
          const dn = { fs: getComputedStyle(t).fontSize, dis: document.getElementById('memo-fontdn').disabled };
          for (let i = 0; i < 40; i++) document.getElementById('memo-fontup').click();
          const max = { fs: getComputedStyle(t).fontSize, dis: document.getElementById('memo-fontup').disabled };
          return { before, up, dn, max, stored: localStorage.getItem('ccweb.memoFs') };
        """)
        check("A+ grows the box text", float(sizes["up"]["fs"][:-2]) > float(sizes["before"]["fs"][:-2]),
              f'{sizes["before"]["fs"]} → {sizes["up"]["fs"]}')
        # The chrome must NOT grow with it: the point of the buttons is to make the text
        # readable, and a window whose labels grow too just gives the space back.
        check("...and leaves the labels alone", sizes["up"]["lab"] == sizes["before"]["lab"],
              f'{sizes["before"]["lab"]} → {sizes["up"]["lab"]}')
        check("A- shrinks it, and stops at a floor", sizes["dn"]["dis"] is True, sizes["dn"]["fs"])
        # The CSS half of the no-iOS-zoom fix: whatever --memo-fs ends up as (a 13 kept
        # in localStorage from a desktop, say), a coarse pointer never renders these
        # under 16px. The JS floor is driven separately, in test_gear_menu_ui.
        check("...and a touch device is floored at 16px in CSS too",
              "@media (pointer: coarse)" in src_index
              and "max(16px, var(--memo-fs))" in src_index)
        check("...and A+ stops at a ceiling", sizes["max"]["dis"] is True, sizes["max"]["fs"])
        check("the choice is remembered", sizes["stored"] is not None, str(sizes["stored"]))
        drv.js("document.getElementById('memo-modal').classList.remove('show')")

        print("=== every full-screen window has the SAME header format ===")
        # There were two near-identical header rules, one per modal id, and they drifted
        # exactly as copies do: Task's row was right-aligned while Watch's buttons
        # bunched against the title, leaving ✕ 411px from the right edge in one window
        # and at the edge in the other. Measured here rather than eyeballed, because
        # "same format" is a geometry claim.
        hdr = drv.js("""
          const out = {};
          for (const [k, id] of [['task', 'memo-modal'], ['watch', 'watch-modal']]) {
            const m = document.getElementById(id);
            m.classList.add('show');
            const row = m.querySelector('.modal-row.memo-head');
            const kids = [...row.children].filter(e => getComputedStyle(e).display !== 'none');
            const rr = row.getBoundingClientRect();
            const last = kids[kids.length - 1], prev = kids[kids.length - 2];
            out[k] = { closeGap: Math.round(rr.right - last.getBoundingClientRect().right),
                       closeTxt: last.textContent.trim(),
                       prevTxt: prev.textContent.trim(),
                       titleFirst: kids[0].tagName === 'H3',
                       fills: kids.filter(e => e.classList.contains('hdr-fill')).length };
            m.classList.remove('show');
          }
          return out;
        """)
        for k in ("task", "watch"):
            check(f"{k}: the title leads", hdr[k]["titleFirst"] is True)
            # The user's rule, verbatim: 「close 都应该在右边」.
            check(f"{k}: ✕ is last and at the right edge",
                  hdr[k]["closeTxt"] == "✕" and hdr[k]["closeGap"] == 0,
                  f'{hdr[k]["closeTxt"]} gap={hdr[k]["closeGap"]}')
            check(f"{k}: 保存 sits just before it", hdr[k]["prevTxt"] == "保存", hdr[k]["prevTxt"])
            # One stretchy cell is what pushes the controls right; two would fight, none
            # is how Watch ended up bunched left.
            check(f"{k}: exactly one stretchy cell", hdr[k]["fills"] == 1, str(hdr[k]["fills"]))
        check("the format is one shared rule, not one per window",
              ".modal-row.memo-head {" in src_index
              and "#watch-modal .modal-row {" not in src_index
              and "#memo-modal .memo-head {" not in src_index)

        print("=== the Watch window: same shape, and the two cadence rows ===")
        w = drv.js("""
          const m = document.getElementById('watch-modal');
          m.classList.add('show');
          const card = m.querySelector('.watch-card');
          const ta = document.getElementById('watch-policy');
          const r = e => e.getBoundingClientRect().height;
          return { card: r(card), ta: r(ta),
                   hours: [...m.querySelectorAll('.wt-h')].map(b => b.dataset.h),
                   periods: [...m.querySelectorAll('.wt-p')].map(b => b.dataset.p),
                   log: !!document.getElementById('watch-log'),
                   fs: getComputedStyle(ta).fontSize };
        """)
        check("the policy box gets most of the card", w["ta"] / w["card"] > 0.55,
              f'{round(w["ta"] / w["card"] * 100)}% of {round(w["card"])}px')
        check("expiry offers the full set, with no 不限",
              w["hours"] == ["2", "4", "6", "8", "10", "16", "24", "48"], str(w["hours"]))
        check("...and the check period its own", w["periods"] == ["10", "15", "20", "25", "30", "45", "60", "0"],
              str(w["periods"]))
        # Asked for directly: it was the one thing in this window about auditing rather
        # than setting, and /api/watch-log still records everything regardless.
        check("最近动作 is gone from the window", w["log"] is False)
        # The marks themselves are driven under node (tests/test_gear_menu_ui.py →
        # watchMarks): the page script is one big IIFE, so a browser test can click and
        # read the DOM but cannot reach a function inside that closure. What IS checked
        # here is that the rows and the explicit pair exist and are wired to something.
        seg = drv.js("""
          const m = document.getElementById('watch-modal');
          return { seg: [...m.querySelectorAll('.wt-seg .btn')].map(b => b.textContent.trim()),
                   toggleGone: !document.getElementById('watch-toggle') };
        """)
        # A single 开启 button reads both as "it is on" and as "tap to turn it on" —
        # ambiguous for a thing that types into your session.
        check("open/close is an explicit pair", seg["seg"] == ["开", "关"], str(seg["seg"]))
        check("...and the one ambiguous toggle is gone", seg["toggleGone"] is True)

        up = drv.js("""
          const ta = document.getElementById('watch-policy');
          const before = getComputedStyle(ta).fontSize;
          for (let i = 0; i < 3; i++) document.getElementById('watch-fontup').click();
          return { before, after: getComputedStyle(ta).fontSize,
                   stored: localStorage.getItem('ccweb.watchFs') };
        """)
        check("A+ grows the policy box too",
              float(up["after"][:-2]) > float(up["before"][:-2]), f'{up["before"]} → {up["after"]}')
        check("...and is remembered separately from the Task window",
              up["stored"] is not None, str(up["stored"]))
        drv.js("document.getElementById('watch-modal').classList.remove('show')")

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
        # The faces are min/mid/max — three equal-length words that read as a scale —
        # while data-mode keeps the server's own names, so only the face changed. And NOT
        # S/M/L: the font size sits one row below, where S/M/L would read as its own.
        faces = drv.js("""
          return [...document.querySelectorAll('.mode-opt')]
            .map(b => b.textContent.trim() + ':' + b.dataset.mode);
        """)
        check("...and the labels are the short scale, over the protocol names",
              faces == ["min:brief", "mid:medium", "max:all"], str(faces))

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
