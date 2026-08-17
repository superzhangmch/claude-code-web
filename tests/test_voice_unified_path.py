#!/usr/bin/env python3
"""End-to-end tests for the ONE voice recording path (⚙ realtime and ⚙ batch).

Batch used to be a second, less capable implementation: its own MediaRecorder globals, a
two-step "Stop → transcribe → now choose" bar, no ⏸, no 5-minute cap, no status line, no
mic-taken-away detection. It is now the same Voice session as realtime with no stream
attached (`batchOnly`), and it records in SEGMENTS: ⏸ closes the current clip, pushes it
through /api/asr and appends the words to the transcript. So batch behaves like realtime
where it counts — text shows up while you talk, and Polish / Edit / Send always act on
text you have already read.

This extracts the REAL `const Voice = {...}` out of static/index.html and drives it under
node against stubs for the DOM / MediaRecorder / AudioContext / WebSocket / fetch, so both
modes are exercised as shipped. Pinned properties worth naming:

  * ⏸ transcribes; ▶ opens a fresh clip; several rounds accumulate IN ORDER.
  * a segment whose upload fails keeps its audio and is retried at Stop, in its own slot —
    a spoken sentence must never be silently dropped, and the retry must not reorder it.
  * Send stays disabled until a transcript exists (in BOTH modes) — it submits at once, so
    it must never submit nothing.
  * the 5-min cap lives in the shared capture loop, so it covers batch too, and it parks
    (transcribe + let the user choose) instead of deciding.
  * every dead end parks with a reason instead of closing the popup.

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
    for needed in ("start(stream, batchOnly)", "cutSegment()", "transcribeClip(blob)"):
        if needed not in voice:
            print(f"  FAIL  Voice.{needed} is gone — did the merge get reverted?"); return 1
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
const tick = (ms) => new Promise(r => setTimeout(r, ms || 40));

// ---------- the page around Voice (stubs; the real ones only touch the DOM) ----------
const _escHtml = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const mkBtn = () => ({ style: {}, disabled: false, title: "", textContent: "", setAttribute() {},
                       classList: { add() {}, remove() {}, toggle() {} } });
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
const micStates = [], parked = [], sent = [], fetches = [], spins = [];
function _recLiveStatus(msg) { _recStatusHtml = "STATUS:" + msg; recLiveEl.innerHTML = _recStatusHtml; }
function _setMic(st) { micStates.push(st); }
function _syncPauseBtn() {}
function _recTimerPause() {}
function _recTimerResume() {}
function _recPolishStart(l) { spins.push("start:" + (l || "")); }
function _recPolishStop() { spins.push("stop"); }
function send() { sent.push(inputEl.value); }
// Mirrors the real 2-liner: park the result for Polish/Edit/Send/Cancel + show the reason.
function _voiceParkResult(raw, before, reason) { _batchResult = { raw: raw || "", before: before || "" }; parked.push({ raw, before, reason }); }
function _downTo24k(f32) { return f32; }
function _f32ToPcm16(f32) { return new Int16Array(f32.length); }
// /api/asr: takes replies from asrQueue when it has any (a string = that transcript,
// {fail:1} = HTTP 502), else falls back to asrDefault. /api/polish tidies whatever it got.
let asrQueue = [], asrDefault = "so this is what i said";
const asrCalls = () => fetches.filter(f => f.url.startsWith("/api/asr")).length;
async function authedFetch(url, opts) {
  fetches.push({ url, opts });
  if (url.startsWith("/api/asr")) {
    const r = asrQueue.length ? asrQueue.shift() : asrDefault;
    if (r && r.fail) return { ok: false, status: 502, json: async () => ({}), text: async () => "" };
    return { ok: true, status: 200, json: async () => ({ text: String(r) }), text: async () => "" };
  }
  return { ok: true, status: 200, json: async () => ({ text: "POLISHED(" + (JSON.parse(opts.body).text) + ")" }), text: async () => "" };
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
  [micStates, parked, sent, fetches, sockets, recorders, spins].forEach(a => a.length = 0);
  endedHandler = null; FakeAC.last = null;
  inputEl.value = ""; recLiveEl.innerHTML = ""; _recStatusHtml = ""; _batchResult = null;
  _voiceParkReason = ""; asrQueue = []; asrDefault = "so this is what i said"; lastAsrRaw = "";
  recSendEl.disabled = false; recSendEl.title = ""; recPauseEl.style.display = "none";
  Voice.s = null; _recording = false;
}

__VOICE__

const bar = () => recLiveEl.innerHTML;

// =====================================================================
console.log("=== ⚙ batch: the same session, with no stream ===");
reset();
Voice.start(mkStream(), true);
const s0 = Voice.s;
check("no WebSocket is opened for a batch recording", sockets.length === 0);
check("the session is batch-only from the start", s0.batchOnly === true && s0.dropped === true);
check("...with no realtime provider", s0.provider === "");
check("a clip is being recorded", recorders.length === 1 && recorders[0].state === "recording");
check("the bar names the mode and engine", /batch mode/.test(bar()) && bar().includes("whisper-big"), bar().slice(0, 95));
check("...whether it is recording or paused", /— recording/.test(bar()), bar().slice(0, 95));
check("...and nothing else: status, not a how-to", !/tap ⏸/.test(bar()) && !/Polish/.test(bar()));
check("...without pretending to connect anything", !/connecting/i.test(bar()));
check("⏸ is available in batch too", recPauseEl.style.display === "");
check("Send waits for a transcript, exactly like realtime",
      recSendEl.disabled === true && /⏸/.test(recSendEl.title), recSendEl.title.slice(0, 55));
check("the capture loop buffers no PCM for batch", (feedAudio(3), s0.pending.length === 0 && s0.pendBytes === 0));

console.log("=== ⏸ transcribes the segment, ▶ opens the next one ===");
asrQueue = ["first bit"];
Voice.togglePause();
await tick();
check("⏸ posts the clip to /api/asr", asrCalls() === 1);
check("...honouring the ⚙-selected batch engine + session context",
      fetches[0].url.includes("which=whisper-big") && fetches[0].url.includes("sid=sid1"), fetches[0].url);
check("...the words appear in the bar", bar().includes("first bit"), bar().slice(0, 60));
check("...followed by the status only, never instructions",
      /batch mode/.test(bar()) && !/tap ▶/.test(bar()) && !/Polish \/ Edit \/ Send/.test(bar()), bar().slice(0, 110));
check("...and in the input box", inputEl.value === "first bit", inputEl.value);
check("...Send becomes usable, as it does on the first realtime token", recSendEl.disabled === false);
check("...the spinner ran in the time slot, leaving the transcript alone",
      spins.includes("start:asr") && spins.includes("stop"), spins.join(","));
check("the clip is closed rather than paused (each segment is a whole file)",
      recorders.length === 1 && recorders[0].state === "inactive" && recorders[0].pauses === 0);
asrQueue = ["and the second bit"];
Voice.togglePause();                 // ▶
check("▶ opens a fresh clip", recorders.length === 2 && recorders[1].state === "recording");
feedAudio(2);
Voice.togglePause();                 // ⏸ again
await tick();
check("the second segment is appended, in order",
      Voice.s.finalText === "first bit and the second bit", JSON.stringify(Voice.s.finalText));
check("...one POST per segment, no re-uploads", asrCalls() === 2);
Voice.stop("send");
await tick(80);
check("Stop submits the accumulated text without re-transcribing", asrCalls() === 2);
check("...and it is what was sent", sent.length === 1 && sent[0] === "first bit and the second bit", sent.join("|"));

console.log("=== Polish / Edit / Send on a batch recording ===");
reset(); Voice.start(mkStream(), true); feedAudio(2);
asrQueue = ["all of it at once"];
Voice.stop("edit"); await tick(60);
check("Stop with no ⏸ at all transcribes the open clip",
      inputEl.value === "all of it at once" && asrCalls() === 1, inputEl.value);
check("...Edit does not polish or submit",
      !fetches.some(f => f.url.startsWith("/api/polish")) && sent.length === 0);
check("the long-press debug view gets the raw ASR text", lastAsrRaw === "all of it at once");

reset(); Voice.start(mkStream(), true); feedAudio(1);
asrQueue = ["please tidy this"];
Voice.stop("polish"); await tick(80);
check("Polish transcribes then polishes", inputEl.value === "POLISHED(please tidy this)", inputEl.value);
check("...and submits nothing", sent.length === 0);

console.log("=== a segment whose upload fails is retried, not dropped ===");
reset(); Voice.start(mkStream(), true); feedAudio(1);
asrQueue = [{ fail: 1 }];
Voice.togglePause(); await tick();
check("the failed clip is kept for a retry", Voice.s.segFail.filter(Boolean).length === 1);
check("...and the bar says so instead of looking like silence",
      /⚠/.test(bar()) && /failed/.test(bar()), bar().slice(-90));
Voice.togglePause(); feedAudio(1);                       // ▶ keep talking
asrQueue = ["second segment", "first segment retried"];  // Stop: open clip first, then the retry
Voice.stop("edit"); await tick(120);
check("Stop retries it", asrCalls() === 3);
check("...and the retried words go back in their own slot, not at the end",
      inputEl.value === "first segment retried second segment", inputEl.value);

reset(); Voice.start(mkStream(), true); feedAudio(1);
asrQueue = [{ fail: 1 }];
Voice.togglePause(); await tick();
Voice.togglePause(); feedAudio(1);
asrQueue = ["the part that worked", { fail: 1 }];        // retry fails too
Voice.stop("edit"); await tick(120);
check("a segment that fails twice → keep the rest, say what is missing, decide nothing",
      parked.length === 1 && /1 段没能转写/.test(parked[0].reason) && parked[0].raw === "the part that worked",
      parked.length ? parked[0].reason : "not parked");

console.log("=== the 5-min cap now covers batch as well ===");
reset(); Voice.start(mkStream(), true);
asrQueue = ["talked for five minutes"];
Voice.s.startMs = Date.now() - (VOICE_MAX_MS + 1000);
feedAudio(1);
await tick(80);
check("the recording auto-stops at the cap", Voice.s === null && recorders[0].state === "inactive");
check("...and parks: transcribed, reason shown, nothing decided for you",
      parked.length === 1 && /5 分钟/.test(parked[0].reason) && parked[0].raw === "talked for five minutes",
      parked.length ? parked[0].reason : "not parked");
check("...with the result kept for Polish/Edit/Send", !!_batchResult && _batchResult.raw === "talked for five minutes");

console.log("=== batch dead ends park with a reason (they used to vanish) ===");
reset(); Voice.start(mkStream(), true); feedAudio(1);
asrQueue = [{ fail: 1 }, { fail: 1 }];
Voice.stop("polish"); await tick(120);
check("nothing transcribable at all → park, don't close the popup",
      parked.length === 1 && /识别失败/.test(parked[0].reason), parked.length ? parked[0].reason : "not parked");
reset(); Voice.start(mkStream(), true); feedAudio(1);
asrQueue = ["   "];
Voice.stop("polish"); await tick(80);
check("an empty transcript parks too", parked.length === 1 && /没有识别到内容/.test(parked[0].reason),
      parked.length ? parked[0].reason : "not parked");

console.log("=== batch inherits the rest of the realtime session's care ===");
reset(); inputEl.value = "already typed";
Voice.start(mkStream(), true);
check("dictation appends after existing text", Voice.s.before === "already typed ");
Voice.cancel();
// Restores `before` — the text you had plus the separator space dictation would have gone
// after. No dictated words survive, which is the point.
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
check("⏸ in realtime does NOT cut a segment (the stream is the transcriber)",
      (Voice.togglePause(), asrCalls() === 0 && recorders[0].pauses === 1));
Voice.togglePause();
check("...it pauses and resumes the one recorder", recorders.length === 1 && recorders[0].resumes === 1);

console.log("=== realtime that never connects, stopped by hand → batch, at once ===");
asrQueue = ["recovered from the local clip"];
Voice.stop("polish");
check("no 30s drain wait when nothing was ever connected", Voice.s._finDeadline <= Date.now());
await tick(100);
check("it falls back to the batch engine", asrCalls() === 1);
check("...and the words are not lost", inputEl.value === "POLISHED(recovered from the local clip)", inputEl.value);

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
