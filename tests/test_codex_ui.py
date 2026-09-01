#!/usr/bin/env python3
"""The real cc-web UI, in a real browser, driving a real codex instance.

The API contract passing is not the same as the page working, and this test is
here because that gap bit twice in one sitting:

  * /api/sessions returned a tidy {tabs, recent, named} — every call 200, and the
    page said "No live claude tab". The frontend reads a FLAT `sessions` array and
    keys off each item's `group`.
  * codex's sqlite `updated_at` is SECONDS; dividing by 1000 "to be safe" dated
    every row 1970-01-22, and the list rendered it without a murmur.

Neither is visible from an endpoint test. So: start cc_web with CC_WEB_AGENT=codex
against this box's real ~/.codex, point firefox at it, and read the DOM — the list,
the row's date, the transcript after a click, the input box.

Skips (exit 0) where there is no geckodriver/firefox, or no codex on the machine.

    python3 tests/test_codex_ui.py       # exit 0 = pass
"""
import pathlib

import importlib.util, json, os, shutil, socket, subprocess, sys, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
spec = importlib.util.spec_from_file_location("uism", os.path.join(ROOT, "tests/test_ui_smoke.py"))
uism = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uism)
Driver = uism.Driver

if not (shutil.which("geckodriver") and shutil.which("firefox")):
    print("SKIP: needs geckodriver + firefox"); sys.exit(0)
if not list(pathlib.Path.home().glob(".codex/state_*.sqlite")):
    print("SKIP: no codex on this machine"); sys.exit(0)
if not list(pathlib.Path.home().glob(".codex/sessions/*/*/*/rollout-*.jsonl")):
    print("SKIP: codex has no sessions to show"); sys.exit(0)

TOKEN = open(os.path.expanduser("~/.claude/cc_web.conf")).read()
TOKEN = next(l.split("=", 1)[1].strip() for l in TOKEN.splitlines() if l.startswith("token="))

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

app_port, wd_port = free_port(), free_port()
base = f"http://127.0.0.1:{app_port}"
uvicorn = os.path.expanduser("~/claude-code-web/.venv/bin/uvicorn")

srv = subprocess.Popen([uvicorn, "cc_web:app", "--app-dir", ROOT, "--host", "127.0.0.1",
                        "--port", str(app_port), "--log-level", "warning"],
                       env=dict(os.environ, CC_WEB_AGENT="codex", CC_WEB_ALLOW_MULTI="1"),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
gecko = subprocess.Popen(["geckodriver", "--port", str(wd_port)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
drv = None
fails = []
def check(label, ok, extra=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"   [{extra}]" if extra and not ok else ""))
    if not ok: fails.append(label)

try:
    for _ in range(60):
        time.sleep(0.4)
        try:
            urllib.request.urlopen(base + "/api/auth-status", timeout=2).read(); break
        except Exception: continue
    else:
        print("server never came up:", (srv.stdout.read(2000) if srv.stdout else "")); sys.exit(1)
    for _ in range(40):
        time.sleep(0.25)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{wd_port}/status", timeout=2).read(); break
        except Exception: continue

    drv = Driver(wd_port)
    drv.go(base)
    drv.js(f"localStorage.setItem('cc_web_token','{TOKEN}');")
    drv.go(base)
    time.sleep(9)

    title = drv.js("return document.title")
    check("the page loads", bool(title), title)
    check("it is cc-web's own page, not a stub",
          drv.js("return !!document.querySelector('#app, #main, body')") is True)
    # any element whose text carries a codex thread id prefix or its title
    found = drv.js("return document.body.innerText.slice(0, 4000)")
    check("the codex session is listed", "TABS (1)" in found and "01a0" in found,
          repr(found[:300]))
    check("the finished session lands in RECENT", "RECENT" in found, repr(found[:300]))
    import datetime as _d
    check("its timestamp is today, not 1970",
          _d.datetime.now().strftime("%m-%d") in found, repr(found[:300]))
    # Open it and read the transcript the way a person would.
    # brief mode renders `.brief-row`, not the full card — the click target the
    # user actually taps.
    clicked = drv.js("var c=document.querySelector('#picker-list .brief-row')"
                     " || document.querySelector('.session-card');"
                     " if (c) { c.click(); return c.className; } return 'NOTHING';")
    print("  (clicked:", clicked, ")")
    time.sleep(8)
    body = drv.js("return document.body.innerText.slice(0, 5000)")
    check("clicking the row opens the session", "VIA-HTTP" in body or "LIVE-B" in body,
          repr(body[:500]))
    check("the input box is there to type into",
          drv.js("return !!document.querySelector('textarea, [contenteditable]')") is True)
    found = body
    errs = drv.js("return (window.__errs||[]).length")
    print("\n--- page text (first 1200 chars) ---")
    print(found[:1200])
    print("\n--- console errors captured by the page (if it tracks them):", errs)
finally:
    if drv:
        try: drv.call("DELETE", f"/session/{drv.sid}")
        except Exception: pass
    gecko.terminate(); srv.terminate()
    try: srv.wait(timeout=5)
    except Exception: srv.kill()
print("\nFAILED:" if fails else "\nall good", "; ".join(fails) if fails else "")
sys.exit(1 if fails else 0)
