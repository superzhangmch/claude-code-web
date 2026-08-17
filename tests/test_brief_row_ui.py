#!/usr/bin/env python3
"""Front-end test for the brief list's one-line row.

Extracts the REAL briefRow() out of static/index.html and runs it under node, so the row
the user actually sees is what gets asserted. It deliberately mirrors the ⇆ switcher's
shape (wXtY · sid4 · [tab-name] · session-name … last-use), because that is the layout
this view was asked for.

    python3 tests/test_brief_row_ui.py      # exit 0 = pass  (needs `node`)
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
    m = re.search(r"\n  (function briefRow\(s, singleWin\) \{.*?\n  \})\n", src, re.S)
    if not m:
        print("  FAIL  could not extract briefRow() from static/index.html"); return 1
    chrome = re.search(r"\n  (function syncBriefChrome\(\) \{.*?\n  \})\n", src, re.S)
    if not chrome:
        print("  FAIL  could not extract syncBriefChrome() from static/index.html"); return 1

    js = r"""
const _fails = [];
function check(name, cond, detail) {
  console.log((cond ? "  ok  " : "  FAIL") + "  " + name + (detail ? "  [" + detail + "]" : ""));
  if (!cond) _fails.push(name);
}
// minimal DOM: briefRow only creates spans and appends them
function mkEl(tag) {
  return { tagName: tag, className: "", textContent: "", children: [], dataset: {},
           classList: { add(c) { this.__el.className += " " + c; }, remove() {}, toggle() {} },
           appendChild(c) { this.children.push(c); return c; },
           addEventListener(ev, fn) { this.__click = fn; } };
}
const chromeEls = { "picker-quickfilter": { style: {} }, "picker-search": { style: {} } };
const document = { createElement: (t) => { const e = mkEl(t); e.classList.__el = e; return e; },
                   getElementById: (id) => chromeEls[id] || null };
let listBrief = true;
let entered = null, attached = null;
function enterTranscript(sid, label) { entered = { sid, label }; }
function attachSession(sid, label) { attached = { sid, label }; }
const clampU = (s, n) => (s.length > n ? s.slice(0, n) + "…" : s);
const tabLabel = (s) => clampU(s, 24);
let attachedSid = "";

__ROW__
__CHROME__

const parts = (row) => row.children.map(c => c.className.trim() + "=" + c.textContent);
const text = (row) => row.children.map(c => c.textContent).join("|");
// pick a span by class, not by position — the row omits spans it has no data for
const span = (row, cls) => (row.children.find(c => c.className.trim().split(/\s+/).includes(cls)) || {}).textContent;

console.log("=== a live tab row ===");
let s = { claude_session_id: "d585bf36-aaaa-bbbb", group: "tabs", window_index: 0, tab_index: 0,
          tab_name: "Compare Hermes and the others", user_name: "", summary_title: "本地AI Agent 对比",
          summary: "…", bound: true, last_visit: "08-17 11:38" };
let row = briefRow(s, false);
check("window/tab comes first", parts(row)[0] === "sw-wt=w1t1", parts(row)[0]);
check("...then the 4-char session id", parts(row)[1] === "sw-sid=d585", parts(row)[1]);
check("...then the terminal tab name in brackets",
      /^sw-tab=\[Compare Hermes/.test(parts(row)[2]), parts(row)[2]);
check("...then the session name", span(row, "sw-sess") === "本地AI Agent 对比", span(row, "sw-sess"));
check("...and the last-use time last", parts(row).at(-1) === "br-time=08-17 11:38", parts(row).at(-1));
check("a bound session is marked", parts(row).some(p => p.startsWith("br-dot=")), parts(row).join(" "));
check("no transcript excerpt anywhere (brief carries none)", !text(row).includes("…\n"));

console.log("=== a single-window machine drops the wX ===");
row = briefRow(s, true);
check("t1 instead of w1t1", parts(row)[0] === "sw-wt=t1", parts(row)[0]);
attachedSid = "d585bf36-aaaa-bbbb";
row = briefRow(s, true);
check("the tab you are in is marked with *", parts(row)[0] === "sw-wt=t1*", parts(row)[0]);
check("...and the row is styled current", / current/.test(row.className), row.className);
attachedSid = "";

console.log("=== names: user override wins, summary is the last resort ===");
row = briefRow({ ...s, user_name: "my own name" }, true);
check("a user-set name beats the LLM title", span(row, "sw-sess") === "my own name", span(row, "sw-sess"));
row = briefRow({ ...s, user_name: "", summary_title: "", title: "", summary: "只有摘要" }, true);
check("with no name at all the summary is shown", span(row, "sw-sess") === "只有摘要", span(row, "sw-sess"));
row = briefRow({ ...s, tab_name: "", user_name: "", summary_title: "", title: "", summary: "" }, true);
check("with nothing at all it says so rather than rendering blank",
      text(row).includes("(no title yet)"), text(row));

console.log("=== an approximate timestamp is marked, not presented as fact ===");
row = briefRow({ ...s, ts_approx: true }, true);
check("~ prefixes a mtime-derived time", parts(row).at(-1) === "br-time=~08-17 11:38", parts(row).at(-1));

console.log("=== clicking ===");
row = briefRow({ ...s, bound: true }, true); row.__click();
check("a bound session opens its transcript", entered && entered.sid === "d585bf36-aaaa-bbbb",
      JSON.stringify(entered));
entered = null;
row = briefRow({ ...s, bound: false }, true); row.__click();
check("an unbound one goes through attach", attached && attached.sid === "d585bf36-aaaa-bbbb",
      JSON.stringify(attached));

console.log("=== brief hides the search chrome ===");
listBrief = true; syncBriefChrome();
check("the quick filter is hidden in brief", chromeEls["picker-quickfilter"].style.display === "none");
check("the full-search panel is hidden in brief", chromeEls["picker-search"].style.display === "none");
listBrief = false; syncBriefChrome();
check("...and both come back in full", chromeEls["picker-quickfilter"].style.display === ""
      && chromeEls["picker-search"].style.display === "");

console.log(_fails.length ? "\nFAILED: " + _fails.join(", ") : "\nall pass");
process.exit(_fails.length ? 1 : 0);
"""
    js = js.replace("__ROW__", m.group(1)).replace("__CHROME__", chrome.group(1))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(js)
        path = fh.name
    try:
        r = subprocess.run([node, path], capture_output=True, text=True)
        print(r.stdout.rstrip())
        if r.returncode and r.stderr:
            print(r.stderr[:900])
        return r.returncode
    finally:
        os.unlink(path)


if __name__ == "__main__":
    sys.exit(main())
