#!/usr/bin/env python3
"""Esc / ↑ / clear-line on the tail window — the keys you want while WATCHING.

The tail box shows the live terminal; until now the three keys you reach for when you see
something you want to stop lived only on the full-screen window. Opening that over the
transcript you were reading, to press one Esc, is the wrong trade.

What matters and is therefore driven here, not eyeballed:

  * the bytes. Esc is 0x1b; ↑ is the CSI sequence, not the letter; clearing a line is
    Ctrl-E then Ctrl-U (Ctrl-U alone leaves everything right of the caret).
  * that the box re-reads itself afterwards. A key that visibly changes nothing is
    indistinguishable from a dead button — twice, because claude sometimes takes a beat.
  * one key path. The screen window's buttons and these go through the same sendRaw(),
    so the sequences cannot drift apart.

    python3 tests/test_tail_keys.py      # exit 0 = pass  (needs `node`)
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "static", "index.html")

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


JS = r"""
const _fails = [];
function check(name, cond, detail) {
  console.log((cond ? "  ok  " : "  FAIL") + "  " + name + (detail ? "  [" + detail + "]" : ""));
  if (!cond) _fails.push(name);
}
// --- just enough browser -------------------------------------------------------------
const els = {};
function mkBtn(id) {
  return els[id] = { id, handlers: {},
    addEventListener(ev, fn) { this.handlers[ev] = fn; },
    click() { return this.handlers.click({ stopPropagation() { log.push("stopProp"); } }); } };
}
["tail-esc", "tail-up", "tail-clear"].forEach(mkBtn);
const document = { getElementById: (id) => els[id] || null };
const log = [];
const timers = [];
const setTimeout = (fn, ms) => { timers.push([fn, ms]); return timers.length; };
const UP = "\x1b[A";
async function sendRaw(text, pressEnter) { log.push(["send", text, !!pressEnter]); }
async function tailScreen() { log.push("refresh"); }

__WIRE__

const seqOf = (l) => l.find(x => Array.isArray(x) && x[0] === "send");

(async () => {
  console.log("=== the bytes each button sends ===");
  for (const [id, want, label] of [["tail-esc", "\x1b", "Esc"],
                                   ["tail-up", "\x1b[A", "↑"],
                                   ["tail-clear", "\x05\x15", "clear line"]]) {
    log.length = 0; timers.length = 0;
    await els[id].click();
    const sent = seqOf(log);
    check(label + " sends " + JSON.stringify(want),
          sent && sent[1] === want, JSON.stringify(sent));
    // press_enter must stay false: an Enter riding along with Esc would submit
    // whatever was in the box instead of cancelling.
    check("..." + label + " sends no Enter with it", sent && sent[2] === false,
          JSON.stringify(sent));
    check("..." + label + " then re-reads the box", log.indexOf("refresh") > log.indexOf(sent),
          JSON.stringify(log));
    check("..." + label + " and once more after a moment",
          timers.length === 1 && timers[0][1] >= 200 && timers[0][1] <= 1500,
          JSON.stringify(timers.map(t => t[1])));
    log.length = 0;
    timers[0][0]();
    check("..." + label + "'s second read is another refresh, not a re-send",
          log.length === 1 && log[0] === "refresh", JSON.stringify(log));
  }

  console.log("=== the click does not fall through to the page ===");
  // The tail box sits over the transcript; a click that also reached the page would
  // scroll or select something behind it.
  log.length = 0; timers.length = 0;
  await els["tail-esc"].click();
  check("each handler stops the event", log.includes("stopProp"), JSON.stringify(log));

  console.log(_fails.length ? "\nFAILED: " + _fails.join(", ") : "\nall pass");
  process.exit(_fails.length ? 1 : 0);
})();
"""


def main():
    src = open(INDEX, encoding="utf-8").read()

    print("=== one key path, shared with the screen window ===")
    # Both sets of buttons call sendRaw(); the escape sequences exist once. A second copy
    # is how ⏎ once sent a stray \r that the server stripped and nobody noticed.
    check("the tail keys go through sendRaw(), like the screen window's",
          "await sendRaw(seq, false);" in src)
    check("...↑ is the shared UP constant, not a second literal",
          src.count('const UP = "\\x1b[A"') == 1 and 'tailKey(UP)' in src)
    check("...and clear-line is the same Ctrl-E Ctrl-U pair",
          src.count('"\\x05\\x15"') == 2, str(src.count('"\\x05\\x15"')))

    print("=== the buttons are in the tail box, under the output ===")
    box = src[src.index('<div id="tail-box"'):]
    box = box[:box.index("</div>\n  </main>") if "</div>\n  </main>" in box else 900]
    check("all three live inside #tail-box",
          all(i in box for i in ("tail-esc", "tail-up", "tail-clear")), box[:120])
    check("...after the output, not before it",
          box.index("tail-pre") < box.index("tail-keys"))
    check("...and each says what it is for",
          box.count("title=") >= 5 and "取消" in box and "输入行" in box)

    node = shutil.which("node")
    if not node:
        print("SKIP (behaviour): needs node")
        print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
        return 1 if _fails else 0

    m = re.search(r"\n  (function tailKey\(seq\) \{.*?tail-clear\"\)\.addEventListener\([^\n]*\n)",
                  src, re.S)
    if not m:
        check("could extract the tail-key wiring from the page", False)
        print("\nFAILED: " + ", ".join(_fails))
        return 1
    js = JS.replace("__WIRE__", m.group(1))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(js); path = fh.name
    try:
        r = subprocess.run([node, path], capture_output=True, text=True)
        print(r.stdout.rstrip())
        if r.returncode and r.stderr:
            print(r.stderr[:1200])
        return 1 if (r.returncode or _fails) else 0
    finally:
        os.unlink(path)


if __name__ == "__main__":
    sys.exit(main())
