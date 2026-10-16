#!/usr/bin/env python3
"""🔗 finds the paths in a message — including the ones with Chinese in the name.

Reported: `~/Desktop/卡片现状_v7.html` never appeared in the list. The matcher used
`[\\w.\\-+@]`, and in JavaScript `\\w` is `[A-Za-z0-9_]` — so a CJK filename matched
nothing at all, and `/Users/me/x/卡片.md` came out truncated at the first Chinese
character (`/Users/me/x/`), which is a path to a directory that is not what was written.
Anyone writing in Chinese had a link button that could not see half their files.

Unicode classes fix both, and bring two things with them: `\\p{L}\\p{N}` excludes CJK
punctuation by construction (so 。、，after a path are not eaten into it), and a path
written as `~/one-segment.md` — which the old "two segments or more" rule missed — is
found too.

The boundary group in front is a stand-in for a lookbehind (iOS Safari only got those in
16.4). Without it the `/or` in `and/or` is a file path.

This drives the real extractLinks() out of the page under node.

    python3 tests/test_extract_links.py      # exit 0 = pass  (needs `node`)
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "static", "index.html")

JS = r"""
const _fails = [];
function check(name, cond, detail) {
  console.log((cond ? "  ok  " : "  FAIL") + "  " + name + (detail ? "  [" + detail + "]" : ""));
  if (!cond) _fails.push(name);
}
__EXTRACT__
const P = (t) => extractLinks(t).paths;
const U = (t) => extractLinks(t).urls;
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

console.log("=== the one that was reported ===");
check("a ~ path with a Chinese name is found",
      eq(P("~/Desktop/卡片现状_v7.html"), ["~/Desktop/卡片现状_v7.html"]),
      JSON.stringify(P("~/Desktop/卡片现状_v7.html")));
check("...in a sentence, with Chinese punctuation after it",
      eq(P("文件在 ~/Desktop/卡片现状_v7.html 里。"), ["~/Desktop/卡片现状_v7.html"]),
      JSON.stringify(P("文件在 ~/Desktop/卡片现状_v7.html 里。")));
// The quieter half of the same bug: it matched, but stopped at the first CJK byte, so
// the row offered a DIRECTORY that happens to exist. Worse than finding nothing.
check("an absolute path with a Chinese name is not truncated",
      eq(P("/Users/me/x/卡片.md"), ["/Users/me/x/卡片.md"]),
      JSON.stringify(P("/Users/me/x/卡片.md")));
check("...and accented Latin works for the same reason",
      eq(P("/tmp/résumé/notes.md"), ["/tmp/résumé/notes.md"]),
      JSON.stringify(P("/tmp/résumé/notes.md")));

console.log("=== what it used to miss, and what it must still refuse ===");
check("one segment under ~ is a path", eq(P("~/notes.md"), ["~/notes.md"]));
check("...and under ./ or ../ too",
      eq(P("./rel/路径.txt"), ["./rel/路径.txt"]) && eq(P("../up/one.md"), ["../up/one.md"]));
check("a bare /x still needs two segments (so 'and/or' is not a file)",
      eq(P("and/or"), []) && eq(P("A/B 不是路径"), []), JSON.stringify(P("and/or")));
check("...but /etc/hosts is", eq(P("读 /etc/hosts 吧"), ["/etc/hosts"]), JSON.stringify(P("读 /etc/hosts 吧")));
check("several in one line, in order",
      eq(P("两个 /a/b.md 和 ~/c/d.json"), ["/a/b.md", "~/c/d.json"]),
      JSON.stringify(P("两个 /a/b.md 和 ~/c/d.json")));
check("brackets and backticks around it are not part of it",
      eq(P("(/tmp/x/y.log)"), ["/tmp/x/y.log"]) && eq(P("`~/.claude/cc_web.conf`"), ["~/.claude/cc_web.conf"]),
      JSON.stringify(P("(/tmp/x/y.log)")) + JSON.stringify(P("`~/.claude/cc_web.conf`")));
check("a trailing full stop is punctuation, not the file",
      eq(P("见 /tmp/a/b.txt."), ["/tmp/a/b.txt"]), JSON.stringify(P("见 /tmp/a/b.txt.")));
check("the same path twice is one row", eq(P("/a/b.md 和 /a/b.md"), ["/a/b.md"]));

console.log("=== URLs are still URLs, and are not re-read as paths ===");
check("a url is found whole",
      eq(U("see https://x.com/a/b?q=1 here"), ["https://x.com/a/b?q=1"]),
      JSON.stringify(U("see https://x.com/a/b?q=1 here")));
check("...and its path part is not ALSO listed as a local file",
      eq(P("see https://x.com/a/b here"), []), JSON.stringify(P("see https://x.com/a/b here")));

console.log(_fails.length ? "\nFAILED: " + _fails.join(", ") : "\nall pass");
process.exit(_fails.length ? 1 : 0);
"""


def main():
    node = shutil.which("node")
    if not node:
        print("SKIP: needs node"); return 0
    src = open(INDEX, encoding="utf-8").read()
    # extractLinks shares its path pattern with the ↗ badge in a message, so the
    # constants come along — one reading of "what is a path", not two.
    parts = []
    for pat, what in ((r'\n  (const PATH_SEG = .*?\+ "\)\+\\\\/\?\)";)\n', "PATH_PATTERN"),
                      (r"\n  (function extractLinks\(text\) \{.*?\n  \})\n", "extractLinks")):
        m = re.search(pat, src, re.S)
        if not m:
            print(f"  FAIL  could not extract {what} from static/index.html"); return 1
        parts.append(m.group(1))
    js = JS.replace("__EXTRACT__", "\n".join(parts))
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as fh:
        fh.write(js); path = fh.name
    try:
        r = subprocess.run([node, path], capture_output=True, text=True)
        print(r.stdout.rstrip())
        if r.returncode and r.stderr:
            print(r.stderr[:1200])
        return r.returncode
    finally:
        os.unlink(path)


if __name__ == "__main__":
    sys.exit(main())
