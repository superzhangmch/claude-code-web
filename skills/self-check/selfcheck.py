#!/usr/bin/env python3
"""selfcheck — the mechanical half of a session's self-check.

A self-check written entirely by the model is a self-graded exam: the failure mode of
drift is that the model believes it is on task, so its verdict passes exactly when it
is most wrong. This script exists to take the parts that do NOT need judgement away
from the model, and to REFUSE the shapes of report that are known to rot:

  * a verdict with no evidence behind it            → rejected
  * a checklist quietly rewritten between runs      → rejected
  * a report about a task version that has moved on → the script stamps the version
                                                       itself, the model cannot
  * "this task is not mine" with no reason/evidence → rejected

What is left for the model is judgement, and only judgement, written into slots this
script validates.

    selfcheck.py facts                 # what to check + the deterministic facts (JSON)
    selfcheck.py save --file r.json    # validate + store the report; prints a summary
    selfcheck.py show [--sid ID]       # print the last stored report

Report lands in ONE fixed place, one file per session:

    ~/.claude/cc_web_check.d/<session-id>.json          (codex: cc_web_check.codex.d)

Deliberately NOT inside the human's memo file: this is an agent's output, and an
agent's read-modify-write must never be able to clobber the sentence a human typed.
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
STATUSES = ("done", "not_done", "partial", "unverifiable")
VERDICTS = ("ok", "deviations", "not_mine", "disputed", "no_task")
SID_RE = re.compile(r"[0-9a-zA-Z._%-]{4,80}")


# ---------------------------------------------------------------- identity

def _own_sid() -> str:
    """This session's own id.

    claude keeps a per-pid store (~/.claude/sessions/<pid>.json) — walk up the process
    tree to the process that owns one, exactly as the my-session-id skill does. codex
    keeps no such file but inherits $TMUX_PANE, and cc-web can map a pane to a thread;
    rather than duplicate that lookup here, accept $CC_SESSION_ID for the codex case.
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
        try:
            ppid = int(Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            try:
                ppid = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                                          capture_output=True, text=True).stdout.strip())
            except (ValueError, OSError):
                return ""
        if ppid <= 1:
            return ""
        pid = ppid
    return ""


def _dirs(sid: str):
    """(memo_file, check_file, agent) — the codex instance keeps its own domain, so a
    codex thread id is never looked up in claude's directory or vice versa."""
    for agent, suffix in (("claude", ""), ("codex", ".codex")):
        memo = HOME / ".claude" / f"cc_web_memo{suffix}.d" / f"{sid}.json"
        if memo.is_file():
            return memo, HOME / ".claude" / f"cc_web_check{suffix}.d" / f"{sid}.json", agent
    # No memo yet: default to the claude domain unless this is plainly a codex run.
    codexish = bool(os.environ.get("CODEX_HOME")) or (HOME / ".codex").is_dir() and not (HOME / ".claude" / "sessions").is_dir()
    suffix = ".codex" if codexish else ""
    return (HOME / ".claude" / f"cc_web_memo{suffix}.d" / f"{sid}.json",
            HOME / ".claude" / f"cc_web_check{suffix}.d" / f"{sid}.json",
            "codex" if codexish else "claude")


# ---------------------------------------------------------------- facts

def _run(cmd, cwd=None, limit=4000):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=20)
        return (p.stdout + p.stderr)[:limit].strip()
    except (OSError, subprocess.SubprocessError) as e:
        return f"({type(e).__name__}: {e})"


def _git_facts(cwd: str) -> dict:
    """Deterministic repo state. This is the layer the model must not have to
    introspect about: whether the tree is dirty is a fact, not a recollection."""
    inside = _run(["git", "rev-parse", "--is-inside-work-tree"], cwd) == "true"
    if not inside:
        return {"repo": False}
    dirty = _run(["git", "status", "--porcelain"], cwd)
    return {
        "repo": True,
        "branch": _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd),
        "head": _run(["git", "rev-parse", "--short", "HEAD"], cwd),
        "dirty_files": [l[3:] for l in dirty.splitlines() if l][:40],
        "dirty_count": len([l for l in dirty.splitlines() if l]),
        "unpushed": _run(["git", "log", "--oneline", "@{u}..HEAD"], cwd).splitlines()[:20],
        "recent_commits": _run(["git", "log", "--oneline", "-8"], cwd).splitlines(),
    }


def _memo(memo_file: Path) -> dict:
    try:
        raw = json.loads(memo_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _memo_ver(memo: dict) -> str:
    """The task version a report is about. Both fields' updated_at, so rewriting either
    the task or the standing notes invalidates a pinned checklist — the checklist is
    derived from both."""
    t = (memo.get("task") or {}).get("updated_at") or ""
    n = (memo.get("notes") or {}).get("updated_at") or ""
    return f"{t}|{n}"


def cmd_facts(args) -> int:
    sid = args.sid or _own_sid()
    if not sid:
        print(json.dumps({"error": "cannot determine this session's id; pass --sid or set "
                                   "CC_SESSION_ID"}, ensure_ascii=False))
        return 2
    memo_file, check_file, agent = _dirs(sid)
    memo = _memo(memo_file)
    prev = _memo(check_file)
    task = (memo.get("task") or {}).get("text") or ""
    notes = (memo.get("notes") or {}).get("text") or ""
    cwd = os.getcwd()
    out = {
        "session_id": sid,
        "agent": agent,
        "cwd": cwd,
        "memo_file": str(memo_file),
        "report_file": str(check_file),
        "memo_ver": _memo_ver(memo),
        "task": task,
        "notes": notes,
        "has_task": bool(task or notes),
        # A pinned checklist is reused verbatim while the task text is unchanged, so two
        # runs are comparable and a quietly dropped item is visible. Re-derived only when
        # the task moves.
        "pinned": (prev.get("pinned") if prev.get("pinned", {}).get("memo_ver") == _memo_ver(memo) else None),
        "previous_report": {k: prev.get(k) for k in ("checked_at", "verdict", "summary")} if prev else None,
        "git": _git_facts(cwd),
        "now": dt.datetime.now().isoformat(timespec="seconds"),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


# ---------------------------------------------------------------- save

def _fail(msg: str) -> int:
    print("REJECTED: " + msg, file=sys.stderr)
    print("  (nothing was written — fix the report and save again)", file=sys.stderr)
    return 3


def cmd_save(args) -> int:
    try:
        rep = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return _fail(f"cannot read {args.file}: {e}")
    if not isinstance(rep, dict):
        return _fail("report must be a JSON object")

    sid = args.sid or rep.get("session_id") or _own_sid()
    if not sid or not SID_RE.fullmatch(sid):
        return _fail("no usable session id")
    memo_file, check_file, agent = _dirs(sid)
    memo = _memo(memo_file)
    task = (memo.get("task") or {}).get("text") or ""
    notes = (memo.get("notes") or {}).get("text") or ""
    prev = _memo(check_file)

    verdict = rep.get("verdict")
    if verdict not in VERDICTS:
        return _fail(f"verdict must be one of {VERDICTS}")
    if not (task or notes) and verdict != "no_task":
        return _fail("there is no task or notes for this session, so the only honest "
                     "verdict is no_task")

    items = rep.get("items") or []
    if not isinstance(items, list):
        return _fail("items must be a list")
    if verdict in ("ok", "deviations") and not items:
        return _fail("a verdict about the task needs at least one checked item")

    # An item whose status is a claim about the world must cite the world. This is the
    # mechanical form of "evidence, not opinions": a report that merely asserts
    # "done" cannot be stored, so a wrong verdict is always accompanied by the thing
    # that makes it checkable in ten seconds.
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            return _fail(f"item {i} must be an object")
        if not (it.get("claim") or "").strip():
            return _fail(f"item {i} has no claim")
        if it.get("status") not in STATUSES:
            return _fail(f"item {i}: status must be one of {STATUSES}")
        ev = it.get("evidence") or []
        if it["status"] == "unverifiable":
            if not (it.get("note") or "").strip():
                return _fail(f"item {i} is unverifiable, so it must say what would make "
                             f"it checkable")
            continue
        if not isinstance(ev, list) or not ev:
            return _fail(f"item {i} claims '{it['status']}' with no evidence — run "
                         f"something that shows it, or mark it unverifiable")
        for e in ev:
            if not isinstance(e, dict) or not (e.get("cmd") or "").strip() \
               or not str(e.get("out") or "").strip():
                return _fail(f"item {i}: each evidence entry needs a cmd and its real "
                             f"output (no paraphrasing)")

    # A checklist the model may rewrite each run is a checklist it can quietly shorten
    # on the day an item is inconvenient. While the task text is unchanged, the pinned
    # claims are the contract.
    ver = _memo_ver(memo)
    pinned = prev.get("pinned") if isinstance(prev.get("pinned"), dict) else None
    claims = [(it.get("claim") or "").strip() for it in items]
    if pinned and pinned.get("memo_ver") == ver and verdict in ("ok", "deviations"):
        if claims != list(pinned.get("claims") or []):
            missing = [c for c in (pinned.get("claims") or []) if c not in claims]
            added = [c for c in claims if c not in (pinned.get("claims") or [])]
            return _fail("the task has not changed, so the checklist must not either.\n"
                         f"  dropped: {missing}\n  added:   {added}\n"
                         "  (if an item is genuinely wrong, change the task/notes — that "
                         "re-derives the list on purpose and on the record)")
    else:
        pinned = {"memo_ver": ver, "claims": claims,
                  "pinned_at": dt.datetime.now().isoformat(timespec="seconds")}

    if verdict == "not_mine":
        # The case the human asked for explicitly: a task that is not this session's
        # work must be REPORTED, never executed. It also must not be a bare assertion —
        # "not mine" is the most convenient possible verdict.
        why = (rep.get("not_mine_reason") or "").strip()
        if not why:
            return _fail("not_mine needs not_mine_reason: what the task is about vs what "
                         "this session is")
        if not (rep.get("not_mine_evidence") or []):
            return _fail("not_mine needs not_mine_evidence (e.g. this session's cwd, the "
                         "repo it is in, the files it has touched)")
    if verdict == "disputed" and not (rep.get("disputes") or []):
        return _fail("disputed needs disputes: which part of the task/notes is wrong, and why")

    bad = [it for it in items if it.get("status") in ("not_done", "partial")]
    if bad and verdict == "ok":
        return _fail("some items are not done, so the verdict cannot be ok")
    if not bad and verdict == "deviations":
        return _fail("nothing deviates, so the verdict should be ok")

    out = {
        "schema": 1,
        "session_id": sid,
        "agent": agent,
        # Stamped HERE, not by the model: a report must not be able to claim it is about
        # a version of the task that it is not. cc-web can then say "this report is
        # about an older task" instead of showing a stale green.
        "memo_ver": ver,
        "task": task,
        "notes": notes,
        "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
        "cwd": os.getcwd(),
        "verdict": verdict,
        "summary": (rep.get("summary") or "").strip(),
        "items": items,
        "deviations": [it.get("claim") for it in bad],
        "disputes": rep.get("disputes") or [],
        "not_mine_reason": rep.get("not_mine_reason") or "",
        "not_mine_evidence": rep.get("not_mine_evidence") or [],
        "pinned": pinned,
        "git": _git_facts(os.getcwd()),
    }
    check_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = check_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(check_file)
    print(f"written: {check_file}")
    _print_summary(out)
    return 0


def _print_summary(rep: dict) -> None:
    """Short on purpose. A green report that prints twelve lines is a report you stop
    reading in a fortnight — and it will usually be green. Deviations get the space."""
    v = rep.get("verdict")
    n = len(rep.get("items") or [])
    if v == "ok":
        print(f"✓ 全部符合 ({n} 项) — {rep.get('summary','')}")
        return
    if v == "no_task":
        print("· 这个 session 没有设定任务/注意事项 —— 无可自检")
        return
    if v == "not_mine":
        print("⚠ 这个任务不是给这个 session 的,已拒绝执行")
        print("  理由: " + rep.get("not_mine_reason", ""))
        for e in rep.get("not_mine_evidence") or []:
            print(f"    · {e.get('cmd','')} → {str(e.get('out',''))[:120]}")
        return
    if v == "disputed":
        print("⚠ 任务本身有问题:")
        for d in rep.get("disputes") or []:
            print(f"  · [{d.get('about','?')}] {d.get('reason','')}")
    bad = [it for it in (rep.get("items") or []) if it.get("status") in ("not_done", "partial")]
    unk = [it for it in (rep.get("items") or []) if it.get("status") == "unverifiable"]
    print(f"✗ {len(bad)}/{n} 项未满足" + (f",{len(unk)} 项无法验证" if unk else ""))
    for it in bad + unk:
        print(f"  · [{it.get('status')}] {it.get('claim')}")
        for e in (it.get("evidence") or [])[:2]:
            print(f"      {e.get('cmd','')} → {str(e.get('out',''))[:120]}")
        if it.get("note"):
            print(f"      note: {it['note']}")


def cmd_show(args) -> int:
    sid = args.sid or _own_sid()
    _, check_file, _ = _dirs(sid)
    rep = _memo(check_file)
    if not rep:
        print(f"(no report yet at {check_file})")
        return 1
    memo = _memo(_dirs(sid)[0])
    if rep.get("memo_ver") != _memo_ver(memo):
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
    s = sub.add_parser("save", help="validate + store a report")
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
