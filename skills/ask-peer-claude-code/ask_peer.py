#!/usr/bin/env python3
"""ask_peer — talk to another claude-code session through the claude-code-web
bridge (cc-web). Sends a message into the peer's iTerm tab, then polls the peer
until it finishes its turn, and prints the reply as JSON.

Read side is the clean structured transcript (/api/state, brief). Idle/turn-done
comes from cc-web's own `claude_idle` + `pending_confirm`. Nothing here scrolls
or disturbs the peer beyond delivering the message.

Usage:
  ask_peer.py --to <SID> [--host IP] [--token T]
              [--timeout SEC] [--mode brief|medium] [--rounds N]
              [--no-send] [--no-wait] [--history [--before IDX]] [--screen]
              [MESSAGE]

  MESSAGE from arg or stdin (stdin avoids all shell-quoting issues — preferred
  for multi-line / code). Every sent message is tagged "[⇄ from peer claude
  <id>]" — there is no untagged send.

Read-only modes (no message sent):
  --no-send   peek: current idle/pending state + recent assistant text + a
              tool-activity line ("Bash[…] · Read[…]", same as the web brief).
  --history   dump the last --rounds rounds (brief: text + tool activity); page
              further back with --before <earliest_idx> (reuses /api/state
              before_idx paging — bounded per call, concurrency-safe).
  --screen    the peer's current TUI screen snapshot (refresh=false, non-
              intrusive; current view only, not history).

Output: one JSON object. status ∈
  done | pending_confirm | timeout | maybe_error | peek | sent | history | screen
  (done/peek also carry `activity`.)
"""
import argparse, json, os, re, subprocess, sys, time, urllib.request, urllib.error


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


_LABEL_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+/-]{0,23}$")


def _short_label(name: str) -> str:
    """A name fit to sit in the tag, or "".

    Only applied to the AUTO-derived name, and only accepts a short ASCII-ish string —
    the kind of thing a tab is actually called (`cc-web`, `new_reader`, `gen-spark-0d`).
    A long or CJK title is dropped rather than shortened: the tag's job is to say who is
    speaking in a couple of words, and cc-web's other name for a session is an
    LLM-written sentence ("cc-web多机部署完善与静默失败修复") that would swamp the line.
    A name the CALLER passed with --from-name is never filtered — if the user gave this
    session a working name, that is the name, whatever alphabet it is in.
    """
    # Strip the status glyph / process suffix a terminal tab title carries ("✳ cc-web",
    # "cc-web (claude)") before judging it — cc-web's own UI does the same. Dropping such
    # a name for its decoration would throw away a perfectly good label.
    n = re.sub(r"^[\s\u2800-\u28ff✳✻✽✢✣✱●○◍•·]+", "", (name or "").strip())
    n = re.sub(r"\s*\((?:claude|caffeinate)\)\s*$", "", n).strip()
    return n if _LABEL_OK.match(n) else ""


def _own_session() -> tuple:
    """(sid, name) for THIS session, found the way the my-session-id skill finds it: walk
    up the process tree until a ~/.claude/sessions/<pid>.json turns up, and read it.

    Auto-detected rather than passed in. `--from` used to be optional and a forgotten one
    produced a tag with no id at all — a message the peer physically cannot reply to. The
    readable name comes from the same file (claude keeps it there, e.g. "cc-web"), which
    is why it costs nothing: one read, both fields. Before this, the name could only come
    from `name=` in cc_web.conf — a MACHINE name, present on one host and absent on
    another, so most tags carried no readable identity at all. What the peer wants to know
    is which session is talking, not which laptop.
    """
    pid = os.getpid()
    for _ in range(12):
        f = os.path.expanduser(f"~/.claude/sessions/{pid}.json")
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh) or {}
            sid = d.get("sessionId") or ""
            if sid:
                return sid, (d.get("name") or "").strip()
        except (OSError, ValueError):
            pass
        try:
            out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=3).stdout.strip()
            nxt = int(out)
        except Exception:
            return "", ""
        if nxt <= 1 or nxt == pid:
            return "", ""
        pid = nxt
    return "", ""


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


def _ts_cli():
    """The tailscale CLI. Includes the Linux locations — it used to look only in
    the two Homebrew/macOS paths, so on Linux _local_ip() silently fell through to
    127.0.0.1 and every lookup went to a host cc-web isn't even bound to."""
    import shutil
    p = shutil.which("tailscale")
    if p:
        return p
    for c in ("/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale",
              "/usr/bin/tailscale", "/usr/sbin/tailscale",
              "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        if os.path.exists(c):
            return c
    return None


def _ts_status():
    ts = _ts_cli()
    if not ts:
        return {}
    try:
        import subprocess
        out = subprocess.run([ts, "status", "--json"], capture_output=True,
                             text=True, timeout=6).stdout
        return json.loads(out) if out else {}
    except Exception:
        return {}


def _local_ip():
    ts = _ts_cli()
    if ts:
        try:
            import subprocess
            return subprocess.run([ts, "ip", "-4"], capture_output=True,
                                  text=True, timeout=4).stdout.split()[0]
        except Exception:
            pass
    return "127.0.0.1"


_IPISH = re.compile(r"^[\d.]+$|^[0-9a-fA-F:]+$")


def _ts_name_for(ip):
    """tailnet DNS name of a tailscale IP, or None. Needed because the TLS cert is
    issued for the *.ts.net NAME — https://<ip>:8443 fails hostname verification."""
    st = _ts_status()
    nodes = [st.get("Self") or {}] + list((st.get("Peer") or {}).values())
    for n in nodes:
        if ip in (n.get("TailscaleIPs") or []):
            return (n.get("DNSName") or "").rstrip(".") or None
    return None


def _bases(host):
    """Base URLs to try for a host, best first. cc-web is HTTPS-only now (browser
    mic capture needs a secure context), so :8443 comes first; plain :8765 is kept
    as a fallback for any instance still on HTTP. For the https candidate a bare IP
    is swapped for its tailnet DNS name so the cert validates."""
    out = []
    name = _ts_name_for(host) if _IPISH.match(host or "") else host
    if name:
        out.append(f"https://{name}:8443")
    out.append(f"http://{host}:8765")
    return out


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
    is already a full UUID, verify it lives on THIS host. Unique substring match →
    resolve; zero or multiple matches → (None, reason). cc-web needs the exact id."""
    try:
        tabs = _req("GET", f"{base}/api/tabs", token, timeout=20).get("tabs", [])
    except Exception as e:
        return None, f"could not list tabs to resolve '{to}': {e}"
    sids = [t.get("sid", "") for t in tabs if t.get("sid")]
    if _UUID.match(to):
        # A full id used to be accepted WITHOUT checking this host — so with
        # auto-search the first candidate (local) always won and every later call
        # came back 409 "session not bound" even when the session was on another
        # machine. Verify, so the search can move on to the next host.
        return (to, None) if to in sids else (None, f"session {to[:8]} not live on this host")
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
    """Concatenate assistant (non-system) text from the transcript delta, OLDEST FIRST.

    Two things about that ordering, both of which have already bitten:

    1. **Only pass it a delta.** On a `--rounds` window it returns several unrelated
       answers glued together and the caller reads the lot as "the reply". That is what a
       peek used to do — 3.0KB of a 3.4KB response, most of it older than the question
       being asked (fixed: peeks use _last_assistant_text). The three remaining callers
       all read `state(since=baseline)`, where baseline was taken BEFORE our message went
       out, so everything in it was produced in answer to us.
    2. **Never clamp this from the end.** Oldest-first means the tail is the NEWEST text,
       so `reply[:N]` keeps the least relevant part and drops the answer. (`_ERR_RE` is
       deliberately matched against `reply[-400:]` for the same reason — the error, if
       any, is at the new end.) If you want less text, take the last entry, don't cut the
       string.
    """
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


def _last_assistant_text(state):
    """Just the peer's MOST RECENT answer.

    What a peek is for is "what is it doing / what did it just say", and
    _assistant_text() concatenates every answer in the --rounds window, oldest first —
    3.0KB of the 3.4KB a default peek returned, most of it older than the question being
    asked. Worse, oldest-first means a naive length clamp would cut the NEWEST text.
    Reading several rounds back already has its own mode (--history, with paging), so a
    peek duplicating it bought nothing and cost the most on the slowest link.
    """
    last = ""
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
            last = t.strip()
    return last


def _entry_text(e):
    c = (e.get("message") or {}).get("content")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text").strip()
    return ""


def _tool_calls(e):
    """['Bash[desc]', 'Read[path]', …] for the tool_use blocks in an assistant
    entry. Reuses the name + one-line `desc` cc-web's brief view already computes
    server-side (_trim_brief/_tool_summary) — same thing the web shows."""
    out = []
    c = (e.get("message") or {}).get("content")
    if isinstance(c, list):
        for b in c:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                nm = b.get("name") or "?"
                desc = b.get("desc")
                out.append(f"{nm}[{desc}]" if desc else nm)
    return out


def _activity(state):
    """Compact tool-activity line across the assistant turns in the window —
    'Bash[…] · Read[…]' — mirroring the web brief view."""
    calls = []
    for e in state.get("transcript", []):
        if e.get("_system"):
            continue
        if (e.get("type") or (e.get("message") or {}).get("role")) != "assistant":
            continue
        calls.extend(_tool_calls(e))
    return " · ".join(calls)


def _render_transcript(state):
    """Human-readable brief transcript for --history: interleaved
    [human]/[claude] text + a '· Tool[…]' line per assistant turn. Skips the
    'Queued' enqueue placeholders (their delivery renders separately) so there's
    no double; keeps _qcmd (a delivered queued msg) as a normal human turn."""
    lines = []
    for e in state.get("transcript", []):
        if e.get("_queued"):
            continue
        role = e.get("type") or (e.get("message") or {}).get("role")
        if role not in ("user", "assistant"):
            continue
        text = _entry_text(e)
        if role == "user":
            label = "sys" if e.get("_system") else "human"
            if text:
                lines.append(f"[{label}] {text}")
        else:
            if text:
                lines.append(f"[claude] {text}")
            tools = _tool_calls(e)
            if tools:
                lines.append("  · " + " · ".join(tools))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True)
    ap.add_argument("--host", default=None)
    ap.add_argument("--token", default=None)
    ap.add_argument("--from", dest="frm", default="",
                    help="my own session id. Normally OMITTED — it is detected from the "
                         "process tree; pass it only to override, e.g. when running "
                         "outside a claude session.")
    ap.add_argument("--from-name", dest="frm_name", default=None,
                    help="human name in the tag (recipients recognize you without the id); "
                         "defaults to name= in ~/.claude/cc_web.conf or $CC_WEB_NAME")
    ap.add_argument("--timeout", type=float, default=480)
    ap.add_argument("--interval", type=float, default=3)
    ap.add_argument("--mode", default="brief")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--history", action="store_true",
                    help="read-only: dump the last --rounds rounds of the peer's transcript "
                         "(brief: text + tool activity). Page further back with --before <idx>.")
    ap.add_argument("--before", type=int, default=None,
                    help="with --history: fetch the rounds just BEFORE this _idx (load-earlier); "
                         "reuses /api/state's before_idx paging (same as the web).")
    ap.add_argument("--screen", action="store_true",
                    help="read-only: return the peer's CURRENT TUI screen snapshot "
                         "(refresh=false → non-intrusive). Current view only, not history.")
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
    host = full = base = None
    tried = []
    for h in candidates:
        for b in _bases(h):           # https://<name>:8443 first, then http://<h>:8765
            f_id, rnote = _resolve_to(b, token, a.to)
            if f_id:
                host, full, base = h, f_id, b
                break
            tried.append(f"{b}: {rnote}")
        if full:
            break
    if full is None:
        print(json.dumps({"status": "error",
                          "note": "session not found on any known host — " + " | ".join(tried)},
                         ensure_ascii=False))
        return
    a.to = full

    # --screen: current TUI screen snapshot (read-only, non-intrusive). refresh=false
    # so we never send Ctrl+L to the peer's tab; delta defaults off so concurrent
    # readers don't share/clobber a per-session delta baseline. Current view only.
    if a.screen:
        try:
            scr = _req("GET", f"{base}/api/screen?claude_session_id={a.to}&refresh=false",
                       token, timeout=30)
        except Exception as e:
            print(json.dumps({"status": "error", "note": f"cannot read screen: {e}"}))
            return
        print(json.dumps({"status": "screen", "host": host,
                          "screen": scr.get("screen", "")}, ensure_ascii=False))
        return

    # --history: paginated transcript read. Reuses /api/state windowing —
    # rounds=tail, before_idx=page-earlier (same mechanism the web's "load earlier"
    # uses; concurrency-safe, cursor is client-held). Each call is BOUNDED to
    # --rounds; page further back by re-calling with --before <earliest_idx>.
    if a.history:
        url = f"{base}/api/state?claude_session_id={a.to}&mode={a.mode}&rounds={a.rounds}"
        if a.before is not None:
            url += f"&before_idx={a.before}"
        try:
            st = _req("GET", url, token, timeout=30)
        except Exception as e:
            print(json.dumps({"status": "error", "note": f"cannot reach {base}: {e}"}))
            return
        tr = st.get("transcript", [])
        earliest = tr[0].get("_idx") if tr else None
        more = st.get("has_more_history")
        print(json.dumps({"status": "history", "host": host,
                          "transcript": _render_transcript(st),
                          "earliest_idx": earliest, "has_more_history": more,
                          "epoch": st.get("epoch"),
                          "note": (f"more earlier history exists — page back with "
                                   f"--before {earliest}" if more and earliest is not None
                                   else "reached the earliest history")},
                         ensure_ascii=False))
        return

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
                          "activity": _activity(st0),
                          # last answer only — see _last_assistant_text. The `done` path
                          # keeps the full concatenation: there it is the reply to OUR
                          # message, and all of it is the answer.
                          "reply": _last_assistant_text(st0)}, ensure_ascii=False))
        return

    msg = a.message
    if msg is None:
        msg = sys.stdin.read()
    # ALWAYS tag peer messages. There is no untagged send: the receiving session
    # must be able to tell a peer relay from a real human, and the tag is the
    # only signal. (No --raw — see SKILL.md.)
    # Tag: "[⇄ from peer claude · internal · sid=<sid> (name)]".
    #
    # `internal` is spelled out because the receiving side has to choose between two
    # different reply mechanisms, and it used to have to infer which from the wording —
    # "peer claude" versus "external peer session", differing at the third word, both
    # starting "[⇄ from ". Worse, `req=` appears in BOTH kinds with different obligations
    # (echo it in text vs pass it to reply_to_bridge.py), so the most eye-catching token
    # in the tag was not a discriminator at all. Now the word itself says which.
    #
    # sid is MANDATORY and labelled so it cannot be read as anything else: it is the only
    # thing that lets the peer answer, or reach back later. name stays optional — a
    # human-friendly label, nothing depends on it.
    #
    # Eight hex chars, not the full 36. --to resolves a prefix against the target host's
    # live tabs, and the full id buys no extra reach: both branches require the session to
    # be live there anyway. Measured across the real fleet — 23 sessions on three hosts —
    # the shortest globally unique prefix is THREE characters, and 1-2 within any one
    # host; eight leaves 4.3 billion values against 23 in use. A collision is a safe
    # failure besides: the resolver reports "ambiguous — use a longer id" rather than
    # delivering to the wrong session. What the extra 28 characters did buy was pushing
    # the actual message off the first line on a phone.
    own_sid, own_name = _own_session()
    sid = a.frm or own_sid
    if not sid:
        # Refuse rather than send: a message with no sid is one the peer cannot answer.
        print(json.dumps({"status": "error", "note":
            "cannot determine my own session id (no ~/.claude/sessions/<pid>.json up the "
            "process tree) — pass --from <my-session-id>. Not sending: without a sid the "
            "peer has no way to reply."}, ensure_ascii=False))
        return 2
    # --from-name wins and is NOT filtered: the user may have just given this session a
    # working name ("跑批的那个"), and the model passing it through is the whole point of
    # the flag. Failing that, this session's own short name from the store, then the
    # per-machine name= in cc_web.conf. Session before machine, because "cc-web" says who
    # is speaking and "mac-pro" only says which laptop.
    name = (a.frm_name.strip()[:24] if a.frm_name is not None
            else (_short_label(own_name) or _short_label(_conf_name())))
    who = f" · internal · sid={sid[:8]}"
    if name:
        who += f" ({name})"
    # No correlation id on this channel. It existed for "several requests in flight to
    # one peer", and cost a manual step the responder had to remember — echoing
    # "re req=<id>" back in text — which is exactly the kind of step that gets skipped.
    # Internal correlation is already settled without it: the caller knows which sid it
    # asked and is polling that one transcript. And removing it deletes a whole class of
    # confusion, because `req=` now appears in exactly ONE channel (the external bridge),
    # so it can never again be mistaken for the internal/external discriminator.
    # Header tag + an explicit END marker, so the peer knows exactly where our
    # relayed text stops — anything AFTER the end marker is the human user, not us.
    msg = f"[⇄ from peer claude{who}] {msg}\n[⇄ end of peer message]"

    # Don't clobber a human who is mid-typing: if the peer's input box holds real
    # typed text (ghost/placeholder excluded — see /api/input-state), wait it out
    # (poll every 5s, up to 2 min). If it's STILL occupied, force-clear and send.
    clear_first = False
    waited = 0.0
    while True:
        try:
            busy = bool(_req("GET",
                             f"{base}/api/input-state?claude_session_id={a.to}",
                             token, timeout=15).get("busy"))
        except Exception:
            busy = False          # can't tell → don't block forever
        if not busy:
            break
        if waited >= 120:
            clear_first = True     # waited 2 min → wipe residual, then send
            break
        time.sleep(5)
        waited += 5

    _req("POST", f"{base}/api/input", token,
         {"claude_session_id": a.to, "text": msg, "clear_first": clear_first},
         timeout=30)

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
                out = {"status": status, "host": host, "reply": reply,
                       "activity": _activity(st),
                       "idle": True, "pending_confirm": None,
                       "elapsed": round(time.time() - t0, 1),
                       "since_idx": st.get("since_idx", baseline),
                       "note": note}
                print(json.dumps(out, ensure_ascii=False))
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
    # `main()` bare meant every error path exited 0 — including the refusal to send an
    # un-addressable message, which a caller (often a backgrounded Bash tool) then read
    # as success. Existing paths return None and still exit 0; only the explicit codes
    # now carry.
    sys.exit(main() or 0)
