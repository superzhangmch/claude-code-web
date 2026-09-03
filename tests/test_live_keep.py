#!/usr/bin/env python3
"""The live tail's commit box: offer to keep it only if someone was looking.

"gone in 10s · keep 5m · remove" exists for one reason: if you are mid-read when a
turn ends, the tail must not flash away. It follows that when you were NOT looking
there is nothing to protect — the finished messages are in the transcript below — and
the box should not appear at all. It used to: coming back to a session whose turn had
ended while the page was away re-armed a fresh 10s countdown on minutes-old content.

Extracts the REAL hbStale/markCommitted/sweepBuf out of static/index.html and runs
them under node.

    python3 tests/test_live_keep.py      # exit 0 = pass  (needs `node`)
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "static", "index.html")


def main():
    node = shutil.which("node")
    if not node:
        print("SKIP: needs node"); return 0

    src = open(INDEX, encoding="utf-8").read()
    deps = []
    for pat, what in ((r"\n  (const COMMIT_TTL = .*?)\n", "COMMIT_TTL"),
                      (r"\n  (const KEEP_MS = .*?)\n", "KEEP_MS"),
                      (r"\n  (const LIVE_CAP = .*?)\n", "LIVE_CAP"),
                      (r"\n  (const STALE_MS = .*?;)\n", "STALE_MS"),
                      (r"\n  (const hbStale = \(\) => .*?;)\n", "hbStale"),
                      (r"\n  (function _normMatch\(s\) \{.*?\})\n", "_normMatch"),
                      (r"\n  (function mkLine\(raw\) \{.*?\})\n", "mkLine"),
                      (r"\n  (function sweepBuf\(\) \{.*?\n  \})\n", "sweepBuf"),
                      (r"\n  (function markCommitted\(msgText\) \{.*?\n  \})\n", "markCommitted")):
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
let liveLines = 3;          // full mode (2 = the concise probe, which has no buffer)
let hbBuf = [], _lineSeq = 0;
let hbLastChange = 0, hbAwayAt = 0;

__DEPS__

const MSG = "the answer is forty two";
function fill() {
  hbBuf = ["thinking about it", "still thinking", MSG, "next turn starts here"].map(mkLine);
}
const committedCount = () => hbBuf.filter((l) => l.committed).length;

console.log("=== watching: the box appears and counts down ===");
hbLastChange = Date.now(); hbAwayAt = 0;
fill();
markCommitted(MSG);
check("hbStale() is false while watching", hbStale() === false, String(hbStale()));
check("the matched prefix is committed", committedCount() === 3, String(committedCount()));
const exp = Math.max(...hbBuf.filter((l) => l.committed).map((l) => l.expireAt));
check("...with a countdown about COMMIT_TTL long, so a read is not interrupted",
      exp - Date.now() > COMMIT_TTL - 1500 && exp - Date.now() <= COMMIT_TTL,
      (exp - Date.now()) + "ms of " + COMMIT_TTL);
sweepBuf();
check("...and it survives the sweep (it has not expired yet)", hbBuf.length === 4, String(hbBuf.length));

console.log("=== away since it last changed: no offer, it just goes ===");
// The reported case: the turn ended while the page was hidden / on another session.
hbLastChange = Date.now() - 20 * 1000;    // changed 20s ago — recent by the clock alone
hbAwayAt = Date.now() - 5 * 1000;         // ...but watching stopped AFTER that
fill();
check("hbStale() is true", hbStale() === true, String(hbStale()));
markCommitted(MSG);
check("the stale prefix is gone, not counted down",
      hbBuf.length === 1 && hbBuf[0].raw === "next turn starts here",
      hbBuf.map((l) => l.raw).join(" | "));

console.log("=== never hidden, but the tail last changed long ago ===");
// A phone left face-up fires no visibility event, so the wall clock is the only signal.
hbLastChange = Date.now() - (STALE_MS + 5000); hbAwayAt = 0;
check("past STALE_MS it is stale", hbStale() === true, String(hbStale()));
fill(); markCommitted(MSG);
check("...so that content goes too", hbBuf.length === 1, String(hbBuf.length));
hbLastChange = Date.now() - (STALE_MS - 15000);
check("just inside the window it is NOT stale (a slow turn is still being watched)",
      hbStale() === false, String(hbStale()));
check("...and the threshold is far enough out to clear the 48s fetch backoff",
      STALE_MS > 48000 + 30000, String(STALE_MS));

console.log("=== a block you explicitly kept is not collateral ===");
hbLastChange = Date.now() - 20 * 1000; hbAwayAt = Date.now() - 5 * 1000;
fill();
hbBuf[0].committed = true; hbBuf[0].kept = true; hbBuf[0].expireAt = Date.now() + KEEP_MS;
markCommitted(MSG);
check("the pinned line stays (you asked for it), the rest does not",
      hbBuf.some((l) => l.kept) && !hbBuf.some((l) => l.committed && !l.kept),
      hbBuf.map((l) => l.raw + (l.kept ? "*" : "")).join(" | "));
check("...and it is retired by its own expiry, not held forever", (() => {
  hbBuf.forEach((l) => { if (l.kept) l.expireAt = Date.now() - 1; });
  sweepBuf();
  return !hbBuf.some((l) => l.kept);
})());

console.log("=== nothing fetched yet is not 'stale' ===");
// hbLastChange 0 means this attach has seen no content at all; calling that stale
// would make the predicate true forever, killing the offer even while you watch.
hbLastChange = 0; hbAwayAt = Date.now();
check("no content seen → not stale", hbStale() === false, String(hbStale()));

console.log(_fails.length ? "\nFAILED: " + _fails.join(", ") : "\nall pass");
process.exit(_fails.length ? 1 : 0);
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

    # The other commit site is the idle branch of hbTick(), which is inline in a
    # function with too many collaborators to run here. Assert the wire is connected:
    # without this, the box still appears on re-attach to a session that went idle
    # while away, and every assertion above would keep passing.
    print("=== the idle path asks the same question ===")
    idle = re.search(r"if \(claudeIdle\) \{.*?\n      return;", src, re.S)
    body = idle.group(0) if idle else ""
    ok = "hbStale()" in body
    print(("  ok  " if ok else "  FAIL") + "  hbTick()'s idle branch consults hbStale() before committing the tail")
    ok2 = bool(re.search(r"hbAwayAt = Date\.now\(\);", src)) and src.count("hbLastChange = Date.now()") >= 3
    print(("  ok  " if ok2 else "  FAIL")
          + "  both stamps are fed: away on stopHeartbeat, change on each live fetch"
          + "  [" + str(src.count("hbLastChange = Date.now()")) + " change sites]")
    return rc or (0 if (ok and ok2) else 1)


if __name__ == "__main__":
    sys.exit(main())
