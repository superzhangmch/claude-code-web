#!/usr/bin/env python3
"""End-to-end tests for the ONE voice recording path (⚙ realtime and ⚙ batch).

Batch used to be a second, less capable implementation: its own MediaRecorder globals,
its own two-step "Stop → transcribe → now choose" bar, no ⏸, no 5-minute cap, no status
line, no mic-taken-away detection. It is now the same Voice session as realtime with no
stream attached (`batchOnly` → `dropped` from the start), which is exactly the state a
realtime recording lands in when the ASR is unreachable.

This extracts the REAL `const Voice = {...}` out of static/index.html and drives it under
node against stubs for the DOM / MediaRecorder / AudioContext / WebSocket / fetch, so
both modes are exercised as shipped — including the bits batch never had:

  * ⏸ / ▶ (repeatedly): MediaRecorder.pause() keeps only the spoken segments, so several
    pauses give one clip of the parts you meant.
  * the 5-minute cap, which lives in the capture loop and therefore now covers batch too.
  * Polish / Edit / Send chosen up front, park-with-a-reason on every dead end, and
    Cancel restoring the input box.

    python3 tests/test_voice_unified_path.py     # exit 0 = pass  (needs `node`)
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
    m = re.search(r"\n  (const Voice = \{\n.*?\n  \};)\n", src, re.S)
    if not m:
        print("  FAIL  could not extract the Voice object from static/index.html"); return 1
    voice = m.group(1)
    if "start(stream, batchOnly)" not in voice:
        print("  FAIL  Voice.start no longer takes (stream, batchOnly) — did the merge get reverted?")
        return 1
    # The cap ceiling is declared next to the other voice globals and shared by both modes.
    cap = re.search(r"const VOICE_MAX_MS = (\d+);", src)
    if not cap:
        print("  FAIL  VOICE_MAX_MS is gone — the two modes can drift apart again"); return 1

    js = r"""
const VOICE_MAX_MS = __CAP__;
const _fails = [];
function check(name, cond, detail) {
  console.log((cond ? "  ok  " : "  FAIL") + "  " + name + (detail ? "  [" + detail + "]" : ""));
  if (!cond) _fails.push(name);
}
const tick = (ms) => new Promise(r => setTimeout(r, ms || 30));

// ---------- the page around Voice (stubs; the real ones only touch the DOM) ----------
const _escHtml = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const mkBtn = () => ({ style: {}, disabled: false, title: "", textContent: "", classList: { add() {}, remove() {}, toggle() {} } });
const recLiveEl = Object.assign(mkBtn(), { innerHTML: "", scrollTop: 0, scrollHeight: 0 });
const recWaveEl = mkBtn(), recSendEl = mkBtn(), recStopEl = mkBtn(), recEditEl = mkBtn(),
      recCancelEl = mkBtn(), recPauseEl = mkBtn(), micBtn = mkBtn();
const recBar = { style: {}, classList: { add() {}, remove() {}, toggle() {} } };
const inputEl = { value: "", focus() {}, dispatchEvent() {} };
let _recStatusHtml = "", _recording = false, inputFromVoice = false, _voiceParkReason = "";
let _batchResult = null, _polAbort = null, _polCtx = null, _polSuperseded = false;
let lastAsrRaw = "", lastPolished = "", lastAsrSec = null, lastPolishSec = null;
let asrRtEngine = "soniox", sonioxAvail = true, asrWhich = "whisper-big", attachedSid = "sid1", authToken = "t";
const isPhone = () => false;
const micStates = [], parked = [], sent = [], fetches = [];
function _recLiveStatus(msg) { _recStatusHtml = "STATUS:" + msg; recLiveEl.innerHTML = _recStatusHtml; }
function _setMic(st) { micStates.push(st); }
function _syncPauseBtn() {}
function _recTimerPause() {}
function _recTimerResume() {}
function _recPolishStart() {}
function _recPolishStop() {}
function send() { sent.push(inputEl.value); }
// Mirrors the real 2-liner: park the result for Polish/Edit/Send/Cancel + show the reason.
function _voiceParkResult(raw, before, reason) { _batchResult = { raw: raw || "", before: before || "" }; parked.push({ raw, before, reason }); }
function _downTo24k(f32) { return f32; }
function _f32ToPcm16(f32) { return new Int16Array(f32.length); }
// /api/asr returns text; /api/polish returns a tidied version. Both recorded.
let asrReply = { ok: true, text: "so this is what i said" };
async function authedFetch(url, opts) {
  fetches.push({ url, opts });
  if (url.startsWith("/api/asr")) return { ok: asrReply.ok, status: asrReply.ok ? 200 : 502,
    json: async () => ({ text: asrReply.text }), text: async () => "" };
  return { ok: true, status: 200, json: async () => ({ text: "So this is what I said." }), text: async () => "" };
}
const location = { protocol: "https:", host: "h:8443" };
const sockets = [];
class WebSocket { constructor(u) { this.url = u; this.readyState = 0; sockets.push(this); } send() {} close() { this.readyState = 3; } }
const recorders = [];
class MediaRecorder {
  constructor(stream) { this.stream = stream; this.state = "inactive"; this.mimeType = "audio/webm";
                        this.pauses = 0; this.resumes = 0; recorders.push(this); }
  start() { this.state = "recording"; }
  pause() { this.state = "paused"; this.pauses++; }
  resume() { this.state = "recording"; this.resumes++; }
  stop() { if (this.state === "inactive") return; this.state = "inactive";
           if (this.ondataavailable) this.ondataavailable({ data: new Blob(["audio-bytes"]) });
           if (this.onstop) this.onstop(); }
}
class FakeAC {
  constructor() { this.sampleRate = 48000; this.destination = {}; }
  resume() {} close() {}
  createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
  createScriptProcessor() { const o = { onaudioprocess: null, connect() {}, disconnect() {} }; FakeAC.last = o; return o; }
  createGain() { return { gain: { value: 0 }, connect() {}, disconnect() {} }; }
}
const window = { AudioContext: FakeAC };
let endedHandler = null;
function mkStream() {
  const track = { stop() {}, addEventListener: (ev, fn) => { if (ev === "ended") endedHandler = fn; } };
  return { getTracks: () => [track], getAudioTracks: () => [track] };
}
function feedAudio(n) {   // drive the real capture loop n times
  const ev = { inputBuffer: { getChannelData: () => new Float32Array(4096) } };
  for (let i = 0; i < (n || 1); i++) if (FakeAC.last && FakeAC.last.onaudioprocess) FakeAC.last.onaudioprocess(ev);
}
function reset() {
  micStates.length = 0; parked.length = 0; sent.length = 0; fetches.length = 0;
  sockets.length = 0; recorders.length = 0; endedHandler = null; FakeAC.last = null;
  inputEl.value = ""; recLiveEl.innerHTML = ""; _recStatusHtml = ""; _batchResult = null;
  _voiceParkReason = ""; asrReply = { ok: true, text: "so this is what i said" };
  recSendEl.disabled = false; recSendEl.title = ""; recPauseEl.style.display = "none";
  Voice.s = null; _recording = false;
}

__VOICE__

const bar = () => recLiveEl.innerHTML;

// =====================================================================
console.log("=== ⚙ batch: same session, no stream ===");
reset();
Voice.start(mkStream(), true);
const s = Voice.s;
check("no WebSocket is opened for a batch recording", sockets.length === 0);
check("the session is batch-only from the start", s.batchOnly === true && s.dropped === true);
check("...with no realtime provider", s.provider === "");
check("the local recorder is running (this is what gets transcribed)",
      recorders.length === 1 && recorders[0].state === "recording");
check("the bar says which mode it is in", /batch mode/.test(bar()) && bar().includes("whisper-big"), bar().slice(0, 95));
check("...and when text will appear", /transcribed in one go when you stop/.test(bar()));
check("...without pretending to connect anything", !/connecting/i.test(bar()));
check("⏸ is available in batch too", recPauseEl.style.display === "");
check("Send is live (stop → transcribe → submit)",
      recSendEl.disabled === false && /transcribe/.test(recSendEl.title), recSendEl.title.slice(0, 50));
check("the capture loop buffers no PCM for batch", (feedAudio(3), s.pending.length === 0 && s.pendBytes === 0));

console.log("=== ⏸ / ▶ several times = segmented recording ===");
Voice.togglePause();
check("⏸ pauses the recorder (silence never reaches the clip)",
      recorders[0].state === "paused" && recorders[0].pauses === 1);
check("...and says so", /paused — tap ▶ to record more/.test(bar()), bar().slice(0, 95));
Voice.togglePause();
check("▶ resumes it", recorders[0].state === "recording" && recorders[0].resumes === 1);
Voice.togglePause(); Voice.togglePause();
check("a second ⏸/▶ round works the same", recorders[0].pauses === 2 && recorders[0].resumes === 2);
check("...still batch, still no stream", /batch mode/.test(bar()) && sockets.length === 0, bar().slice(0, 60));

console.log("=== Send on a batch recording: stop → transcribe → submit ===");
Voice.stop("send");
await tick(60);
const asr = fetches.find(f => f.url.startsWith("/api/asr"));
check("it POSTs the clip to /api/asr", !!asr && asr.opts.method === "POST");
check("...honouring the ⚙-selected batch engine", !!asr && asr.url.includes("which=whisper-big"), asr && asr.url);
check("...and the session context", !!asr && asr.url.includes("sid=sid1"));
check("the transcript lands in the input box", inputEl.value === "so this is what i said", inputEl.value);
check("...and is submitted", sent.length === 1 && sent[0] === "so this is what i said");
check("no polish was requested for Send", !fetches.some(f => f.url.startsWith("/api/polish")));
check("Voice.s is released", Voice.s === null);

console.log("=== Polish on a batch recording ===");
reset(); Voice.start(mkStream(), true); feedAudio(2);
Voice.stop("polish");
await tick(80);
check("the raw text is transcribed then polished",
      fetches.some(f => f.url.startsWith("/api/asr")) && fetches.some(f => f.url.startsWith("/api/polish")));
check("the box holds the polished version", inputEl.value === "So this is what I said.", inputEl.value);
check("nothing was submitted", sent.length === 0);

console.log("=== Edit on a batch recording ===");
reset(); Voice.start(mkStream(), true);
Voice.stop("edit");
await tick(60);
check("Edit hands over the raw text without polishing",
      inputEl.value === "so this is what i said" && !fetches.some(f => f.url.startsWith("/api/polish")), inputEl.value);
check("...and does not submit", sent.length === 0);
// The deleted batch pipeline set this itself; only polish() does now, so Edit/Send would
// have left the long-press debug view (raw vs polished, "use raw") empty in batch mode.
check("the long-press debug view still gets the raw ASR text",
      lastAsrRaw === "so this is what i said", JSON.stringify(lastAsrRaw));

console.log("=== the 5-min cap now covers batch as well ===");
reset(); Voice.start(mkStream(), true);
Voice.s.startMs = Date.now() - (VOICE_MAX_MS + 1000);
feedAudio(1);
await tick(60);
check("the recording auto-stops at the cap", Voice.s === null && recorders[0].state === "inactive");
check("...and parks: transcribed, reason shown, nothing decided for you",
      parked.length === 1 && /5 分钟/.test(parked[0].reason) && parked[0].raw === "so this is what i said",
      parked.length ? parked[0].reason : "not parked");
check("...with the result kept for Polish/Edit/Send", !!_batchResult && _batchResult.raw === "so this is what i said");

console.log("=== batch dead ends park with a reason (they used to vanish) ===");
reset(); Voice.start(mkStream(), true); asrReply = { ok: false, text: "" };
Voice.stop("polish"); await tick(60);
check("a failing /api/asr parks with the HTTP code",
      parked.length === 1 && /HTTP 502/.test(parked[0].reason), parked.length ? parked[0].reason : "not parked");
reset(); Voice.start(mkStream(), true); asrReply = { ok: true, text: "   " };
Voice.stop("polish"); await tick(60);
check("an empty transcript parks too, instead of closing silently",
      parked.length === 1 && !!parked[0].reason, parked.length ? parked[0].reason : "not parked");

console.log("=== batch inherits the rest of the realtime session's care ===");
reset(); inputEl.value = "already typed";
Voice.start(mkStream(), true);
check("dictation appends after existing text", Voice.s.before === "already typed ");
Voice.cancel();
// Restores `before` — i.e. the text you had, plus the separator space dictation would
// have been appended after. No dictated words survive, which is the point.
check("Cancel restores the box (the old batch path just dropped it)",
      inputEl.value === "already typed " && Voice.s === null, JSON.stringify(inputEl.value));
reset(); Voice.start(mkStream(), true);
check("the mic-taken-away hook is installed for batch too", typeof endedHandler === "function");
_recording = true; endedHandler();
check("...and losing the mic parks with a reason", parked.length === 1 && /麦克风/.test(parked[0].reason),
      parked.length ? parked[0].reason : "not parked");

console.log("=== ⚙ realtime is still the streaming session ===");
reset(); Voice.start(mkStream(), false);
check("a stream IS opened", sockets.length === 1 && /asr-stream/.test(sockets[0].url));
check("...with the chosen engine", sockets[0].url.includes("provider=soniox"));
check("the session is not batch-only", Voice.s.batchOnly === false && Voice.s.dropped === false);
check("Send waits for the first realtime token", recSendEl.disabled === true);
check("the bar says it is connecting", /connecting ASR/.test(bar()), bar().slice(0, 60));
check("realtime DOES buffer PCM while connecting", (feedAudio(2), Voice.s.pendBytes > 0));

console.log("=== realtime that never connects, stopped by hand → batch, at once ===");
Voice.stop("polish");
check("no 30s drain wait when nothing was ever connected", Voice.s._finDeadline <= Date.now());
await tick(80);
check("it falls back to the batch engine", fetches.some(f => f.url.startsWith("/api/asr")));
check("...and the words are not lost", inputEl.value === "So this is what I said.", inputEl.value);

console.log(_fails.length ? "\nFAILED: " + _fails.join(", ") : "\nall pass");
process.exit(_fails.length ? 1 : 0);
"""
    js = js.replace("__VOICE__", voice).replace("__CAP__", cap.group(1))
    # top-level await → .mjs
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as fh:
        fh.write(js)
        path = fh.name
    try:
        r = subprocess.run([node, path], capture_output=True, text=True)
        print(r.stdout.rstrip())
        if r.returncode and r.stderr:
            print(r.stderr[:1500])
        return r.returncode
    finally:
        os.unlink(path)


if __name__ == "__main__":
    sys.exit(main())
