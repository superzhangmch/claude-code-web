"""Bridge to tmux (Linux): mirrors ItermBridge's interface using the `tmux`
CLI + /proc, so cc_web runs unchanged on a Linux box.

Design mirror of iterm_bridge, minus the macOS iTerm2 websocket API:

  - "iterm_session_id" (the opaque handle cc_web stores in a binding and passes
    back to get_screen_for / send_text_to) == a tmux PANE id, e.g. "%3".
  - claude-session pairing reuses the exact same tty→pid→cwd→(--resume argv)
    chain as the iTerm bridge; only the OS probes differ (ps/proc vs iTerm API).
  - Pure, platform-agnostic helpers (_resume_sid_from_cmd, _is_claude_cmd,
    _pid_start_time, _strip_input_area, ClaudeSessionRef) are imported straight
    from iterm_bridge — single source of truth, no duplication.

Constraint: claude must run INSIDE a tmux pane (there is no scriptable API for a
bare Linux terminal emulator). A claude started in a raw terminal can't be
attached — same spirit as needing iTerm on macOS.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from typing import Optional

from iterm_bridge import (        # pure, iterm2-free helpers — reused as-is
    ClaudeSessionRef,
    _is_claude_cmd,
    _resume_sid_from_cmd,
    _strip_input_area,
)

_TMUX = "tmux"
_CLI_TIMEOUT = 5


def _run(args: list[str], timeout: int = _CLI_TIMEOUT):
    """Run a tmux subcommand; return CompletedProcess or None on failure."""
    try:
        return subprocess.run([_TMUX, *args], capture_output=True,
                              text=True, timeout=timeout)
    except Exception:
        return None


def _norm_tty(tty: str) -> str:
    """Normalize a Linux tty to a comparable id: '/dev/pts/3' → 'pts/3',
    'pts/3' → 'pts/3'. (tmux `pane_tty` prints '/dev/pts/3'; `ps -o tty` prints
    'pts/3'.)"""
    t = (tty or "").strip()
    if t.startswith("/dev/"):
        t = t[len("/dev/"):]
    return t


def _claude_procs_by_tty() -> dict[str, tuple[int, str]]:
    """ONE `ps` over all processes → {norm_tty: (pid, resume_sid)} for every
    process whose argv looks like a `claude` invocation. Mirror of the iTerm
    bridge's helper, but Linux `ps`."""
    res: dict[str, tuple[int, str]] = {}
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,tty=,args="],
            capture_output=True, text=True, timeout=4,
        ).stdout
    except Exception:
        return res
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, tty, args = parts
        if tty in ("?", "-", ""):
            continue
        if not _is_claude_cmd(args):
            continue
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        # first (parent-most) claude per tty wins, like the iTerm bridge
        res.setdefault(_norm_tty(tty), (pid, _resume_sid_from_cmd(args)))
    return res


def _pid_cwd(pid: int) -> str:
    """cwd of a pid via /proc; '' on failure."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def _panes() -> list[dict]:
    """Every tmux pane across all sessions/windows. [] if no tmux server."""
    fmt = ("#{pane_id}\t#{pane_tty}\t#{window_index}\t"
           "#{window_name}\t#{session_name}\t#{pane_active}\t#{pane_title}")
    r = _run(["list-panes", "-a", "-F", fmt])
    if r is None or r.returncode != 0:
        return []
    panes: list[dict] = []
    for line in r.stdout.splitlines():
        # maxsplit=6 → 7 fields; pane_title is last so any tab INSIDE the
        # OSC-set title stays intact (unbounded split would truncate it).
        f = line.split("\t", 6)
        if len(f) < 6:
            continue
        # tab display name: prefer pane_title (claude sets it via the OSC title
        # escape, e.g. "✳ Locate SAS…") over window_name (just the launch label
        # like "resume_xxxx"/"claude"). Mirrors iTerm's session.name behaviour.
        window_name = f[3]
        pane_title = f[6] if len(f) > 6 else ""
        panes.append({
            "pane_id": f[0],
            "tty": f[1],
            "window_index": f[2],
            "window_name": pane_title.strip() or window_name,
            "session_name": f[4],
            "active": f[5] == "1",
        })
    return panes


class TmuxBridge:
    """Same public surface cc_web calls on the iTerm bridge:
    connect / ensure_connected / list_claude_tabs / list_all_tabs /
    get_screen_for / send_text_to / open_resume_claude_tab / open_new_claude_tab.
    """

    def __init__(self) -> None:
        self._send_lock = asyncio.Lock()

    # tmux is a stateless CLI — there's no persistent connection to hold open.
    async def connect(self) -> None:
        return

    async def ensure_connected(self) -> None:
        return

    async def list_claude_tabs(self) -> list[ClaudeSessionRef]:
        panes = await asyncio.to_thread(_panes)
        procs = await asyncio.to_thread(_claude_procs_by_tty)
        refs: list[ClaudeSessionRef] = []
        for idx, p in enumerate(panes):
            key = _norm_tty(p["tty"])
            hit = procs.get(key)
            if hit is None:
                continue
            pid, resume_sid = hit
            cwd = await asyncio.to_thread(_pid_cwd, pid)
            if not cwd:
                continue
            refs.append(ClaudeSessionRef(
                iterm_session_id=p["pane_id"],
                tty=p["tty"],
                pid=pid,
                cwd=cwd,
                name=p["window_name"] or "",
                window_index=int(p["window_index"] or 0),
                tab_index=idx,
                claude_session_id=resume_sid,   # ground truth from --resume argv
            ))
        return refs

    # Back-compat alias (old code path).
    async def list_claude_sessions(self) -> list[ClaudeSessionRef]:
        return await self.list_claude_tabs()

    async def list_all_tabs(self) -> list[dict]:
        panes = await asyncio.to_thread(_panes)
        claude_ttys = set((await asyncio.to_thread(_claude_procs_by_tty)).keys())
        out: list[dict] = []
        for idx, p in enumerate(panes):
            out.append({
                "iterm_session_id": p["pane_id"],
                "name": p["window_name"] or "",
                "window_index": int(p["window_index"] or 0),
                "tab_index": idx,
                "is_claude": _norm_tty(p["tty"]) in claude_ttys,
            })
        return out

    async def get_screen_for(self, iterm_session_id: str, max_lines: int = 80,
                             refresh: bool = False,
                             strip_input: bool = True,
                             scrollback: bool = False,
                             with_cursor: bool = False):
        """Capture the pane's text. `scrollback` reads `max_lines` of history
        above the visible grid; otherwise just the current screen. `strip_input`
        drops claude's Ink input box + footer (reuses the iTerm bridge's parser).
        `refresh` is a no-op for tmux (capture-pane always reflects live state).

        `with_cursor` → return (text, cursor) where cursor is (row, col, vis):
        the pane cursor from a SEPARATE display-message call (capture-pane has no
        cursor). Only for the visible grid (not scrollback); row is 0-based from
        the top of the visible pane, matching the captured lines."""
        args = ["capture-pane", "-p", "-t", iterm_session_id]
        if scrollback:
            args += ["-S", f"-{max_lines}"]
        r = await asyncio.to_thread(_run, args)
        if r is None or r.returncode != 0:
            return (None, None) if with_cursor else None
        lines = [ln.rstrip() for ln in r.stdout.split("\n")]
        cursor = None
        if with_cursor and not scrollback:
            cursor = await self.get_cursor_for(iterm_session_id)
        if strip_input:
            lines = _strip_input_area(lines)
        while lines and not lines[-1]:
            lines.pop()
        text = "\n".join(lines[-max_lines:])
        return (text, cursor) if with_cursor else text

    async def get_cursor_for(self, iterm_session_id: str):
        """(row, col, vis) of the pane cursor within the visible grid, or None.
        vis = tmux #{cursor_flag} (0 while claude hides the cursor)."""
        r = await asyncio.to_thread(
            _run, ["display-message", "-p", "-t", iterm_session_id,
                   "#{cursor_y}\t#{cursor_x}\t#{cursor_flag}"])
        if r is None or r.returncode != 0:
            return None
        try:
            y, x, vis = r.stdout.strip().split("\t")
            return (int(y), int(x), int(vis))
        except (ValueError, AttributeError):
            return None

    async def input_typed_text(self, iterm_session_id: str) -> str:
        """The text the human has actually TYPED into the input box, excluding
        the greyed autosuggest/placeholder ghost. Uses tmux's OWN cursor
        position (#{cursor_x}/#{cursor_y}) — structural, no assumption about how
        the terminal renders the cursor/ghost. Real input = the cursor line up to
        the cursor column (the ghost lives after the cursor), minus the prompt."""
        pos = await asyncio.to_thread(
            _run, ["display-message", "-p", "-t", iterm_session_id,
                   "#{cursor_x}\t#{cursor_y}"])
        if pos is None or pos.returncode != 0:
            return ""
        try:
            cx, cy = (int(v) for v in pos.stdout.strip().split("\t"))
        except (ValueError, TypeError):
            return ""
        cap = await asyncio.to_thread(_run, ["capture-pane", "-p", "-t", iterm_session_id])
        if cap is None or cap.returncode != 0:
            return ""
        lines = cap.stdout.split("\n")
        if not (0 <= cy < len(lines)):
            return ""
        pre = lines[cy][:cx].lstrip()
        if pre[:1] in ("❯", ">"):        # drop the prompt glyph if present
            pre = pre[1:]
        return pre.strip()

    async def set_tab_name(self, iterm_session_id: str, name: str) -> bool:
        """Set the pane title (the tmux analog of the tab-name we read back in
        _panes). Best-effort: claude re-emits its OSC title periodically, so this
        may be overwritten — unlike iTerm's sticky tab-title override."""
        r = await asyncio.to_thread(
            _run, ["select-pane", "-t", iterm_session_id, "-T", name])
        return r is not None and r.returncode == 0

    async def resize_cols(self, iterm_session_id: str, dcols: int):
        """Read the pane's window size; if dcols != 0, widen/narrow COLUMNS so
        claude reflows (needs window-size manual, else the client size wins).
        dcols == 0 → read only. `iterm_session_id` is a pane id (%N). {cols,rows}|None."""
        pane = iterm_session_id
        info = _run(["display-message", "-p", "-t", pane,
                     "#{window_id} #{window_width} #{window_height}"])
        if not info or info.returncode != 0 or not info.stdout.strip():
            return None
        try:
            win, w, h = info.stdout.split()[:3]
            w, h = int(w), int(h)
        except Exception:
            return None
        cols = w
        if dcols:
            cols = max(20, min(400, w + dcols))
            _run(["set-option", "-w", "-t", win, "window-size", "manual"])
            _run(["resize-window", "-t", win, "-x", str(cols), "-y", str(h)])
        return {"cols": cols, "rows": h}

    async def send_text_to(self, iterm_session_id: str, text: str) -> bool:
        """Inject keystrokes into a pane. A trailing CR is treated as a submit:
        send the body, small delay so the TUI's input renderer absorbs it, then
        the CR alone — same gesture as the iTerm bridge. Serialized so rapid
        Sends don't interleave."""
        async with self._send_lock:
            if text.endswith("\r"):
                body = text[:-1]
                if body:
                    if not await self._send_bytes(iterm_session_id, body):
                        return False
                    await asyncio.sleep(0.1)
                ok = await self._send_bytes(iterm_session_id, "\r")
                await asyncio.sleep(0.4)   # let Ink process submit + clear input
                return ok
            return await self._send_bytes(iterm_session_id, text)

    async def _send_bytes(self, pane: str, text: str) -> bool:
        # send-keys -H <hex...> delivers exact bytes to the pty, so ESC
        # sequences (e.g. arrow-key menu nav "\x1b[B") and CR pass through
        # verbatim — the tmux analog of iTerm's async_send_text.
        # CHUNK it: one hex arg per byte, so a big multi-line paste (several KB of
        # CJK = thousands of args) would overflow a single send-keys' argv and the
        # call fails — surfacing as the misleading "iterm session vanished". Split
        # into ≤_SEND_CHUNK-byte batches; the pty reassembles them (a bracketed
        # paste split across batches still lands as one paste).
        try:
            raw = text.encode("utf-8")
        except Exception:
            return False
        if not raw:
            return True
        _SEND_CHUNK = 400
        for i in range(0, len(raw), _SEND_CHUNK):
            hexpairs = [f"{b:02x}" for b in raw[i:i + _SEND_CHUNK]]
            r = await asyncio.to_thread(_run, ["send-keys", "-t", pane, "-H", *hexpairs])
            if r is None or r.returncode != 0:
                return False
        return True

    async def open_resume_claude_tab(self, cwd: str, session_id: str,
                                     label: str) -> Optional[str]:
        return await self._open(cwd, label, session_id)

    async def open_new_claude_tab(self, cwd: str, label: str) -> Optional[str]:
        return await self._open(cwd, label, None)

    async def _open(self, cwd: str, label: str,
                    resume_id: Optional[str]) -> Optional[str]:
        """Open a new tmux window running claude, return its pane id.

        The command runs in a login shell that SOURCES nvm first, so `claude`
        (commonly installed under ~/.nvm/.../bin) is on PATH — a plain
        `bash -lc` misses it because nvm lives in .bashrc (interactive only).
        Keeps the pane alive after claude exits (exec $SHELL) so it doesn't
        vanish. All windows hang off a dedicated detached 'ccweb' session."""
        claude = "claude" + (f" --resume {shlex.quote(resume_id)}" if resume_id else "")
        inner = (
            'export NVM_DIR="$HOME/.nvm"; '
            '[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"; '
            f'cd {shlex.quote(cwd)} 2>/dev/null; {claude}; exec "$SHELL"'
        )
        has = await asyncio.to_thread(_run, ["has-session", "-t", "ccweb"])
        if has is None or has.returncode != 0:
            r = await asyncio.to_thread(
                _run, ["new-session", "-d", "-s", "ccweb", "-n", label,
                       "-x", "220", "-y", "50", "bash", "-lc", inner])
            if r is None or r.returncode != 0:
                return None
            q = await asyncio.to_thread(
                _run, ["list-panes", "-t", "ccweb", "-F", "#{pane_id}"])
            if q and q.returncode == 0 and q.stdout.strip():
                return q.stdout.strip().splitlines()[0]
            return None
        r = await asyncio.to_thread(
            _run, ["new-window", "-t", "ccweb", "-n", label,
                   "-P", "-F", "#{pane_id}", "bash", "-lc", inner])
        if r and r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        return None
