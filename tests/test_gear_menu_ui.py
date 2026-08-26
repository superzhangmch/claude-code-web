#!/usr/bin/env python3
"""The ⚙ menu's voice section: one labelled line per choice, not one line per option.

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
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(JS.replace("__RENDER__", m.group(1)))
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
