"""codex_shim — make codex sessions look like claude sessions to cc_web.

The frontend is the part worth keeping. It already renders transcripts, tool
activity, brief/medium modes, queued messages, voice input, the session tree —
tens of thousands of lines of behaviour that has nothing to do with which agent
produced the text. So the translation happens as far DOWN as it can: this module
converts codex's own records into the exact entry shape cc_web's helpers already
consume, and everything above it — _filter_entries, _is_claude_idle,
_last_n_rounds, since_idx paging, the whole UI — runs unmodified.

Two shapes are produced:

  threads_as_tabs()  →  rows shaped like /api/tabs' rows (sid, tab_name, cwd, …)
  entries_for()      →  claude-JSONL-shaped entries:
                        {"type": "user"|"assistant",
                         "message": {"role", "content": [...]}, "_idx": n, …}

The one non-obvious mapping is idleness. cc_web decides a session is idle by
reading `message.stop_reason == "end_turn"` off the LAST assistant entry, so a
codex turn that has completed must carry that field and one still running must
not. codex states this outright in its rollout (`task_complete` /`task_started`),
which is why the flag can be set honestly rather than guessed from a spinner.
"""
from __future__ import annotations

import time
from typing import Optional

import codex_backend as codex

AGENT_NAME = "codex"


# ---------------------------------------------------------------------------
# sessions → the tab rows /api/tabs returns
# ---------------------------------------------------------------------------

def threads_as_tabs(limit: int = 60, live_only: bool = True) -> list[dict]:
    """codex threads in /api/tabs' row shape.

    `live_only` because a tab list means "sessions you can talk to now": a thread
    whose writer lock nobody holds has no terminal and cannot be queued to. The
    finished ones are still listed by the picker (see threads_as_sessions)."""
    rows = []
    for i, t in enumerate(codex.list_threads(limit)):
        if live_only and not t["live"]:
            continue
        name = t["title"] or "(codex)"
        rows.append({
            "sid": t["thread_id"],
            "session_id": t["thread_id"],
            "tab_name": name,
            "name": name,
            "session_name": name,
            "cwd": t["cwd"],
            "parent": None,
            "tab_index": i,
            "window_index": 0,
            "pid": t["pid"] or 0,
            "pane": t["pane"],
            "agent": AGENT_NAME,
            "live": t["live"],
            "tokens_used": t["tokens_used"],
        })
    return rows


def threads_as_sessions(limit: int = 80) -> list[dict]:
    """The picker's list, in brief_picker_sessions' item shape.

    Field-for-field, because the frontend reads a FLAT `sessions` array and keys
    off `group` and `claude_session_id` — returning the obvious-looking
    {tabs, recent, named} instead rendered "No live claude tab" on a page whose
    API calls were all succeeding. A real browser caught that; the endpoint tests
    could not have.

    group="tabs" for a session with a live process (talkable now), "recent" for a
    finished one — which is exactly the split `codex resume` cares about."""
    out = []
    for i, t in enumerate(codex.list_threads(limit)):
        name = t["title"] or "(codex)"
        # SECONDS, verified against the live db — not milliseconds. Dividing by
        # 1000 "to be safe" put every row at 1970-01-22, which the list rendered
        # without complaint. (codex's rollout timestamps ARE ms; its sqlite is not.)
        mtime = float(t["updated_at"] or 0)
        out.append({
            "claude_session_id": t["thread_id"],
            "group": "tabs" if t["live"] else "recent",
            "title": name,
            "project_path": t["cwd"],
            "last_visit": (time.strftime("%m-%d %H:%M", time.localtime(mtime))
                           if mtime else ""),
            "mtime": mtime,
            "ts_approx": False,          # codex stamps updated_at itself; no guessing
            "file_size": 0,
            "named": False,
            "bound": t["live"],          # nothing to attach: live == talkable
            "summary": "",
            "summary_title": "",
            "user_name": "",
            "brief": True,
            "parent": "",
            "tab_count": 1,
            "tab_positions": [],
            "tab_name": name,
            "window_index": 0,
            "tab_index": i,
            "iterm_session_id": t["pane"],
            "pid": t["pid"],
            "proc_start": None,
            "cwd": t["cwd"],
            "agent": AGENT_NAME,
            "tokens_used": t["tokens_used"],
        })
    return out


def find_thread(thread_id: str) -> Optional[dict]:
    return next((t for t in codex.list_threads(200) if t["thread_id"] == thread_id), None)


# ---------------------------------------------------------------------------
# rollout → claude-shaped entries
# ---------------------------------------------------------------------------

def _user(text: str, idx: int, ts: str) -> dict:
    return {"type": "user", "_idx": idx, "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _assistant(content: list, idx: int, ts: str, done: bool) -> dict:
    msg = {"role": "assistant", "content": content}
    if done:
        # The single field cc_web's idle test reads. Set only on a turn codex has
        # actually reported complete — claiming it early would make the UI offer
        # the input box while the agent is still working.
        msg["stop_reason"] = "end_turn"
    return {"type": "assistant", "_idx": idx, "timestamp": ts, "message": msg}


def entries_for(thread: dict, since_ordinal: Optional[int] = None) -> tuple[list[dict], int]:
    """(entries, tip_ordinal) for a thread, in cc_web's entry shape.

    Ordinals become `_idx`, which is what the client's since_idx cursor tracks —
    they are already monotonic per rollout, so the existing paging works as-is.

    A tool call becomes an assistant entry carrying a `tool_use` block, which is
    how the UI's activity line finds it. Tool OUTPUT is deliberately dropped: in
    brief mode cc_web filters out anything with `toolUseResult` anyway, and
    forging a claude tool_result pair here would be inventing structure to have
    it thrown away."""
    if not thread.get("rollout_path"):
        return [], -1
    r = codex.parse_rollout(thread["rollout_path"], since_ordinal)
    raw = r["entries"]
    # Whether the LAST turn is finished decides the stop_reason on the last
    # assistant entry. turn_state reads the same events the UI's spinner cares
    # about; ask it rather than re-deriving.
    idle = codex.turn_state(raw)["idle"]
    out: list[dict] = []
    for e in raw:
        kind, idx, ts = e.get("kind"), e.get("idx") or 0, e.get("ts") or ""
        if kind == "msg":
            text = (e.get("text") or "").strip()
            if not text:
                continue
            if e.get("role") == "user":
                out.append(_user(text, idx, ts))
            elif e.get("role") == "assistant":
                out.append(_assistant([{"type": "text", "text": text}], idx, ts, False))
        elif kind == "tool":
            out.append(_assistant([{
                "type": "tool_use",
                "id": e.get("call_id") or f"codex_{idx}",
                "name": e.get("tool") or "exec",
                "input": {"command": e.get("text") or ""},
            }], idx, ts, False))
    # Mark the final assistant entry done, once, at the end: doing it inline would
    # need to know which entry is last before the loop ends.
    if idle:
        for e in reversed(out):
            if e["type"] == "assistant":
                e["message"]["stop_reason"] = "end_turn"
                break
    return out, r["ordinal"]


def status_line(thread: dict, entries: list[dict]) -> str:
    """The bottom status bar. claude's comes from its own TUI; codex has no
    equivalent single line, so state the facts the UI has room for."""
    bits = [f"codex · {thread.get('cwd', '')}"]
    if thread.get("tokens_used"):
        bits.append(f"{thread['tokens_used']:,} tok")
    if thread.get("pane"):
        bits.append(f"pane {thread['pane']}")
    if not thread.get("live"):
        bits.append("not running (resume in a terminal to continue)")
    return " · ".join(bits)
