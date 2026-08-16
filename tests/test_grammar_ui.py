#!/usr/bin/env python3
"""Front-end regression tests for the ✎ correction UI.

Extracts the REAL functions out of static/index.html and runs them under node, so
these assertions break if the shipped code changes — no re-typed copy to drift.

Two things are pinned:

  1. What each `status` renders. An empty result used to print "looks natural — no
     changes" whether the model approved the text, was never configured, or the call
     failed: a pass mark on text nobody checked. Only status:"ok" may look like
     approval.
  2. What gets CACHED. manualGrammarMap.set() was unconditional, and the cache-hit
     branch short-circuits every later ✎ tap — so one flaky call (502, timeout,
     litellm restarting) pinned that message on "⚠ LLM call failed" until a full
     page reload, with no way to retry. Failures must not be cached; a real result
     must be, including status:"ok" with nothing to fix.

    python3 tests/test_grammar_ui.py        # exit 0 = pass  (needs `node`)
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "static", "index.html")


def extract(pattern, label):
    src = open(INDEX, encoding="utf-8").read()
    m = re.search(pattern, src, re.S)
    if not m:
        print(f"  FAIL  could not extract {label} from static/index.html — did it get renamed?")
        sys.exit(1)
    return m.group(1)


def main():
    node = shutil.which("node")
    if not node:
        print("SKIP: needs node"); return 0

    show = extract(r"(  function _showManualCorrection\(div, res\) \{.*?\n  \})", "_showManualCorrection")
    # The cache condition lives inline in the ✎ click handler; pull just that line so
    # the test tracks the shipped expression rather than a copy of it.
    # Match the set() call WITH OR WITHOUT its guard, so dropping the guard shows up
    # as a real assertion failure below ("a failed call is NOT cached") instead of an
    # unhelpful "could not extract".
    cache_line = extract(r"\n\s*((?:if \([^\n]*\) )?manualGrammarMap\.set\(key, res\);)",
                         "the manualGrammarMap.set call")

    js = r"""
const _fails = [];
function check(name, cond, detail) {
  console.log((cond ? "  ok  " : "  FAIL") + "  " + name + (detail ? "  [" + detail + "]" : ""));
  if (!cond) _fails.push(name);
}
// minimal DOM: _showManualCorrection only builds divs/spans and appends them
function mkEl(tag) {
  return { tagName: tag, className: "", textContent: "", children: [],
           appendChild(c) { this.children.push(c); return c; },
           querySelector() { return null; }, remove() {} };
}
const document = { createElement: mkEl, createTextNode: t => ({ text: String(t) }) };
function flat(el) {
  let s = (el.className || "") + " " + (el.textContent || "") + " " + (el.text || "");
  for (const c of el.children || []) s += " " + flat(c);
  return s;
}
__SHOW__

function render(res) { const div = mkEl("div"); _showManualCorrection(div, res); return flat(div); }

console.log("=== what each status renders ===");
let h = render({ status: "ok", correction: "I went to the store.", native: "I popped to the shop." });
check("a correction is shown", h.includes("I went to the store."), h.trim().slice(0, 60));
check("the native version is shown", h.includes("I popped to the shop."));
check("labels are English, not 批改/地道", !h.includes("批改") && !h.includes("地道") && h.includes("fix") && h.includes("native"));

h = render({ status: "ok", correction: "", native: "" });
check("status ok with nothing to fix says 'no mistakes found'", h.includes("no mistakes found"), h.trim().slice(0, 60));
check("the old 'looks natural' wording is gone for good", !h.includes("looks natural"));

h = render({ status: "disabled", correction: "", native: "" });
check("status disabled names the missing config", h.includes("no LLM configured"), h.trim().slice(0, 60));
check("...and does NOT imply the text was checked",
      !h.includes("no mistakes") && !h.includes("looks natural"));

h = render({ status: "error", error: "URLError", correction: "", native: "" });
check("status error names the failure", h.includes("LLM call failed") && h.includes("URLError"), h.trim().slice(0, 60));
check("...and does NOT imply the text was checked", !h.includes("no mistakes"));

h = render({ status: "", correction: "", native: "" });
check("a reply with no status says so instead of guessing", h.includes("no result"), h.trim().slice(0, 60));

console.log("=== what gets cached (a failure must stay retryable) ===");
const manualGrammarMap = new Map();
let key = 0;
function cached(res) {
  const k = "k" + (++key);
  const _key = k;
  (function (key, res) { __CACHE__ })(k, res);
  return manualGrammarMap.has(k);
}
check("a real correction is cached", cached({ status: "ok", correction: "fixed", native: "" }) === true);
check("status ok with nothing to fix is cached too (don't re-bill the same call)",
      cached({ status: "ok", correction: "", native: "" }) === true);
check("a native-only result is cached", cached({ status: "ok", correction: "", native: "n" }) === true);
check("an LLM-not-configured result is NOT cached", cached({ status: "disabled", correction: "" }) === false);
check("a failed call is NOT cached, so ✎ can retry",
      cached({ status: "error", error: "HTTP 502", correction: "" }) === false);
check("a timeout is NOT cached", cached({ status: "error", error: "AbortError" }) === false);
check("a statusless reply is NOT cached", cached({ status: "", correction: "" }) === false);

console.log(_fails.length ? "\nFAILED: " + _fails.join(", ") : "\nall pass");
process.exit(_fails.length ? 1 : 0);
"""
    js = js.replace("__SHOW__", show).replace("__CACHE__", cache_line)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(js)
        path = fh.name
    try:
        r = subprocess.run([node, path], capture_output=True, text=True)
        print(r.stdout.rstrip())
        if r.returncode and r.stderr:
            print(r.stderr[:800])
        return r.returncode
    finally:
        os.unlink(path)


if __name__ == "__main__":
    sys.exit(main())
