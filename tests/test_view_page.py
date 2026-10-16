#!/usr/bin/env python3
"""/view — a page that shows a text file. That is the whole feature.

It was built as an overlay inside the session app first: a #file= route, rules about
what to boot behind it, what ✕ should reveal, how it interacted with #s=. Reading a file
has nothing to do with a claude session, and each of those rules was a way for it to go
wrong — a URL carrying both a session and a file opened the session and dropped the
file, which is exactly how it was reported.

So: its own page, its own URL, nothing else running.

    /view?path=/abs/or/~/path[&token=…]

Driven in a real browser against a real server: open the URL, read what is on the page.
No part of this test reaches inside anything.

    python3 tests/test_view_page.py      # exit 0 = pass  (skips without firefox)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


def free_port():
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def main():
    if not (shutil.which("geckodriver") and shutil.which("firefox")):
        print("SKIP: needs geckodriver + firefox"); return 0
    # Same lookup the smoke test uses: the deployed tree keeps its venv beside it.
    uvicorn = next((c for c in (os.path.join(ROOT, ".venv/bin/uvicorn"),
                                os.path.expanduser("~/claude-code-web/.venv/bin/uvicorn"))
                    if os.path.exists(c)), shutil.which("uvicorn"))
    if not uvicorn:
        print("SKIP: no uvicorn"); return 0
    from test_ui_smoke import Driver

    home = tempfile.mkdtemp(prefix="ccweb-view-")
    os.makedirs(os.path.join(home, ".claude"))
    open(os.path.join(home, ".claude", "cc_web.conf"), "w").write("token=t\n")
    docs = {
        # A bare URL and a table on purpose: the md path's last steps linkify URLs and
        # wrap tables, and those were the parts that referenced things the page did not
        # have — renderMarkdown threw, the throw landed in an unawaited promise, and the
        # md view was blank with nothing in the console.
        "doc.md": ("# Title\n\nsome **bold** needle text and $E=mc^2$.\n\n"
                   "see https://example.com/a/b for more\n\n"
                   "| a | b |\n|---|---|\n| 1 | 2 |\n\n- one\n- two\n"),
        "data.json": '{"a":1,"b":[1,2],"c":{"d":"needle"}}',
        "rows.csv": 'name,note,n\nalice,"hello, world",1\nbob,"she said ""hi""",2\n',
        "lines.jsonl": '{"i":1,"t":"first"}\n{"i":2,"t":"needle"}\nnot json\n',
        "prog.py": "def f():\n    return 'needle'\n",
        "big.txt": "x" * (3 * 1024 * 1024),
        # A saved report. The browser renders this; showing its markup is not reading it.
        "report.html": "<h1 id='rep'>report v6</h1><p>body</p>",
    }
    for name, body in docs.items():
        open(os.path.join(home, name), "w", encoding="utf-8").write(body)

    port, wd = free_port(), free_port()
    base = f"http://127.0.0.1:{port}"
    srv = subprocess.Popen([uvicorn, "cc_web:app", "--app-dir", ROOT, "--host", "127.0.0.1",
                            "--port", str(port), "--log-level", "warning"],
                           env=dict(os.environ, HOME=home, CC_WEB_TOKEN="t"),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    gecko = subprocess.Popen(["geckodriver", "--port", str(wd)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    drv = None
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/healthz", timeout=1); break
            except Exception:
                try:
                    urllib.request.urlopen(base + "/", timeout=1); break
                except Exception:
                    time.sleep(0.25)
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{wd}/status", timeout=2); break
            except Exception:
                time.sleep(0.25)
        drv = Driver(wd, width=1100, height=850)

        def view(name, token=True, path=None):
            p = path if path is not None else os.path.join(home, name)
            u = base + "/view?path=" + urllib.parse.quote(p) + ("&token=t" if token else "")
            drv.go(u)
            drv.wait("document.getElementById('fv-body') ? 1 : 0", tries=60)
            time.sleep(0.5)
            return drv.js("""
              return { name: document.getElementById('fv-name').textContent,
                       views: [...document.querySelectorAll('#fv-views button')].map(b => b.textContent),
                       cur: (document.querySelector('#fv-views button.current') || {}).textContent,
                       text: (document.getElementById('fv-body').textContent || '').slice(0, 400),
                       h1: !!document.querySelector('#fv-body h1'),
                       bold: !!document.querySelector('#fv-body strong'),
                       table: !!document.querySelector('#fv-body table.fv-table'),
                       rows: [...document.querySelectorAll('#fv-body table.fv-table tbody tr')]
                               .map(tr => [...tr.querySelectorAll('td')].map(td => td.textContent)),
                       jl: [...document.querySelectorAll('#fv-body .fv-jl .jl-row')].length,
                       gutter: (document.querySelector('#fv-body .fv-gutter') || {}).textContent || '',
                       token: !!(document.getElementById('tokbox') || {}).offsetParent,
                       url: location.pathname + location.search };
            """)

        print("=== the page is the file, and nothing else ===")
        r = view("doc.md")
        check("a .md renders", r["cur"] == "md" and r["h1"] and r["bold"], str(r)[:140])
        rich = drv.js("""
          return { links: document.querySelectorAll('#fv-body a[href^="https://"]').length,
                   tables: document.querySelectorAll('#fv-body .table-wrap table').length,
                   htmlLen: document.getElementById('fv-body').innerHTML.length };
        """)
        check("...the whole of it: a bare URL became a link, a table got its wrapper",
              rich["links"] >= 1 and rich["tables"] == 1 and rich["htmlLen"] > 200, str(rich))
        check("...with the other readings offered", r["views"] == ["md", "code", "raw"], str(r["views"]))
        check("...and the token is taken out of the URL once it is stored",
              "token=" not in r["url"], r["url"])
        # No session machinery: the one thing the complaint was about.
        none = drv.js("""
          return { picker: !!document.getElementById('picker'),
                   transcript: !!document.getElementById('transcript-view'),
                   tabs: !!document.getElementById('tab-bar'),
                   polls: !!window.__ccPoll };
        """)
        check("no session list, no transcript, no tab bar on this page",
              not any(none.values()), str(none))

        print("=== each kind read the way it should be ===")
        r = view("data.json")
        check("a .json is pretty-printed", r["cur"] == "json" and '"a": 1' in r["text"], r["text"][:60])
        r = view("rows.csv")
        check("a .csv is a table", r["cur"] == "csv" and r["table"], str(r["views"]))
        check("...with quoted commas and doubled quotes intact",
              r["rows"][0][2] == "hello, world" and r["rows"][1][2] == 'she said "hi"', str(r["rows"]))
        r = view("lines.jsonl")
        check("a .jsonl is one row per line", r["cur"] == "jsonl" and r["jl"] == 3, str(r["jl"]))
        r = view("doc.md")
        gut = drv.js("""
          const b = [...document.querySelectorAll('#fv-views button')].find(x => x.textContent === 'code');
          if (b) b.click();
          return (document.querySelector('#fv-body .fv-gutter') || {}).textContent || '';
        """)
        check("the code view puts line numbers outside the text",
              gut.strip().split("\n")[0] == "1", repr(gut[:10]))

        print("=== search, and the things that are not documents ===")
        hits = drv.js("""
          const q = document.getElementById('fv-q');
          q.value = 'needle'; q.dispatchEvent(new Event('input'));
          const n = document.querySelectorAll('#fv-body mark.fv-hit').length;
          const label = document.getElementById('fv-hits').textContent;
          document.getElementById('fv-next').click();
          return { n, label, after: document.getElementById('fv-hits').textContent };
        """)
        check("search marks and counts", hits["n"] >= 1 and hits["label"].endswith(str(hits["n"])),
              str(hits))
        r = view("big.txt")
        check("a 3MB file is refused, with the size said out loud",
              "2MB" in r["text"] and "3072KB" in r["text"], r["text"][:80])
        r = view("nope.md")
        # Naming the host matters with four of them: the way this goes wrong in practice
        # is a path copied from one machine and opened on another.
        check("a path that is not there says so, and on WHICH machine",
              "没有这个文件" in r["text"] and "127.0.0.1" in r["text"], r["text"][:80])

        print("=== the raw endpoint sends a browser here ===")
        raw = base + "/api/fs/file?path=" + urllib.parse.quote(os.path.join(home, "doc.md")) + "&token=t"
        drv.go(raw)
        # …and wait for the DOCUMENT, not just for the page: the fetch happens after
        # load, so "the element exists" was true a moment before anything was in it.
        drv.wait("document.querySelector('#fv-body h1') ? 1 : 0", tries=60)
        landed = drv.js("return location.pathname + ' ' + !!document.querySelector('#fv-body h1');")
        check("opening the raw URL in a browser lands on the reader",
              landed.startswith("/view") and landed.endswith("true"), landed)

        print("=== …and everything without a renderer is handed back to the browser ===")
        # Reported as "煞笔. html 应该直接吐出啊": a report opened through this page showed
        # its own markup. A page is not a document whose source you came to read — and
        # the link that does it already exists, so the PAGE has to bounce, not just the
        # app stop producing new ones.
        drv.go(base + "/view?path=" + urllib.parse.quote(os.path.join(home, "report.html")) + "&token=t")
        drv.wait("document.getElementById('rep') ? 1 : 0", tries=60)
        got = drv.js("""return { where: location.pathname,
                                 rendered: (document.getElementById('rep') || {}).textContent || '',
                                 viewer: !!document.getElementById('fv-body') };""")
        check("an .html lands on the file itself, rendered",
              got["where"] == "/api/fs/file" and got["rendered"] == "report v6"
              and not got["viewer"], str(got))
        # Source, on the other hand, belongs here: colour and line numbers.
        r = view("prog.py")
        check("a .py still opens here, as code with line numbers",
              r["cur"] == "code" and r["gutter"].strip().split("\n")[0] == "1",
              str(r["cur"]) + " " + repr(r["gutter"][:10]))
        back = drv.js("return history.length;")
        check("...by replacing this page, so Back does not bounce through it", back >= 1, str(back))
    finally:
        if drv:
            drv.quit()
        gecko.terminate(); srv.terminate()
        for pr in (gecko, srv):
            try: pr.wait(timeout=5)
            except Exception: pr.kill()

    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
