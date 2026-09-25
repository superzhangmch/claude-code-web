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
  const e = { tagName: tag, className: "", textContent: "", title: "",
              children: [], dataset: {}, style: {},
              handlers: {},
              appendChild(c) { this.children.push(c); return c; },
              addEventListener(ev, fn) { this.handlers[ev] = fn; },
              click() { if (this.handlers.click) this.handlers.click({ stopPropagation() {} }); } };
  e.classList = { add(c) { e.className = (e.className + " " + c).trim(); },
                  toggle() {}, remove() {} };
  // renderAsrMenu() starts with `sec.innerHTML = ""`. A plain property swallowed that,
  // so a re-render APPENDED a second set of rows and every "after the click" reading
  // was of the stale first set.
  Object.defineProperty(e, "innerHTML", {
    get() { return e._html || ""; },
    set(v) { e._html = v; if (!v) e.children = []; },
  });
  return e;
}
let SEC = null;
const document = {
  createElement: mkEl,
  getElementById: (id) => (id === "mm-asr-sec" ? SEC : null),
};
const localStorage = { getItem: () => null, setItem: () => {} };
let asrConfigs = [], asrWhich = "", asrRtAvail = false, realtimeEngines = [],
    asrRtEngine = "", asrRt = false,
    asrLangs = ["zh", "en", "zh+en"], asrLangDefault = "zh+en", asrLang = "zh+en",
    asrPane = "model";

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
// TWO rows, always: the mode row, and one row whose contents depend on it. A third row
// for the languages was the obvious thing and the wrong one — rows are what runs out in
// a menu you operate with a thumb, and the two-level shape was already here.
check("exactly two rows, not one per option", r.length === 2, JSON.stringify(r.map(x => x[0])));
check("the first is the mode, plus a `lang` chip",
      r[0][0] === "voice" && JSON.stringify(r[0][1]) === JSON.stringify(["⚡ live", "🎤 batch", "lang"]),
      JSON.stringify(r[0]));
check("the second is the model", r[1][0] === "model", JSON.stringify(r[1][0]));
check("the shared vendor prefix is dropped",
      JSON.stringify(r[1][1]) === JSON.stringify(["4o-mini", "Whisper", "4o"]),
      JSON.stringify(r[1][1]));
check("...and the full name survives in the tooltip",
      r[1][2][0] === "OpenAI 4o-mini", JSON.stringify(r[1][2]));
check("no button carries a decorative mic any more (the row says voice)",
      !r[1][1].some(t => /🎤|🎧/.test(t)), JSON.stringify(r[1][1]));

console.log("=== the lang chip swaps what the second row lists ===");
{
  reset(); asrPane = "lang"; renderAsrMenu();
  const rr = rows();
  check("still two rows", rr.length === 2, JSON.stringify(rr.map(x => x[0])));
  // One button per option the CONF offers (asr_langs=zh|en|zh+en) — the page has no
  // opinion about which languages exist.
  check("the second one now lists the configured languages",
        rr[1][0] === "lang" && JSON.stringify(rr[1][1]) === JSON.stringify(["zh", "en", "zh+en"]),
        JSON.stringify(rr[1]));
  check("...and the chip that got you there is marked",
        rr[0][1][2] === "lang", JSON.stringify(rr[0][1]));
  asrPane = "model";
}

console.log("=== the three chips are tabs: press one, see ITS options ===");
{
  // Pressed for real, through the rendered buttons — the claim is about what a finger
  // does. Picking a mode used to leave the row on the language list: you pressed
  // `⚡ live` and the engines did not come back.
  const chips = () => rows() && SEC.children[0].children[1].children;
  reset(); asrPane = "model"; asrRt = false; renderAsrMenu();
  chips()[2].click();                                  // lang
  check("lang → the languages", rows()[1][0] === "lang", JSON.stringify(rows()[1][0]));
  chips()[0].click();                                  // ⚡ live
  check("⚡ live → the streaming engines",
        rows()[1][0] === "model"
        && JSON.stringify(rows()[1][1]) === JSON.stringify(["Soniox", "OpenAI realtime"]),
        JSON.stringify(rows()[1]));
  chips()[2].click();                                  // lang again
  chips()[1].click();                                  // 🎤 batch
  check("🎤 batch → the batch engines",
        rows()[1][0] === "model"
        && JSON.stringify(rows()[1][1]) === JSON.stringify(["4o-mini", "Whisper", "4o"]),
        JSON.stringify(rows()[1]));
  check("...and pressing lang twice keeps you on the languages",
        (chips()[2].click(), chips()[2].click(), rows()[1][0]) === "lang",
        JSON.stringify(rows()[1][0]));
  asrPane = "model"; asrRt = false;
}

console.log("=== no configured languages, no chip and no list ===");
{
  const keep = asrLangs;
  reset(); asrLangs = []; renderAsrMenu();
  const rr = rows();
  check("the chip disappears rather than offering a guess",
        JSON.stringify(rr[0][1]) === JSON.stringify(["⚡ live", "🎤 batch"]),
        JSON.stringify(rr[0][1]));
  check("...and nothing lists languages", !rr.some(x => x[0] === "lang"),
        JSON.stringify(rr.map(x => x[0])));
  asrLangs = keep;
}

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

console.log("=== nothing configured: one line, not a paragraph ===");
// The mic button is hidden when no engine is configured, so this row is the only place
// you can find out that voice input exists at all. It used to spell the conf keys out
// right here in four lines — a paragraph inside a menu, needed exactly once. The words
// moved to the row's tooltip: still findable, no height.
reset(); asrConfigs = []; asrRtAvail = false; realtimeEngines = [];
renderAsrMenu();
const un = rows();
const unBox = SEC.children.filter(c => (c.className || "").includes("scr-cfg-row"))[0].children[1];
check("one row, labelled voice like the configured one",
      un.length === 1 && un[0][0] === "voice", JSON.stringify(un));
check("...saying only that it is not configured",
      unBox.textContent === "not configured yet", JSON.stringify(unBox.textContent));
check("...with no how-to in the row itself",
      !/cc_web\.conf|HTTPS/.test(unBox.textContent + (unBox.innerHTML || "")),
      JSON.stringify((unBox.innerHTML || "") + unBox.textContent));
check("...but the how-to kept on hover, naming the exact keys and file",
      /cc_web\.conf/.test(unBox.title) && /asr=/.test(unBox.title)
      && /soniox=/.test(unBox.title) && /HTTPS/.test(unBox.title),
      JSON.stringify((unBox.title || "").slice(0, 44)));

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

__FSFLOOR__

__FOLD__

__ENTRYTEXT__

console.log("=== what find() reads out of an entry ===");
// It reads the ENTRY and not the rendered node, because a long message is folded in
// the middle — and the middle is the part you are most likely looking for.
check("a plain string body", entryTextOf({ message: { content: "找我" } }) === "找我");
check("a list of text blocks",
      entryTextOf({ message: { content: [{ type: "text", text: "一" }, { type: "text", text: "二" }] } })
        === "一\n二");
check("...skipping the non-text parts",
      entryTextOf({ message: { content: [{ type: "tool_use", name: "Bash" }, { type: "text", text: "只要这句" }] } })
        === "只要这句");
check("an entry with no message at all", entryTextOf({}) === "");
check("...or a null one", entryTextOf(null) === "");
check("...or a shape nobody expected", entryTextOf({ message: { content: 42 } }) === "");

console.log("=== your own long messages fold by CHARACTERS ===");
// Lines were the first rule and they are a bad proxy: one can be two words or two
// hundred. A queued message showed 「对于 connector 的推荐:」 and one more short line,
// with five lines hidden between them — almost nothing kept, and the part that mattered
// gone ("省略掉的太多").
const F = (t) => foldParts(t);
check("a short message is left whole", F("一句话") === null);
// The threshold is not a number of its own: it is head + tail + "worth a seam"
// (200 + 100 + 40). A separate one went dead as soon as the budgets shrank, so it was
// folded into this.
check("...and so is one that has nothing worth hiding", F("x".repeat(340)) === null);
check("...while just past that, it folds", F("x".repeat(360)) !== null,
      JSON.stringify(F("x".repeat(360)) && F("x".repeat(360)).hidden));
const big = F("x".repeat(1200));
check("a long one keeps a readable head", big && big.head.length === 200, String(big && big.head.length));
check("...and a readable tail", big && big.tail.length === 100, String(big && big.tail.length));
check("...and hides the rest", big && big.hidden === 900, String(big && big.hidden));
check("...with nothing lost between the pieces",
      big && big.head.length + big.hidden + big.tail.length === 1200);
// The seam prefers a line end: cutting mid-word reads as a bug, cutting at a break
// reads as an omission.
// Snapping only happens when a break is WITHIN 80 characters of the cut — otherwise a
// long unbroken paragraph would lose most of its budget to the nearest newline. So the
// fixture puts the breaks inside that window: a 180-char first line (head target 200)
// and a 60-char last one (tail target 100).
const lined = F("头".repeat(180) + "\n" + "中段".repeat(400) + "\n" + "尾".repeat(60));
check("it cuts at a line break when one is near",
      lined.head === "头".repeat(180), JSON.stringify(lined.head.slice(-6)) + " len=" + lined.head.length);
check("...and the tail starts at one",
      lined.tail === "尾".repeat(60), JSON.stringify(lined.tail.slice(0, 6)) + " len=" + lined.tail.length);
// ...and when no break is near, it cuts at the budget rather than throwing the budget
// away to reach one.
const solid = F("字".repeat(1000));
check("...but a solid block is cut at the budget", solid.head.length === 200, String(solid.head.length));
// The case from the screenshot: short, and it must not be folded at all now.
const shot = F("对于 connector 的推荐:\nconnector :\n" + "a".repeat(60) + "\n按这个实现.");
check("the message that prompted this is not folded any more", shot === null, JSON.stringify(shot));
check("trailing blank lines still do not count", F("1\n2\n3\n4\n\n\n") === null);
check("an empty message does not fold", F("") === null);

console.log("=== the A-/A+ floor keeps iOS from zooming the page ===");
// Focusing a field under 16px makes iOS Safari zoom the whole page. Four other inputs
// in this file carry a "≥16px → no iOS focus auto-zoom" comment; the two memo boxes
// did not, so tapping into the Task box on a phone blew the page up. The fix is the
// FLOOR, not the size: a desktop keeps the small text that A- exists for.
globalThis.window = { matchMedia: () => ({ matches: false }) };
check("a desktop can still go down to 11", fsFloor() === 11, String(fsFloor()));
globalThis.window = { matchMedia: (q) => ({ matches: q.indexOf("coarse") >= 0 }) };
check("a touch device stops at 16", fsFloor() === 16, String(fsFloor()));
// A browser without matchMedia must not throw on the way to a font size.
globalThis.window = {};
check("...and a browser without matchMedia falls back, not throws", fsFloor() === 11,
      String(fsFloor()));

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
    # What find() matches against. Pulled out because the DOM half of that feature
    # cannot be driven from a browser test (it is all inside the page's IIFE), so the
    # text half is at least driven here.
    et = re.search(r"\n  (function entryTextOf\(e\) \{.*?\n  \})\n", src, re.S)
    if not et:
        print("  FAIL  could not extract entryTextOf() from static/index.html"); return 1
    # Folding your own long messages. Extracted because it is arithmetic — which line
    # is kept, how many are reported hidden — and arithmetic is worth driving rather
    # than reading.
    fp = re.search(r"\n  (const MSG_HEAD_CHARS = .*?\n  function foldParts\(text\) \{.*?\n  \})\n", src, re.S)
    if not fp:
        print("  FAIL  could not extract foldParts() from static/index.html"); return 1
    # The A-/A+ floor. iOS zooms the whole page when you focus a field under 16px, so
    # the floor has to move on touch devices — and it is a function, not a constant,
    # which is the sort of thing that silently stops being called.
    f = re.search(r"\n  (const FS_TOUCH = .*?\n  const fsFloor = [^\n]*\n)", src, re.S)
    if not f:
        print("  FAIL  could not extract fsFloor() from static/index.html"); return 1
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(JS.replace("__RENDER__", m.group(1)).replace("__CLAMP__", c.group(1))
                   .replace("__WMARKS__", w.group(1)).replace("__FSFLOOR__", f.group(1))
                   .replace("__FOLD__", fp.group(1)).replace("__ENTRYTEXT__", et.group(1)))
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
