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
    # The join between "what was already in the box" and "what you just dictated" is
    # pulled out of the page too, rather than copied here: every insertion AND the
    # equality check that lets a polished version replace the raw one go through it, so a
    # copy that drifted would leave the real seam untested while this file stayed green.
    jm = re.search(r"\n  (const _CJK = .*?\n  function _joinDict\(before, add\) \{.*?\n  \})\n", src, re.S)
    if not jm:
        print("  FAIL  _joinDict is gone from static/index.html"); return 1
    # Where dictation lands. The composer appends; a Task box takes it at the CARET,
    # which is the whole point of dictating into something you are editing — and it is
    # the part that is easy to get subtly wrong, so it is driven rather than read.
    vt = re.search(r"\n  (const vtEl = .*?\n  function vtCurrent\(before, text\) \{[^}]*\})\n", src, re.S)
    if not vt:
        print("  FAIL  could not extract the vt* helpers from static/index.html"); return 1
    # ✕ has to work from every state the bar can be in. It did not: the handler was a
    # list of "if in THIS phase, undo it" branches with no else, so in any state they
    # did not describe the button did nothing at all and the bar could not be dismissed.
    cn = re.search(r"\n  (async function _voiceCancel\(\) \{.*?\n  \}\n  // One ending.*?\n  function _voiceDone\(\) \{.*?\n  \})\n", src, re.S)
    if not cn:
        print("  FAIL  could not extract _voiceCancel/_voiceDone from static/index.html"); return 1
    # Dictating into a Task box ends in a choice — raw or polished — and that choice is
    # made ON THE RECORDING BAR, under the text it already echoes (it was a full-screen
    # window for a day; covering the thing you are choosing between is not a choice).
    # Driven, not read: which version 插入 actually writes is the whole feature.
    pk = re.search(r"\n  (let _pick = null;.*?\n  function _pickTake\(\) \{.*?\n  \})\n", src, re.S)
    if not pk:
        print("  FAIL  could not extract the _pick block from static/index.html"); return 1
    for needed in ("function _pickSwap(", "function _pickTake(", "function voicePickShow("):
        if needed not in pk.group(1):
            print(f"  FAIL  {needed} is gone from the _pick block"); return 1
    # Which bar this is, decided from the first frame. The _setMic("rec") stub below calls
    # this, exactly as the real _showRecBar does.
    mm = re.search(r"\n  (function _recMemoMode\(\) \{.*?\n  \})\n", src, re.S)
    if not mm:
        print("  FAIL  _recMemoMode is gone from static/index.html"); return 1
    # Tapping ✕ (or a mic that never opens) while the device is still opening: nothing
    # was captured, so the marker must come out and the target go back. Those paths used
    # to call _setMic("idle") directly, which does neither.
    mt = re.search(r"async function micTap\(\) \{.*?\n  \}\n", src, re.S)
    if not mt or '_setMic("idle")' in mt.group(0):
        print('  FAIL  micTap abandons a recording with _setMic("idle") — that leaves the '
              "[🎤] marker in the box and the target pointing at it"); return 1
    for fn in ("_showRecBar", "_recArmBar"):
        body = re.search(r"function " + fn + r"\(\) \{.*?\n  \}", src, re.S)
        if not body or "_recMemoMode()" not in body.group(0):
            print(f"  FAIL  {fn} no longer sets the bar's mode — a Task box's bar would "
                  "come up looking and behaving like the composer's"); return 1

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
      recCancelEl = mkBtn(), recPauseEl = mkBtn(), micBtn = mkBtn(),
      recSwapEl = mkBtn(), recUseEl = mkBtn(), recTimeEl = mkBtn();
// A classList that actually remembers, because the Task-box bar is a DIFFERENT bar and
// the difference is carried by classes: the composer must never wear them.
function mkClassList() {
  const set = new Set();
  return { _set: set, add: (...c) => c.forEach(x => set.add(x)),
           remove: (...c) => c.forEach(x => set.delete(x)),
           toggle: (c, on) => (on ? set.add(c) : set.delete(c)),
           contains: (c) => set.has(c) };
}
const recBar = { style: {}, classList: mkClassList() };
let _recTimer = null;
const inputEl = { value: "", focus() {}, dispatchEvent() {} };
let _recStatusHtml = "", _recording = false, inputFromVoice = false, _voiceParkReason = "";
let _batchResult = null, _polAbort = null, _polCtx = null, _polSuperseded = false;
let lastAsrRaw = "", lastPolished = "", lastAsrSec = null, lastPolishSec = null,
    lastAsrBefore = "", lastAsrAfter = "";
// The dictation target. Null = the composer (unchanged); a textarea = insert at its
// caret. Extracted from the page so the real slot/write logic is what runs here.
let voiceTarget = null;
function mkTA(value, caret) {
  return { value, selectionStart: caret, selectionEnd: caret,
           focus() {}, dispatchEvent() {},
           setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; } };
}
let _micArming = null;
function _polishAbort() { _polAbort = null; }
__VT__
__CANCEL__
__JOIN__
__PICK__
__MEMOMODE__
let asrRtEngine = "soniox", sonioxAvail = true, asrWhich = "whisper-big", attachedSid = "sid1", authToken = "t";
let asrLang = "zh,en";   // the voice menu's language row; both call sites read it
const isPhone = () => false;
const micStates = [], parked = [], sent = [], fetches = [], spins = [];
function _recLiveStatus(msg) { _recStatusHtml = "STATUS:" + msg; recLiveEl.innerHTML = _recStatusHtml; }
// Mirrors the real chain: _setMic("rec") → _showRecBar() → _recMemoMode(), the step that
// dresses the bar as the composer's or as a Task box's.
function _setMic(st) { micStates.push(st); if (st === "rec") _recMemoMode(); }
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
class WebSocket {
  constructor(u) { this.url = u; this.readyState = 0; this.sent = []; sockets.push(this); }
  send(d) { this.sent.push(d); }        // counted: "nothing goes out while paused" is the claim
  close() { this.readyState = 3; }
}
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
  recBar.classList._set.clear(); _pick = null; voiceTarget = null;
  [recSwapEl, recUseEl, recStopEl].forEach(b => { b.disabled = false; b.style.display = ""; b.textContent = ""; });
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
// NOT in the input box, on purpose. Writing each token in as it arrived consumed the
// [🎤] marker on the first one — so the only thing showing where the words would land
// disappeared the moment you started talking. The box is written once, by the button you
// press (Polish / Edit / Send), which the cases below drive.
check("...and NOT into the input box while you are still talking",
      inputEl.value === "", JSON.stringify(inputEl.value));
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
// Two things in one line now, and both matter at this instant: capture has started
// (so it says you may speak — the counterpart to the "先别说话" shown while the mic was
// still opening) and the socket has not connected yet.
check("the bar says you may speak now", /可以说了/.test(bar()), bar().slice(0, 60));
check("...and that the recognizer is still connecting", /连接识别服务/.test(bar()), bar().slice(0, 60));
check("realtime DOES buffer PCM while connecting", (feedAudio(2), Voice.s.pendBytes > 0));
check("⏸ in realtime does NOT cut a segment (the stream is the transcriber)",
      (Voice.togglePause(), asrCalls() === 0 && recorders[0].pauses === 1));
Voice.togglePause();
check("...it pauses and resumes the one recorder", recorders.length === 1 && recorders[0].resumes === 1);

console.log("=== paused / stopped: the mic stays open, the wire goes quiet ===");
// The mic device is released late (teardown, after the provider drains) — accepted.
// What must NOT happen is audio leaving the browser while paused or after the finish
// button: capture drops the frame entirely rather than buffering it, so there is
// nothing to flush later either.
reset();
Voice.start(mkStream(), false);
const ws0 = sockets[0];
ws0.readyState = 1; ws0.onopen && ws0.onopen();      // connected → frames go straight out
feedAudio(3);
const beforePause = ws0.sent.length;
check("while recording, frames go to the server", beforePause >= 3, String(beforePause));
Voice.togglePause();
const atPause = ws0.sent.length;
feedAudio(5);
check("⏸ and nothing more is sent", ws0.sent.length === atPause,
      atPause + " → " + ws0.sent.length);
check("...and nothing is buffered for a later flush either",
      Voice.s.pending.length === 0 && Voice.s.pendBytes === 0,
      Voice.s.pending.length + "/" + Voice.s.pendBytes);
// A reconnect while paused must not become a back door for the frames dropped above.
Voice.flushPending(ws0);
check("...so even an explicit flush has nothing to send", ws0.sent.length === atPause);
Voice.togglePause();                                  // ▶
feedAudio(2);
check("▶ starts sending again", ws0.sent.length > atPause, atPause + " → " + ws0.sent.length);

const afterResume = ws0.sent.length;
Voice.stop("polish");                                 // the finish button
const atStop = ws0.sent.length;                       // stop() flushes + sends its finish frame
feedAudio(5);
check("after Polish, audio frames stop at once", ws0.sent.length === atStop,
      atStop + " → " + ws0.sent.length);
check("...the capture callback is detached, not just ignored",
      Voice.s.sp.onaudioprocess === null);
check("...and the local recorder is closed", recorders[recorders.length - 1].state === "inactive");
check("...while the mic itself is still open until the transcription ends (accepted)",
      Voice.s.stream.getTracks()[0].stopped !== true);
check("...the only thing sent at the tap was the finish marker",
      atStop - afterResume <= 1, String(atStop - afterResume));

console.log("=== realtime that never connects, stopped by hand → batch, at once ===");
asrQueue = ["recovered from the local clip"];
Voice.stop("polish");
check("no 30s drain wait when nothing was ever connected", Voice.s._finDeadline <= Date.now());
await tick(100);
check("it falls back to the batch engine", asrCalls() === 1);
check("...and the words are not lost", inputEl.value === "POLISHED(recovered from the local clip)", inputEl.value);

console.log("=== dictating into a box that already has text ===");
// You can keep talking with a half-written message in the box: the new speech is
// APPENDED, and the text already there is handed to /api/polish as context. Without
// that context the new part was polished in isolation — no way to know the sentence was
// left half-finished, which terms were already established, or what "那个" pointed at.
check("_joinDict came out of the page, not a copy here", typeof _joinDict === "function");
check("no space at a Chinese seam", _joinDict("加了四个分量, ", "并把 total 也输出") === "加了四个分量,并把 total 也输出",
      _joinDict("加了四个分量, ", "并把 total 也输出"));
check("...but the space stays between two English words",
      _joinDict("fix the scorer ", "and rerun it") === "fix the scorer and rerun it",
      _joinDict("fix the scorer ", "and rerun it"));
check("...and between English and Chinese, where it belongs",
      _joinDict("run the scorer ", "然后看结果") === "run the scorer 然后看结果");
check("an empty box joins to nothing", _joinDict("", "说的话") === "说的话");

inputEl.value = "我改了 scorer, 加了四个 reward 分量,";
fetches.length = 0; asrQueue = ["然后把 total reward 也输出出来"];
const s9 = Voice.start(null, true);
await tick(); Voice.cutSegment(); await tick(60);
await Voice.stop("polish"); await tick(200);
const pol = fetches.filter(f => f.url.startsWith("/api/polish")).map(f => JSON.parse(f.opts.body));
check("the polish call carries what was already in the box", pol.length === 1
      && /我改了 scorer/.test(pol[0].before || ""), JSON.stringify(pol[0] || {}).slice(0, 90));
check("...and only the NEW speech as the text to rewrite",
      pol.length === 1 && !/我改了 scorer/.test(pol[0].text || ""), (pol[0] || {}).text);
// The failure this guards: replacing the box instead of appending, which eats the
// half-written message you were adding to.
check("the existing text is still there", /^我改了 scorer, 加了四个 reward 分量/.test(inputEl.value), inputEl.value);
check("...with the dictation after it", /POLISHED/.test(inputEl.value), inputEl.value);
// The seam rule itself is covered by the unit cases above; what matters here is that
// the insertion path goes THROUGH the helper rather than concatenating on its own. (The
// stub polish returns "POLISHED(...)", which starts with a Latin letter, so a space at
// this particular seam is the correct answer — asserting "no space" here was wrong.)
check("...joined by the same helper, not by a second rule",
      inputEl.value === _joinDict("我改了 scorer, 加了四个 reward 分量, ",
                                  "POLISHED(然后把 total reward 也输出出来)"), inputEl.value);

console.log("=== ✕ always closes the bar ===");
// Reported from a real session: the bar was up with Polish/Edit/Send, no recording
// behind it, and ✕ did nothing at all. The handler was a list of "if in THIS phase,
// undo it" branches with no else — so any state they did not describe was a dead end.
{
  micStates.length = 0;
  Voice.s = null; _polAbort = null; _batchResult = null; _micArming = null;
  await _voiceCancel();
  check("with nothing in flight, it still closes", micStates.includes("idle"),
        JSON.stringify(micStates));
  check("...and hands the target back to the composer", voiceTarget === null);
}
{
  // Cancelling a Task-box dictation must restore THAT box — this wrote the box's text
  // into the composer, because two restores were missed when the target became a value.
  const ta = mkTA("原本的内容", 5);
  voiceTarget = { el: ta, pick: true, after: "" };
  const slots = vtSlots(ta); voiceTarget.after = slots.after;
  inputEl.value = "composer 里的草稿";
  _batchResult = { raw: "说了点什么", before: slots.before };
  Voice.s = null; _polAbort = null; _micArming = null;
  await _voiceCancel();
  check("the box goes back to what it held", ta.value === "原本的内容", JSON.stringify(ta.value));
  check("...and the composer was never touched",
        inputEl.value === "composer 里的草稿", JSON.stringify(inputEl.value));
  check("...and the parked result is dropped", _batchResult === null);
}

console.log("=== dictating into a Task box lands at the caret ===");
// The composer appends, because you are about to read the whole message before
// sending it. A task description is something you EDIT — so the words go where the
// cursor is, which is also where you were looking when you reached for the mic.
{
  const ta = mkTA("前面的内容。后面的内容。", 6);   // caret right after 「前面的内容。」
  voiceTarget = { el: ta, pick: true, after: "" };
  const slots = vtSlots(ta);
  voiceTarget.after = slots.after;
  check("the text before the caret is kept", slots.before === "前面的内容。", JSON.stringify(slots.before));
  check("...and so is the text after it", slots.after === "后面的内容。", JSON.stringify(slots.after));
  vtWrite(slots.before, "插进来的话");
  check("the new words land in the middle",
        ta.value === "前面的内容。插进来的话后面的内容。", JSON.stringify(ta.value));
  check("...and the caret follows them, ready to keep typing",
        ta.selectionStart === "前面的内容。插进来的话".length, String(ta.selectionStart));
}
{
  // A selection, not a caret: dictation replaces what was selected, like typing would.
  const ta = mkTA("把这段换掉吧", 1);
  ta.selectionStart = 1; ta.selectionEnd = 4;
  voiceTarget = { el: ta, pick: true, after: "" };
  const slots = vtSlots(ta);
  voiceTarget.after = slots.after;
  vtWrite(slots.before, "新的");
  // 「把这段换掉吧」 with 1..4 selected is 「这段换」 — so what is left is 把 + 新的 + 掉吧.
  check("a selected range is replaced, not pushed aside",
        ta.value === "把新的掉吧", JSON.stringify(ta.value));
}
{
  // The composer inserts at the caret now, like the Task boxes. With the caret at the
  // end — which is where typing and a restored draft both leave it — that is the same
  // appending behaviour it always had, space included.
  voiceTarget = { el: inputEl, pick: false, after: "" };
  inputEl.value = "已经写了半句";
  inputEl.selectionStart = inputEl.selectionEnd = inputEl.value.length;
  const slots = vtSlots(inputEl);
  check("the composer still appends, with its space", slots.before === "已经写了半句 " && slots.after === "",
        JSON.stringify(slots));
  vtWrite(slots.before, "接着说");
  // The seam rule applies here as everywhere: the trailing space that `before` carries
  // is dropped between two Chinese sides and kept between Latin ones.
  check("...and writes straight in, Chinese seam closed",
        inputEl.value === "已经写了半句接着说", JSON.stringify(inputEl.value));
  inputEl.value = "half a sentence";
  // A real textarea moves the caret to the end when .value is assigned; this stub does
  // not, so say it here rather than inherit the previous case's caret.
  inputEl.selectionStart = inputEl.selectionEnd = inputEl.value.length;
  const en = vtSlots(inputEl);
  vtWrite(en.before, "and the rest");
  check("...while an English seam keeps its space",
        inputEl.value === "half a sentence and the rest", JSON.stringify(inputEl.value));
  check("...still flagged as voice-typed, so the grammar pass knows", inputFromVoice === true);
}
{
  // Into an empty box at position 0 — the common case the first time you use it.
  const ta = mkTA("", 0);
  voiceTarget = { el: ta, pick: true, after: "" };
  const slots = vtSlots(ta);
  vtWrite(slots.before, "第一句话");
  check("an empty box just gets the words", ta.value === "第一句话", JSON.stringify(ta.value));
  voiceTarget = null;
}

// =====================================================================
console.log("=== Task box: nothing is written until you pick ===");
reset();
const ta1 = mkTA("先写了一句。", 6);          // caret at the end of what is already there
voiceTarget = { el: ta1, pick: true, after: "", label: "当前任务" };
vtMarkIn();
Voice.start(mkStream(), true);
feedAudio(2);
asrQueue = ["把测试跑一遍"];
Voice.togglePause(); await tick(60);          // ⏸ transcribes the segment
check("the words show up in the bar as they arrive", bar().includes("把测试跑一遍"), bar().slice(0, 60));
check("...but no words go into the Task box — only the marker holding the spot",
      ta1.value === "先写了一句。[🎤]", JSON.stringify(ta1.value));
check("the bar wears the Task-box mode from the start", recBar.classList.contains("rec-memo"));

console.log("=== Polish → choose raw or polished, on the bar ===");
Voice.togglePause(); feedAudio(1);            // ▶ and carry on
asrQueue = [""];
Voice.stop("polish"); await tick(120);
check("Polish polishes", fetches.some(f => f.url === "/api/polish"));
check("...and the bar shows the polished version, saying which one it is",
      /润色后/.test(bar()) && bar().includes("POLISHED(把测试跑一遍)"), bar().slice(0, 120));
check("...with the box still wordless — polish decides nothing for you",
      ta1.value === "先写了一句。[🎤]", JSON.stringify(ta1.value));
check("...the bar is still up (it used to close and take the text with it)",
      recBar.style.display === "flex" && recBar.classList.contains("rec-picked"));
check("...and there are two versions, so the swap is offered",
      recBar.classList.contains("rec-two") && recSwapEl.textContent === "看原文", recSwapEl.textContent);
check("Polish is gone once it has run — there is nothing left for it to do",
      recBar.classList.contains("rec-picked"));
_pickSwap();
check("看原文 shows the raw text, and offers the way back",
      /识别原文/.test(bar()) && bar().includes("把测试跑一遍") && !bar().includes("POLISHED")
      && recSwapEl.textContent === "看润色", bar().slice(0, 120));
_pickTake();
check("插入 writes the version ON SCREEN, at the caret",
      ta1.value === "先写了一句。把测试跑一遍", JSON.stringify(ta1.value));
check("...and the composer was never touched", inputEl.value === "", JSON.stringify(inputEl.value));
check("...the bar closes and hands the target back",
      voiceTarget === null && !recBar.classList.contains("rec-memo") && micStates.includes("idle"));

console.log("=== ...or 插入 the polished one ===");
reset();
const ta2 = mkTA("", 0);
voiceTarget = { el: ta2, pick: true, after: "" };
vtMarkIn();
Voice.start(mkStream(), true); feedAudio(1);
asrQueue = ["这段话有点乱"];
Voice.stop("polish"); await tick(120);
_pickTake();
check("the polished version is what goes in", ta2.value === "POLISHED(这段话有点乱)", JSON.stringify(ta2.value));

console.log("=== 插入 mid-recording: no polish, no choice, just the words ===");
reset();
const ta3 = mkTA("注意:", 3);
voiceTarget = { el: ta3, pick: true, after: "" };
vtMarkIn();
Voice.start(mkStream(), true); feedAudio(1);
asrQueue = ["别动那个文件"];
Voice.stop("send"); await tick(120);          // the 插入 button while still recording
check("the raw text goes straight in", ta3.value === "注意:别动那个文件", JSON.stringify(ta3.value));
check("...nothing was polished — one button, one meaning",
      !fetches.some(f => f.url === "/api/polish"));
check("...and nothing was SENT: a Task box has nothing to submit", sent.length === 0);

console.log("=== a failed polish must not read as \"it was already fine\" ===");
reset();
const ta4 = mkTA("", 0);
voiceTarget = { el: ta4, pick: true, after: "" };
vtMarkIn();
Voice.start(mkStream(), true); feedAudio(1);
asrQueue = ["原样保留这句"];
const _af = authedFetch;
authedFetch = async (url, opts) => {
  if (url === "/api/polish") { fetches.push({ url, opts }); return { ok: false, status: 503, json: async () => ({}) }; }
  return _af(url, opts);
};
Voice.stop("polish"); await tick(120);
authedFetch = _af;
check("the bar says the polish did not happen, and why",
      /润色没成功/.test(bar()) && /no LLM configured/.test(bar()), bar().slice(0, 140));
check("...shows the raw text instead of nothing", bar().includes("原样保留这句"), bar().slice(0, 140));
check("...offers no swap, because there is only one version",
      !recBar.classList.contains("rec-two"));
_pickTake();
check("...and 插入 still works", ta4.value === "原样保留这句", JSON.stringify(ta4.value));

console.log("=== 插入 while the polish is still out ===");
// You read the raw version on the bar, decided it was fine, and took it. The reply that
// arrives afterwards must not re-open the bar over a box you have finished with.
reset();
const ta6 = mkTA("", 0);
voiceTarget = { el: ta6, pick: true, after: "" };
vtMarkIn();
Voice.start(mkStream(), true); feedAudio(1);
asrQueue = ["够用了不用润色"];
let releasePolish;
const _af2 = authedFetch;
authedFetch = (url, opts) => {
  if (url === "/api/polish") {
    fetches.push({ url, opts });
    return new Promise(r => { releasePolish = () => r({ ok: true, status: 200, json: async () => ({ text: "POLISHED(x)" }) }); });
  }
  return _af2(url, opts);
};
Voice.stop("polish"); await tick(80);
check("the raw text is on the bar while the polish is out",
      bar().includes("够用了不用润色") && _pick !== null, bar().slice(0, 90));
_pickTake();
check("插入 takes it at once", ta6.value === "够用了不用润色", JSON.stringify(ta6.value));
releasePolish(); await tick(80);
authedFetch = _af2;
check("...and the late reply changes nothing — no bar, no overwrite",
      _pick === null && ta6.value === "够用了不用润色"
      && !recBar.classList.contains("rec-picked"), JSON.stringify(ta6.value));

console.log("=== ✕ from the choice: box unchanged, bar gone ===");
reset();
const ta5 = mkTA("本来的内容", 5);
voiceTarget = { el: ta5, pick: true, after: "" };
vtMarkIn();
Voice.start(mkStream(), true); feedAudio(1);
asrQueue = ["说了但不想要"];
Voice.stop("polish"); await tick(120);
await _voiceCancel();
check("the box keeps exactly what it held", ta5.value === "本来的内容", JSON.stringify(ta5.value));
check("...and the bar is dismissed", _pick === null && voiceTarget === null
      && !recBar.classList.contains("rec-memo") && !recBar.classList.contains("rec-picked"));

console.log("=== the composer's bar is NOT this bar ===");
reset();
Voice.start(mkStream(), true); feedAudio(1);
check("dictating into the composer wears none of the Task-box classes",
      !recBar.classList.contains("rec-memo") && !recBar.classList.contains("rec-picked")
      && !recBar.classList.contains("rec-two"), [...recBar.classList._set].join(","));
asrQueue = ["a normal message"];
Voice.stop("send"); await tick(120);
check("...and Send still sends, unchanged", sent.length === 1 && sent[0] === "a normal message", sent.join("|"));
check("...with no choice offered", _pick === null);

console.log("=== the insertion point is something you can SEE ===");
// Nothing is written while you talk, so until this marker existed there was nothing on
// screen saying where the words would land — and an unfocused textarea shows no caret.
reset();
const tm = mkTA("头部。尾部。", 3);          // caret between the two sentences
voiceTarget = { el: tm, pick: true, after: "" };
vtMarkIn();
check("a marker goes in at the caret", tm.value === "头部。[🎤]尾部。", JSON.stringify(tm.value));
check("...and the caret sits after it, so typing carries on where you were",
      tm.selectionStart === "头部。[🎤]".length, String(tm.selectionStart));
Voice.start(mkStream(), true); feedAudio(1);
asrQueue = ["插在中间的话"];
Voice.togglePause(); await tick(60);
check("while you talk the box holds the marker and nothing else",
      tm.value === "头部。[🎤]尾部。", JSON.stringify(tm.value));
Voice.stop("send"); await tick(100);
check("插入 replaces the marker with the words", tm.value === "头部。插在中间的话尾部。", JSON.stringify(tm.value));
check("...leaving no marker behind", !tm.value.includes("🎤"));

console.log("=== edit the box while talking: the marker is where it lands ===");
reset();
const te = mkTA("原句。", 3);
voiceTarget = { el: te, pick: true, after: "" };
vtMarkIn();
Voice.start(mkStream(), true); feedAudio(1);
// You keep typing while dictating — the marker moves with your text, and the old
// character offsets no longer describe the box at all.
te.value = "改过的开头。" + "[🎤]" + "后来补的结尾。";
asrQueue = ["说出来的那句"];
Voice.stop("send"); await tick(100);
check("the words land at the marker, not at a stale offset",
      te.value === "改过的开头。说出来的那句后来补的结尾。", JSON.stringify(te.value));

console.log("=== cancel takes the marker with it ===");
reset();
const tc = mkTA("一个字都不该变。", 4);
voiceTarget = { el: tc, pick: true, after: "" };
vtMarkIn();
Voice.start(mkStream(), true); feedAudio(1);
await _voiceCancel();
check("✕ mid-recording leaves the box exactly as it was",
      tc.value === "一个字都不该变。" && !tc.value.includes("🎤"), JSON.stringify(tc.value));
reset();
const tc2 = mkTA("说了但不要。", 6);
voiceTarget = { el: tc2, pick: true, after: "" };
vtMarkIn();
Voice.start(mkStream(), true); feedAudio(1);
asrQueue = ["录了一句"];
Voice.stop("polish"); await tick(120);
await _voiceCancel();
check("...and so does ✕ from the raw/polished choice",
      tc2.value === "说了但不要。" && !tc2.value.includes("🎤"), JSON.stringify(tc2.value));

console.log("=== give up while the mic is still opening ===");
reset();
const tg = mkTA("别留下垃圾。", 6);
voiceTarget = { el: tg, pick: true, after: "" };
vtMarkIn();
check("the marker is in", tg.value.includes("[🎤]"), tg.value);
_micArming = { cancelled: false };      // the gap between tapping 🎤 and the device opening
await _voiceCancel();
_micArming = null;
check("✕ during the opening gap cleans the box up",
      tg.value === "别留下垃圾。", JSON.stringify(tg.value));
check("...and hands the target back, so the next dictation is not aimed at this box",
      voiceTarget === null);

console.log("=== the composer gets the marker too, and the caret ===");
// Same insertion point as a Task box, for the same reason: the bar covers the composer
// while you talk, so where the words will land has to be visible. What does NOT change
// is the rest of the composer — it still writes as you speak, and still has Polish /
// Edit / Send. Only the destination moved from "the end" to "the caret".
reset();
inputEl.value = "开头。结尾。";
inputEl.selectionStart = inputEl.selectionEnd = 3;   // between the two
voiceTarget = { el: inputEl, pick: false, after: "" };   // what the 🎤 button does
vtMarkIn();
check("a marker goes in at the caret", inputEl.value === "开头。[🎤]结尾。", JSON.stringify(inputEl.value));
Voice.start(mkStream(), true); feedAudio(1);
asrQueue = ["插在中间"];
Voice.stop("send"); await tick(120);
check("...the words replace it, in place", sent[0] === "开头。插在中间结尾。", sent.join("|"));
check("...so the text after the caret survives", /结尾。$/.test(sent[0] || ""), sent.join("|"));

reset();
inputEl.value = "";
voiceTarget = { el: inputEl, pick: false, after: "" };
vtMarkIn();
Voice.start(mkStream(), true); feedAudio(1);
asrQueue = ["普通的一条消息"];
Voice.stop("send"); await tick(100);
check("an empty composer behaves exactly as before",
      sent.length === 1 && sent[0] === "普通的一条消息", sent.join("|"));

reset();
inputEl.value = "写了一半";
inputEl.selectionStart = inputEl.selectionEnd = 4;
voiceTarget = { el: inputEl, pick: false, after: "" };
vtMarkIn();
Voice.start(mkStream(), true); feedAudio(1);
await _voiceCancel();
check("✕ leaves the composer exactly as it was",
      inputEl.value === "写了一半" && !inputEl.value.includes("🎤"), JSON.stringify(inputEl.value));

console.log("=== ✕ means 'as it was', not 'before + nothing' ===");
// `before` is not the original text: for the composer it carries the seam space the
// dictated words were going to sit after, so rebuilding the box from it left a stray
// space behind. Cancel remembers the actual value instead.
{
  reset();
  inputEl.value = "写了一半";
  inputEl.selectionStart = inputEl.selectionEnd = 4;      // caret at the end
  voiceTarget = { el: inputEl, pick: false, after: "" };
  vtMarkIn();
  check("the marker sits after the seam space", inputEl.value === "写了一半 [🎤]",
        JSON.stringify(inputEl.value));
  Voice.start(mkStream(), true); feedAudio(1);
  await _voiceCancel();
  check("...and ✕ gives back the exact original", inputEl.value === "写了一半",
        JSON.stringify(inputEl.value));
  check("...with the target handed back too", voiceTarget === null);
}

console.log("=== the [🎤] marker survives the whole recording ===");
// The point of the marker is to say where the words will land. Writing them in as they
// streamed consumed it on the first token — so it was gone exactly while you were
// talking, which is when you are looking for it.
{
  reset();
  inputEl.value = "前面的话后面的话";
  inputEl.selectionStart = inputEl.selectionEnd = 4;      // caret in the MIDDLE
  voiceTarget = { el: inputEl, pick: false, after: "" };
  vtMarkIn();
  check("the marker goes in at the caret", inputEl.value === "前面的话[🎤]后面的话",
        JSON.stringify(inputEl.value));
  // Batch, because that is the path this harness can drive — and ⏸ mid-recording is
  // exactly the moment the old code wrote into the box.
  Voice.start(mkStream(), true); feedAudio(2);
  asrQueue = ["说的第一句"];
  Voice.togglePause(); await tick();
  check("...and is still there after a segment comes back",
        inputEl.value === "前面的话[🎤]后面的话", JSON.stringify(inputEl.value));
  check("...with the words visible in the bar instead",
        bar().includes("说的第一句"), bar().slice(0, 60));
  Voice.stop("edit"); await tick(60);
  check("...and Edit is what puts them in, exactly where the marker was",
        inputEl.value === "前面的话说的第一句后面的话", JSON.stringify(inputEl.value));
}

console.log(_fails.length ? "\nFAILED: " + _fails.join(", ") : "\nall pass");
process.exit(_fails.length ? 1 : 0);
"""
    js = (js.replace("__VOICE__", voice).replace("__CAP__", cap.group(1))
            .replace("__JOIN__", jm.group(1)).replace("__VT__", vt.group(1))
            .replace("__CANCEL__", cn.group(1)).replace("__PICK__", pk.group(1))
            .replace("__MEMOMODE__", mm.group(1)))
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
