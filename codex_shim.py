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
    """A thread by id, and a PENDING id by the pane it stands for.

    A session with no thread yet is listed under a synthetic `pending-pane-%N`.
    The moment its first message lands, codex writes a real thread and the
    synthetic id would go dead — leaving whoever is looking at that URL stranded
    on a session that just came alive. So a pending id also resolves to whatever
    real thread now occupies that pane."""
    threads = codex.list_threads(200)
    hit = next((t for t in threads if t["thread_id"] == thread_id), None)
    if hit is not None and not hit.get("pending"):
        return hit
    if thread_id.startswith(codex.PENDING_PREFIX):
        pane = "%" + thread_id[len(codex.PENDING_PREFIX):]
        real = next((t for t in threads
                     if t.get("pane") == pane and not t.get("pending")), None)
        if real is not None:
            return real
    return hit


# ---------------------------------------------------------------------------
# rollout → claude-shaped entries
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The deep seam: one rollout LINE → one cc_web entry
# ---------------------------------------------------------------------------
# Translating here, inside the transcript reader, rather than in an endpoint is
# what makes codex "just be" a session to the rest of the server. Everything
# JsonlCache does then applies unchanged: byte-window reads, incremental append,
# _idx/_round numbering, the epoch, gap detection, load-earlier, /api/tool. The
# alternative — assembling a response per endpoint — is how you end up not knowing
# which endpoints you have covered.
#
# Necessarily per-line and stateless, because the reader hands out byte windows
# and may never have seen the start of the file. That rules out marking an earlier
# entry when a later `task_complete` arrives, so end-of-turn comes from the
# assistant message's own `phase == "final_answer"` — which is what it means.

def translate_line(row: dict):
    """One codex rollout record → one cc_web-shaped entry, or None to drop it.

    Dropped: session_meta, world_state, turn_context, reasoning, token counts,
    turn markers, the developer-role preambles, and the injected
    <environment_context> user turn — everything that is machinery rather than
    conversation. What survives is what a person said and what the agent said."""
    if not isinstance(row, dict):
        return None
    rtype, payload = row.get("type"), (row.get("payload") or {})
    if not isinstance(payload, dict):
        return None
    ts, ptype, role = row.get("timestamp") or "", payload.get("type"), payload.get("role")

    if rtype != "response_item":
        return None                      # event_msg / session_meta / world_state / …

    if ptype == "message":
        text = codex._text_of(payload)
        if not text or role == "developer":
            return None
        if role == "user":
            if codex._INJECTED.match(text):
                return None
            return {"type": "user", "timestamp": ts,
                    "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        if role == "assistant":
            msg = {"role": "assistant", "content": [{"type": "text", "text": text}]}
            if payload.get("phase") == "final_answer":
                # The turn's answer. cc_web's idle test reads exactly this field,
                # and a per-line translator cannot go back and mark it later.
                msg["stop_reason"] = "end_turn"
            return {"type": "assistant", "timestamp": ts, "message": msg}
        return None

    if ptype in ("custom_tool_call", "function_call", "local_shell_call"):
        return {"type": "assistant", "timestamp": ts,
                "message": {"role": "assistant", "content": [{
                    "type": "tool_use",
                    "id": payload.get("call_id") or "codex_tool",
                    "name": payload.get("name") or ptype,
                    "input": {"command": codex._tool_input(payload)}}]}}

    if ptype in ("custom_tool_call_output", "function_call_output"):
        out = codex._tool_output(payload)
        # Shaped like claude's tool result — a user entry carrying toolUseResult —
        # so brief mode drops it and medium/full show it, with no new rules.
        return {"type": "user", "timestamp": ts, "toolUseResult": {"stdout": out},
                "message": {"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": payload.get("call_id") or "codex_tool",
                    "content": out}]}}
    return None
