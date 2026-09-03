#!/usr/bin/env python3
"""The links popup's path resolution + direct-jump URL.

A path written in a transcript is often relative to the session's cwd and written WITH
a leading slash ("/differential_geometry/riemannian_geometry.html"), so the string
alone cannot say which reading is real. fsResolve() asks the server. This extracts the
REAL fsResolve/fsFileUrl out of static/index.html and runs them under node against a
stubbed fetch, so what is asserted is what ships.

Also checks the server side that makes the asking cheap: HEAD on /api/fs/file.

    python3 tests/test_links_jump.py      # exit 0 = pass  (needs `node`)
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "static", "index.html")
SERVER = os.path.join(ROOT, "cc_web.py")


def main():
    node = shutil.which("node")
    if not node:
        print("SKIP: needs node"); return 0

    src = open(INDEX, encoding="utf-8").read()
    deps = []
    for pat, what in ((r"\n  (const fsFileUrl = .*?;)\n", "fsFileUrl"),
                      # the per-cwd+path cache fsResolve() memoises into — extracted,
                      # not restated, so the "asked once per row" assertion is about
                      # the real cache and not a copy of it
                      (r"\n  (const _fsResolved = new Map\(\);)\n", "_fsResolved"),
                      (r"\n  (function fsResolve\(p\) \{.*?\n  \})\n", "fsResolve")):
        hit = re.search(pat, src, re.S)
        if not hit:
            print(f"  FAIL  could not extract {what} from static/index.html"); return 1
        deps.append(hit.group(1))

    js = r"""
const _fails = [];
function check(name, cond, detail) {
  console.log((cond ? "  ok  " : "  FAIL") + "  " + name + (detail ? "  [" + detail + "]" : ""));
  if (!cond) _fails.push(name);
}
const location = { origin: "https://box.ts.net:8443" };
let authToken = "tok-123";
let CWD = "/Users/me/Desktop/demo";
function _sessionCwd() { return CWD; }
// The stub server: only these paths exist. Every probe is recorded, so the ORDER of
// the candidates is asserted, not just the winner.
let EXISTS = new Set(), asked = [];
async function authedFetch(url, opts) {
  const p = decodeURIComponent(new URL(url, location.origin).searchParams.get("path"));
  asked.push((opts && opts.method) + " " + p);
  return { ok: EXISTS.has(p) };
}
const _fsResolvedReset = () => { for (const k of [..._fsResolved.keys()]) _fsResolved.delete(k); };

__DEPS__

console.log("=== the URL a direct jump goes to ===");
const u = fsFileUrl("/Users/me/Desktop/demo/differential_geometry/riemannian_geometry.html");
check("it is this instance's own /api/fs/file",
      u.startsWith("https://box.ts.net:8443/api/fs/file?path="), u);
check("...with the path percent-encoded", u.includes("%2FUsers%2Fme%2FDesktop%2Fdemo%2F"), u);
check("...and the token in the query, since a browser tab can't send a header",
      u.endsWith("&token=tok-123"), u.slice(-30));
authToken = "a b&c=d";
check("a token needing escapes is escaped, not concatenated raw",
      fsFileUrl("/x").endsWith("&token=a%20b%26c%3Dd"), fsFileUrl("/x"));
authToken = "tok-123";

console.log("=== resolving what the transcript wrote ===");
(async () => {
  // 1. a real absolute path: taken as-is, and nothing else is even tried
  _fsResolvedReset(); asked = []; EXISTS = new Set(["/Users/me/Desktop/demo/a.html"]);
  let hit = await fsResolve("/Users/me/Desktop/demo/a.html");
  check("an absolute path that exists is used unchanged", hit === "/Users/me/Desktop/demo/a.html", String(hit));
  check("...and no second guess is made", asked.length === 1, asked.join(" | "));
  check("...asked with HEAD, so no file is transferred", asked[0].startsWith("HEAD "), asked[0]);

  // 2. THE case from the screenshot: looks absolute, is relative to the cwd
  _fsResolvedReset(); asked = [];
  EXISTS = new Set(["/Users/me/Desktop/demo/differential_geometry/riemannian_geometry.html"]);
  hit = await fsResolve("/differential_geometry/riemannian_geometry.html");
  check("a leading-slash path that is really cwd-relative resolves under the cwd",
        hit === "/Users/me/Desktop/demo/differential_geometry/riemannian_geometry.html", String(hit));
  check("...literal reading tried FIRST (a real absolute path must win)",
        asked[0].endsWith(" /differential_geometry/riemannian_geometry.html"), asked.join(" | "));
  check("...and exactly two candidates, joined without a doubled slash",
        asked.length === 2 && !asked[1].includes("//"), asked.join(" | "));

  // 3. nowhere to be found → null, so the caller can dim the button instead of
  //    handing over a link that 404s
  _fsResolvedReset(); asked = []; EXISTS = new Set();
  hit = await fsResolve("/nope/x.html");
  check("a path that is nowhere resolves to null", hit === null, String(hit));
  check("...after trying both readings", asked.length === 2, asked.join(" | "));

  // 4. one answer per row, not three: the viewer, the jump link and the thumbnail
  //    all ask, and a popup with twenty rows should not fire sixty requests
  _fsResolvedReset(); asked = []; EXISTS = new Set(["/Users/me/Desktop/demo/b.png"]);
  await Promise.all([fsResolve("/b.png"), fsResolve("/b.png"), fsResolve("/b.png")]);
  check("the answer is cached per path", asked.length === 2, asked.join(" | "));

  // 5. the cache is keyed by cwd too — the same relative path means a different file
  //    in a different session, and a stale hit would open the wrong file
  asked = []; CWD = "/Users/me/other";
  EXISTS = new Set(["/Users/me/other/b.png"]);
  hit = await fsResolve("/b.png");
  check("...but re-asked when the session's cwd differs", hit === "/Users/me/other/b.png", String(hit));

  // 6. no cwd known (never attached) → only the literal reading is possible
  _fsResolvedReset(); asked = []; CWD = ""; EXISTS = new Set();
  await fsResolve("/x.html");
  check("with no cwd there is nothing to join, so only one probe",
        asked.length === 1, asked.join(" | "));

  // 7. an absolute path already under the cwd must not be joined onto it again
  _fsResolvedReset(); asked = []; CWD = "/Users/me/Desktop/demo"; EXISTS = new Set();
  await fsResolve("/Users/me/Desktop/demo/deep/x.html");
  check("a path already under the cwd is not doubled up",
        asked.length === 1, asked.join(" | "));

  console.log(_fails.length ? "\nFAILED: " + _fails.join(", ") : "\nall pass");
  process.exit(_fails.length ? 1 : 0);
})();
"""
    js = js.replace("__DEPS__", "\n".join(deps))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(js)
        path = fh.name
    try:
        r = subprocess.run([node, path], capture_output=True, text=True)
        print(r.stdout.rstrip())
        if r.returncode and r.stderr:
            print(r.stderr[:900])
        rc = r.returncode
    finally:
        os.unlink(path)

    # The server half: the probe above is only cheap because HEAD is served. If this
    # route ever goes back to GET-only, every resolution downloads the file.
    print("=== the server answers HEAD, which is what makes asking cheap ===")
    server = open(SERVER, encoding="utf-8").read()
    m = re.search(r'@app\.api_route\("/api/fs/file", methods=\[([^\]]*)\]\)', server)
    methods = (m.group(1) if m else "")
    ok = bool(m) and '"HEAD"' in methods and '"GET"' in methods
    print(("  ok  " if ok else "  FAIL") + "  /api/fs/file serves GET and HEAD  [" + (methods or "GET only") + "]")
    return rc or (0 if ok else 1)


if __name__ == "__main__":
    sys.exit(main())
