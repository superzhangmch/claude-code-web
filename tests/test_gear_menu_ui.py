#!/usr/bin/env python3
"""Two small popups in the transcript view: the ⚙ menu, and the jump-to-ask list.

The menu used to spend two lines on every setting — a heading line, then a full-width
button — and one whole line per speech model ("🎤 OpenAI 4o-mini", "🎤 OpenAI Whisper",
"🎤 OpenAI 4o"). It now uses the same row as the picker's ⚙: grey label on the left, its
buttons on the right, on ONE line. Measured in a browser: 9 lines instead of ~16.

This drives the shipped renderAsrMenu() under node, because the interesting part is what
it emits:

  1. two labelled rows — the mode, then the model — not a stack of buttons;
  2. the vendor prefix every option shares is dropped ("OpenAI 4o-mini" → "4o-mini"),
     since on one line that prefix is exactly what there is no room for, and the full
     name stays in the tooltip;
  3. ...but only when they really all share it — otherwise the names are left alone;
  4. an unconfigured setup still explains itself instead of rendering nothing.

Also pinned here: clampHT(), which labels the jump-to-ask list. Those labels replaced
the ↑/↓ "previous/next request" buttons — stepping one at a time to find the request you
meant is worse than being shown the list — and a long ask has to be recognisable in one
line. Truncation is head…TAIL, because the middle is the least identifying part: the
opening says what it is about and the actual request is usually at the end ("…所以帮我把
A 改成 B"). Dropping the tail, which a plain head-clamp does, throws away the half worth
keeping. (The fit is done in PIXELS at render time — a CJK glyph is twice a Latin one, so
a fixed character count fits on a laptop and overflows on a phone; that part is measured
in the browser, not here.)

    python3 tests/test_gear_menu_ui.py      # exit 0 = pass  (needs `node`)
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
function mkEl(tag) {
  const e = { tagName: tag, className: "", textContent: "", title: "", innerHTML: "",
              children: [], dataset: {}, style: {},
              appendChild(c) { this.children.push(c); return c; },
              addEventListener() {} };
  e.classList = { add(c) { e.className = (e.className + " " + c).trim(); },
                  toggle() {}, remove() {} };
  return e;
}
let SEC = null;
const document = {
  createElement: mkEl,
  getElementById: (id) => (id === "mm-asr-sec" ? SEC : null),
};
const localStorage = { getItem: () => null, setItem: () => {} };
let asrConfigs = [], asrWhich = "", asrRtAvail = false, realtimeEngines = [],
    asrRtEngine = "", asrRt = false;

__RENDER__
__CLAMP__

// rows the renderer produced: [label, [button texts...]]
function rows() {
  return SEC.children
    .filter(c => (c.className || "").includes("scr-cfg-row"))
    .map(r => {
      const label = (r.children[0] || {}).textContent;
      const box = r.children[1] || { children: [] };
      return [label, box.children.map(b => b.textContent), box.children.map(b => b.title)];
    });
}
const reset = () => { SEC = mkEl("div"); SEC.children = []; };

console.log("=== batch mode: one line for the mode, one for the model ===");
reset();
asrRtAvail = true; asrRt = false;
asrConfigs = [{label: "a", display: "OpenAI 4o-mini"},
              {label: "b", display: "OpenAI Whisper"},
              {label: "c", display: "OpenAI 4o"}];
realtimeEngines = [{id: "s", display: "Soniox"}, {id: "o", display: "OpenAI realtime"}];
renderAsrMenu();
let r = rows();
check("exactly two rows, not one per option", r.length === 2, JSON.stringify(r.map(x => x[0])));
check("the first is the mode", r[0][0] === "语音输入" && r[0][1].length === 2, JSON.stringify(r[0]));
check("the second is the model", r[1][0] === "模型", JSON.stringify(r[1][0]));
check("the shared vendor prefix is dropped",
      JSON.stringify(r[1][1]) === JSON.stringify(["4o-mini", "Whisper", "4o"]),
      JSON.stringify(r[1][1]));
check("...and the full name survives in the tooltip",
      r[1][2][0] === "OpenAI 4o-mini", JSON.stringify(r[1][2]));
check("no button carries a decorative mic any more (the row says 语音输入)",
      !r[1][1].some(t => /🎤|🎧/.test(t)), JSON.stringify(r[1][1]));

console.log("=== realtime mode lists the streaming engines instead ===");
reset(); asrRt = true; renderAsrMenu();
r = rows();
check("still two rows", r.length === 2, JSON.stringify(r.map(x => x[0])));
check("names are NOT butchered when they share no prefix",
      JSON.stringify(r[1][1]) === JSON.stringify(["Soniox", "OpenAI realtime"]),
      JSON.stringify(r[1][1]));

console.log("=== a single option is not 'a common prefix' ===");
reset(); asrRt = false; asrConfigs = [{label: "a", display: "OpenAI 4o-mini"}];
renderAsrMenu();
check("one option keeps its whole name", rows()[1][1][0] === "OpenAI 4o-mini",
      JSON.stringify(rows()[1][1]));

console.log("=== nothing configured still explains itself ===");
reset(); asrConfigs = []; asrRtAvail = false; realtimeEngines = [];
renderAsrMenu();
const flat = JSON.stringify(SEC.children.map(c => c.innerHTML || c.textContent || ""));
check("it names the config keys rather than rendering blank",
      /cc_web\.conf/.test(flat) && /asr=/.test(flat), flat.slice(0, 120));

console.log("=== a long ask keeps its head AND its tail ===");
check("a short ask is left alone", clampHT("短的一条", 40) === "短的一条", clampHT("短的一条", 40));
check("whitespace is collapsed (a pasted block must stay one line)",
      clampHT("  a\n\n  b   c ", 40) === "a b c", clampHT("  a\n\n  b   c ", 40));
const long = "开头说明了背景" + "x".repeat(300) + "所以把 A 改成 B";
const cut = clampHT(long, 30);
check("a long one is cut to the budget", cut.length === 30, String(cut.length));
check("...with the ellipsis in the MIDDLE, not at the end",
      cut.indexOf("…") > 0 && cut.indexOf("…") < cut.length - 1, cut);
check("...the head survives", cut.startsWith("开头说明了背景"), cut);
check("...and so does the tail — that's where the request is",
      cut.endsWith("所以把 A 改成 B"), cut);
check("a tiny budget still produces something usable, not a crash",
      clampHT(long, 5).length === 5 && clampHT(long, 5).includes("…"), clampHT(long, 5));

__WMARKS__

console.log("=== Watch: which button is lit in each row ===");
// The bug this replaced: the expiry mark was computed from `expires_at`, an absolute
// timestamp that cannot say which button produced it — so only 不限 could ever light up
// and every real expiry rendered as nothing selected. Which is what it looked like.
const M = (sup, armed) => watchMarks(sup, armed);
check("the stored expiry choice is the one marked", M({ hours: 8 }, true).hours === 8,
      String(M({ hours: 8 }, true).hours));
check("...and 48h too", M({ hours: 48 }, true).hours === 48, String(M({ hours: 48 }, true).hours));
// NaN, not 0: nothing compares equal to it, so with no choice stored NO button lights up
// rather than the one whose data-h happens to be 0.
check("with nothing stored, no expiry button is marked",
      Number.isNaN(M({}, true).hours), String(M({}, true).hours));
check("...and a 0 stored counts as nothing", Number.isNaN(M({ hours: 0 }, true).hours));
check("the period is marked, including the 默认 zero",
      M({ period_min: 20 }, true).period === 20 && M({}, true).period === 0,
      M({ period_min: 20 }, true).period + "/" + M({}, true).period);
// on/off comes from ARMED, not from `enabled`. An expired watcher is not a running one,
// and that distinction is the whole reason the expiry exists.
check("开 is marked when it is actually armed", M({ enabled: true }, true).on === true);
check("...关 when it is off", M({ enabled: false }, false).on === false);
check("...and 关 when it is enabled but EXPIRED", M({ enabled: true }, false).on === false);
check("a missing supervisor record does not throw",
      Number.isNaN(M(null, false).hours) && M(null, false).on === false);

console.log(_fails.length ? "\nFAILED: " + _fails.join(", ") : "\nall pass");
process.exit(_fails.length ? 1 : 0);
"""


def main():
    node = shutil.which("node")
    if not node:
        print("SKIP: needs node"); return 0
    src = open(INDEX, encoding="utf-8").read()
    m = re.search(r"\n  (function renderAsrMenu\(\) \{.*?\n  \})\n", src, re.S)
    if not m:
        print("  FAIL  could not extract renderAsrMenu() from static/index.html"); return 1
    c = re.search(r"\n  (function clampHT\(str, n\) \{.*?\n  \})\n", src, re.S)
    if not c:
        print("  FAIL  could not extract clampHT() from static/index.html"); return 1
    # Which button is lit in each of the Watch window's three rows. Extracted and driven
    # rather than eyeballed because the page script is one big IIFE: a browser test can
    # click and read the DOM, but it cannot reach a function inside that closure, and
    # this logic was wrong in a way that looked like "the buttons don't work".
    w = re.search(r"\n  (function watchMarks\(sup, armed\) \{.*?\n  \})\n", src, re.S)
    if not w:
        print("  FAIL  could not extract watchMarks() from static/index.html"); return 1
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(JS.replace("__RENDER__", m.group(1)).replace("__CLAMP__", c.group(1))
                   .replace("__WMARKS__", w.group(1)))
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
