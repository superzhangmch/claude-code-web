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

import json
import re
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
                    **_exec_call_fields(payload, ptype)}]}}

    if ptype in ("custom_tool_call_output", "function_call_output"):
        out = _readable_output(payload)
        # Shaped like claude's tool result — a user entry carrying toolUseResult —
        # so brief mode drops it and medium/full show it, with no new rules.
        return {"type": "user", "timestamp": ts, "toolUseResult": {"stdout": out},
                "message": {"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": payload.get("call_id") or "codex_tool",
                    "content": out}]}}
    return None


# ---------------------------------------------------------------------------
# codex's one tool, made legible
# ---------------------------------------------------------------------------
# codex has a single tool, `exec`, whose input is a JavaScript SNIPPET that calls
# sub-tools:
#
#   const r = await tools.exec_command({"cmd":"date","workdir":"…",
#       "sandbox_permissions":"require_escalated",
#       "justification":"允许我在沙箱外运行只读的 date 命令吗?"});
#   text(r.output);
#
# Handed over as-is it makes the activity line a wall of JS with the interesting
# part — what it wants to run, and what it is asking permission for — buried in the
# middle. claude's tools each have their own fields, which is why cc_web's headline
# table works there; codex's have to be dug out of the script first.
_TOOLS_CALL_RE = re.compile(r"tools\.([A-Za-z_]\w*)\s*\(\s*(\{)")


def _exec_call_fields(payload: dict, ptype: str) -> dict:
    """{name, input} for a tool_use block, dug out of codex's exec script.

    Never lossy: the original script is kept under `script`, so the detail view
    shows exactly what ran even when this parsing guesses wrong — a prettier
    headline is not worth hiding what happened."""
    raw = codex._tool_input(payload)
    name = payload.get("name") or ptype
    inp: dict = {"script": raw}
    m = _TOOLS_CALL_RE.search(raw or "")
    if m is None:
        inp["command"] = raw
        return {"name": name, "input": inp}
    sub, arg = m.group(1), _first_json_object(raw, m.start(2))
    extra = m.string.count("tools.") - 1          # further calls in the same script
    if isinstance(arg, dict):
        # The identifying field, per sub-tool. `cmd` is what a person recognises for a
        # shell call; a path for anything file-shaped.
        for k in ("cmd", "command", "path", "file_path", "pattern", "query"):
            v = arg.get(k)
            if isinstance(v, str) and v.strip():
                inp["path" if k in ("path", "file_path") else "command"] = v.strip()
                break
        for k in ("justification", "workdir", "sandbox_permissions"):
            v = arg.get(k)
            if isinstance(v, str) and v.strip():
                inp[k] = v.strip()
    if extra > 0:
        inp["command"] = (inp.get("command") or sub) + f"  (+{extra} more)"
    return {"name": sub or name, "input": inp}


def _first_json_object(text: str, start: int):
    """The JSON object beginning at `start`, by brace matching.

    A regex cannot do this: the argument contains nested braces and strings with
    braces in them. Returns None rather than guessing if it does not parse."""
    depth, i, in_str, esc = 0, start, False, False
    while i < len(text):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                blob = text[start:i + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    pass
                # It is a JS object literal, not JSON: codex writes some calls with
                # BARE keys (`{path:"…", detail:"original"}`), which json refuses.
                # Quote the keys and retry once; give up rather than guess further.
                quoted = re.sub(r'([{,]\s*)([A-Za-z_]\w*)(\s*:)', r'\1"\2"\3', blob)
                try:
                    return json.loads(quoted)
                except json.JSONDecodeError:
                    return None
        i += 1
    return None


# codex wraps every exec result in three fixed lines:
#
#   Script completed|failed|running with cell ID N
#   Wall time 0.1 seconds
#   Output:
#   <the part anyone actually wants>
#
# and delivers it either as a plain string or as a list of {type, text} chunks. Left
# alone, the result panel shows a JSON array whose first element is boilerplate.
_OUT_HEAD_RE = re.compile(
    r"^\s*Script (?P<status>[a-z]+)[^\n]*\n\s*Wall time (?P<secs>[\d.]+) seconds\s*\n\s*Output:\s*\n?",
    re.I)


def _readable_output(payload: dict) -> str:
    """A tool result a person can read, without losing anything.

    The three wrapper lines become one short prefix ("failed · 0.0s") and the rest is
    passed through verbatim — nothing is dropped, because a result that looks tidy
    and hides the error is worse than an ugly one."""
    raw = payload.get("output")
    if isinstance(raw, list):
        parts = []
        for c in raw:
            if isinstance(c, dict) and isinstance(c.get("text"), str):
                parts.append(c["text"])
            elif isinstance(c, str):
                parts.append(c)
        text = "".join(parts)
    elif isinstance(raw, str):
        text = raw
    else:
        text = codex._tool_output(payload)
    m = _OUT_HEAD_RE.match(text or "")
    if m is None:
        return text
    body = text[m.end():]
    head = f"{m.group('status')} · {m.group('secs')}s"
    return f"{head}\n{body}" if body.strip() else head
