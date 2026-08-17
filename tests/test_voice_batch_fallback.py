#!/usr/bin/env python3
"""Front-end tests for the realtime→batch fallback of ONE voice recording.

Extracts the REAL Voice methods out of static/index.html and drives them under node,
so these assertions break if the shipped code changes.

What is pinned:

  1. Once a recording has fallen into batch (realtime unreachable / dropped mid-way),
     it STAYS batch for the rest of that recording. Tapping ▶ resumes into batch and
     must not claim to be "connecting ASR" — we already gave up on it. The fallback is
     per-SESSION on purpose: the next tap on 🎤 reads the ⚙ mode again, so a bad minute
     of network does not silently become a sticky mode switch.
  2. A ▶ resume on a HEALTHY session still says "connecting ASR" / "listening" — the
     normal path must not be collateral damage.
  3. An upstream dying while ⏸ is held keeps the session alive as batch-only. The old
     check was `s.mr.state === "recording"`, which is false while paused, so a pause
     long enough to time out the provider ran into Voice.fail() and killed the whole
     recording ("语音连接中断") — there was then nothing left to resume, and the audio
     the local recorder was still holding was thrown away.
  4. Whatever realtime managed to transcribe survives the switch, and the bar always
     says what happens next (silence that isn't explained reads as a freeze).

    python3 tests/test_voice_batch_fallback.py      # exit 0 = pass  (needs `node`)
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


def method(name):
    # Object-literal methods sit at 4-space indent and end with a 4-space "}," line.
    return extract(r"\n    (" + name + r"\([^)]*\) \{.*?\n    \}),", "Voice." + name)


def main():
    node = shutil.which("node")
    if not node:
        print("SKIP: needs node"); return 0

    methods = ", ".join(method(n) for n in ("goBatch", "paintBatchOnly", "togglePause", "streamDrop"))

    js = r"""
const _fails = [];
function check(name, cond, detail) {
  console.log((cond ? "  ok  " : "  FAIL") + "  " + name + (detail ? "  [" + detail + "]" : ""));
  if (!cond) _fails.push(name);
}

// ---- the bits of the page these methods touch ----------------------------------
const _escHtml = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const recLiveEl = { style: {}, innerHTML: "", textContent: "" };
const recWaveEl = { style: {} };
const recBar = { classList: { toggle() {}, add() {}, remove() {} } };
let _recStatusHtml = "";
const calls = [];
function _recLiveStatus(msg) { _recStatusHtml = "STATUS:" + msg; recLiveEl.innerHTML = _recStatusHtml; }
function _recTimerPause() {}
function _recTimerResume() {}
function _syncPauseBtn() {}

const Voice = {
  s: null,
  teardownStream() { calls.push("teardownStream"); if (Voice.s) { Voice.s.ws = null; Voice.s.opened = false; } },
  openStream() { calls.push("openStream"); },
  finalize() { calls.push("finalize"); },
  fail(r) { calls.push("fail:" + r); },
  render() { calls.push("render"); },
  flushPending() {},
  __METHODS__
};

function mkSession(over) {
  return Object.assign({
    before: "", provider: "soniox", opened: false, dropped: false, gotAny: false, retries: 0,
    finalText: "", partial: "", stopping: false, paused: false, pausedAt: 0, upReady: false,
    pending: [new ArrayBuffer(8)], pendBytes: 8, startMs: 0,
    ws: { readyState: 1, close() {}, send() {} },
    mr: { state: "recording", pause() { this.state = "paused"; }, resume() { this.state = "recording"; } },
  }, over || {});
}
const bar = () => recLiveEl.innerHTML;

console.log("=== falling into batch mid-recording ===");
Voice.s = mkSession(); calls.length = 0;
Voice.goBatch("realtime ASR unreachable");
check("goBatch marks the session batch-only", Voice.s.dropped === true);
check("...frees the PCM that can never be shipped", Voice.s.pending.length === 0 && Voice.s.pendBytes === 0);
check("...closes the socket it gave up on", calls.includes("teardownStream"));
check("...and says recording continues, in batch", /recording \(batch\)/.test(bar()), bar().slice(0, 90));
check("...as a status line, not a how-to", !/tap /.test(bar()) && !/Polish/.test(bar()));
check("...without claiming it is still connecting", !/connecting/i.test(bar()));
check("a second goBatch is a no-op (no double teardown)",
      (() => { calls.length = 0; Voice.goBatch("again"); return calls.length === 0; })());

console.log("=== ⏸ / ▶ on a session that already fell into batch ===");
Voice.s = mkSession(); Voice.goBatch("realtime ASR unreachable");
Voice.togglePause();
check("⏸ on a batch-only session says so", Voice.s.paused === true && /paused/.test(bar()), bar().slice(0, 90));
check("...and does not resurrect 'connecting ASR'", !/connecting/i.test(bar()));
Voice.togglePause();
check("▶ resumes capture", Voice.s.paused === false && Voice.s.mr.state === "recording");
check("▶ stays in batch for the REST of this recording",
      /recording \(batch\)/.test(bar()), bar().slice(0, 90));
check("▶ never claims to connect an ASR we gave up on", !/connecting/i.test(bar()));
check("▶ does not reopen the stream", !calls.includes("openStream"));

console.log("=== the healthy path is untouched ===");
Voice.s = mkSession(); calls.length = 0;
Voice.togglePause(); Voice.togglePause();
check("▶ on a live session still says it is connecting",
      /connecting ASR/.test(bar()) && Voice.s.dropped === false, bar().slice(0, 80));
Voice.s = mkSession({ upReady: true });
Voice.togglePause(); Voice.togglePause();
check("▶ with the upstream already up says it is listening", /listening/.test(bar()), bar().slice(0, 80));

console.log("=== an upstream dying while ⏸ is held must not kill the recording ===");
Voice.s = mkSession({ retries: 3 }); calls.length = 0;
Voice.togglePause();                                   // mr.state -> "paused"
Voice.streamDrop("closed");
check("the session survives (no Voice.fail)", !calls.some(c => c.startsWith("fail:")), calls.join(","));
check("...as batch-only", Voice.s.dropped === true);
check("...showing it is paused, in batch", /paused \(batch\)/.test(bar()), bar().slice(0, 90));
Voice.togglePause();
check("...resuming after that drop records in batch", Voice.s.paused === false && /batch/.test(bar()));

console.log("=== streamDrop, other shapes ===");
Voice.s = mkSession({ gotAny: true, finalText: "half a sentence" }); calls.length = 0;
Voice.streamDrop("error");
check("a drop while recording with text → batch-only, text kept",
      Voice.s.dropped === true && bar().includes("half a sentence"), bar().slice(0, 90));
check("...no fail()", !calls.some(c => c.startsWith("fail:")));

Voice.s = mkSession(); calls.length = 0;              // fresh, no text → silent reconnect
Voice.streamDrop("closed");
check("an early drop still retries instead of falling back", Voice.s.retries === 1 && Voice.s.dropped === false);
check("...and says it is reconnecting while recording", /reconnecting/.test(bar()), bar().slice(0, 80));

Voice.s = mkSession({ retries: 3, mr: null }); calls.length = 0;
Voice.streamDrop("closed");
check("no local recording + nothing transcribed → still parks with a reason",
      calls.some(c => c.startsWith("fail:")), calls.join(","));

Voice.s = mkSession({ stopping: true }); calls.length = 0;
Voice.streamDrop("closed");
check("a drop while draining after Stop finalizes at once", calls.includes("finalize"));

console.log(_fails.length ? "\nFAILED: " + _fails.join(", ") : "\nall pass");
process.exit(_fails.length ? 1 : 0);
"""
    js = js.replace("__METHODS__", methods)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(js)
        path = fh.name
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
