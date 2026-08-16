#!/usr/bin/env python3
"""End-to-end regression test for the realtime ASR bridge (/api/asr-stream, soniox).

Runs a REAL cc_web against a FAKE soniox upstream, so it exercises the actual
websocket relay rather than a mock of it. Two behaviours are pinned, both of which
were broken in ways that silently ate the user's speech:

  1. A slow/flaky upstream handshake must not cost audio, and must not close the
     browser socket. The bridge used to connect upstream FIRST (4 attempts at
     open_timeout=20 → ~80s) and only then start reading the browser, closing it
     on failure — the client then rebuilt the socket up to 3 more times, paying
     that again each round, while the phone captured nothing. Now one reader runs
     from the first frame, buffers, and flushes in order once connected.
  2. An EMPTY text frame is the documented FINISH signal. `elif data.get("text"):`
     dropped it (empty string is falsy) — both branches skipped it, nothing was
     logged, and a client that followed the docs would press stop and wait for a
     drain that never came.

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
                await ws.send("")                          # EMPTY text frame == FINISH (bug 2)
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
    # bug 2: "" must be forwarded as the FINISH signal (soniox: an empty text frame)
    check("an EMPTY text frame is relayed as FINISH",
          any(isinstance(m, str) and m == "" for m in received[1:]),
          repr([m for m in received[1:] if isinstance(m, str)])[:60])

    print(("\nFAILED: " + ", ".join(_fails)) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
