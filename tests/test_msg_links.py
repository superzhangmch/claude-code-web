#!/usr/bin/env python3
"""Two additions to a message: ↗ after a file path, and `#1234` as a link.

Both exist because the thing you want next was two taps away. A path in an answer meant
opening the 🔗 popup and finding it in the list; a `#1234` meant selecting it, switching
tabs and pasting. Now the path carries a superscript ↗ straight to
/api/fs/file?path=…&token=… and the reference carries a link — IF the conf says where
references point (`issue_link=…/{#N}`). Unset is the default and changes nothing: on a
machine with no tracker a bare number means nothing in particular.

What has to be right, and is therefore driven here rather than eyeballed:

  * the path text is NOT touched. People copy those. The arrow is a sibling, and at the
    end of an inline `code` it goes OUTSIDE the grey box instead of inside it.
  * a directory gets no arrow (/api/fs/file answers 403), nor does anything in a fenced
    block (one diff mentions twenty paths), nor a path that is already a link.
  * `#1234` only when it stands alone: not a hex colour, not `v1.2#3`, not inside code.

The two passes are lifted out of static/index.html and run against a REAL DOM (a browser
tab, because TreeWalker and DocumentFragment are the whole mechanism here), so what is
asserted is the shipped code. The wiring — that renderMarkdown ends by calling both — is
checked against the source, since that is what it is.

    python3 tests/test_msg_links.py      # exit 0 = pass  (skips without firefox)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "static", "index.html")
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


def extract(src, pat, what):
    m = re.search(pat, src, re.S)
    if not m:
        check(f"could extract {what} from static/index.html", False)
        return None
    return m.group(1)


def main():
    src = open(INDEX, encoding="utf-8").read()

    print("=== the renderer ends by running both passes ===")
    # Either call missing means the feature exists and does nothing, which is the hardest
    # kind of broken to notice.
    tail = src[src.index("function renderMarkdown(text)"):]
    tail = tail[:tail.index("return tmp.innerHTML;") + 30]
    check("renderMarkdown calls linkifyRefs(tmp) then markPathLinks(tmp)",
          tail.index("linkifyRefs(tmp)") < tail.index("markPathLinks(tmp)") < tail.index("return tmp.innerHTML"))
    check("...and the template is read from the server, not hardcoded",
          '"issue_link"' in open(os.path.join(ROOT, "cc_web.py"), encoding="utf-8").read()
          and "d.issue_link" in src)

    pieces = [extract(src, r'\n  (const PATH_SEG = .*?const PATH_FILE_RE = [^\n]*\n)', "the path pattern"),
              extract(src, r'\n  (function mkPathBadge\(href, title\) \{.*?\n  \})\n', "mkPathBadge"),
              extract(src, r'\n  (function markPathLinks\(root\) \{.*?\n  \})\n', "markPathLinks"),
              extract(src, r'\n  (let ISSUE_TPL = "";.*?\n  \})\n', "linkifyRefs")]
    if any(p is None for p in pieces):
        print("\nFAILED: " + ", ".join(_fails)); return 1

    if not (shutil.which("geckodriver") and shutil.which("firefox")):
        print("SKIP (behaviour): needs geckodriver + firefox")
        print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
        return 1 if _fails else 0
    uvicorn = next((c for c in (os.path.join(ROOT, ".venv/bin/uvicorn"),
                                os.path.expanduser("~/claude-code-web/.venv/bin/uvicorn"))
                    if os.path.exists(c)), shutil.which("uvicorn"))
    if not uvicorn:
        print("SKIP (behaviour): no uvicorn")
        print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
        return 1 if _fails else 0
    from test_ui_smoke import Driver

    import tempfile
    home = tempfile.mkdtemp(prefix="ccweb-msglinks-")
    os.makedirs(os.path.join(home, ".claude"))
    # The template as a host would set it — a FAKE repo. Where the real numbers live is
    # local knowledge and belongs in ~/.claude/cc_web.conf.
    TPL = "https://github.example/acme/widgets/pull/{#N}"
    open(os.path.join(home, ".claude", "cc_web.conf"), "w").write(
        f"token=t\nissue_link={TPL}\n")

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

        print("=== the server hands the template to the page ===")
        req = urllib.request.Request(base + "/api/server-info", headers={"Authorization": "Bearer t"})
        info = json.load(urllib.request.urlopen(req, timeout=10))
        check("/api/server-info carries issue_link from the conf", info.get("issue_link") == TPL,
              str(info.get("issue_link")))

        drv = Driver(wd, width=1100, height=900)
        drv.go(base + "/view?path=/dev/null")      # any page on the origin: we need a DOM
        drv.wait("document.body ? 1 : 0", tries=60)
        # The shipped passes, plus the two things they read from the app around them.
        drv.js(r"""
          window.authToken = 't';
          window.URL_RE = /(https?:\/\/[^\s<>"'）)\]]+)/g;
          window.fsFileUrl = (p) => location.origin + "/api/fs/file?path="
            + encodeURIComponent(p) + "&token=" + encodeURIComponent(authToken);
        """ + "\n".join(pieces) + """
          window.__t_mark = markPathLinks;
          window.__t_refs = linkifyRefs;
          window.__t_tpl = (v) => { ISSUE_TPL = v; };
          window.__t_render = (html) => {
            const d = document.createElement('div');
            d.innerHTML = html;
            __t_refs(d); __t_mark(d);
            return { text: d.textContent,
                     sups: [...d.querySelectorAll('a.path-sup')].map(a => a.getAttribute('href')),
                     where: [...d.querySelectorAll('a.path-sup')].map(
                       a => (a.previousSibling ? a.previousSibling.nodeName : '-') + '/' + a.parentElement.tagName),
                     refs: [...d.querySelectorAll('a.ref-link')].map(
                       a => a.textContent + ' -> ' + a.getAttribute('href')) };
          };
          return 1;
        """)
        drv.js("__t_tpl(%s); return 1;" % json.dumps(TPL))

        def render(html):
            return drv.js("return __t_render(%s);" % json.dumps(html))

        print("=== ↗ after a file path ===")
        r = render("<p>see <code>/Users/me/.cache/report/v6.html</code> and /tmp/a/b.md</p>")
        check("every file path gets one arrow", len(r["sups"]) == 2, str(r["sups"]))
        check("...pointing at the file, with the token",
              all("/api/fs/file?path=" in h and "&token=t" in h for h in r["sups"]), str(r["sups"]))
        check("...percent-encoded, so a path with a space cannot cut the query short",
              "%2FUsers%2Fme%2F.cache%2Freport%2Fv6.html" in r["sups"][0], r["sups"][0])
        check("the path text itself is untouched — it is what people copy",
              "/Users/me/.cache/report/v6.html" in r["text"] and "/tmp/a/b.md" in r["text"],
              r["text"][:90])
        check("...and at the end of a `code` the arrow sits outside the grey box",
              r["where"][0] == "CODE/P", str(r["where"]))

        print("=== a URL that nothing else linkified ===")
        # In a `code` span the linkifier above does not touch it, so the text cannot be
        # clicked — that is the one place a URL needs the arrow. "https://... 这样的是不是
        # 也应该有 nage sup-script"
        r = render("<p>open <code>https://host.example:8443/api/fs/file?path=%2Fa%2Fb.html&amp;token=t</code> now</p>")
        check("a URL inside `code` gets an arrow straight to it",
              r["sups"] == ["https://host.example:8443/api/fs/file?path=%2Fa%2Fb.html&token=t"],
              str(r["sups"]))
        check("...placed outside the grey box, like a path",
              r["where"] == ["CODE/P"], str(r["where"]))
        r = render('<p>see <a href="https://x.example/a">https://x.example/a</a> please</p>')
        check("a URL that IS a link gets none (the text already goes there)",
              not r["sups"], str(r["sups"]))
        r = render("<p>at <code>https://host.example/d/e.html</code> ok</p>")
        check("...and the path inside a URL is not also read as a local file",
              len(r["sups"]) == 1 and r["sups"][0].startswith("https://host.example/d/e.html"),
              str(r["sups"]))
        r = render("<p>both <code>/tmp/a/b.md and https://x.example/y.html</code></p>")
        check("a path and a URL in one span get one arrow each, in order",
              len(r["sups"]) == 2 and "%2Ftmp%2Fa%2Fb.md" in r["sups"][0]
              and r["sups"][1] == "https://x.example/y.html", str(r["sups"]))

        print("=== where it stays out of the way ===")
        for html, why in [("<p>the dir /Users/me/.cache/report/ is where they land</p>",
                           "a directory has nothing to open"),
                          ("<pre><code>--- /tmp/one/two.py\n+++ /tmp/one/three.py\n</code></pre>",
                           "a fenced block would be all arrows"),
                          ('<p><a href="/x">/Users/me/report/v6.html</a></p>',
                           "it is already a link")]:
            r = render(html)
            check(f"no arrow: {why}", not r["sups"], str(r["sups"]))

        print("=== #1234 as a link ===")
        r = render("<p>fixed in #1234, see also #7 — not #12345678</p>")
        check("a bare reference becomes a link",
              r["refs"][:2] == ["#1234 -> https://github.example/acme/widgets/pull/1234",
                                "#7 -> https://github.example/acme/widgets/pull/7"], str(r["refs"]))
        check("...and an eight-digit number is not one", len(r["refs"]) == 2, str(r["refs"]))
        check("...while the text still reads as written",
              "#1234" in r["text"] and "#12345678" in r["text"], r["text"])
        for html, why in [("<p>colour #1a2b3c here</p>", "a hex colour (#1 followed by a letter)"),
                          ("<p>build #12ab of it</p>", "digits with a word after them"),
                          ("<p>version v1.2#3 of it</p>", "part of a token"),
                          ("<p>run <code>sed -n '#42p'</code></p>", "inside code"),
                          ('<p><a href="https://x/y#12">https://x/y#12</a></p>', "a URL fragment")]:
            r = render(html)
            check(f"not linked: {why}", not r["refs"], str(r["refs"]))

        print("=== unset means nothing happens ===")
        drv.js("__t_tpl(''); return 1;")
        r = render("<p>fixed in #1234, file /tmp/a/b.md</p>")
        check("no template → #1234 stays plain text", not r["refs"] and "#1234" in r["text"],
              str(r["refs"]))
        check("...but the path arrow does not depend on it", len(r["sups"]) == 1, str(r["sups"]))
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
