#!/usr/bin/env python3
"""End-to-end regression test for the realtime ASR bridge (/api/asr-stream, soniox).

Runs a REAL cc_web against a FAKE soniox upstream, so it exercises the actual
websocket relay rather than a mock of it. Three scenarios, pinning behaviours that
were all broken in ways that silently ate the user's speech:

  1. A slow/flaky upstream handshake must not cost audio, and must not close the
     browser socket. The bridge used to connect upstream FIRST (4 attempts at
     open_timeout=20 → ~80s) and only then start reading the browser, closing it
     on failure — the client then rebuilt the socket up to 3 more times, paying
     that again each round, while the phone captured nothing. Now one reader runs
     from the first frame, buffers, and flushes in order once connected.
  2. Stop pressed BEFORE the upstream exists: the deferred FINISH must arrive AFTER
     the buffered audio, or the provider finalises an empty stream. Exercised with
     the EMPTY text frame — the documented FINISH spelling, which
     `elif data.get("text"):` dropped because "" is falsy (neither branch matched,
     nothing logged, and a client following the docs waited for a drain that never
     came). Scenario 1 uses the non-empty frame the browser actually sends, so both
     spellings stay pinned.
  3. An upstream that never comes up must end in an explicit error frame within the
     bridge's budget, not hang.

Scenarios 2 and 3 follow the structure of pocketchat's test_soniox_prebuffer.py
(same voice code, maintained by another session) — including its two traps: strip
the warmup silence by CONTENT (frame boundaries differ from what was sent), and give
every audio chunk a distinct byte value so a lost middle chunk can't pass.

Isolated by pointing $HOME at a throwaway dir, so it never reads or writes the
real ~/.claude (config, bindings, or the single-instance lock).

    python3 tests/test_asr_stream_e2e.py        # exit 0 = pass
    (needs the venv's python: it imports `websockets` and runs uvicorn)
"""
import asyncio
import http
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

try:
    import websockets
    from websockets.asyncio.server import serve
except Exception as e:                                    # pragma: no cover
    print("SKIP: needs the `websockets` package (use .venv/bin/python):", e)
    sys.exit(0)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UP_PORT = 8991                 # fake soniox
CC_PORT = 8992                 # cc_web under test
TOKEN = "e2e-test-token"
FRAMES = [bytes([i]) * 320 for i in range(1, 6)]          # distinguishable "audio"
REJECT_FIRST = 2               # fake upstream refuses this many handshakes

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


def _uvicorn():
    for c in (os.path.join(ROOT, ".venv/bin/uvicorn"),
              os.path.expanduser("~/claude-code-web/.venv/bin/uvicorn")):
        if os.path.exists(c):
            return c
    return shutil.which("uvicorn")


async def main():
    attempts = {"n": 0}
    received = []                                          # what the fake upstream got
    accepted = asyncio.Event()

    async def upstream(conn):
        async for msg in conn:
            received.append(msg)

    def process_request(conn, request):
        attempts["n"] += 1
        if attempts["n"] <= REJECT_FIRST:                  # force the retry path
            return conn.respond(http.HTTPStatus.SERVICE_UNAVAILABLE, "nope\n")
        accepted.set()
        return None

    uv = _uvicorn()
    if not uv:
        print("SKIP: no uvicorn found"); return 0

    home = tempfile.mkdtemp(prefix="ccweb-e2e-")
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    with open(os.path.join(home, ".claude", "cc_web.conf"), "w") as f:
        f.write(f"token={TOKEN}\nsoniox=ws://127.0.0.1:{UP_PORT}|fake-key|Fake\n")

    async with serve(upstream, "127.0.0.1", UP_PORT, process_request=process_request):
        srv = subprocess.Popen(
            [uv, "cc_web:app", "--host", "127.0.0.1", "--port", str(CC_PORT), "--log-level", "warning"],
            cwd=ROOT, env=dict(os.environ, HOME=home),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        url = (f"ws://127.0.0.1:{CC_PORT}/api/asr-stream"
               f"?token={TOKEN}&provider=soniox&rate=24000")
        try:
            for _ in range(60):                            # wait for the server to bind
                await asyncio.sleep(0.5)
                try:
                    async with websockets.connect(url):
                        break
                except Exception:
                    continue
            else:
                print("FAIL: cc_web never came up"); _fails.append("startup"); return 1

            attempts["n"] = 0; received.clear(); accepted.clear()
            statuses = []
            t0 = time.monotonic()
            async with websockets.connect(url) as ws:
                for fr in FRAMES:                          # speak while it is still failing
                    await ws.send(fr)
                    await asyncio.sleep(0.05)
                try:                                       # progress reports, not a close
                    while not accepted.is_set():
                        statuses.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=8)))
                except Exception:
                    pass
                await asyncio.wait_for(accepted.wait(), timeout=20)
                await asyncio.sleep(0.6)
                # What the real browser sends. Scenario 2 covers the EMPTY frame, so a
                # regression to `if data.get("text"):` fails there and not here — both
                # spellings stay pinned.
                await ws.send(json.dumps({"type": "finish"}))
                await asyncio.sleep(0.8)
                still_open = ws.state.name == "OPEN"
            elapsed = time.monotonic() - t0
        finally:
            srv.terminate()
            try: srv.wait(timeout=10)
            except Exception: srv.kill()
            shutil.rmtree(home, ignore_errors=True)

    # ---- what the browser saw ----
    check("browser socket survives a failing upstream handshake", still_open)
    check("bridge reports progress instead of disconnecting",
          any(s.get("state") == "upstream-connecting" for s in statuses),
          ",".join(s.get("state", "?") for s in statuses))
    check("bridge announces the upstream once it is live",
          any(s.get("state") == "upstream-ready" for s in statuses))
    check("retried the handshake rather than giving up", attempts["n"] > REJECT_FIRST,
          f"attempts={attempts['n']}")
    check("connect window stayed short", elapsed < 25, f"{elapsed:.1f}s")

    # ---- what the upstream got ----
    if not received:
        check("upstream received anything", False)
        return 1
    conf = json.loads(received[0]) if isinstance(received[0], str) else None
    check("first upstream message is the config JSON", bool(conf and conf.get("model")))
    binaries = [m for m in received[1:] if isinstance(m, (bytes, bytearray))]
    warm = binaries[0] if binaries else b""
    check("warmup silence precedes the audio", len(warm) == 24000 * 2 and set(warm) == {0},
          f"{len(warm)}B")
    audio = b"".join(binaries[1:])
    expect = b"".join(FRAMES)
    check("every byte captured while connecting arrives, in order", audio == expect,
          f"{len(audio)}/{len(expect)}B")
    check("a non-empty finish frame is relayed as FINISH (what the browser sends)",
          any(isinstance(m, str) and m == "" for m in received[1:]),
          repr([m for m in received[1:] if isinstance(m, str)])[:60])

    return 1 if _fails else 0




# --- scenario 2: stop pressed BEFORE the upstream exists -----------------------
# The deferred FINISH (finish_pending) must fire AFTER the buffered audio is
# flushed, or the provider finalises an empty stream. Uses the EMPTY text frame,
# which is the documented spelling and the one bug 2 dropped.
async def scenario_stop_while_connecting(uv):
    received, accepted = [], asyncio.Event()

    async def upstream(conn):
        async for msg in conn:
            received.append(msg)

    def process_request(conn, request):
        accepted.set()
        return None

    home = tempfile.mkdtemp(prefix="ccweb-e2e2-")
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    with open(os.path.join(home, ".claude", "cc_web.conf"), "w") as f:
        f.write(f"token={TOKEN}\nsoniox=ws://127.0.0.1:{UP_PORT + 1}|fake-key|Fake\n")
    url = (f"ws://127.0.0.1:{CC_PORT + 1}/api/asr-stream"
           f"?token={TOKEN}&provider=soniox&rate=24000")
    srv = subprocess.Popen(
        [uv, "cc_web:app", "--host", "127.0.0.1", "--port", str(CC_PORT + 1), "--log-level", "warning"],
        cwd=ROOT, env=dict(os.environ, HOME=home),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    chunks = [bytes([0x11]) * 3200, bytes([0x22]) * 3200]
    try:
        for _ in range(60):
            await asyncio.sleep(0.5)
            try:
                async with websockets.connect(url):
                    break
            except Exception:
                continue
        async with websockets.connect(url, max_size=None) as ws:
            async def drain():
                try:
                    async for _ in ws:
                        pass
                except Exception:
                    pass
            d = asyncio.create_task(drain())
            for ch in chunks:
                await ws.send(ch)
            await ws.send("")                      # EMPTY text frame == stop (bug 2)
            await asyncio.sleep(0.5)
            async with serve(upstream, "127.0.0.1", UP_PORT + 1,
                             process_request=process_request):   # upstream shows up late
                await asyncio.sleep(4.0)
            d.cancel()
    finally:
        srv.terminate()
        try: srv.wait(timeout=10)
        except Exception: srv.kill()
        shutil.rmtree(home, ignore_errors=True)

    bins = [m for m in received[1:] if isinstance(m, (bytes, bytearray))]
    # skip the warmup silence: judge by content, not length — the server's frame
    # boundaries differ from what was sent.
    audio, skipping = b"", True
    for c in bins:
        if skipping and set(c) == {0}:
            continue
        skipping = False
        audio += bytes(c)
    check("audio captured before the stop still reaches the upstream",
          audio == b"".join(chunks), f"{len(audio)}/{len(b''.join(chunks))}B")
    check("an EMPTY text frame is relayed as FINISH, after the flush",
          any(isinstance(m, str) and m == "" for m in received[1:]),
          repr([m for m in received[1:] if isinstance(m, str)])[:40])


# --- scenario 3: upstream never comes up --------------------------------------
# Must end with an explicit error frame within the bridge's budget, not hang.
async def scenario_never_connects(uv):
    home = tempfile.mkdtemp(prefix="ccweb-e2e3-")
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    with open(os.path.join(home, ".claude", "cc_web.conf"), "w") as f:
        f.write(f"token={TOKEN}\nsoniox=ws://127.0.0.1:{UP_PORT + 2}|fake-key|Fake\n")
    url = (f"ws://127.0.0.1:{CC_PORT + 2}/api/asr-stream"
           f"?token={TOKEN}&provider=soniox&rate=24000")
    srv = subprocess.Popen(
        [uv, "cc_web:app", "--host", "127.0.0.1", "--port", str(CC_PORT + 2), "--log-level", "warning"],
        cwd=ROOT, env=dict(os.environ, HOME=home),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    err, dt = None, 0.0
    try:
        for _ in range(60):
            await asyncio.sleep(0.5)
            try:
                async with websockets.connect(url):
                    break
            except Exception:
                continue
        t0 = time.monotonic()
        async with websockets.connect(url, max_size=None) as ws:
            await ws.send(bytes([0x33]) * 1600)
            try:
                while True:
                    d = json.loads(await asyncio.wait_for(ws.recv(), timeout=45))
                    if d.get("type") == "error":
                        err = d; break
            except Exception:
                pass
        dt = time.monotonic() - t0
    finally:
        srv.terminate()
        try: srv.wait(timeout=10)
        except Exception: srv.kill()
        shutil.rmtree(home, ignore_errors=True)
    check("a dead upstream ends in an explicit error frame", err is not None, str(err)[:60])
    check("giving up is bounded, not a hang", 0 < dt < 40, f"{dt:.1f}s")


async def run_all():
    rc = await main()
    uv = _uvicorn()
    if uv:
        await scenario_stop_while_connecting(uv)
        await scenario_never_connects(uv)
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run_all()))
