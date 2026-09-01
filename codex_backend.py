"""codex_backend — read and drive a local codex CLI the way cc_web does claude.

cc_web grew up assuming one agent. This module is the other half of that
assumption: it answers the same four questions for codex, so the server can
serve both without the claude path changing at all.

    which sessions exist   →  list_threads()
    what was said          →  parse_rollout()
    is it working or idle   →  turn_state()
    say something to it     →  send_message()

Everything below was measured against codex-cli 0.152.0 on Linux, not read off a
doc page. Where codex offers two ways to learn something, the notes say which one
was chosen and why, because the wrong choice here is invisible until a codex
upgrade breaks it:

  * The session LIST comes from `~/.codex/state_<N>.sqlite`, table `threads`
    (id, cwd, title, tokens_used, rollout_path, …). `codex agents` looks like the
    obvious answer and is not: it demands a TTY ("ERROR: stdin is not a
    terminal") because it is an interactive browser, not a query.
  * The CONTENT comes from the rollout JSONL, never from sqlite. The sqlite
    copy in thread_history_<N> is a projection OF that file — it literally
    tracks `next_rollout_byte_offset` — and every one of those db filenames
    carries a schema version that will change under us. The rollout is also
    append-only with stable ordinals, which is the shape cc_web's tail-and-cache
    already speaks.
  * LIVENESS comes from who holds `~/.codex/thread-writer-locks/<id>.lock`.
    That is an flock held by the process actually writing the thread, so it
    cannot go stale the way a pid file can: no holder means the session ended.
  * WRITING goes through `codex queue`, a supported command that injects a
    message into a running session by thread id — verified to land in a live TUI
    and be executed. cc_web types into claude's composer instead, and everything
    unpleasant about that (don't-interrupt-typing polling, Ctrl+U to clear a
    half-typed line, echo detection) exists only because there was no such door.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

# codex's own env var, so a test can point this at a fixture tree the same way a
# user can point codex at an alternate home.
def _home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _versioned_db(prefix: str) -> Optional[Path]:
    """Newest `<prefix>_<N>.sqlite`. The N is a SCHEMA version, not an index —
    codex ships state_5, thread_history_1, queue_1, logs_2 — so hardcoding one
    means silently reading a dead file after an upgrade. Highest N wins."""
    best: tuple[int, Optional[Path]] = (-1, None)
    for p in _home().glob(f"{prefix}_*.sqlite"):
        m = re.fullmatch(rf"{re.escape(prefix)}_(\d+)", p.stem)
        if m and int(m.group(1)) > best[0]:
            best = (int(m.group(1)), p)
    return best[1]


def available() -> bool:
    return _versioned_db("state") is not None


def _query(db: Path, sql: str, args: tuple = ()) -> list[tuple]:
    """Read-only, and read-only for a reason: this is another program's live
    database, opened while it is running (WAL — note the -shm files)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    try:
        return list(con.execute(sql, args))
    finally:
        con.close()


# ---------------------------------------------------------------------------
# thread → live pid → terminal pane
# ---------------------------------------------------------------------------

def _lock_holders() -> dict[str, int]:
    """{thread_id: pid} for threads a process currently holds the writer lock on.

    One /proc pass over our own fds. `fuser`/`lsof` answer the same question but
    cost a subprocess per lock file, and this runs on every poll. On macOS there
    is no /proc, so fall back to one lsof for all lock files at once."""
    lockdir = _home() / "thread-writer-locks"
    if not lockdir.is_dir():
        return {}
    locks = {str(p.resolve()): p.stem for p in lockdir.glob("*.lock")}
    if not locks:
        return {}
    out: dict[str, int] = {}
    if Path("/proc").is_dir():
        for pdir in Path("/proc").iterdir():
            if not pdir.name.isdigit():
                continue
            try:
                for fd in (pdir / "fd").iterdir():
                    try:
                        tgt = os.readlink(fd)
                    except OSError:
                        continue
                    tid = locks.get(tgt)
                    if tid:
                        out[tid] = int(pdir.name)
            except OSError:
                continue                      # process exited mid-scan, or not ours
        return out
    try:                                      # macOS
        r = subprocess.run(["lsof", "-t", "-Fp", *locks.keys()],
                           capture_output=True, text=True, timeout=5)
        pids = [int(x[1:]) for x in r.stdout.split() if x.startswith("p") and x[1:].isdigit()]
        # lsof -F groups by file, but with one pid per lock in practice a single
        # holder set is enough to mark liveness; map them in file order.
        for tid, pid in zip(locks.values(), pids):
            out[tid] = pid
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return out


def _env_of(pid: int) -> dict[str, str]:
    """A process's environment. codex records no pane of its own, but it INHERITS
    TMUX_PANE, which is a direct pane handle — better than the tty-then-walk route
    the claude bridge has to take (a restored iTerm tab reports tty=None, and its
    foreground job is often `caffeinate`, not claude)."""
    raw = ""
    try:
        if Path("/proc").is_dir():
            raw = (Path(f"/proc/{pid}/environ").read_bytes()
                   .decode("utf-8", "replace"))
        else:
            raw = subprocess.run(["ps", "-Eww", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout
            raw = raw.replace(" ", "\0")      # ps separates with spaces, not NULs
    except (OSError, subprocess.SubprocessError):
        return {}
    env = {}
    for item in raw.split("\0"):
        if "=" in item:
            k, v = item.split("=", 1)
            env[k] = v
    return env


def _cwd_of(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def list_threads(limit: int = 60) -> list[dict]:
    """Sessions, newest first. `live` distinguishes a running TUI from a finished
    session — and a finished one is exactly what `codex resume` takes, so the
    same list serves both the tab list and the resume picker."""
    db = _versioned_db("state")
    if db is None:
        return []
    try:
        rows = _query(db, "select id, cwd, title, updated_at, created_at, tokens_used,"
                          " approval_mode, model_provider, rollout_path"
                          " from threads order by updated_at desc limit ?", (limit,))
    except sqlite3.Error:
        return []
    holders = _lock_holders()
    out = []
    for (tid, cwd, title, upd, created, tokens, appr, provider, rollout) in rows:
        pid = holders.get(tid)
        env = _env_of(pid) if pid else {}
        out.append({
            "agent": "codex",
            "thread_id": tid,
            "cwd": cwd or (_cwd_of(pid) if pid else ""),
            "title": (title or "").strip(),
            "updated_at": upd,
            "created_at": created,
            "tokens_used": tokens or 0,
            "approval_mode": appr or "",
            "model_provider": provider or "",
            "rollout_path": rollout or "",
            "pid": pid,
            "pane": env.get("TMUX_PANE", ""),
            "live": pid is not None,
        })
    return out


# ---------------------------------------------------------------------------
# rollout → transcript
# ---------------------------------------------------------------------------

# A codex turn opens with an injected `<environment_context>` user message and up
# to three developer-role messages (the permissions/team/multi-agent preambles).
# They are addressed to the model, not written by the human, and showing them
# would put a wall of policy text where the user's question belongs.
_INJECTED = re.compile(r"^\s*<(environment_context|user_instructions|permissions)\b")


def _text_of(payload: dict) -> str:
    parts = []
    for c in payload.get("content") or []:
        if isinstance(c, dict) and c.get("text"):
            parts.append(c["text"])
        elif isinstance(c, str):
            parts.append(c)
    return "\n".join(parts).strip()


def parse_rollout(path: str | Path, since_ordinal: Optional[int] = None) -> dict:
    """Normalize a rollout into {entries, meta, ordinal}. Entries mirror what the
    claude side hands the UI: a role, text, and tool activity.

    Unknown `type`s are kept as kind='other' rather than dropped: this is another
    program's evolving format, and a new item type should make the transcript
    slightly less informative, never make it wrong or empty."""
    p = Path(path)
    entries: list[dict] = []
    meta: dict[str, Any] = {}
    last_ordinal = -1
    calls: dict[str, str] = {}                 # call_id → tool name, for outputs
    try:
        raw_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"entries": [], "meta": {}, "ordinal": -1}

    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue                           # a half-written tail line; next poll gets it
        ordinal = row.get("ordinal")
        if isinstance(ordinal, int):
            last_ordinal = max(last_ordinal, ordinal)
            if since_ordinal is not None and ordinal <= since_ordinal:
                continue
        rtype, payload = row.get("type"), (row.get("payload") or {})
        ts = row.get("timestamp") or ""
        ptype = payload.get("type")

        if rtype == "session_meta":
            meta = {"thread_id": payload.get("session_id") or payload.get("id"),
                    "cwd": payload.get("cwd", ""),
                    "originator": payload.get("originator", ""),
                    "cli_version": payload.get("cli_version", ""),
                    "started_at": payload.get("timestamp", "")}
            continue

        if rtype == "event_msg":
            if ptype in ("task_started", "task_complete"):
                entries.append({"idx": ordinal, "ts": ts, "kind": "turn",
                                "event": ptype, "turn_id": payload.get("turn_id", ""),
                                "text": payload.get("last_agent_message") or ""})
            elif ptype == "token_count":
                info = (payload.get("info") or {}).get("total_token_usage") or {}
                entries.append({"idx": ordinal, "ts": ts, "kind": "tokens",
                                "total": info.get("total_tokens")})
            continue

        if rtype != "response_item":
            continue                           # world_state / turn_context: internal

        role = payload.get("role")
        if role == "developer":
            continue
        if ptype == "message":
            text = _text_of(payload)
            if not text or (role == "user" and _INJECTED.match(text)):
                continue
            entries.append({"idx": ordinal, "ts": ts, "kind": "msg", "role": role or "",
                            "phase": payload.get("phase", ""), "text": text})
        elif ptype in ("custom_tool_call", "function_call", "local_shell_call"):
            name = payload.get("name") or ptype
            calls[payload.get("call_id", "")] = name
            entries.append({"idx": ordinal, "ts": ts, "kind": "tool",
                            "tool": name, "call_id": payload.get("call_id", ""),
                            "text": _tool_input(payload)})
        elif ptype in ("custom_tool_call_output", "function_call_output"):
            entries.append({"idx": ordinal, "ts": ts, "kind": "tool_out",
                            "tool": calls.get(payload.get("call_id", ""), ""),
                            "call_id": payload.get("call_id", ""),
                            "text": _tool_output(payload)})
        elif ptype == "reasoning":
            entries.append({"idx": ordinal, "ts": ts, "kind": "reasoning", "text": ""})
        else:
            entries.append({"idx": ordinal, "ts": ts, "kind": "other", "ptype": ptype or ""})
    return {"entries": entries, "meta": meta, "ordinal": last_ordinal}


def _tool_input(payload: dict) -> str:
    for k in ("input", "arguments", "command"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (list, dict)):
            return json.dumps(v, ensure_ascii=False)
    return ""


def _tool_output(payload: dict) -> str:
    v = payload.get("output")
    if isinstance(v, str):
        return v
    if v is not None:
        return json.dumps(v, ensure_ascii=False)
    return ""


def turn_state(entries: list[dict]) -> dict:
    """Busy or idle, from codex's OWN turn events rather than from a spinner.

    This is the one place codex is plainly easier than claude: claude's
    end-of-turn has to be inferred (transcript shape + screen + a settle delay),
    while a rollout says `task_started` / `task_complete` outright, and the
    complete event even carries the final message."""
    last = None
    for e in entries:
        if e.get("kind") == "turn":
            last = e
    if last is None:
        return {"idle": True, "turn_id": "", "last_agent_message": ""}
    return {"idle": last.get("event") == "task_complete",
            "turn_id": last.get("turn_id", ""),
            "last_agent_message": last.get("text") or ""}


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

def codex_bin() -> Optional[str]:
    for c in (os.environ.get("CODEX_BIN"),
              str(Path.home() / ".local/bin/codex"),
              str(Path.home() / ".npm-global/bin/codex"),
              "/usr/local/bin/codex", "/usr/bin/codex"):
        if c and os.access(c, os.X_OK):
            return c
    return None


def _node_dir() -> Optional[str]:
    """Directory of a usable `node`, for the PATH we hand codex.

    `codex` is a `#!/usr/bin/env node` script, and cc_web runs as a systemd user
    unit whose PATH is the minimal one — no nvm. So the command that works in a
    login shell fails inside the server with `/usr/bin/env: "node": no such file`.
    Found by calling the endpoint on the live box; nothing short of that would
    have shown it, since every interactive test inherits a shell PATH that works.
    nvm dirs are sorted by version so the newest wins."""
    from shutil import which
    n = which("node")
    if n:
        return str(Path(n).parent)
    cands = sorted((Path.home() / ".nvm/versions/node").glob("v*/bin/node"),
                   key=lambda p: [int(x) for x in re.findall(r"\d+", p.parts[-3])],
                   reverse=True)
    for c in (*cands, Path("/usr/local/bin/node"), Path("/usr/bin/node"),
              Path("/opt/homebrew/bin/node")):
        if os.access(c, os.X_OK):
            return str(c.parent)
    return None


def _exec_env() -> dict:
    env = os.environ.copy()
    nd = _node_dir()
    if nd and nd not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = nd + os.pathsep + env.get("PATH", "")
    return env


def send_message(thread_id: str, text: str, timeout: float = 30.0) -> dict:
    """Queue a message into a session. `codex queue` returns as soon as the
    message is recorded; a live TUI picks it up and runs it.

    stdin is closed deliberately. codex's non-interactive commands READ stdin and
    append it to the prompt — a heredoc's leftovers ended up inside a prompt
    while this was being written — so anything we invoke gets /dev/null."""
    exe = codex_bin()
    if exe is None:
        return {"ok": False, "error": "codex not installed"}
    if not thread_id or not text.strip():
        return {"ok": False, "error": "thread_id and text required"}
    try:
        r = subprocess.run([exe, "queue", "--thread", thread_id, "--message", text],
                           stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=timeout, env=_exec_env())
    except subprocess.SubprocessError as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        # The one failure worth naming: a thread with no rollout yet cannot be
        # queued to. That is the same "no transcript until the first exchange"
        # trap that made freshly opened claude tabs unopenable in cc_web.
        no_rollout = "no rollout found" in err.lower()
        return {"ok": False, "error": err or out or f"exit {r.returncode}",
                "reason": "no_rollout" if no_rollout else "error"}
    m = re.search(r"Queued message (\S+)", out)
    return {"ok": True, "queued_id": m.group(1) if m else "", "stdout": out}
