#!/usr/bin/env python3
"""A finished run of tool calls folds to its tally; the running one stays open.

In brief mode consecutive tool calls stack into one block — which is right while the
turn is happening (you are watching it work) and wrong once it is over: twenty
`Bash[…] · Bash[…] · …` rows sit between two messages you actually wanted to read.

Finished now reads as "Bash ×16 · Read ×3" with a button to open it again. The two
states are told apart by CLOSING: a stack is closed by whatever block comes after it,
and after a tool run that block is the answer — so closed means the turn moved on. The
last stack in the transcript is never closed, which is exactly the one still running.

Folded, not rebuilt: the items stay in the DOM (each opens its own detail on click), so
expanding costs nothing.

This drives the real closeStack() under node, and checks the call sites in the page.

    python3 tests/test_tool_stack_fold.py      # exit 0 = pass  (needs `node`)
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
  const el = {
    // parentNode, spelled the way the DOM spells it: closeStack() bails when the box
    // has no parent, so a stub that called it `parent` made every case look like
    // "nothing to fold" — the tests passed the wrong thing rather than failing.
    tagName: tag, className: "", textContent: "", title: "", children: [], parentNode: null,
    handlers: {},
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    insertBefore(c, ref) {
      c.parentNode = this;
      const i = this.children.indexOf(ref);
      this.children.splice(i < 0 ? this.children.length : i, 0, c);
      return c;
    },
    addEventListener(ev, fn) { this.handlers[ev] = fn; },
    click() { if (this.handlers.click) this.handlers.click({ stopPropagation() {} }); },
    querySelector(sel) {
      const want = sel.replace(".", "");
      for (const c of this.children) if ((c.className || "").split(" ").includes(want)) return c;
      return null;
    },
  };
  el.classList = {
    add: (c) => { if (!el.className.split(" ").includes(c)) el.className = (el.className + " " + c).trim(); },
    contains: (c) => el.className.split(" ").includes(c),
    toggle: (c) => {
      if (el.classList.contains(c)) { el.className = el.className.split(" ").filter(x => x !== c).join(" "); return false; }
      el.classList.add(c); return true;
    },
  };
  return el;
}
const document = { createElement: mkEl };
let _lastStack = null;
__CLOSE__

// A stack as renderBlock builds it: a .sys-lines box inside a .msg div, plus the tally.
function mkStack(pairs) {
  const div = mkEl("div"); div.className = "msg system stack-tool";
  const box = mkEl("div"); box.className = "sys-lines";
  div.appendChild(box);
  const counts = new Map(pairs);
  for (const [, n] of pairs) for (let i = 0; i < n; i++) box.appendChild(mkEl("span"));
  return { div, box, st: { kind: "tool", box, counts } };
}
const sumOf = (div) => div.querySelector(".stack-sum");
const tallyOf = (div) => { const s = sumOf(div); return s ? s.children[0].textContent : null; };

console.log("=== a finished run folds to its tally ===");
{
  const { div, box, st } = mkStack([["Bash", 16], ["Read", 3]]);
  _lastStack = st;
  closeStack();
  check("the tally counts each tool", tallyOf(div) === "Bash ×16 · Read ×3", tallyOf(div));
  check("...and the list is folded away, not deleted",
        box.classList.contains("stack-folded") && box.children.length === 19,
        box.className + " n=" + box.children.length);
  check("...the summary goes ABOVE the list", div.children[0] === sumOf(div));
  const btn = sumOf(div).children[1];
  check("...with a button that says what it will do",
        btn.textContent === "[expand]" && /19/.test(btn.title), btn.textContent + " / " + btn.title);
  btn.click();
  check("...opening it shows the list again",
        box.classList.contains("show") && btn.textContent === "[collapse]", box.className);
  btn.click();
  check("...and it folds back", !box.classList.contains("show") && btn.textContent === "[expand]");
  // Bracketed on purpose: a bare lowercase word at the end of "Bash ×16 · Read ×3"
  // reads as one more tool name.
  check("...and the control is bracketed, so it is not read as a tool",
        /^\[.+\]$/.test(btn.textContent), btn.textContent);
}

console.log("=== one or two calls are left alone ===");
{
  // The tally is no shorter than the list it replaces, and you would have to open it to
  // learn anything at all.
  for (const pairs of [[["Bash", 1]], [["Bash", 2]], [["Bash", 1], ["Read", 1]]]) {
    const { div, st } = mkStack(pairs);
    _lastStack = st;
    closeStack();
    check(JSON.stringify(pairs) + " gets no summary row", sumOf(div) === null);
  }
  const { div, st } = mkStack([["Bash", 2], ["Read", 1]]);
  _lastStack = st;
  closeStack();
  check("...but three do fold", tallyOf(div) === "Bash ×2 · Read", tallyOf(div));
}

console.log("=== a run that is still going is not folded ===");
{
  const { div, st } = mkStack([["Bash", 9]]);
  _lastStack = st;                       // nothing closed it — this is the live one
  check("the open stack has no summary", sumOf(div) === null);
  check("...and _lastStack still points at it", _lastStack === st);
}

console.log("=== closing twice does not stack up summaries ===");
{
  const { div, st } = mkStack([["Bash", 4]]);
  _lastStack = st; closeStack();
  _lastStack = st; closeStack();
  check("only one summary row", div.children.filter(c => (c.className || "").includes("stack-sum")).length === 1,
        String(div.children.length));
  check("...and _lastStack is cleared", _lastStack === null);
}

console.log("=== a System stack is not a tool run ===");
{
  const { div, st } = mkStack([["x", 5]]);
  st.kind = "sys"; st.counts = null;
  _lastStack = st;
  closeStack();
  check("system rows are left as they are", sumOf(div) === null);
}

console.log(_fails.length ? "\nFAILED: " + _fails.join(", ") : "\nall pass");
process.exit(_fails.length ? 1 : 0);
"""


def main():
    node = shutil.which("node")
    if not node:
        print("SKIP: needs node"); return 0
    src = open(INDEX, encoding="utf-8").read()
    m = re.search(r"\n  (function closeStack\(\) \{.*?\n  \})\n", src, re.S)
    if not m:
        print("  FAIL  could not extract closeStack from static/index.html"); return 1

    fails = []

    def check(name, cond, detail=""):
        print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
        if not cond:
            fails.append(name)

    print("=== the call sites (a stack folds only when something closes it) ===")
    # The three places a run ends. Miss one and either a finished run stays expanded or
    # — worse — the live one folds while you are watching it.
    check("a following block closes the run",
          "closeStack();        // any other block breaks a run" in src)
    check("...a stack of a different kind does too",
          "closeStack();                                     // a different kind ends" in src)
    check("...and so does the end of a prepended batch",
          "if (atTop) { closeStack();" in src)
    # The one that must NOT be there: nothing may close the stack at the end of a normal
    # render, or the run in progress would fold on every poll.
    _re_body = re.search(r"function renderEntriesInto\(container, entries, atTop\) \{.*?\n  \}", src, re.S)
    check("nothing folds the live run at the end of a normal render",
          _re_body is not None and _re_body.group(0).count("closeStack()") == 1,
          str(_re_body and _re_body.group(0).count("closeStack()")))

    js = JS.replace("__CLOSE__", m.group(1))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(js); path = fh.name
    try:
        r = subprocess.run([node, path], capture_output=True, text=True)
        print(r.stdout.rstrip())
        if r.returncode and r.stderr:
            print(r.stderr[:1200])
        return 1 if (r.returncode or fails) else 0
    finally:
        os.unlink(path)


if __name__ == "__main__":
    sys.exit(main())
