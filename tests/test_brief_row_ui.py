#!/usr/bin/env python3
"""Front-end test for the brief list's one-line row.

Extracts the REAL briefRow() out of static/index.html and runs it under node, so the row
the user actually sees is what gets asserted. It deliberately mirrors the ⇆ switcher's
shape (sid4 · [tab-name] · session-name … last-use), because that is the layout this
view was asked for. The wXtY chip the switcher shows is deliberately NOT here: the names
are what you read, and the Tabs group is still sorted by tab position.

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
// sessionLine() cleans names itself now; mirror the real stripTab closely enough that
// the "decorations are stripped" assertions mean something.
const stripTab = (s) => (s || "")
  .replace(/^[\s\u2800-\u28ff✳✻✽✢✣✱●○◍•·]+/, "")
  .replace(/\s*\((?:claude|caffeinate)\)\s*$/i, "")
  .trim();
let attachedSid = "";
// treePlan works through this one lookup; the test states the shape directly.
let TREE = {};
const parentOf = (sid) => TREE[sid] || "";
// What makeCopyBtn needs: the two icons, an agent name (the copy text is keyed by it)
// and a clipboard. The clipboard is a SPY, not a real one — headless browsers refuse
// both writeText and execCommand, so asserting the button's TEXT is the only way to
// assert what it copies.
const COPY_SVG = "<copy/>", DONE_SVG = "<done/>";
let AGENT = "claude";
let copied = null;
const navigator = { clipboard: { writeText: (t) => { copied = t; return Promise.resolve(); } } };
const location = { host: "somehost.ts.net:8443" };
const setTimeout = () => 0;

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
// The layout is the ⇆ switcher's, deliberately: one shape everywhere a session is
// listed. (An earlier revision dropped the position chip here; it came back when the
// switcher's line became the one format for all four lists.)
check("the tab position leads", parts(row)[0] === "sw-wt=w1t1", parts(row)[0]);
check("...then the 4-char session id", parts(row)[1] === "sw-sid=d585", parts(row)[1]);
check("...then the terminal tab name in brackets",
      /^sw-tab=\[Compare Hermes/.test(parts(row)[2]), parts(row)[2]);
check("a single-window machine drops the wX",
      parts(briefRow(s, true))[0] === "sw-wt=t1", parts(briefRow(s, true))[0]);
check("...then the session name", span(row, "sw-sess") === "本地AI Agent 对比", span(row, "sw-sess"));
// Last of the TEXT columns — the copy button sits after it, at the row's right edge.
check("...and the last-use time last", span(row, "br-time") === "08-17 11:38", span(row, "br-time"));
check("...with the copy button after it, at the edge",
      row.children.at(-1).tagName === "button", row.children.at(-1).tagName);
check("a bound session is marked", parts(row).some(p => p.startsWith("br-dot=")), parts(row).join(" "));
check("no transcript excerpt anywhere (brief carries none)", !text(row).includes("…\n"));

console.log("=== the tab you are in ===");
attachedSid = "d585bf36-aaaa-bbbb";
row = briefRow(s, true);
check("is marked with * on the position, like the switcher",
      parts(row)[0] === "sw-wt=t1*", parts(row)[0]);
check("...and by the row's accent border", / current/.test(row.className), row.className);
attachedSid = "";

console.log("=== names: user override wins, summary is the last resort ===");
row = briefRow({ ...s, user_name: "my own name" }, true);
check("a user-set name beats the LLM title", span(row, "sw-sess") === "my own name", span(row, "sw-sess"));
row = briefRow({ ...s, user_name: "", summary_title: "", title: "", summary: "只有摘要" });
check("with no name at all the summary is shown", span(row, "sw-sess") === "只有摘要", span(row, "sw-sess"));
row = briefRow({ ...s, tab_name: "", user_name: "", summary_title: "", title: "", summary: "" });
check("with nothing at all it says so rather than rendering blank",
      text(row).includes("(no title yet)"), text(row));

console.log("=== an approximate timestamp is marked, not presented as fact ===");
row = briefRow({ ...s, ts_approx: true });
check("~ prefixes a mtime-derived time", span(row, "br-time") === "~08-17 11:38", span(row, "br-time"));

console.log("=== the time column is an AGE, not a date ===");
// "09-01 10:18" is eleven near-identical characters per row; the age is what is read.
const NOW = Date.now() / 1000;
const age = (secsAgo, extra) => span(briefRow({ ...s, mtime: NOW - secsAgo, ...extra }), "br-time");
check("minutes under an hour", age(15 * 60) === "15m", age(15 * 60));
check("...with a decimal below ten of a unit", age(3.4 * 60) === "3.4m", age(3.4 * 60));
check("hours under a day", age(5.2 * 3600) === "5.2h", age(5.2 * 3600));
check("...and integer hours above ten", age(18 * 3600) === "18h", age(18 * 3600));
check("days after that", age(47 * 86400) === "47d", age(47 * 86400));
check("...and a decimal for the first ten days", age(1.1 * 86400) === "1.1d", age(1.1 * 86400));
check("just now reads as now, and a clock-skewed future does too",
      age(2) === "now" && age(-600) === "now", age(2) + "/" + age(-600));
check("still marked ~ when the epoch came from the file mtime",
      age(3 * 86400, { ts_approx: true }) === "~3.0d", age(3 * 86400, { ts_approx: true }));
// Nothing is lost: the absolute stamp moves to the hover text.
row = briefRow({ ...s, mtime: NOW - 47 * 86400 });
const tEl = row.children.find(c => c.className.trim() === "br-time");
check("the absolute stamp is on hover", tEl.title === "08-17 11:38", String(tEl.title));
check("an old server with no mtime still shows its formatted stamp",
      span(briefRow({ ...s, mtime: 0 }), "br-time") === "08-17 11:38",
      span(briefRow({ ...s, mtime: 0 }), "br-time"));

console.log("=== the row copies the session identifier without opening it ===");
row = briefRow({ ...s, bound: true });
const btn = row.children.at(-1);
copied = null; entered = null; attached = null;
btn.__click({ stopPropagation() {} });
check("clicking it copies the identifier the transcript header copies",
      copied === "claude_code_session=d585bf36-aaaa-bbbb at somehost.ts.net:8443"
              + ", with tab_name=Compare Hermes and the others", String(copied));
check("...and does NOT enter or attach the session",
      entered === null && attached === null, JSON.stringify({ entered, attached }));
AGENT = "codex";
row = briefRow({ ...s, bound: true }); copied = null;
row.children.at(-1).__click({ stopPropagation() {} });
check("on a codex instance it says codex_session=, like the header does",
      copied.startsWith("codex_session=d585bf36"), String(copied));
AGENT = "claude";
// Caught live on x13: the row copied "tab_name=✳ Compare Hermes…" while the header
// copied the same name WITHOUT the terminal's activity glyph — two strings for one
// thing, which is the one failure this button was supposed to make impossible.
row = briefRow({ ...s, tab_name: "✳ Compare Hermes and the others (claude)" }); copied = null;
row.children.at(-1).__click({ stopPropagation() {} });
check("the terminal's decorations are stripped, exactly as the header strips them",
      copied.endsWith(", with tab_name=Compare Hermes and the others"), String(copied));
check("...and the name is NOT clamped like the row's display is",
      !copied.includes("…"), String(copied));
row = briefRow({ ...s, tab_name: "" }); copied = null;
row.children.at(-1).__click({ stopPropagation() {} });
check("a session with no tab name drops the clause instead of copying an empty one",
      copied === "claude_code_session=d585bf36-aaaa-bbbb at somehost.ts.net:8443", String(copied));

console.log("=== clicking ===");
row = briefRow({ ...s, bound: true }); row.__click();
check("a bound session opens its transcript", entered && entered.sid === "d585bf36-aaaa-bbbb",
      JSON.stringify(entered));
entered = null;
row = briefRow({ ...s, bound: false }); row.__click();
check("an unbound one goes through attach", attached && attached.sid === "d585bf36-aaaa-bbbb",
      JSON.stringify(attached));

console.log("=== a session sitting in two tabs ===");
// The real case: session 3982d22e ran in w1t3 AND w1t15. The row says only how many —
// short, and right after the sid, since the sid is what is duplicated. WHICH other tabs
// is in the tooltip (a row can only show its own position, so the count sitting next to
// "t3" read as a contradiction when the second copy was at t15).
const twoTabs = [{ window_index: 0, tab_index: 2 }, { window_index: 0, tab_index: 14 }];
row = briefRow({ ...s, tab_index: 2, tab_count: 2, tab_positions: twoTabs }, true);
check("the row still leads with its own position", span(row, "sw-wt") === "t3", span(row, "sw-wt"));
check("the marker is just the count", span(row, "sw-dup") === "Δ×2", span(row, "sw-dup"));
const cls = parts(row).map(p => p.split("=")[0]);
check("...and it sits immediately after the sid",
      cls.indexOf("sw-dup") === cls.indexOf("sw-sid") + 1, cls.join(" "));
const dupEl = row.children.find(c => c.className.trim() === "sw-dup");
check("...with the other tab named in the tooltip, not in the row",
      /t15/.test(dupEl.title) && !/t15/.test(span(row, "sw-dup")), dupEl.title);
row = briefRow({ ...s, tab_index: 14, tab_count: 2, tab_positions: twoTabs }, true);
check("...and from t15 the tooltip points back at t3",
      /t3/.test(row.children.find(c => c.className.trim() === "sw-dup").title),
      row.children.find(c => c.className.trim() === "sw-dup").title);
check("an ordinary row carries no marker at all",
      span(briefRow({ ...s, tab_count: 1 }, true), "sw-dup") === undefined,
      String(span(briefRow({ ...s, tab_count: 1 }, true), "sw-dup")));
// Across windows the wN matters — "also at t1" is ambiguous with three windows open.
row = briefRow({ ...s, tab_index: 2, tab_count: 2,
                 tab_positions: [{ window_index: 0, tab_index: 2 },
                                 { window_index: 1, tab_index: 0 }] }, false);
check("with more than one window the tooltip names the window too",
      /w2t1/.test(row.children.find(c => c.className.trim() === "sw-dup").title),
      row.children.find(c => c.className.trim() === "sw-dup").title);
// A list that only has the count (or an older server) must still mark it.
row = briefRow({ ...s, tab_count: 2, tab_positions: undefined }, true);
check("no positions available → still marked, never a crash",
      span(row, "sw-dup") === "Δ×2", span(row, "sw-dup"));

console.log("=== the session tree: order and depth ===");
// treePlan is where all THREE lists get order and depth from — three lists each
// computing their own line is what this codebase spent a day undoing. One field
// (a parent pointer) is the whole structure: no folders to name, no headers, no
// collapse state. Depth is what gets drawn, and only that.
const T = (ids) => ids.map(id => ({ sid: id }));
const shape = (items) => treePlan(items, x => x.sid)
  .map(p => "  ".repeat(p.depth) + p.item.sid);

// The guide symbols: what the ├ └ │ cells are drawn from. One per level.
TREE = { b: "a", c: "b", d: "a" };
const guides = (ids) => treePlan(T(ids), x => x.sid)
  .map(p => p.item.sid + ":" + (p.guides.join("") || "-"));
check("a child with siblings below gets a tee, the last one an elbow",
      JSON.stringify(guides(["a","b","c","d"])) ===
      JSON.stringify(["a:-", "b:t", "c:vl", "d:l"]),
      JSON.stringify(guides(["a","b","c","d"])));
// b is NOT the last child of a (d follows), so b's own children must have a vertical
// passing through their first column — otherwise the line under b just stops in mid-air.
check("an ancestor with siblings below keeps its line running through deeper rows",
      guides(["a","b","c","d"])[2] === "c:vl", JSON.stringify(guides(["a","b","c","d"])));
TREE = { b: "a", c: "b", d: "b" };
check("...and when the ancestor IS last, the column is blank instead",
      JSON.stringify(guides(["a","b","c","d"])) ===
      JSON.stringify(["a:-", "b:l", "c:" + " t", "d:" + " l"]),
      JSON.stringify(guides(["a","b","c","d"])));
check("a root has no guides at all", guides(["a"])[0] === "a:-", "");

TREE = { b: "a", c: "b", d: "a" };
check("a child sits directly under its parent, at depth+1",
      JSON.stringify(shape(T(["a","b","c","d","e"]))) === JSON.stringify(["a","  b","    c","  d","e"]),
      JSON.stringify(shape(T(["a","b","c","d","e"]))));
check("roots keep the order they came in",
      shape(T(["e","a"])).join(",") === "e,a," + "  b,    c,  d".split(",").join(",") ||
      shape(T(["e","a","b","c","d"]))[0] === "e",
      JSON.stringify(shape(T(["e","a","b","c","d"]))));

// The row must never disappear because its parent is filtered out / in another window /
// no longer listed. It becomes a root instead.
check("a child whose parent is absent is shown as a root, not hidden",
      JSON.stringify(shape(T(["b","c","d"]))) === JSON.stringify(["b","  c","d"]),
      JSON.stringify(shape(T(["b","c","d"]))));

// Depth is not capped in the data (the CSS caps the indent). A long chain still renders.
TREE = { b: "a", c: "b", d: "c", e: "d" };
check("a deep chain keeps going", shape(T(["a","b","c","d","e"])).at(-1) === "        e",
      JSON.stringify(shape(T(["a","b","c","d","e"]))));

// The server refuses loops, but a corrupt file must not hang the browser.
// THE invariant: every row that goes in comes out, exactly once. A cycle leaves its
// members with no root to hang off, and dropping them would make live sessions vanish
// from the list — the exact failure this project spent a week hunting.
TREE = { a: "b", b: "a" };
check("a cycle in the data still renders both rows (never drops one)",
      shape(T(["a","b"])).length === 2, JSON.stringify(shape(T(["a","b"]))));
TREE = { b: "a", c: "b", d: "a", x: "y" };
for (const ids of [["a","b","c","d"], ["b","c"], ["d"], ["x"], ["a","b","c","d","x","e"]]) {
  const got = treePlan(T(ids), z => z.sid);
  check("in=" + ids.length + " out=" + got.length + " for [" + ids + "]",
        got.length === ids.length
        && new Set(got.map(g => g.item.sid)).size === ids.length, "");
}
TREE = {};
check("no tree at all → a flat list, unchanged",
      JSON.stringify(shape(T(["a","b","c"]))) === JSON.stringify(["a","b","c"]), "");

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
    # briefRow → sessionLine → tabPos: pull the whole chain so the row under test is
    # built by exactly the shipped code.
    deps = []
    for pat, what in ((r"\n  (function shortSid\(sid\) \{.*?\n  \})\n", "shortSid"),
                      (r"\n  (function tabPos\(p\) \{.*?\n  \})\n", "tabPos"),
                      (r"\n  (function sessionLine\(el, p\) \{.*?\n  \})\n", "sessionLine"),
                      (r"\n  (function treePlan\(items, getSid\) \{.*?\n  \})\n", "treePlan"),
                      # The row's own two helpers, and the copy button it hands the
                      # text to. Extracted, not restated: a stub would let the row's
                      # copy text drift from the transcript header's, which is the one
                      # thing the row button promises not to do.
                      (r"\n  (function relAge\(sec\) \{.*?\n  \})\n", "relAge"),
                      (r"\n  (function sessionCopyText\(sid, tabName\) \{.*?\n  \})\n", "sessionCopyText"),
                      (r"\n  (function makeCopyBtn\(getRaw\) \{.*?\n  \})\n", "makeCopyBtn")):
        hit = re.search(pat, src, re.S)
        if not hit:
            print(f"  FAIL  could not extract {what}() — briefRow depends on it"); return 1
        deps.append(hit.group(1))
    js = (js.replace("__ROW__", "\n".join(deps) + "\n" + m.group(1))
            .replace("__CHROME__", chrome.group(1)))
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
