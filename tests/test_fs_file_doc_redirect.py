#!/usr/bin/env python3
"""Opening a text file's URL in a browser lands on the reading page, not on bytes.

`/api/fs/file?path=…&token=…` is the raw endpoint, and it has to stay raw: it is what
`<img>` tags, PDF tabs and "copy link address" use. But typing one of those URLs into a
browser — or clicking one copied days ago — for a .txt gave a wall of unwrapped text with
no search, no rendering and nothing to refresh back into. The reading page already
existed; the link just never pointed at it.

So a TOP-LEVEL NAVIGATION to a text file redirects to it. Everything else is untouched,
and each exclusion is a real caller:

  * sec-fetch-dest: empty   → fetch()/XHR. The viewer itself reads the file through here.
  * image / iframe          → thumbnails and the browser's own preview pane.
  * HEAD                    → the "does this path exist" probe.
  * &raw=1                  → "no, really, give me the file".
  * not text / over 2MB     → not a document.

    python3 tests/test_fs_file_doc_redirect.py      # exit 0 = pass
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


class Req:
    """The two things the endpoint reads off the request."""
    def __init__(self, method="GET"):
        self.method = method


def main():
    home = tempfile.mkdtemp(prefix="ccweb-red-")
    os.makedirs(os.path.join(home, ".claude"))
    open(os.path.join(home, ".claude", "cc_web.conf"), "w").write("token=t\n")
    os.environ["HOME"] = home
    os.environ["CC_WEB_TOKEN"] = "t"
    try:
        import cc_web
    except Exception as e:                                  # pragma: no cover
        print("SKIP: cannot import cc_web:", e); return 0

    doc = os.path.join(home, "a.txt"); open(doc, "w").write("hello\n")
    cjk = os.path.join(home, "卡片 现状&v7.md"); open(cjk, "w").write("# hi\n")
    img = os.path.join(home, "b.png"); open(img, "wb").write(b"\x89PNG\r\n\x1a\n" + b"0" * 50)
    big = os.path.join(home, "big.txt"); open(big, "w").write("x" * (3 * 1024 * 1024))

    def call(p, method="GET", dest="document", raw=""):
        r = cc_web.fs_file(Req(method), p, token="t", raw=raw, authorization="Bearer t",
                           sec_fetch_dest=dest)
        return r

    print("=== a text file, opened as a page ===")
    r = call(doc)
    check("a navigation is redirected", getattr(r, "status_code", None) == 302,
          type(r).__name__)
    # The token rides along. `?token=` on this endpoint exists so a pasted link works
    # in a browser that has never been unlocked — a redirect that dropped it turned
    # exactly that link into an "Auth required" screen (measured in a clean browser
    # against the real server, which is how it was found).
    check("...to the reading page for that file, token and all",
          r.headers.get("location") == "/view?path=" + doc + "&token=t", r.headers.get("location"))
    hdr_only = cc_web.fs_file(Req(), doc, token="", raw="", authorization="Bearer t",
                              sec_fetch_dest="document")
    check("...and a header-authenticated caller is not given one it did not have",
          hdr_only.headers.get("location") == "/view?path=" + doc, hdr_only.headers.get("location"))
    # The fragment is built with quote(), so a name with a space or an & cannot cut the
    # hash short (the page reads /[#&]file=([^&]+)/).
    loc = call(cjk).headers.get("location", "")
    check("...with the path escaped, so a space or an & cannot cut the query short",
          "%20" in loc and "%26" in loc and loc.count("&") <= 1, loc)

    print("=== and everything that is not that is untouched ===")
    for name, kw in [("fetch()/XHR", {"dest": "empty"}),
                     ("no Sec-Fetch-Dest at all (curl, old browsers)", {"dest": None}),
                     ("an <img> or an <iframe>", {"dest": "image"}),
                     ("&raw=1", {"raw": "1"}),
                     ("HEAD, the existence probe", {"method": "HEAD"})]:
        got = call(doc, **kw)
        check(f"{name} still gets the file", type(got).__name__ == "FileResponse",
              type(got).__name__)
    check("an image still gets the file", type(call(img)).__name__ == "FileResponse")
    # A PAGE is not a document to read the source of — it was served as its own markup
    # once, through here. Source files are: they get colour and line numbers there.
    for name, want in (("report.html", "FileResponse"), ("icon.svg", "FileResponse"),
                       ("prog.py", "RedirectResponse"), ("run.sh", "RedirectResponse"),
                       ("conf.yaml", "RedirectResponse")):
        f = os.path.join(home, name)
        open(f, "w").write("<h1>x</h1>" if name.endswith((".html", ".svg")) else "x = 1\n")
        verb = "is served as-is" if want == "FileResponse" else "goes to the reader"
        check(f"a .{name.split('.')[-1]} navigation {verb}",
              type(call(f)).__name__ == want, type(call(f)).__name__)
    check("a 3MB text file still gets the file (not a document)",
          type(call(big)).__name__ == "FileResponse")

    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
