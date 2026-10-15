#!/usr/bin/env python3
"""The first sentence, and how to stop guessing about it.

Reported: the first sentence of a dictation often came back in English — sometimes as a
sentence that was never said at all — and everything after it was fine. The server log
had the reason in it:

    flushed 139264B captured while connecting
    connected model=stt-rt-v4 langs=['zh','en'] buffered=139264B

139KB of PCM16 at 24kHz is 2.9 SECONDS of speech, and it was being handed to the
recogniser inside one millisecond, at the very start of the stream, with nothing before
it. That is the worst possible input for a streaming model: it has to choose a language
for the whole burst and gets no chance to run its own VAD incrementally. The audio after
it arrives frame by frame, which is why only the first sentence was wrong.

So the backlog is REPLAYED at a few times real time rather than dumped — that is
_pace_delays, and it is a pure function precisely so it can be tested here.

The other half is evidence: keeping the audio of recent dictations (off unless
asr_keep_days is set) turns "it misheard me" into a file you can play and re-run through
either engine. With a size cap, because a week of dictation is not small.

    python3 tests/test_asr_audio_keep.py      # exit 0 = pass
"""
import json
import os
import struct
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


def main():
    home = tempfile.mkdtemp(prefix="ccweb-keep-")
    os.makedirs(os.path.join(home, ".claude"))
    conf = os.path.join(home, ".claude", "cc_web.conf")
    open(conf, "w").write("token=t\nasr_keep_days=7\nasr_keep_mb=1\n")
    os.environ["HOME"] = home
    os.environ["CC_WEB_TOKEN"] = "t"
    try:
        import cc_web
    except Exception as e:                                  # pragma: no cover
        print("SKIP: cannot import cc_web:", e); return 0

    RATE = 24000
    FRAME = 4096 * 2                       # the browser's chunk: 4096 samples, PCM16
    print("=== the backlog is replayed, not dumped ===")
    # 17 frames ≈ 2.9s — the case from the log.
    d = cc_web._pace_delays([FRAME] * 17, RATE)
    check("every frame gets a wait, in order", len(d) == 17 and all(x > 0 for x in d), str(d[:3]))
    # A frame is 4096/24000 = 171ms of audio; at 6× that is ~28ms of wait.
    check("...proportional to the audio it carries",
          abs(d[0] - (4096 / RATE) / 6.0) < 1e-6, f"{d[0]*1000:.1f}ms")
    check("...so 2.9s of speech is replayed in about half a second",
          0.3 < sum(d) < 0.7, f"{sum(d):.2f}s")
    # A long handshake can leave 30s buffered; the tail of that decides nothing, and
    # nobody should wait 5s for it.
    long_d = cc_web._pace_delays([FRAME] * 180, RATE)
    check("a very long backlog is capped, not paced to the end",
          abs(sum(long_d) - 3.0) < 0.01 and long_d[-1] == 0,
          f"{sum(long_d):.2f}s, last={long_d[-1]}")
    # 3s of budget at ~28ms a frame pays for ~105 of them; the rest go out with no wait.
    check("...and the frames after the cap go out at full speed",
          sum(1 for x in long_d if x == 0) >= 60
          and sum(1 for x in long_d if x > 0) <= 120,
          f'{sum(1 for x in long_d if x == 0)} free / {sum(1 for x in long_d if x > 0)} paced')
    check("nothing buffered → nothing to wait for", cc_web._pace_delays([], RATE) == [])
    # The rate is negotiated (?rate=), so the arithmetic must use it rather than assume.
    d16 = cc_web._pace_delays([FRAME], 16000)
    check("the wait follows the NEGOTIATED rate", d16[0] > d[0], f"{d16[0]:.4f} vs {d[0]:.4f}")

    print("=== the bridge replays through that helper ===")
    src = open(os.path.join(ROOT, "cc_web.py"), encoding="utf-8").read()
    i = src.index("async def _flush_pre():")
    blk = src[i:i + 900]
    check("the flush paces itself", "_pace_delays(pre, rate)" in blk and "asyncio.sleep(d)" in blk,
          blk[:120])
    check("...and says how much audio that was, in seconds",
          "of audio) captured while" in blk)

    print("=== keeping the audio: a WAV you can play ===")
    raw = b"\x01\x02" * 1000
    hdr = cc_web._wav_header(len(raw), RATE)
    check("44 bytes, RIFF/WAVE, PCM16 mono at the session rate",
          len(hdr) == 44 and hdr[:4] == b"RIFF" and hdr[8:12] == b"WAVE"
          and struct.unpack("<H", hdr[22:24])[0] == 1
          and struct.unpack("<I", hdr[24:28])[0] == RATE, hdr[:16].hex())
    check("...with the two sizes the header has to carry",
          struct.unpack("<I", hdr[4:8])[0] == 36 + len(raw)
          and struct.unpack("<I", hdr[40:44])[0] == len(raw))

    base = cc_web._asr_keep_save(hdr + raw, ".wav",
                                 {"path": "realtime", "langs": ["zh", "en"], "sid": "s1"})
    kept = cc_web._asr_keep_dir()
    check("the audio is written", base and (kept / (base + ".wav")).exists(), str(base))
    side = json.loads((kept / (base + ".json")).read_text())
    check("...with a sidecar saying where it came from",
          side["path"] == "realtime" and side["langs"] == ["zh", "en"]
          and side["bytes"] == len(hdr) + len(raw) and side["file"] == base + ".wav",
          str(side))

    print("=== both realtime bridges keep, through the same two calls ===")
    # There are two realtime providers, and the tee was written into one of them and only
    # REFERENCED in the other: `if keep_tmp is not None` with nothing assigning keep_tmp
    # in that function. Python resolves that as a global at runtime, so the first audio
    # frame of an OpenAI-realtime dictation raised NameError inside the reader task,
    # where it was caught as "client:NameError" and the dictation just stopped. Hence
    # open/close helpers rather than two inline copies — and this check, which is about
    # the shape that allowed it: a name used in a function that nothing there binds.
    import ast
    tree = ast.parse(src)
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for fname in ("_soniox_bridge", "asr_stream"):
        fn = fns[fname]
        bound = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
                 for t in ast.walk(n) if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store)}
        used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        calls = {ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}
        check(f"{fname} binds every keep name it reads",
              not ({"keep_tmp"} & used - bound), f"reads keep_tmp, binds {sorted(bound & {'keep_tmp'})}")
        check(f"...and {fname} both opens and closes one",
              {"_asr_keep_open", "_asr_keep_close"} <= calls,
              str(sorted(calls & {"_asr_keep_open", "_asr_keep_close"})))

    # The pair, driven: a tee with a second of PCM in it comes out as a WAV that says it
    # is a second long, and the temp file does not survive.
    tee = cc_web._asr_keep_open()
    check("keeping is on → there is a tee to write into", tee is not None)
    tee.write(b"\x00\x01" * RATE)
    base2 = cc_web._asr_keep_close(tee, RATE, {"path": "realtime", "engine": "openai"})
    wav = kept / (base2 + ".wav")
    side2 = json.loads((kept / (base2 + ".json")).read_text())
    check("...and closing it leaves a WAV of the right length",
          wav.exists() and wav.stat().st_size == 44 + 2 * RATE, str(wav.stat().st_size))
    check("...with the duration written down, from the engine that recorded it",
          side2["seconds"] == 1.0 and side2["engine"] == "openai", str(side2)[:90])
    check("...and the temp PCM is not left behind", not os.path.exists(tee.name), tee.name)
    check("a bridge that never opened one closes cleanly",
          cc_web._asr_keep_close(None, RATE, {}) == "")

    print("=== off unless it is asked for ===")
    # Storing someone's voice is not a default. No asr_keep_days → nothing is written.
    open(conf, "w").write("token=t\n")
    before = len(list(kept.iterdir()))
    check("no asr_keep_days, no files", cc_web._asr_keep_save(hdr + raw, ".wav", {}) == ""
          and len(list(kept.iterdir())) == before, str(before))
    open(conf, "w").write("token=t\nasr_keep_days=7\nasr_keep_mb=1\n")

    print("=== a week, and a size cap ===")
    old = kept / "20200101-000000-aaaaaa.wav"
    old.write_bytes(b"x" * 10)
    os.utime(old, (time.time() - 9 * 86400,) * 2)
    gone = cc_web._asr_keep_prune(7, 0)
    check("what is past its week goes", not old.exists() and gone >= 1, str(gone))
    check("...and what is not stays", (kept / (base + ".wav")).exists())
    # Over the cap: oldest first, which is what makes it a rolling window rather than a
    # random cull.
    for n in range(6):
        f = kept / f"2026010{n}-000000-bbbbb{n}.wav"
        f.write_bytes(b"y" * 300_000)
        os.utime(f, (time.time() - (10 - n) * 60,) * 2)
    cc_web._asr_keep_prune(0, 1)          # 1MB cap
    left = sorted(f.name for f in kept.iterdir() if f.suffix == ".wav")
    total = sum(f.stat().st_size for f in kept.iterdir())
    check("the directory is brought under the cap", total <= 1024 * 1024, f"{total} bytes")
    check("...by dropping the oldest, keeping the newest",
          "20260105-000000-bbbbb5.wav" in left and "20260100-000000-bbbbb0.wav" not in left,
          str(left))

    print("=== a failure to keep must never break a dictation ===")
    # It is a debug aid. If the directory is unwritable, the words still land.
    import pathlib
    real = cc_web._asr_keep_dir
    cc_web._asr_keep_dir = lambda: pathlib.Path("/proc/nonexistent-dir")
    try:
        check("an unwritable store returns empty instead of raising",
              cc_web._asr_keep_save(hdr + raw, ".wav", {}) == "")
    finally:
        cc_web._asr_keep_dir = real

    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
