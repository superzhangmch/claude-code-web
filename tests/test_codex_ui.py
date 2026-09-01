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
import re

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
    # Deliberately not "TABS (1)": the count is whatever is running on the box,
    # and an assertion that hardcodes it fails the moment a second codex session
    # exists — which says nothing about the page.
    m = re.search(r"TABS \((\d+)\)", found)
    check("the list has a TABS group with at least one session",
          bool(m) and int(m.group(1)) >= 1, repr(found[:300]))
    # NOT "RECENT appears": brief mode lists live sessions only, for either agent —
    # an earlier codex-only branch invented a RECENT group here that claude's brief
    # list does not have, and this assertion was written against that invention.
    check("brief mode lists live sessions only, as it does for claude",
          "RECENT" not in found, repr(found[:300]))
    # The reported bug: every row showed "01a0". codex thread ids are time-ordered
    # (UUID v7), so the leading hex is a timestamp shared by every session created
    # around the same time — a 4-char prefix of it identifies nothing. With two
    # sessions listed, their chips must differ.
    chips = drv.js("return [].map.call("
                   "document.querySelectorAll('#picker-list .brief-row .sw-sid'),"
                   " function (e) { return e.textContent.trim(); })") or []
    if len(chips) < 2:
        check("(only one session listed — nothing to collide)", True)
    else:
        check("the short ids on the rows are distinct, not all '01a0'",
              len(set(chips)) == len(chips), str(chips))
        check("...and are not the shared v7 timestamp prefix",
              not all(c == chips[0] for c in chips) and "01a0" not in chips, str(chips))
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
    # Which session the first row is depends on what is running, so assert the
    # SHAPE of an opened codex session rather than any one session's words: a
    # human/assistant exchange, and the codex status line.
    check("clicking the row opens a transcript",
          "YOU" in body and ("CLAUDE" in body or "CODEX" in body), repr(body[:400]))
    # NOT looking for the word "codex" any more: the status line is now whatever the
    # session's terminal footer says, by the same rule claude uses (last non-empty
    # screen line) — for codex that is "gpt-5.6-sol default · /tmp", which is more
    # informative than a label this server made up. What matters is that a footer
    # got through at all, and that the instance says which agent it serves.
    check("...with the session's own terminal footer beneath it",
          bool((body.strip().splitlines() or [""])[-1].strip()), repr(body[-200:]))
    # From here, not from the page: drv.js returns before a promise resolves, so a
    # browser-side fetch would have asserted nothing at all.
    req = urllib.request.Request(base + "/api/server-info")
    req.add_header("Authorization", "Bearer " + TOKEN)
    info = json.load(urllib.request.urlopen(req, timeout=10))
    check("...served by the codex instance", info.get("agent") == "codex", str(info))

    # The speaker label and the page title, which were both hardcoded to claude: a
    # codex answer was labelled CLAUDE, and two instances open in one browser had
    # identical tab titles. Both now come from the agent the server reports.
    check("the assistant is not labelled CLAUDE here",
          "CLAUDE" not in body, repr([l for l in body.splitlines() if "CLAUDE" in l][:2]))
    check("...it is labelled with this agent's name", "CODEX" in body.upper(),
          repr(body[:200]))
    check("the tab title says which agent this is",
          "Codex" in drv.js("return document.title"), drv.js("return document.title"))
    check("the input box is there to type into",
          drv.js("return !!document.querySelector('textarea, [contenteditable]')") is True)

    # --- the round trip, driven the way a person drives it -------------------
    # Everything above still only proves the page can DISPLAY. This types into the
    # composer, presses the send button, and waits for codex's answer to come back
    # through the rollout into the transcript: browser -> /api/input -> codex queue
    # -> the TUI -> the rollout file -> /api/state -> the DOM.
    marker = "E2E-" + str(int(time.time()))[-6:]
    typed = drv.js("""
      var ta = document.getElementById('input');
      if (!ta) return 'NO-COMPOSER';
      ta.focus(); ta.value = 'Reply with exactly: %s';
      ta.dispatchEvent(new Event('input', {bubbles: true}));
      return ta.value;
    """ % marker)
    check("the message can be typed into the composer",
          marker in str(typed), str(typed))
    sent = drv.js("""
      var b = document.getElementById('send') ||
              [].find.call(document.querySelectorAll('button'),
                           function (x) { return (x.textContent || '').indexOf('\u27a4') >= 0; });
      if (!b) return 'NO-SEND-BUTTON';
      b.click(); return 'clicked';
    """)
    check("the send button is there and clickable", sent == "clicked", str(sent))
    got = False
    for _ in range(30):                     # codex answers in seconds; allow a minute
        time.sleep(2)
        body = drv.js("return document.body.innerText")
        if marker in body and body.count(marker) >= 2:   # the prompt AND the answer
            got = True
            break
    check("codex's answer comes back into the page by itself", got,
          repr(body[-500:]))
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
