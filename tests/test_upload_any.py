#!/usr/bin/env python3
"""📎 must not swallow a file you picked.

iOS shows 「选取文件」in its attachment sheet even when the <input> says
accept="image/*" — the attribute only filters what the Files browser highlights, it
does not remove the option. So a plain tap on 📎 can come back with a PDF, and the old
client filtered non-images out silently:

    if (!anyType) files = files.filter(f => f.type && f.type.startsWith("image/"));

The option was offered, the pick succeeded, and nothing happened at all — no upload, no
message, no error. Non-images were never actually unsupported: the server takes them
whenever `allow_any` is set, which the long-press menu sets. What the long-press really
buys is a picker that does not HIDE other types; it was never meant to be the only way
to send one.

So the rule is now "whatever you picked, send it": if anything non-image is in the
batch, the whole batch goes by the any-file rules (10MB cap, a confirm over 5MB,
allow_any on the wire). This drives the REAL uploadFiles out of static/index.html under
node, because the thing worth pinning is which path a given pick takes.

    python3 tests/test_upload_any.py      # exit 0 = pass  (needs `node`)
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
// The page around uploadFiles. Only the shapes it touches.
let sent = null, alerts = [], loading = [];
const alert = (s) => alerts.push(s);
const confirm = () => true;                 // the 5–10MB question: answered yes here
const showLoading = (s) => loading.push(s);
const hideLoading = () => {};
const renderUploadRow = () => {};
const addAttachments = () => {};
const pendingUploads = [];
const inputEl = { value: "", focus() {}, dispatchEvent() {} };
const fileToDataUrl = async (f) => "data:" + (f.type || "") + ";base64,QUJD";
async function authedFetch(url, opts) {
  sent = JSON.parse(opts.body);
  return { ok: true, json: async () => ({ files: sent.files.map(f => ({ name: f.name, path: "/tmp/" + f.name, size: 3 })) }) };
}
__UPLOAD__
const mk = (name, type, size) => ({ name, type, size });

(async () => {
  console.log("=== a plain tap that comes back with a file ===");
  sent = null; loading = [];
  await uploadFiles([mk("a.pdf", "application/pdf", 1e6)], false);
  check("a PDF picked from a plain tap is sent, not dropped",
        sent && sent.files.length === 1, JSON.stringify(sent && sent.files.map(f => f.name)));
  // Without this the server answers 400 "not an image" — which is the same silence
  // from one step further away.
  check("...with allow_any, or the server refuses it", sent && sent.allow_any === true);
  check("...and the progress line says file, not image", /1 file/.test(loading[0] || ""), loading[0]);

  console.log("=== the image path is untouched ===");
  sent = null;
  await uploadFiles([mk("p.png", "image/png", 1e6)], false);
  check("an image tap still goes as an image", sent && sent.allow_any === false,
        JSON.stringify(sent && sent.allow_any));

  console.log("=== the size gate covers the new path too ===");
  sent = null; alerts = [];
  await uploadFiles([mk("big.zip", "application/zip", 11 * 1024 * 1024)], false);
  check("an 11MB file is refused before a byte is read",
        sent === null && alerts.length === 1, JSON.stringify(alerts));

  console.log("=== mixed batch ===");
  sent = null;
  await uploadFiles([mk("p.png", "image/png", 1e6), mk("a.pdf", "application/pdf", 1e6)], false);
  check("one upload, under the any-file rules",
        sent && sent.files.length === 2 && sent.allow_any === true,
        JSON.stringify(sent && [sent.files.length, sent.allow_any]));

  console.log("=== long-press is unchanged ===");
  sent = null;
  await uploadFiles([mk("n.txt", "text/plain", 10)], true);
  check("an explicit any-file upload still works", sent && sent.allow_any === true);

  console.log(_fails.length ? "\nFAILED: " + _fails.join(", ") : "\nall pass");
  process.exit(_fails.length ? 1 : 0);
})();
"""


def main():
    node = shutil.which("node")
    if not node:
        print("SKIP: needs node"); return 0
    src = open(INDEX, encoding="utf-8").read()
    m = re.search(r"\n  (async function uploadFiles\(fileList, anyType\) \{.*?\n  \})\n", src, re.S)
    if not m:
        print("  FAIL  could not extract uploadFiles from static/index.html"); return 1
    # The silent filter, by shape rather than by name: any `filter` on the picked list
    # that keeps only images would put the bug straight back.
    if re.search(r"files\s*=\s*files\.filter\([^)]*image/", m.group(1)):
        print("  FAIL  uploadFiles filters non-images out again — that is the bug"); return 1
    js = JS.replace("__UPLOAD__", m.group(1))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(js); path = fh.name
    try:
        r = subprocess.run([node, path], capture_output=True, text=True)
        print(r.stdout.rstrip())
        if r.returncode and r.stderr:
            print(r.stderr[:1200])
        return r.returncode
    finally:
        os.unlink(path)


if __name__ == "__main__":
    sys.exit(main())
