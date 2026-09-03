#!/usr/bin/env python3
"""selfcheck — the mechanical half of a session's self-check.

A self-check is a self-graded exam: the failure mode of drift is that the model
believes it is on task, so a self-report passes exactly when it is most wrong. So the
useful part is not what the model writes — it is what it cannot get stored.

Those refusals live in **cc-web**, not here (POST /api/session-check): a validator
inside this script would be advice, since whatever runs it could write the report file
directly instead. Behind the endpoint that owns the file it is a gate, and both agents
get the same one.

This script therefore does only what a server cannot: work out which session it is
running inside, collect the deterministic facts about the working tree, and print the
result short.

    selfcheck.py facts                 # what to check + the facts (JSON)
    selfcheck.py save --file r.json    # send the report through the gate
    selfcheck.py show                  # print the last stored report

CONFIG: one file, the same one the rest of this project uses —
`~/.claude/cc_web.conf` (`token=`). Nothing else is configured here: where reports are
kept and which agent this instance serves are cc-web's to know, and are asked of it.
Re-deriving its directory naming here is how a skill ends up silently reading an empty
directory after the server renames one.
"""
import argparse
import datetime as dt
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
CONF = HOME / ".claude" / "cc_web.conf"
_NOVERIFY = ssl.create_default_context()
_NOVERIFY.check_hostname = False
_NOVERIFY.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------- identity

def _own_sid() -> str:
    """This session's own id.

    claude keeps a per-pid store (~/.claude/sessions/<pid>.json) — walk up the process
    tree to the process that owns one, the same way the my-session-id skill does.
    codex writes no such file but inherits $TMUX_PANE and cc-web can map a pane to a
    thread, so a codex run passes its id in $CC_SESSION_ID (see AGENTS.codex.md).
    """
    env = os.environ.get("CC_SESSION_ID", "").strip()
    if env:
        return env
    pid = os.getpid()
    for _ in range(12):
        f = HOME / ".claude" / "sessions" / f"{pid}.json"
        if f.is_file():
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                sid = (d.get("sessionId") or d.get("session_id") or "").strip()
                if sid:
                    return sid
            except (OSError, ValueError):
                pass
        pid = _ppid(pid)
        if pid <= 1:
            break
    return ""


def _ppid(pid: int) -> int:
    """/proc where it exists, `ps` otherwise — macOS has no /proc, and this walk is the
    only way a session identifies itself there."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return int(raw[raw.rindex(")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return int(out or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


# ---------------------------------------------------------------- cc-web

def _token() -> str:
    try:
        for line in CONF.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("token="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _instances():
    """Every cc-web on this machine, from the running processes' own argv.

    Not a configured port: there is more than one instance (claude and codex run the
    same app with different CC_WEB_AGENT), and cc_web binds the tailnet address rather
    than loopback — so 127.0.0.1 is NOT listening. Asking the process what it bound is
    the only reading that is right on every host.
    """
    try:
        ps = subprocess.run(["ps", "-Ao", "args="], capture_output=True, text=True,
                            timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in ps.splitlines():
        if "/uvicorn" not in line or "cc_web:app" not in line:
            continue
        parts = line.split()
        host = port = ""
        for i, p in enumerate(parts):
            if p == "--host" and i + 1 < len(parts):
                host = parts[i + 1]
            elif p == "--port" and i + 1 < len(parts):
                port = parts[i + 1]
        if port:
            out.append((host or "127.0.0.1", port))
    return out


def _api(method: str, path: str, body=None, base=None):
    tok = _token()
    if not tok:
        raise SystemExit(f"selfcheck: no token= in {CONF}")
    bases = [base] if base else [f"https://{h}:{p}" for h, p in _instances()]
    if not bases:
        raise SystemExit("selfcheck: no cc-web instance is running on this machine "
                         "(the task memo and the report both live in it)")
    last = ""
    for b in bases:
        req = urllib.request.Request(
            b + path, method=method,
            data=(json.dumps(body).encode() if body is not None else None),
            headers={"authorization": "Bearer " + tok,
                     **({"content-type": "application/json"} if body is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=20, context=_NOVERIFY) as r:
                return json.loads(r.read().decode()), b
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except ValueError:
                pass
            # 422 is the gate talking: that is an answer, not a wrong address.
            if e.code in (400, 422):
                return {"_rejected": detail, "_status": e.code}, b
            last = f"{e.code} {detail[:200]}"
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = str(e)
    raise SystemExit(f"selfcheck: cannot reach cc-web ({last})")


def _whoami(sid: str):
    """Which agent this instance serves — from cc-web, because it knows (CC_WEB_AGENT)
    and this script would only be guessing. With two instances up, pick the one that
    actually has a memo for this session id: a claude session id means nothing to the
    codex instance and vice versa."""
    cands = [f"https://{h}:{p}" for h, p in _instances()]
    best = None
    for b in cands:
        try:
            info, _ = _api("GET", "/api/server-info", base=b)
            memo, _ = _api("GET", "/api/session-memo?claude_session_id=" + sid, base=b)
        except SystemExit:
            continue
        has = bool((memo.get("task") or {}).get("text") or (memo.get("notes") or {}).get("text"))
        if has:
            return b, info.get("agent", "?"), memo
        best = best or (b, info.get("agent", "?"), memo)
    if not best:
        raise SystemExit("selfcheck: cannot reach cc-web on this machine")
    return best


# ---------------------------------------------------------------- facts

def _run(cmd, limit=4000):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return (p.stdout + p.stderr)[:limit].strip()
    except (OSError, subprocess.SubprocessError) as e:
        return f"({type(e).__name__}: {e})"


def _git_facts() -> dict:
    """Deterministic repo state. Whether the tree is dirty is a fact, not a
    recollection — the point of gathering it here is that the model never has to
    introspect about it."""
    if _run(["git", "rev-parse", "--is-inside-work-tree"]) != "true":
        return {"repo": False}
    dirty = [l for l in _run(["git", "status", "--porcelain"]).splitlines() if l]
    return {
        "repo": True,
        "branch": _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "head": _run(["git", "rev-parse", "--short", "HEAD"]),
        "dirty_files": [l[3:] for l in dirty][:40],
        "dirty_count": len(dirty),
        "unpushed": _run(["git", "log", "--oneline", "@{u}..HEAD"]).splitlines()[:20],
        "recent_commits": _run(["git", "log", "--oneline", "-8"]).splitlines(),
    }


def cmd_facts(args) -> int:
    sid = args.sid or _own_sid()
    if not sid:
        print(json.dumps({"error": "cannot work out this session's id — pass --sid, or "
                                   "set CC_SESSION_ID (codex)"}, ensure_ascii=False))
        return 2
    base, agent, memo = _whoami(sid)
    prev, _ = _api("GET", "/api/session-check?claude_session_id=" + sid, base=base)
    task = (memo.get("task") or {}).get("text") or ""
    notes = (memo.get("notes") or {}).get("text") or ""
    rep = prev.get("report") or {}
    print(json.dumps({
        "session_id": sid,
        "agent": agent,
        "cc_web": base,
        "cwd": os.getcwd(),
        "memo_ver": prev.get("memo_ver") or "",
        "task": task,
        "notes": notes,
        "has_task": bool(task or notes),
        # A pinned checklist is reused verbatim while the task text is unchanged, so
        # two runs are comparable and a dropped item is visible. null → derive it.
        "pinned": (rep.get("pinned") if not prev.get("stale") else None),
        "previous_report": ({k: rep.get(k) for k in ("checked_at", "verdict", "summary")}
                            if rep else None),
        "previous_stale": bool(prev.get("stale")),
        "git": _git_facts(),
        "now": dt.datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=1))
    return 0


# ---------------------------------------------------------------- save / show

def cmd_save(args) -> int:
    try:
        rep = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"selfcheck: cannot read {args.file}: {e}", file=sys.stderr)
        return 2
    sid = args.sid or rep.get("session_id") or _own_sid()
    if not sid:
        print("selfcheck: cannot work out this session's id", file=sys.stderr)
        return 2
    base, _agent, _memo = _whoami(sid)
    body = {"claude_session_id": sid,
            "verdict": rep.get("verdict"),
            "summary": rep.get("summary") or "",
            "items": rep.get("items") or [],
            "disputes": rep.get("disputes") or [],
            "not_mine_reason": rep.get("not_mine_reason") or "",
            "not_mine_evidence": rep.get("not_mine_evidence") or []}
    out, _ = _api("POST", "/api/session-check", body, base=base)
    if out.get("_rejected"):
        print("REJECTED: " + str(out["_rejected"]), file=sys.stderr)
        print("  (nothing was stored — fix the report and save again)", file=sys.stderr)
        return 3
    print(f"stored in cc-web ({base}) for session {sid[:8]}")
    _print_summary(out)
    return 0


def _print_summary(rep: dict) -> None:
    """Short on purpose. A green report that prints twelve lines is one you stop reading
    in a fortnight — and it will usually be green. Deviations get the space."""
    v = rep.get("verdict")
    n = len(rep.get("items") or [])
    if v == "ok":
        print(f"✓ 全部符合 ({n} 项) — {rep.get('summary', '')}")
        return
    if v == "no_task":
        print("· 这个 session 没有设定任务/注意事项 —— 无可自检")
        return
    if v == "not_mine":
        print("⚠ 这个任务不是给这个 session 的,已拒绝执行")
        print("  理由: " + rep.get("not_mine_reason", ""))
        for e in rep.get("not_mine_evidence") or []:
            print(f"    · {e.get('cmd', '')} → {str(e.get('out', ''))[:120]}")
        return
    if v == "disputed":
        print("⚠ 任务本身有问题:")
        for d in rep.get("disputes") or []:
            print(f"  · [{d.get('about', '?')}] {d.get('reason', '')}")
    bad = [it for it in (rep.get("items") or []) if it.get("status") in ("not_done", "partial")]
    unk = [it for it in (rep.get("items") or []) if it.get("status") == "unverifiable"]
    if bad or unk:
        print(f"✗ {len(bad)}/{n} 项未满足" + (f",{len(unk)} 项无法验证" if unk else ""))
    for it in bad + unk:
        print(f"  · [{it.get('status')}] {it.get('claim')}")
        for e in (it.get("evidence") or [])[:2]:
            print(f"      {e.get('cmd', '')} → {str(e.get('out', ''))[:120]}")
        if it.get("note"):
            print(f"      note: {it['note']}")


def cmd_show(args) -> int:
    sid = args.sid or _own_sid()
    if not sid:
        print("selfcheck: cannot work out this session's id", file=sys.stderr)
        return 2
    base, _agent, _memo = _whoami(sid)
    got, _ = _api("GET", "/api/session-check?claude_session_id=" + sid, base=base)
    if not got.get("exists"):
        print(f"(no report yet for {sid[:8]})")
        return 1
    rep = got["report"]
    if got.get("stale"):
        print("⚠ 这份报告针对的是更早版本的任务(任务此后被改过) —— 重新自检再看")
    print(f"[{rep.get('checked_at')}] {sid[:8]} {rep.get('agent')}")
    _print_summary(rep)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("facts", help="what to check + deterministic facts (JSON)")
    f.add_argument("--sid")
    f.set_defaults(fn=cmd_facts)
    s = sub.add_parser("save", help="send a report through cc-web's gate")
    s.add_argument("--file", required=True)
    s.add_argument("--sid")
    s.set_defaults(fn=cmd_save)
    w = sub.add_parser("show", help="print the last stored report")
    w.add_argument("--sid")
    w.add_argument("--json", action="store_true")
    w.set_defaults(fn=cmd_show)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
