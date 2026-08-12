#!/usr/bin/env python3
"""Pre-flight check for a claude-code-web deployment.

Run it BEFORE wondering why something is broken:

    .venv/bin/python doctor.py          # use the venv's python so deps are checked
    python3 doctor.py                   # still useful; skips the dependency check

Every failure mode listed here has cost someone real time, because the symptom
shows up far from the cause: a missing `token=` looks like "I typed the password
wrong", a missing `api_base` looked like "the LLM says my English is fine", a
blocked CDN looked like "the app renders raw markdown", no HTTPS looks like "the
mic button is broken", and a stale second instance looks like a mystery crash.

Prints one line per check: OK / WARN / FAIL, and for anything not-OK the exact
thing to change. Exit code 1 if any FAIL. Never prints secrets.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

HOME = Path.home()
CONF = HOME / ".claude" / "cc_web.conf"
LOCK = HOME / ".claude" / "cc_web.lock"
HERE = Path(__file__).resolve().parent
IS_MAC = platform.system() == "Darwin"

_fails = 0
_warns = 0


def ok(label, detail=""):
    print(f"  \033[32mOK\033[0m   {label}" + (f" — {detail}" if detail else ""))


def warn(label, detail="", fix=""):
    global _warns
    _warns += 1
    print(f"  \033[33mWARN\033[0m {label}" + (f" — {detail}" if detail else ""))
    if fix:
        print(f"       fix: {fix}")


def fail(label, detail="", fix=""):
    global _fails
    _fails += 1
    print(f"  \033[31mFAIL\033[0m {label}" + (f" — {detail}" if detail else ""))
    if fix:
        print(f"       fix: {fix}")


def section(title):
    print(f"\n{title}")


def conf_lines() -> list[str]:
    try:
        return CONF.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def conf_get(key: str) -> list[str]:
    """All values for `key=` (the file allows repeats, e.g. cwd= / asr=)."""
    out = []
    for line in conf_lines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == key:
            out.append(v.strip())
    return out


# ---------------------------------------------------------------- python + deps
def check_python():
    section("python / dependencies")
    v = sys.version_info
    if v < (3, 10):
        fail("python version", f"{v.major}.{v.minor}", "the code uses 3.10+ syntax; 3.11 is what it's developed on")
    else:
        ok("python version", f"{v.major}.{v.minor}.{v.micro}")

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    need = ["fastapi", "uvicorn", "pydantic"]
    need += ["iterm2"] if IS_MAC else []
    missing = []
    for mod in need:
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    if not in_venv:
        warn("running outside the venv", "dependency check is meaningless here",
             "re-run as .venv/bin/python doctor.py")
    elif missing:
        req = "requirements.txt" if IS_MAC else "requirements-linux.txt"
        fail("missing packages", ", ".join(missing), f".venv/bin/pip install -r {req}")
    else:
        ok("packages importable", ", ".join(need))


# ------------------------------------------------------------------------- conf
def check_conf():
    section(f"config ({CONF})")
    if not CONF.exists():
        fail("config file missing", str(CONF),
             f"mkdir -p ~/.claude && cp {HERE}/config.example/cc_web.conf {CONF} && chmod 600 {CONF}")
        return
    ok("config file exists")

    mode = CONF.stat().st_mode & 0o777
    if mode & 0o077:
        warn("config is group/world readable", oct(mode), f"chmod 600 {CONF}")

    tok = [t for t in conf_get("token") if t]
    if os.environ.get("CC_WEB_TOKEN"):
        ok("auth token", "taken from $CC_WEB_TOKEN (overrides the file)")
    elif not tok:
        fail("no token=", "the server will invent a random one at startup",
             "nothing you type on the login page can match it, and it changes on every "
             f"restart. Set token=<something unguessable> in {CONF}")
    elif tok[0].startswith("YOUR_"):
        fail("token= is still the template value", tok[0][:9] + "…",
             "replace it with a real secret")
    else:
        ok("auth token", f"set ({len(tok[0])} chars)")

    api_base = next((v for v in conf_get("api_base") if v), "")
    model = next((v for v in conf_get("model") if v), "")
    if not api_base or not model:
        warn("no LLM configured (api_base / model)",
             "voice-input polish returns 503 and the ✎ correction reports 'no LLM configured'",
             f"set api_base / api_key / model in {CONF} (any OpenAI-compatible endpoint)")
    else:
        ok("LLM endpoint", f"{api_base} model={model}")
        try:
            req = urllib.request.Request(api_base.rstrip("/") + "/v1/models")
            key = next((v for v in conf_get("api_key") if v), "")
            if key:
                req.add_header("authorization", "Bearer " + key)
            with urllib.request.urlopen(req, timeout=6) as r:
                ok("LLM endpoint reachable", f"HTTP {r.status}")
        except Exception as e:
            warn("LLM endpoint unreachable", type(e).__name__,
                 "polish + ✎ will report a failure (they no longer pretend to succeed)")

    asr = conf_get("asr")
    rt = [k for k in ("soniox", "openai_realtime") if any(conf_get(k))]
    if not asr and not rt:
        warn("no voice backend (asr= / soniox= / openai_realtime=)",
             "the 🎤 button stays hidden; the ⚙ menu says 'not configured yet'",
             f"see the voice sections of {HERE}/config.example/cc_web.conf")
    else:
        ok("voice backends", f"batch={len(asr)} realtime={','.join(rt) or 'none'}")

    cwds = [c for c in conf_get("cwd") if c]
    missing_cwd = [c for c in cwds if not Path(c).expanduser().is_dir()]
    if not cwds:
        warn("no cwd= lines", "the 'New session' dialog will have nothing to offer")
    elif missing_cwd:
        warn("cwd= points at missing dirs", ", ".join(missing_cwd[:3]))
    else:
        ok("new-session dirs", f"{len(cwds)} configured")


# -------------------------------------------------------------------- terminal
def check_terminal():
    section("terminal bridge")
    if IS_MAC:
        try:
            import iterm2  # noqa: F401
            ok("iterm2 python module", "importable")
        except Exception:
            fail("iterm2 module missing", "", ".venv/bin/pip install -r requirements.txt")
        print("       note: iTerm2 → Settings → General → Magic → Enable Python API, then restart iTerm2.")
        print("       note: the FIRST connection pops 'Allow this script to control iTerm?'. Under launchd")
        print("             nobody clicks it and startup hangs — start it once from an iTerm tab and accept.")
    else:
        if not shutil.which("tmux"):
            fail("tmux not installed", "the Linux bridge drives claude through tmux",
                 "install tmux, then run your claude sessions inside it")
            return
        ok("tmux installed", shutil.which("tmux"))
        try:
            out = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_current_command}"],
                                 capture_output=True, text=True, timeout=6).stdout
            n = sum(1 for l in out.splitlines() if "claude" in l)
            if n:
                ok("claude sessions visible", f"{n} pane(s) running claude")
            else:
                warn("no claude pane found", "the picker will be empty",
                     "claude must run INSIDE tmux — a claude in a bare terminal is invisible to the bridge")
        except Exception as e:
            warn("could not list tmux panes", type(e).__name__)


# ------------------------------------------------------------------------- TLS
def _cert_days_left(path: Path):
    try:
        out = subprocess.run(["openssl", "x509", "-in", str(path), "-noout", "-enddate"],
                             capture_output=True, text=True, timeout=6).stdout
        return out.strip().split("=", 1)[1]
    except Exception:
        return None


def check_tls():
    section("HTTPS (required for 🎤 voice input — browsers need a secure context)")
    ts = shutil.which("tailscale") or next(
        (p for p in ("/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale", "/usr/bin/tailscale",
                     "/Applications/Tailscale.app/Contents/MacOS/Tailscale") if Path(p).exists()), None)
    if not ts:
        warn("tailscale CLI not found", "", "not required, but it's the easiest way to get a trusted cert")
    else:
        ok("tailscale CLI", ts)
        try:
            st = json.loads(subprocess.run([ts, "status", "--json"], capture_output=True,
                                           text=True, timeout=8).stdout or "{}")
            name = ((st.get("Self") or {}).get("DNSName") or "").rstrip(".")
            if name:
                ok("tailnet DNS name", name + "  (use THIS in the URL, not the 100.x IP — the cert is for the name)")
            certs = st.get("CertDomains") or []
            if not certs:
                warn("tailnet has no CertDomains", "HTTPS certs look disabled for this tailnet",
                     "enable MagicDNS + HTTPS Certificates in the Tailscale admin console")
        except Exception as e:
            warn("could not read tailscale status", type(e).__name__)
        # Can this user run `tailscale cert` at all? On Linux it needs root unless
        # the operator grant was made — the step that costs an afternoon, because
        # no cert → no HTTPS → the mic "just doesn't work". Checked read-only via
        # `debug prefs`; actually invoking `cert` would hit the control plane.
        # On macOS the Tailscale app already runs as you, so no grant is needed.
        try:
            prefs = json.loads(subprocess.run([ts, "debug", "prefs"], capture_output=True,
                                              text=True, timeout=8).stdout or "{}")
            oper = prefs.get("OperatorUser") or ""
            me = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
            if IS_MAC:
                ok("`tailscale cert` permission", "macOS app runs as you — no operator grant needed")
            elif oper and oper == me:
                ok("`tailscale cert` permission", f"operator = {oper}")
            else:
                warn("`tailscale cert` will need root",
                     f"OperatorUser={oper or 'unset'}, you are {me or '?'}",
                     "run ONCE in a real terminal: sudo tailscale set --operator=$USER  "
                     "(sudo cannot prompt for a password from a non-interactive shell)")
        except Exception:
            pass

    d = HOME / "cc_https"
    crt, key = d / "tls.crt", d / "tls.key"
    if crt.exists() and key.exists():
        end = _cert_days_left(crt)
        ok("cert files present", f"{d}  expires {end or '?'}")
    else:
        warn("no cert at ~/cc_https/tls.{crt,key}", "so the server can only serve plain HTTP",
             "voice input needs HTTPS; see the README's HTTPS section")


# ----------------------------------------------------------------- static files
def check_static():
    section("static assets")
    for rel, why in (("static/index.html", "the whole UI"),
                     ("static/vendor/marked.min.js", "markdown rendering (vendored so a blocked CDN can't break it)"),
                     ("static/favicon.svg", "browser tab icon"),
                     ("static/manifest.webmanifest", "PWA install")):
        p = HERE / rel
        if p.exists() and p.stat().st_size:
            ok(rel, f"{p.stat().st_size}B")
        else:
            (fail if rel.endswith(("index.html", "marked.min.js")) else warn)(
                f"{rel} missing", why, "re-deploy this file; a partial copy of static/ breaks the UI")


# -------------------------------------------------------------------- instances
def check_instances():
    section("running instance")
    try:
        out = subprocess.run(["ps", "-Ao", "pid=,args="], capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return
    pids = []
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 4 and f[2].endswith("/uvicorn") and f[3] == "cc_web:app":
            pids.append(f[0])
    if not pids:
        ok("no cc_web running", "nothing holds ~/.claude/cc_web.lock")
    elif len(pids) == 1:
        ok("one cc_web running", f"pid {pids[0]}")
    else:
        fail("several cc_web processes", " ".join(pids),
             "they share every file under ~/.claude and clobber each other. Since the flock guard "
             "landed, the 2nd one exits with code 3 — if you see several, one predates the guard "
             "or CC_WEB_ALLOW_MULTI=1 is set")
    if LOCK.exists():
        try:
            print(f"       lock: {LOCK.read_text(encoding='utf-8').strip()[:110]}")
        except OSError:
            pass


def main():
    print(f"claude-code-web doctor — {platform.system()} {platform.release()}  repo={HERE}")
    check_python()
    check_conf()
    check_terminal()
    check_tls()
    check_static()
    check_instances()
    print(f"\n{_fails} FAIL, {_warns} WARN")
    if _fails:
        print("FAIL means it will not work as intended. WARN means a feature is off or degraded.")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
