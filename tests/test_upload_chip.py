#!/usr/bin/env python3
"""An attached file shows as @1_png, not as its whole path.

A picture you attach arrives in the message as
"@/Users/me/.claude/cc_web_uploads/1789983721_5f955e92.png" — on a phone that is a
paragraph of machinery for one image, and there can be six of them in a row. Rendered
as "@1_png": numbered within the message, extension kept (the only part that says what
it is), full path on hover, click to open it.

Two things this must not break, and both are why only the RENDERED text changes:

  * copy / find / the 🔧 correction all read the raw entry text, so what you send and
    what you copy still carry the real path;
  * a path you typed yourself is a path you meant to see — only files under
    cc_web_uploads (ours) are compacted.

Drives the real compactUploads/uploadChip out of static/index.html under node against a
minimal DOM (text nodes + a TreeWalker), because what matters is what comes out the
other side, not that the regex looks right.

    python3 tests/test_upload_chip.py      # exit 0 = pass  (needs `node`)
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "static", "index.html")

HARNESS = r"""// 真的 compactUploads + uploadChip, 在一个最小 DOM 上跑
const grab = (re, what) => { const m = src.match(re); if (!m) { console.log("FAIL extract " + what); process.exit(1); } return m[1]; };

let opened = [];
const isImagePath = (p) => /\.(png|jpe?g|gif|webp)$/i.test(p);
const fsFileUrl = (p) => "URL(" + p + ")";
const openImagePreview = (u, p) => opened.push(["img", p]);
const openPathInFiles = (p) => opened.push(["files", p]);

// 最小 DOM: 文本节点 + 元素 + TreeWalker
let NODE_TEXT = 3;
function txt(v) { return { nodeType: 3, nodeValue: v, parentNode: null }; }
function el(tag) {
  const e = { tagName: tag, nodeType: 1, className: "", textContent: "", title: "", childNodes: [],
    handlers: {},
    appendChild(c) { c.parentNode = this; this.childNodes.push(c); return c; },
    replaceChild(nu, old) {
      const i = this.childNodes.indexOf(old);
      const kids = nu.__frag ? nu.childNodes : [nu];
      kids.forEach(k => { k.parentNode = this; });
      this.childNodes.splice(i, 1, ...kids);
    },
    addEventListener(ev, fn) { this.handlers[ev] = fn; },
    click() { this.handlers.click && this.handlers.click({ stopPropagation() {} }); },
  };
  return e;
}
const document = {
  createElement: el,
  createTextNode: txt,
  createDocumentFragment: () => Object.assign(el("frag"), { __frag: true }),
  createTreeWalker(root) {
    const out = [];
    (function walk(n) { for (const c of n.childNodes || []) { if (c.nodeType === 3) out.push(c); else walk(c); } })(root);
    let i = -1;
    return { nextNode: () => (++i < out.length ? out[i] : null) };
  },
};
const NodeFilter = { SHOW_TEXT: 4 };"""

BODY = r"""
const flat = (n) => n.nodeType === 3 ? n.nodeValue
  : (n.childNodes || []).map(flat).join("") || n.textContent;

let fails = 0;
const check = (n, c, d) => { console.log((c ? "  ok  " : "  FAIL") + "  " + n + (d ? "  [" + d + "]" : "")); if (!c) fails++; };

{
  const root = el("div");
  root.appendChild(txt("@/Users/me/.claude/cc_web_uploads/1789983721_5f955e92.png @/Users/me/.claude/cc_web_uploads/17_a.jpg 看看这两张"));
  compactUploads(root);
  check("both paths become chips", flat(root) === "@1_png @2_jpg 看看这两张", flat(root));
  const chip = root.childNodes.find(n => n.className === "up-chip");
  check("...with the full path on hover", /cc_web_uploads\/1789983721_5f955e92\.png$/.test(chip.title), chip.title);
  opened = []; chip.click();
  check("...and a click opens the image", JSON.stringify(opened[0]) === JSON.stringify(["img", "/Users/me/.claude/cc_web_uploads/1789983721_5f955e92.png"]), JSON.stringify(opened));
}
{
  const root = el("div");
  root.appendChild(txt("@/Users/me/.claude/cc_web_uploads/9_x.pdf 这个文件"));
  compactUploads(root);
  check("a non-image keeps its extension", flat(root) === "@1_pdf 这个文件", flat(root));
  root.childNodes[0].click();
  check("...and opens in the file viewer", opened.some(o => o[0] === "files"), JSON.stringify(opened));
}
{
  const root = el("div");
  root.appendChild(txt("看 @/Users/me/work/diagram.png 还有 /etc/hosts"));
  compactUploads(root);
  check("a path you typed yourself is left alone", flat(root) === "看 @/Users/me/work/diagram.png 还有 /etc/hosts", flat(root));
}
{
  const root = el("div");
  const p1 = el("p"); p1.appendChild(txt("先 @/h/.claude/cc_web_uploads/a.png"));
  const p2 = el("p"); p2.appendChild(txt("后 @/h/.claude/cc_web_uploads/b.png"));
  root.appendChild(p1); root.appendChild(p2);
  compactUploads(root);
  check("numbering runs across the whole message", flat(root) === "先 @1_png后 @2_png", flat(root));
}
process.exit(fails ? 1 : 0);
"""


def main():
    node = shutil.which("node")
    if not node:
        print("SKIP: needs node"); return 0
    src = open(INDEX, encoding="utf-8").read()
    m = re.search(r"\n  (const UPLOAD_RE = .*?\n  \})\n\n  function renderBlock", src, re.S)
    if not m:
        print("  FAIL  could not extract compactUploads from static/index.html"); return 1
    js = HARNESS + "\n" + m.group(1) + "\n" + BODY
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(js); path = fh.name
    try:
        r = subprocess.run([node, path], capture_output=True, text=True)
        print(r.stdout.rstrip())
        if r.returncode and r.stderr:
            print(r.stderr[:1200])
        print("\nall pass" if r.returncode == 0 else "\nFAILED")
        return r.returncode
    finally:
        os.unlink(path)


if __name__ == "__main__":
    sys.exit(main())
