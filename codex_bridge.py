"""codex_bridge — codex sessions behind the SAME bridge interface as claude's.

This is the second of the two seams (the first is the transcript translation in
cc_web's _parse_jsonl_line). Everything cc_web does with a terminal goes through
`bridge`, so implementing that one interface makes codex "just be" a session to
every endpoint at once — /api/tabs, /api/sessions, /api/attach, /api/input,
/api/screen, /api/live, new-session, close-tab, resume — instead of each endpoint
needing its own branch. The point is not elegance: with per-endpoint branches
nobody can say which endpoints are covered, and the answer arrives one bug report
at a time. With an interface, coverage is countable — the methods below.

The pane-level half is delegated, not reimplemented: capturing a screen, sending
keys, reading the composer, renaming a window, resizing — those are properties of
a tmux pane and identical for either agent. What is genuinely codex-specific is
short:

  list_claude_tabs   which sessions exist, and where     (codex's sqlite + locks)
  send_text_to       `codex queue`, not keystrokes       (a supported door in)
  open_*_tab         `codex` / `codex resume <id>`
  list_all_tabs      the same panes, annotated as codex sessions

Everything else forwards to TmuxBridge.
"""
from __future__ import annotations

import asyncio
import shlex
from typing import Optional

import codex_backend as codex
from iterm_bridge import ClaudeSessionRef
from tmux_bridge import TmuxBridge, _run


class CodexBridge(TmuxBridge):
    """A TmuxBridge that reports codex sessions instead of claude ones."""

    # ---- sessions ---------------------------------------------------------
    async def list_claude_tabs(self) -> list[ClaudeSessionRef]:
        """Live codex sessions as session refs.

        `claude_session_id` carries the codex thread id, which is what makes the
        shared binding path work: _try_autobind matches a ref by that field, so
        attach/state/input/screen/live all resolve without a codex branch.

        Includes sessions that have no thread yet (a tab opened and left at its
        prompt — codex writes a thread row only on the first exchange) under their
        synthetic pending id, so they can be seen and typed into. Without them the
        first message is unreachable from a phone."""
        threads = await asyncio.to_thread(codex.list_threads, 60)
        # Same contract as the parent's: cc_web surfaces `last_error` when a tab
        # list comes back empty, to tell "nothing running" apart from "the terminal
        # is unreachable". An empty list is NOT an error here — no codex session
        # open is the normal state — so only a missing codex is reported.
        self.last_error = ("" if await asyncio.to_thread(codex.available)
                           else "codex 未安装或还没跑过 (找不到 ~/.codex/state_*.sqlite)")
        refs = []
        for i, t in enumerate(threads):
            if not t.get("live") or not t.get("pane"):
                continue
            refs.append(ClaudeSessionRef(
                iterm_session_id=t["pane"],
                tty="",                       # pane id is the handle; tty unused here
                pid=t.get("pid") or 0,
                cwd=t.get("cwd", ""),
                name=t.get("title") or "(codex)",
                window_index=0,
                tab_index=i,
                claude_session_id=t["thread_id"],
                title=t.get("title") or "",
            ))
        return refs

    async def list_claude_sessions(self) -> list[ClaudeSessionRef]:
        return await self.list_claude_tabs()

    async def list_all_tabs(self) -> list[dict]:
        """Every pane, annotated with which ones are codex sessions.

        The enumeration is the parent's — panes are panes. Only the annotation is
        ours, and that is the part that mattered: the claude annotation handed back
        CLAUDE session ids from a codex instance, which is how a claude session
        ended up offered inside the codex UI."""
        panes = await super().list_all_tabs()
        threads = await asyncio.to_thread(codex.list_threads, 60)
        by_pane = {t["pane"]: t for t in threads if t.get("pane") and t.get("live")}
        out = []
        for p in panes:
            t = by_pane.get(p.get("iterm_session_id", ""))
            out.append({**p,
                        "is_claude": t is not None,     # "is an agent session" here
                        "sid": t["thread_id"] if t else "",
                        "session_name": (t.get("title") or "") if t else ""})
        return out

    # ---- writing ----------------------------------------------------------
    # Two different operations that both happen to take a string, kept apart:
    #
    #   send_text_to     put these CHARACTERS on the terminal. Identical for either
    #                    agent — arrows, ESC, a keypress in the screen window — so it
    #                    is inherited, not overridden. Overriding it to mean "queue a
    #                    message" is what made typing in the screen window echo
    #                    nothing: every keypress became a queued message.
    #   deliver_message  get this MESSAGE to the agent. Same requirement for both
    #                    (don't clobber a half-typed line, no bracketed-paste or
    #                    `!`-prefix special cases, queue it if a turn is running) —
    #                    the difference is only that codex offers a door for it and
    #                    claude does not, so cc_web emulates one with keystroke
    #                    choreography there and calls this here.
    #
    # codex's log records nothing until a turn ends — no enqueue entry for a message
    # you just sent, unlike claude's jsonl. cc_web asks this to know whether it has to
    # echo the send itself. A fact about the LOG, not about typing.
    records_sent_messages = False

    # NOTE: an earlier version also exposed `deliver_message` / `delivers_messages`
    # so a codex message could go through `codex queue` instead of being typed. That
    # split is gone: driving a terminal is the same job for both agents, and the
    # queue door bought nothing the shared typing path does not already handle.
    # codex_backend.send_message still exists for cross-agent messaging, which is a
    # different job than "a human sent this from the web".

    async def send_keys_raw(self, pane: str, text: str) -> bool:
        """Retained name for the raw path; send_text_to now means exactly this."""
        return await TmuxBridge.send_text_to(self, pane, text)

    @staticmethod
    def _thread_for_pane(pane: str) -> Optional[dict]:
        return next((t for t in codex.list_threads(60)
                     if t.get("pane") == pane and t.get("live")), None)

    # ---- opening ----------------------------------------------------------
    async def open_new_claude_tab(self, cwd: str, label: str) -> Optional[str]:
        return await self._open_codex(cwd, label, None)

    async def open_resume_claude_tab(self, cwd: str, session_id: str,
                                     label: str) -> Optional[str]:
        return await self._open_codex(cwd, label, session_id)

    async def _open_codex(self, cwd: str, label: str,
                          resume_id: Optional[str]) -> Optional[str]:
        """A new tmux window running codex, mirroring the parent's claude version.

        Same login-shell dance for the same reason: `codex` is usually an npm
        install under ~/.nvm or ~/.local/bin, and a plain `bash -c` has neither on
        PATH. The pane is kept alive after codex exits (exec $SHELL) so it does not
        vanish out from under you."""
        # --approve-for-me is codex's equivalent of claude's "auto mode": approvals go
        # through automatic review instead of stopping to ask, with the
        # workspace-write sandbox. Without it a session opens in on-request +
        # read-only, which means every command waits for a human and nothing can be
        # written — measured on the existing sessions (approval_mode='on-request',
        # sandbox='{"type":"read-only"}').
        cmd = "codex --approve-for-me" + (f" resume {shlex.quote(resume_id)}" if resume_id else "")
        inner = (
            'export NVM_DIR="$HOME/.nvm"; '
            '[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"; '
            'export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"; '
            f'cd {shlex.quote(cwd)} 2>/dev/null; {cmd}; exec "$SHELL"'
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
                pane = q.stdout.strip().splitlines()[0]
                await self.maybe_accept_trust_prompt(pane)
                return pane
            return None
        r = await asyncio.to_thread(
            _run, ["new-window", "-t", "ccweb", "-n", label,
                   "-P", "-F", "#{pane_id}", "bash", "-lc", inner])
        if r and r.returncode == 0 and r.stdout.strip():
            pane = r.stdout.strip()
            # Inherited from TmuxBridge — codex opens on the same kind of blocking
            # "do you trust this directory?" question claude does.
            await self.maybe_accept_trust_prompt(pane)
            return pane
        return None

    async def resolve_session(self, sid: str):
        """The session ref an id refers to — including an id that has been superseded.

        A session with no thread yet is listed as `pending-pane-%N`; once codex writes
        its first record the real thread id takes over and the synthetic one stops
        appearing in any list. Whoever still holds it (an open page, a bookmarked URL,
        a binding made seconds ago) would then get "unknown session_id" for a session
        that is very much alive. Both names point at the same pane, so resolve through
        the pane and hand back the ref under its CURRENT id."""
        import codex_shim
        t = await asyncio.to_thread(codex_shim.find_thread, sid)
        pane = (t or {}).get("pane") or ""
        if not pane:
            return None
        for ref in await self.list_claude_tabs():
            if ref.iterm_session_id == pane:
                return ref
        return None
        import codex_shim
        t = codex_shim.find_thread(sid)
        return (t or {}).get("pane") or ""
