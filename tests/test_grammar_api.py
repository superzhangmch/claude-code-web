#!/usr/bin/env python3
"""Backend regression tests for /api/grammar — the ✎ English-correction endpoint.

Every reply must carry a `status`, because an empty `correction` used to mean three
different things and the UI showed all of them as "looks natural — no changes":
the model approved the text, the LLM was never configured, or the call blew up.
That is a pass mark on text nobody looked at.

Runs a REAL cc_web against a FAKE OpenAI-compatible endpoint (so the LLM branch is
exercised, not mocked), with $HOME pointed at a throwaway dir so the real
~/.claude — config, bindings, the single-instance lock — is never touched.

    .venv/bin/python tests/test_grammar_api.py      # exit 0 = pass

Assertion set adapted from pocketchat's test_grammar_status.py (same code, other
session), including its check that the api_key and api_base never leak into a
response.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "grammar-test-token"
SECRET_KEY = "sk-do-not-leak-me-0123456789"
LLM_PORT = 8993
CC_PORT = 8994

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


# --- fake OpenAI-compatible endpoint -----------------------------------------
# `mode` decides what it does, so one server covers the happy path, a 500, and a
# reply the parser can't use.
STATE = {"mode": "two-line"}


class FakeLLM(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        mode = STATE["mode"]
        if mode == "http500":
            self.send_response(500); self.end_headers(); self.wfile.write(b"boom"); return
        if mode == "two-line":
            content = "CORRECTION: I went to the store yesterday.\nNATIVE: I popped to the shop yesterday."
        elif mode == "ok-sentinel":
            content = "CORRECTION: OK\nNATIVE: Sounds fine already."
        else:
            content = ""
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                     # /v1/models, used by doctor.py-style probes
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def _uvicorn():
    for c in (os.path.join(ROOT, ".venv/bin/uvicorn"),
              os.path.expanduser("~/claude-code-web/.venv/bin/uvicorn")):
        if os.path.exists(c):
            return c
    return shutil.which("uvicorn")


def start_cc(conf_extra, port):
    home = tempfile.mkdtemp(prefix="ccweb-gram-")
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    with open(os.path.join(home, ".claude", "cc_web.conf"), "w") as f:
        f.write(f"token={TOKEN}\n" + conf_extra)
    srv = subprocess.Popen(
        [_uvicorn(), "cc_web:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=dict(os.environ, HOME=home),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(60):
        time.sleep(0.4)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/auth-status", timeout=2).read()
            return srv, home
        except Exception:
            continue
    return srv, home


def call(port, text, manual=True, timeout=40):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/grammar",
        data=json.dumps({"text": text, "manual": manual}).encode(),
        headers={"content-type": "application/json", "authorization": "Bearer " + TOKEN},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main():
    if not _uvicorn():
        print("SKIP: no uvicorn found"); return 0

    httpd = ThreadingHTTPServer(("127.0.0.1", LLM_PORT), FakeLLM)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    # ---- LLM configured and answering -------------------------------------
    srv, home = start_cc(f"api_base=http://127.0.0.1:{LLM_PORT}\n"
                         f"api_key={SECRET_KEY}\nmodel=fake-model\n", CC_PORT)
    try:
        STATE["mode"] = "two-line"
        d = call(CC_PORT, "I has went to the store yesterday.")
        check("a real correction comes back as status ok",
              d.get("status") == "ok" and "went to the store" in (d.get("correction") or ""), str(d)[:70])
        check("the native version is parsed out too", "popped" in (d.get("native") or ""), str(d.get("native"))[:40])

        STATE["mode"] = "ok-sentinel"
        d = call(CC_PORT, "This sentence is already fine.")
        check("the OK sentinel means status ok with an empty correction",
              d.get("status") == "ok" and d.get("correction") == "", str(d)[:70])

        d = call(CC_PORT, "   ")
        check("blank input → status empty", d.get("status") == "empty", str(d)[:50])

        STATE["mode"] = "http500"
        d = call(CC_PORT, "anything at all")
        check("an upstream 500 → status error, never a silent pass",
              d.get("status") == "error", str(d)[:70])
        check("the error is a bare exception type, not a message",
              bool(d.get("error")) and len(str(d.get("error"))) < 40, repr(d.get("error")))

        blob = json.dumps(d) + json.dumps(call(CC_PORT, "another one"))
        check("the api_key never appears in a response", SECRET_KEY not in blob)
        check("the api_base never appears in a response", f"127.0.0.1:{LLM_PORT}" not in blob)
    finally:
        srv.terminate()
        try: srv.wait(timeout=10)
        except Exception: srv.kill()
        shutil.rmtree(home, ignore_errors=True)

    # ---- unreachable LLM --------------------------------------------------
    srv, home = start_cc("api_base=http://127.0.0.1:1\napi_key=x\nmodel=fake-model\n", CC_PORT + 1)
    try:
        d = call(CC_PORT + 1, "connection refused please")
        check("an unreachable endpoint → status error", d.get("status") == "error", str(d)[:70])
    finally:
        srv.terminate()
        try: srv.wait(timeout=10)
        except Exception: srv.kill()
        shutil.rmtree(home, ignore_errors=True)

    # ---- no LLM configured at all ----------------------------------------
    srv, home = start_cc("", CC_PORT + 2)
    try:
        d = call(CC_PORT + 2, "nothing is configured")
        check("no api_base/model → status disabled (NOT a pass mark)",
              d.get("status") == "disabled", str(d)[:70])
        check("and it still answers 200 so sending is never blocked", d.get("correction") == "")
    finally:
        srv.terminate()
        try: srv.wait(timeout=10)
        except Exception: srv.kill()
        shutil.rmtree(home, ignore_errors=True)
        httpd.shutdown()

    print(("\nFAILED: " + ", ".join(_fails)) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
