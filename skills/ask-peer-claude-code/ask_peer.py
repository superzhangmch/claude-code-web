#!/usr/bin/env python3
"""ask_peer — talk to another claude-code session through the claude-code-web
bridge (cc-web). Sends a message into the peer's iTerm tab, then polls the peer
until it finishes its turn, and prints the reply as JSON.

Read side is the clean structured transcript (/api/state, brief). Idle/turn-done
comes from cc-web's own `claude_idle` + `pending_confirm`. Nothing here scrolls
or disturbs the peer beyond delivering the message.

Usage:
  ask_peer.py --to <SID> [--host IP] [--token T] [--from SID]
              [--timeout SEC] [--mode brief|medium] [--rounds N]
              [--no-send] [--no-wait] [MESSAGE]

  MESSAGE from arg or stdin (stdin avoids all shell-quoting issues — preferred
  for multi-line / code). --no-send just reads current state (peek). Every sent
  message is tagged "[⇄ from peer claude <id>]" — there is no untagged send.

Output: one JSON object:
  {status, reply, pending_confirm, idle, elapsed, since_idx, note}
  status ∈ done | pending_confirm | timeout | maybe_error | peek
"""
import argparse, json, os, re, sys, time, urllib.request, urllib.error


def _known_hosts():
    """cc-web hosts to auto-search when --host is omitted (the "sid → url" step).
    Read from $CC_WEB_HOSTS (comma-separated) or a `hosts=` line in
    ~/.claude/cc_web.conf — kept OUT of the committed code so no private IPs
    live in the repo. Returns [] if unconfigured (then only local is tried)."""
    raw = os.environ.get("CC_WEB_HOSTS", "")
    if not raw:
        try:
            for line in open(os.path.expanduser("~/.claude/cc_web.conf")):
                if line.strip().startswith("hosts="):
                    raw = line.split("=", 1)[1].strip()
                    break
        except Exception:
            pass
    return [h.strip() for h in raw.split(",") if h.strip()]


def _conf_token():
    p = os.path.expanduser("~/.claude/cc_web.conf")
    try:
        for line in open(p):
            if line.strip().startswith("token="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return os.environ.get("CC_WEB_TOKEN", "")


def _conf_name():
    """This session/machine's human-friendly peer name (user-set), for the
    message tag so recipients recognize the sender without memorizing an id.
    From $CC_WEB_NAME or a `name=` line in ~/.claude/cc_web.conf."""
    v = os.environ.get("CC_WEB_NAME", "")
    if not v:
        try:
            for line in open(os.path.expanduser("~/.claude/cc_web.conf")):
                if line.strip().startswith("name="):
                    v = line.split("=", 1)[1].strip()
                    break
        except Exception:
            pass
    return v


def _local_ip():
    for ts in ("/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale"):
        if os.path.exists(ts):
            try:
                import subprocess
                return subprocess.run([ts, "ip", "-4"], capture_output=True,
                                      text=True, timeout=4).stdout.split()[0]
            except Exception:
                pass
    return "127.0.0.1"


def _req(method, url, token, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# Only genuine transport/API failures — not words a normal reply might contain
# ("failed to compile", "错误处理", "Error:" in sample output) which caused
# false "maybe_error" flags.
_ERR_RE = re.compile(r"(API Error|overloaded|rate.?limit|ECONNRESET|Connection reset|"
                     r"internal server error|\b(529|503 Service Unavailable)\b)", re.I)

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _resolve_to(base, token, to):
    """Expand a short/prefix/substring --to into the full session id by matching
    against the target host's LIVE claude tabs. Returns (full_id, note). If `to`
    is already a full UUID, use it as-is. Unique substring match → resolve; zero
    or multiple matches → (None, reason). cc-web needs the exact full id."""
    if _UUID.match(to):
        return to, None
    try:
        tabs = _req("GET", f"{base}/api/tabs", token, timeout=20).get("tabs", [])
    except Exception as e:
        return None, f"could not list tabs to resolve '{to}': {e}"
    sids = [t.get("sid", "") for t in tabs if t.get("sid")]
    hits = [s for s in sids if s.startswith(to)]   # prefer prefix (safer than substring)
    if not hits:
        hits = [s for s in sids if to in s]        # fall back to substring
    if len(hits) == 1:
        return hits[0], f"resolved '{to}' → {hits[0]}"
    if not hits:
        return None, f"no live session id contains '{to}'. live ids: {sids}"
    return None, f"'{to}' is ambiguous — matches {hits}; use a longer id"


def _user_texts(state):
    """User-message texts from the transcript delta — used to confirm our sent
    message actually landed as a prompt in the peer's transcript (触达)."""
    out = []
    for e in state.get("transcript", []):
        if e.get("_system"):
            continue
        m = e.get("message") or {}
        if (e.get("type") or m.get("role")) != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.append(" ".join(b.get("text", "") for b in c
                                 if isinstance(b, dict) and b.get("type") == "text"))
    return out


def _assistant_text(state):
    """Concatenate assistant (non-system) text from the transcript delta."""
    out = []
    for e in state.get("transcript", []):
        if e.get("_system"):
            continue
        m = e.get("message") or {}
        if (e.get("type") or m.get("role")) != "assistant":
            continue
        c = m.get("content")
        if isinstance(c, str):
            t = c
        elif isinstance(c, list):
            t = "\n".join(b.get("text", "") for b in c
                          if isinstance(b, dict) and b.get("type") == "text")
        else:
            t = ""
        if t.strip():
            out.append(t.strip())
    return "\n\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True)
    ap.add_argument("--host", default=None)
    ap.add_argument("--token", default=None)
    ap.add_argument("--from", dest="frm", default="")
    ap.add_argument("--from-name", dest="frm_name", default=None,
                    help="human name in the tag (recipients recognize you without the id); "
                         "defaults to name= in ~/.claude/cc_web.conf or $CC_WEB_NAME")
    ap.add_argument("--timeout", type=float, default=480)
    ap.add_argument("--interval", type=float, default=3)
    ap.add_argument("--mode", default="brief")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--no-send", action="store_true")
    ap.add_argument("--no-wait", action="store_true",
                    help="fire-and-confirm: send, confirm delivery (message landed "
                         "in the peer's transcript), return WITHOUT waiting for a reply")
    ap.add_argument("--deliver-timeout", type=float, default=20,
                    help="how long to wait for delivery confirmation in --no-wait mode")
    ap.add_argument("message", nargs="?", default=None)
    a = ap.parse_args()

    token = a.token or _conf_token()

    # Host resolution (the "sid → url" step). Explicit --host wins; otherwise
    # auto-locate the session across known hosts (local first), so the caller can
    # pass just a session id without knowing which machine it's on.
    if a.host:
        candidates = [a.host]
    else:
        local = _local_ip()
        candidates = [local] + [h for h in _known_hosts() if h != local]
    host = full = None
    tried = []
    for h in candidates:
        f_id, rnote = _resolve_to(f"http://{h}:8765", token, a.to)
        if f_id:
            host, full = h, f_id
            break
        tried.append(f"{h}: {rnote}")
    if full is None:
        print(json.dumps({"status": "error",
                          "note": "session not found on any known host — " + " | ".join(tried)},
                         ensure_ascii=False))
        return
    a.to = full
    base = f"http://{host}:8765"

    def state(since=None):
        url = (f"{base}/api/state?claude_session_id={a.to}"
               f"&mode={a.mode}&rounds={a.rounds}")
        if since is not None:
            url += f"&since_idx={since}"
        return _req("GET", url, token, timeout=30)

    # baseline high-water mark BEFORE sending
    try:
        st0 = state()
    except Exception as e:
        print(json.dumps({"status": "error", "note": f"cannot reach {base}: {e}"}))
        return
    baseline = st0.get("since_idx", 0)

    if a.no_send:
        print(json.dumps({"status": "peek", "host": host, "idle": st0.get("claude_idle"),
                          "pending_confirm": st0.get("pending_confirm"),
                          "since_idx": baseline,
                          "reply": _assistant_text(st0)}, ensure_ascii=False))
        return

    msg = a.message
    if msg is None:
        msg = sys.stdin.read()
    # ALWAYS tag peer messages. There is no untagged send: the receiving session
    # must be able to tell a peer relay from a real human, and the tag is the
    # only signal. (No --raw — see SKILL.md.)
    # Tag: "[⇄ from peer claude <id8> (name)]". id and name are both OPTIONAL —
    # name is just a human-friendly label (so the user recognizes the peer and
    # can refer to it by name); when unset the tag is just the id (or bare).
    name = a.frm_name if a.frm_name is not None else _conf_name()
    who = ""
    if a.frm:
        who += f" {a.frm[:8]}"
    if name:
        who += f" ({name})"
    msg = f"[⇄ from peer claude{who}] {msg}"

    _req("POST", f"{base}/api/input", token,
         {"claude_session_id": a.to, "text": msg}, timeout=30)

    # Fire-and-confirm (e.g. delegating a task): return once the message has
    # landed in the peer's transcript — don't block for the reply. If the peer
    # is busy the prompt is queued and shows up once its current turn ends, so
    # we report peer_idle to explain a not-yet-confirmed delivery.
    if a.no_wait:
        needle = msg.strip()[:48]
        t0 = time.time()
        delivered = False
        last_idle = st0.get("claude_idle")
        while time.time() - t0 < a.deliver_timeout:
            time.sleep(a.interval)
            try:
                st = state(since=baseline)
            except Exception:
                continue
            last_idle = st.get("claude_idle")
            if any(needle in u for u in _user_texts(st)):
                delivered = True
                break
        print(json.dumps({"status": "sent", "host": host, "delivered": delivered,
                          "peer_idle": last_idle,
                          "elapsed": round(time.time() - t0, 1),
                          "note": ("message landed in the peer's transcript"
                                   if delivered else
                                   "POST accepted (text is in the peer's tab); not yet "
                                   "seen in transcript — likely queued because the peer "
                                   "is busy, it will be picked up when its turn ends")},
                         ensure_ascii=False))
        return

    t0 = time.time()
    idle_streak = 0
    seen_active = False   # have we observed the peer actually working (non-idle)?
    while time.time() - t0 < a.timeout:
        time.sleep(a.interval)
        try:
            st = state(since=baseline)
        except Exception:
            continue
        pend = st.get("pending_confirm")
        if pend:
            print(json.dumps({"status": "pending_confirm", "pending_confirm": pend,
                              "reply": _assistant_text(st), "idle": True,
                              "elapsed": round(time.time() - t0, 1),
                              "since_idx": st.get("since_idx", baseline),
                              "note": "peer is blocked on a TUI prompt/menu — this needs a "
                                      "human to resolve in the peer's tab; peer messages can't "
                                      "operate its menu"},
                             ensure_ascii=False))
            return
        reply = _assistant_text(st)
        if st.get("claude_idle") and reply:
            idle_streak += 1
            # Accept when we SAW it working then go idle (strong "turn done"
            # signal — avoids returning on a transient end_turn mid-work), or,
            # for a reply so fast we never caught it non-idle, after a longer
            # stable-idle streak as a fallback.
            if seen_active or idle_streak >= 3:
                status = "maybe_error" if _ERR_RE.search(reply[-400:]) else "done"
                note = ("looks like an API/error state — consider re-sending '继续'"
                        if status == "maybe_error" else "")
                print(json.dumps({"status": status, "host": host, "reply": reply,
                                  "idle": True, "pending_confirm": None,
                                  "elapsed": round(time.time() - t0, 1),
                                  "since_idx": st.get("since_idx", baseline),
                                  "note": note}, ensure_ascii=False))
                return
        else:
            idle_streak = 0
            if not st.get("claude_idle"):
                seen_active = True      # peer is actively working

    try:
        final_reply = _assistant_text(state(since=baseline))
    except Exception:
        final_reply = ""                # server unreachable at timeout — still emit JSON
    print(json.dumps({"status": "timeout", "reply": final_reply,
                      "idle": False, "elapsed": round(time.time() - t0, 1),
                      "since_idx": baseline,
                      "note": f"peer not idle within {a.timeout}s — still working?"},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
