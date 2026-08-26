"""Bridge to iTerm2: find the session running `claude`, read screen, send text."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# iterm2 is macOS-only. Import tolerantly so this module (and its pure,
# platform-agnostic helpers — _resume_sid_from_cmd, _is_claude_cmd,
# _pid_start_time, _strip_input_area, ClaudeSessionRef) can be imported on
# Linux too, where cc_web uses the tmux bridge instead. On macOS nothing
# changes: the import succeeds and ItermBridge works exactly as before.
try:
    import iterm2
    import iterm2.app   # for invalidate_app (see list_claude_tabs)
except Exception:  # pragma: no cover - Linux / iterm2 not installed
    iterm2 = None

# iTerm2 RPC hard timeout: a hung/half-open websocket await must never wedge the
# asyncio event loop (which would freeze every concurrent request).
log = logging.getLogger("ccweb")   # same logger cc_web uses, so these land in its log

_RPC_TIMEOUT = 5
_CONNECT_TRIES = 4          # a just-restarted iTerm2 needs a moment before its API listens


class BridgeUnavailable(RuntimeError):
    """iTerm2's Python API could not be reached: the app isn't running, the API is
    switched off, it was restarted under us, or it is wedged. Raised instead of letting
    a websockets/asyncio internal ("no close frame received or sent") reach the UI."""


def bridge_reason(exc, term: str = "iTerm2") -> str:
    """One actionable sentence for the UI. The library's own message is useless to a user:
    a deadlocked iTerm2 surfaces as "no close frame received or sent".

    `term` is the terminal this host actually uses — the text is shown on Linux/tmux hosts
    too, where naming iTerm2 was simply wrong (and confusing: it made a tmux problem look
    like someone else's).
    """
    name = type(exc).__name__ if exc is not None else ""
    if isinstance(exc, asyncio.TimeoutError):
        return f"{term} 没有响应(可能卡死) — 用 ⚙ 里的 reconnect,或重启 {term}"
    if name in ("ConnectionClosedError", "ConnectionClosedOK", "ConnectionClosed",
                "IncompleteReadError"):
        return f"与 {term} 的连接已断开({term} 被重启过?) — 用 ⚙ 里的 reconnect 重连"
    if isinstance(exc, (ConnectionRefusedError, FileNotFoundError, OSError)):
        extra = ("(iTerm2 没运行,或 Preferences > General > Magic 里的 Python API 没开)"
                 if term == "iTerm2" else "(它没在运行?)")
        return f"连不上 {term} {extra}"
    return f"{term} bridge 出错: {name}"


# Session-variable reads that failed outright. Not cosmetic: while this is climbing, the
# tab list — and every snapshot taken from it — is quietly incomplete.
_gv_fail = 0


async def _gv(session, var, timeout=5, tries=2):
    """Read an iTerm2 session variable with a hard timeout; None on any failure.

    Retried, and the failure COUNTED, because None here is indistinguishable from a real
    answer while the consequence is silent: `tty` and `jobPid` are the only two ways a tab
    is recognised as running claude, so one hiccup on one session demotes a live claude tab
    to a plain shell — it drops out of the session list and out of the periodic snapshot,
    with nothing logged anywhere. That is how a tab whose claude had been running since
    Aug 20 came and went from the list three times in one morning, and why pinning it down
    needed process start times instead of a log line.
    """
    global _gv_fail
    for attempt in range(tries):
        try:
            return await asyncio.wait_for(session.async_get_variable(var), timeout)
        except Exception as e:
            if attempt == tries - 1:
                _gv_fail += 1
                log.warning("iTerm2 session variable %r unreadable after %d tries: %s",
                            var, tries, e)
    return None


# Connections we opened minus connections we actually closed. list_claude_tabs() opens a
# fresh one per call, so a close that silently does nothing is invisible until something
# far away falls over — which is exactly what happened: see _close_conn.
_conn_open = 0
_conn_closed = 0


async def _close_conn(old, current) -> None:
    """Really close a superseded iTerm2 connection: cancel its tasks, close its socket.

    The previous version probed for `async_close` and skipped if absent — and iterm2
    2.19's Connection has NO close method at all, not even a private one. So every
    superseded connection stayed open, one per enumeration (~48/day from the snapshot
    timer alone, plus every picker load, ⇆ click and attach). After five days that was
    ~500 live unix sockets on each side, and iTerm2's API server had accumulated enough
    per-connection state to deadlock the whole app — twice, once needing every claude
    session restarted. The leaked dispatchers were also the source of the
    _async_dispatch_forever/ConnectionClosedError traceback spam in the log.

    Connection exposes no API for this, so reach into what it does have: the futures it
    keeps (the forever-dispatcher and its helper tasks) and `websocket`. Found by type
    rather than by name so name mangling and version renames can't quietly turn this
    back into a no-op.
    """
    global _conn_closed
    if old is None or old is current:
        return
    # Cancel first: closing the socket underneath a running dispatcher is what makes it
    # raise ConnectionClosed into the log.
    for val in list(vars(old).values()):
        if isinstance(val, asyncio.Future) and not val.done():
            val.cancel()
        elif isinstance(val, list):
            for t in val:
                if isinstance(t, asyncio.Future) and not t.done():
                    t.cancel()
    ws = getattr(old, "websocket", None)
    closer = getattr(ws, "close", None)
    if closer is None:
        log.warning("iTerm2 connection has no websocket to close (%r) — leaking it", type(old))
        return
    try:
        res = closer()
        if inspect.isawaitable(res):
            await asyncio.wait_for(res, _RPC_TIMEOUT)
        _conn_closed += 1
    except Exception as e:
        log.warning("closing a superseded iTerm2 connection failed: %s", e)
    live = _conn_open - _conn_closed
    if live > 5:
        # One per in-flight call is normal; a growing count means the close path broke
        # again, and this is the only place that would notice before iTerm2 dies.
        log.warning("iTerm2 connections opened but not closed: %d — leaking", live)


@dataclass
class ClaudeSessionRef:
    iterm_session_id: str
    tty: str
    pid: int
    cwd: str
    name: str = ""
    window_index: int = 0
    tab_index: int = 0
    claude_session_id: str = ""  # filename stem of the JSONL the claude process is writing
    title: str = ""              # user-assigned label from ~/.claude/session_index.json


class ItermBridge:
    def __init__(self) -> None:
        self.connection: Optional[iterm2.Connection] = None
        self.app: Optional[iterm2.App] = None
        self._lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        # Held for a WHOLE enumeration, not just the connection swap. _fresh_app takes
        # _lock, swaps self.connection and CLOSES the old one, then releases — while the
        # caller goes on reading session variables over the connection it was handed. A
        # second enumeration arriving in that window closes that connection mid-read, the
        # reads fail, and the tabs they belonged to are reported as not running claude.
        # See _gv: that is how live claude tabs came and went from the list.
        self._enum_lock = asyncio.Lock()
        # Why the last connection attempt failed, "" when the last one worked. The
        # endpoints report it, because an unreachable iTerm2 and "no claude tab open"
        # both produce an empty tab list and used to be indistinguishable on screen.
        self.last_error: str = ""
        # Tabs the LAST enumeration could not read either key for. >0 means the tab list
        # it returned is incomplete in a way that looks exactly like "fewer claude tabs".
        self.last_probe_blind: int = 0

    async def connect(self) -> None:
        self.connection = await iterm2.Connection.async_create()
        self.app = await iterm2.async_get_app(self.connection)

    def drop(self) -> None:
        """Throw away the cached connection so the next call builds a fresh one.
        Used by the manual reconnect: a connection to an iTerm2 that has since been
        restarted looks alive until you actually send on it."""
        old, self.connection, self.app = self.connection, None, None
        if old is not None:
            try:
                close = getattr(old, "async_close", None)
                if close:
                    asyncio.get_event_loop().create_task(close())
            except Exception:
                pass

    async def _connect_retry(self) -> None:
        """Connect, retrying a few times with backoff.

        This used to be a single bare `await self.connect()` outside any try, so one
        transient failure — most commonly iTerm2 having been relaunched seconds earlier,
        its API server not listening yet — propagated a raw websockets error all the way
        into a "resume failed" dialog, and nothing recovered until cc_web was restarted.
        """
        last: Optional[BaseException] = None
        for i in range(_CONNECT_TRIES):
            try:
                await asyncio.wait_for(self.connect(), _RPC_TIMEOUT)
                self.last_error = ""
                return
            except Exception as e:
                last = e
                self.connection = None
                self.app = None
                if i + 1 < _CONNECT_TRIES:
                    await asyncio.sleep(0.4 * (i + 1))
        self.last_error = bridge_reason(last)
        raise BridgeUnavailable(self.last_error) from last

    async def wait_ready(self, timeout: float = 20.0) -> bool:
        """Block until the API actually answers, up to `timeout`. Checking that the
        iTerm2 PROCESS exists is not enough — its API server starts a beat later, and
        anything issued in that window fails."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            try:
                await self.ensure_connected()
                return True
            except Exception:
                if asyncio.get_event_loop().time() >= deadline:
                    return False
                await asyncio.sleep(0.5)

    async def ensure_connected(self) -> None:
        """Lazy connect + reconnect-on-failure. Serialized to avoid duplicate connects.

        The iterm2 App caches windows/sessions locally and keeps itself current
        via a layout-change subscription — but that subscription can silently
        miss tab-creation events, leaving app.windows STALE (it happily reports
        3 tabs while 5 exist). async_get_app on the same connection returns that
        same stale singleton, so we additionally call app.async_refresh() to
        force a fresh roundtrip of the full window/tab/session hierarchy. That
        roundtrip also doubles as the dead-connection check.
        """
        async with self._lock:
            if self.app is not None:
                try:
                    self.app = await asyncio.wait_for(
                        iterm2.async_get_app(self.connection), _RPC_TIMEOUT)
                    await asyncio.wait_for(self.app.async_refresh(), _RPC_TIMEOUT)
                    self.last_error = ""   # the connection answered; drop any stale reason
                    return
                except Exception:
                    self.connection = None
                    self.app = None
            await self._connect_retry()

    async def _claude_by_session(self, flat) -> dict[int, tuple[str, int, str]]:
        """{index into flat: (tty, pid, resume_sid)} for the sessions running a claude.

        TWO keys, and the second one is not optional: after iTerm2 restores a window
        (its crash-recovery path, which runs after any hard restart) the restored
        sessions report `tty` = None — not an error, just empty — while ps still shows
        every claude on its original tty. tty-only matching then finds NOTHING, which is
        how cc_web came to report "no claude tab" with 15 working tabs on screen, and how
        the >_ list came to show 14 of 15 claude tabs as plain shells.

        `jobPid` is the session's foreground job, matched by identity rather than by a
        string iTerm2 may or may not fill in — but the foreground job is not always
        claude ITSELF — while it works, claude keeps a `caffeinate -i -t 300` child in
        the foreground — so an unmatched job is traced up the process tree to its parent
        (see claude_above).
        """
        procs, parent, pid_tty = await asyncio.to_thread(_ps_scan)
        by_pid = {pid: (tty, pid, sid) for tty, (pid, sid) in procs.items()}

        def claude_above(pid: Optional[int]) -> Optional[tuple[str, int, str]]:
            """The claude at or above `pid` in the process tree.

            jobPid is the tab's foreground JOB, and while claude works that is usually
            not claude itself but a child it spawned — `caffeinate -i -t 300`, whose ppid
            IS claude. Matching by identity missed it, and on a restored window (no tty)
            jobPid is the only key, so the tab dropped off the list for as long as the
            child lived. A few hops, because the job can be a grandchild. It can only
            ever return a pid that IS a live foreground claude, so it cannot invent a
            match, and the hop limit means a broken parent chain can't spin.
            """
            hops = 0
            while pid and hops < 8:
                if pid in by_pid:
                    return by_pid[pid]
                nxt = parent.get(pid)
                if nxt is None or nxt == pid or nxt <= 1:
                    return None
                pid, hops = nxt, hops + 1
            return None

        def claude_on_job_tty(pid: Optional[int]) -> Optional[tuple[str, int, str]]:
            """The claude sharing a TERMINAL with `pid`, whatever `pid` is.

            The backstop for when walking up doesn't get there: a job that is not a
            descendant of claude, or whose parent chain `ps` didn't capture. Every line of
            `ps` carries the terminal the process is on, and one terminal is one tab — so
            the job's own tty leads back to the tab regardless of how it's related. Safe
            by construction: `procs` only holds claudes that are in the FOREGROUND on
            their terminal, so a Ctrl+Z'd claude is not claimed here.
            """
            t = pid_tty.get(pid) if pid else None
            if t and t in procs:
                cpid, sid = procs[t]
                return (t, cpid, sid)
            return None

        ttys, jobpids = await asyncio.gather(
            asyncio.gather(*[_gv(s, "tty") for _, _, s in flat]),
            asyncio.gather(*[_gv(s, "jobPid") for _, _, s in flat]),
        )
        hits: dict[int, tuple[str, int, str]] = {}
        blind: list[int] = []
        unmatched: list[str] = []
        for i, (tty, jp) in enumerate(zip(ttys, jobpids)):
            key = _norm_tty(tty or "")
            if tty and key in procs:
                pid, sid = procs[key]
                hits[i] = (tty, pid, sid)
                continue
            try:
                jp = int(jp) if jp is not None else None
            except (TypeError, ValueError):
                jp = None
            # jobPid IS claude, usually. When it isn't — a caffeinate, a tool call's
            # child — claude is above it in the tree; failing that, claude is at least on
            # the same terminal, which is the same tab.
            found = claude_above(jp) or claude_on_job_tty(jp)
            if found:
                tty_key, pid, sid = found
                hits[i] = (tty or tty_key, pid, sid)
                continue
            if tty is None and jp is None:
                # Neither key readable: this tab is UNKNOWN, not "a plain shell". Saying
                # nothing here is what made a dropped tab look like a tab without claude.
                blind.append(i)
            else:
                unmatched.append("w%dt%d(tty=%s,jobPid=%s,known_to_ps=%s)"
                                 % (flat[i][0] + 1, flat[i][1] + 1, tty, jp,
                                    jp in parent if jp else "n/a"))
        # Only meaningful together with the answer: N tabs we could not ask about, out of
        # M. A snapshot taken from a degraded enumeration is short a session, and resuming
        # from it silently doesn't restore that one.
        self.last_probe_blind = len(blind)
        if blind:
            log.warning("iTerm2: %d/%d tabs answered neither tty nor jobPid — "
                        "they are reported as non-claude, which may be wrong "
                        "(window/tab: %s)",
                        len(blind), len(flat),
                        # 1-based, so it reads as the same wXtY the UI shows.
                        ", ".join("w%dt%d" % (flat[i][0] + 1, flat[i][1] + 1)
                                  for i in blind))
        if unmatched:
            # A tab that answered, but whose answer led nowhere. Distinct from `blind`
            # (couldn't ask) and from a plain shell (answered, and it really is a shell).
            log.info("iTerm2: not claude, per its own answer: %s", ", ".join(unmatched))
        return hits

    async def _fresh_app(self) -> None:
        """Point self.connection/self.app at a brand-new connection, and close the old one.

        Both enumerations need this and both used to inline it — which is how the second
        copy silently kept the bug the first one had fixed. Three steps that only work
        together:

          1. a new connection, because a long-lived one's App goes stale (its layout
             subscription misses tab creation and async_refresh() doesn't un-stick it);
          2. invalidate_app() BEFORE async_get_app(), because iterm2 caches App as a
             global singleton bound to the connection it was built on — without this,
             step 1 gets you the old App refreshed over a socket we are about to close,
             so enumeration answers once and then returns nothing;
          3. close the superseded connection (see _close_conn), or one websocket leaks
             per call — ~500 in five days, which deadlocked iTerm2 outright, twice.

        Leaves self.app None if any of it fails; callers treat that as "no tabs".
        """
        global _conn_open
        async with self._lock:
            old = self.connection
            try:
                _conn_open += 1
                self.connection = await asyncio.wait_for(
                    iterm2.Connection.async_create(), _RPC_TIMEOUT)
                iterm2.app.invalidate_app()
                self.app = await asyncio.wait_for(
                    iterm2.async_get_app(self.connection), _RPC_TIMEOUT)
                # Force a full layout fetch — in some event-loop contexts async_get_app
                # returns before the window model is fully populated, yielding a partial
                # tab list.
                await asyncio.wait_for(self.app.async_refresh(), _RPC_TIMEOUT)
            except Exception as e:
                self.connection = None
                self.app = None
                # Remember WHY: callers can only see an empty list, and an unreachable
                # iTerm2 must not read as "you have no tabs open".
                self.last_error = bridge_reason(e)
            await _close_conn(old, self.connection)
        if self.app is not None:
            self.last_error = ""

    async def list_claude_tabs(self) -> list[ClaudeSessionRef]:
        """Enumerate all iTerm2 tabs with a live foreground `claude` process.

        Pure enumeration — does NOT pair to a session_id. Pairing is done
        lazily by the server's attach flow with explicit screen-content scoring.

        Builds a FRESH connection every call. The long-lived connection's App
        singleton goes stale — its layout subscription silently misses
        tab-creation events and app.async_refresh() does NOT un-stick it once
        established (observed: a server that connected while 3 tabs existed kept
        reporting 3 even after 2 more tabs opened). Enumeration is only triggered
        by user actions (attach / sessions list / resume / new tab), so the extra
        connect (~100-200 ms) is fine here.

        Getting a usable App on that new connection is subtler than it looks — see
        _fresh_app(), which both enumerations share for exactly that reason.

        Serialized end-to-end (_enum_lock): the connection this reads from is shared
        mutable state, and a concurrent enumeration closes it out from under us."""
        async with self._enum_lock:
            return await self._list_claude_tabs_locked()

    async def _list_claude_tabs_locked(self) -> list[ClaudeSessionRef]:
        await self._fresh_app()
        if self.app is None:
            return []
        self.last_error = ""
        procs = await asyncio.to_thread(_claude_procs_by_tty)   # ps off the loop
        flat = [(wi, ti, session)
                for wi, window in enumerate(self.app.windows)
                for ti, tab in enumerate(window.tabs)
                for session in tab.sessions]

        # Which sessions run a claude — see _claude_by_session (tty, then jobPid).
        found = await self._claude_by_session(flat)
        hits = [(wi, ti, s, found[i][0], (found[i][1], found[i][2]))
                for i, (wi, ti, s) in enumerate(flat) if i in found]
        # tab.title = what iTerm shows on the TAB STRIP (a manual "Edit Tab
        # Title" override like "SAS-eval", else it falls back to session.name /
        # claude's OSC title). That's the label the user recognizes, so use it as
        # the tab-name; coalesce to session.name if a tab ever lacks it.
        titles = await asyncio.gather(*[_gv(s, "tab.title") for _, _, s, _, _ in hits])
        snames = await asyncio.gather(*[_gv(s, "session.name") for _, _, s, _, _ in hits])
        names = [t or n or "" for t, n in zip(titles, snames)]
        # lsof (cwd) off the loop AND concurrently, not one-at-a-time.
        cwds = await asyncio.gather(*[asyncio.to_thread(_pid_cwd, h[4][0]) for h in hits])
        refs: list[ClaudeSessionRef] = []
        dropped: list[str] = []
        for (wi, ti, session, tty, (pid, resume_sid)), name, cwd in zip(hits, names, cwds):
            if not cwd:
                # Every drop here is a live claude tab that will be absent from the
                # session list, the brief list and the snapshot. Silent until now, which
                # is why "why did t6 disappear" cost an afternoon of black-box probing.
                dropped.append("w%dt%d(pid=%s,tty=%s)" % (wi + 1, ti + 1, pid, tty))
                continue
            refs.append(ClaudeSessionRef(
                iterm_session_id=session.session_id,
                tty=tty,
                pid=pid,
                cwd=cwd,
                name=name or "",
                window_index=wi,
                tab_index=ti,
                claude_session_id=resume_sid,   # ground truth from --resume argv
            ))
        # One line per enumeration, so "the list is short" can be traced to WHERE it got
        # short — sessions seen, claude matched, tabs dropped — instead of inferred from
        # process start times hours later.
        log.info("enum claude tabs: %d session(s), %d matched, %d returned%s",
                 len(flat), len(hits), len(refs),
                 (" — DROPPED (no cwd): " + ", ".join(dropped)) if dropped else "")
        return refs

    # Backward-compat shim — old code still calls this name.
    async def list_claude_sessions(self) -> list[ClaudeSessionRef]:
        return await self.list_claude_tabs()

    async def list_all_tabs(self) -> list[dict]:
        """Enumerate EVERY iTerm2 tab/session (not just claude ones), for the
        'iTerm2 tabs' viewer. Fresh connection for the same staleness reason
        as list_claude_tabs. Each entry: window/tab index, session id, name,
        tty, and whether a foreground claude is running on it.

        Serialized with list_claude_tabs (_enum_lock) — they share the connection, and
        whichever one arrives second used to close it under the first."""
        async with self._enum_lock:
            return await self._list_all_tabs_locked()

    async def _list_all_tabs_locked(self) -> list[dict]:
        await self._fresh_app()
        if self.app is None:
            return []
        # Flatten to (wi, ti, session), then fetch tty+name for ALL sessions
        # concurrently (was 2 sequential RPCs per session, no timeout), and
        # detect claude tabs with ONE `ps` off the loop.
        flat = [(wi, ti, session)
                for wi, window in enumerate(self.app.windows)
                for ti, tab in enumerate(window.tabs)
                for session in tab.sessions]

        async def _name(session):
            # tab.title = tab-strip label (see list_claude_tabs); fall back to
            # session.name when a tab has no override.
            return ((await _gv(session, "tab.title"))
                    or (await _gv(session, "session.name")) or "")

        names = await asyncio.gather(*[_name(s) for _, _, s in flat])
        # Same matcher as list_claude_tabs. It used to test tty membership on its own,
        # so after a window restoration (tty = None) 14 of 15 claude tabs showed up here
        # as plain shells — no sid, no Attach/Enter.
        found = await self._claude_by_session(flat)
        out: list[dict] = []
        for i, ((wi, ti, session), name) in enumerate(zip(flat, names)):
            hit = found.get(i)
            out.append({
                "iterm_session_id": session.session_id,
                "window_index": wi,
                "tab_index": ti,
                "name": name,
                "tty": (hit[0] if hit else "") or "",
                "is_claude": hit is not None,
                "pid": hit[1] if hit else None,
            })
        return out

    async def input_typed_text(self, iterm_session_id: str) -> str:
        """Text the human has TYPED into the input box, excluding the greyed
        autosuggest/placeholder ghost. The ghost renders AFTER the cursor, so we
        take the cursor line up to the cursor column, minus the ❯ prompt."""
        if not self.app:
            return ""
        session = self.app.get_session_by_id(iterm_session_id)
        if session is None:
            return ""
        try:
            contents = await asyncio.wait_for(
                session.async_get_screen_contents(), _RPC_TIMEOUT)
            cur = contents.cursor_coord
            line = contents.line(cur.y).string.replace("\x00", " ")
            pre = line[:cur.x].lstrip()
            if pre[:1] in ("❯", ">"):
                pre = pre[1:]
            return pre.strip()
        except Exception:
            return ""

    async def set_tab_name(self, iterm_session_id: str, name: str) -> bool:
        """Set the TAB-STRIP title (a manual override, like iTerm's "Edit Tab
        Title") for the tab that owns this session. This is exactly the value
        list_claude_tabs reads back as the tab-name (tab.title), and it sticks
        over claude's OSC session.name. Empty name clears the override."""
        await self.ensure_connected()
        if not self.app:
            return False
        for window in self.app.windows:
            for tab in window.tabs:
                if any(s.session_id == iterm_session_id for s in tab.sessions):
                    try:
                        await asyncio.wait_for(tab.async_set_title(name), _RPC_TIMEOUT)
                        return True
                    except Exception:
                        return False
        return False

    async def resize_cols(self, iterm_session_id: str, dcols: int):
        """Read the session's grid size; if dcols != 0, widen/narrow the COLUMNS
        (rows unchanged) so claude's TUI reflows to a new chars-per-line width.
        dcols == 0 → read only (no resize, no redraw). Returns {cols, rows} or None."""
        if not self.app:
            return None
        session = self.app.get_session_by_id(iterm_session_id)
        if session is None:
            return None
        try:
            cur = session.grid_size                       # util.Size: .width=cols .height=rows
            cols, rows = int(cur.width), int(cur.height)
            if dcols:
                cols = max(20, min(400, cols + dcols))
                await asyncio.wait_for(
                    session.async_set_grid_size(iterm2.util.Size(cols, rows)), _RPC_TIMEOUT)
            return {"cols": cols, "rows": rows}
        except Exception:
            return None

    async def send_text_to(self, iterm_session_id: str, text: str) -> bool:
        """Send text to a specific iTerm2 session.

        If the text ends with CR (\\r) we treat it as a "submit" gesture:
          1. Send the body first (no Enter)
          2. Tiny delay so the TUI's input renderer absorbs the chars
          3. Send the \\r alone — submit fires cleanly
          4. Pause again to let the input clear before the next call

        Concurrent calls are serialized via _send_lock so two rapid Sends from
        the web don't interleave (which used to merge into a single message)."""
        if not self.app:
            return False
        session = self.app.get_session_by_id(iterm_session_id)
        if session is None:
            return False
        async with self._send_lock:
            try:
                if text.endswith("\r"):
                    body = text[:-1]
                    if body:
                        await asyncio.wait_for(session.async_send_text(body), _RPC_TIMEOUT)
                        await asyncio.sleep(0.1)
                    await asyncio.wait_for(session.async_send_text("\r"), _RPC_TIMEOUT)
                    await asyncio.sleep(0.4)  # let Ink process submit + clear input
                else:
                    await asyncio.wait_for(session.async_send_text(text), _RPC_TIMEOUT)
            except Exception:
                return False
        return True

    async def open_resume_claude_tab(self, cwd: str, session_id: str, label: str) -> Optional[str]:
        """Open a new iTerm2 tab and run `claude --resume <session_id>` in it."""
        return await self._open_claude_tab(cwd, label, resume_id=session_id)

    async def open_new_claude_tab(self, cwd: str, label: str) -> Optional[str]:
        return await self._open_claude_tab(cwd, label, resume_id=None)

    async def _open_claude_tab(self, cwd: str, label: str, resume_id: Optional[str]) -> Optional[str]:
        """Create a new iTerm2 tab, label it, then run `cd <cwd> && claude [--resume id]`.
        Also auto-confirms claude's "Trust this folder?" prompt — without that,
        claude blocks before creating its JSONL, and we can't bind the session."""
        if not self.app:
            return None
        window = self.app.current_terminal_window
        if window is None:
            try:
                window = await iterm2.Window.async_create(self.connection)
            except Exception:
                window = None
        if window is None:
            return None
        try:
            tab = await window.async_create_tab()
        except Exception:
            tab = None
        if tab is None:
            return None
        session = tab.current_session
        if session is None:
            return None
        try:
            await window.async_activate()
        except Exception:
            pass
        try:
            await session.async_set_name(label)
        except Exception:
            pass
        await asyncio.sleep(0.4)
        import shlex
        cd_part = f"cd {shlex.quote(cwd)} && " if cwd else ""
        claude_cmd = "claude" + (f" --resume {shlex.quote(resume_id)}" if resume_id else "")
        await session.async_send_text(f"{cd_part}{claude_cmd}\n")
        # Auto-accept the "Trust this folder?" dialog if it appears.
        await self._maybe_accept_trust_prompt(session.session_id)
        return session.session_id

    async def _maybe_accept_trust_prompt(self, iterm_session_id: str,
                                         max_wait_sec: float = 8.0) -> bool:
        """Poll the new tab's screen; if claude's "Trust this folder?" dialog
        is up, press 1 + Enter to accept. Returns True if we sent the keys.

        We do nothing if the prompt never appears (claude already trusted
        the dir from a previous run, or it shows the welcome banner directly).
        """
        deadline = asyncio.get_event_loop().time() + max_wait_sec
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.6)
            screen = await self.get_screen_for(iterm_session_id, max_lines=80)
            if not screen:
                continue
            low = screen.lower()
            # Two signals that the trust dialog is currently up:
            # - "trust this folder" appears in the body
            # - "1. yes, i trust this folder" is the active option
            if ("trust this folder" in low
                    or "yes, i trust this folder" in low):
                # Send "1" to highlight option 1, then Enter to confirm.
                # The dialog uses arrow keys + Enter, but option 1 is the
                # default selection so a bare Enter would also work — we
                # send "1\r" defensively to be explicit.
                await self.send_text_to(iterm_session_id, "1\r")
                # Brief grace for claude to process and continue past the
                # dialog into the welcome banner.
                await asyncio.sleep(1.5)
                return True
            # Other signal: claude welcome banner already up → no trust
            # prompt, nothing to do.
            if "welcome back" in low or "claude code v" in low:
                return False
        return False

    async def get_screen_for(self, iterm_session_id: str, max_lines: int = 80,
                             refresh: bool = False,
                             strip_input: bool = True,
                             scrollback: bool = False,
                             with_cursor: bool = False):
        """Read the current screen tail from `iterm_session_id`.

        When `refresh=True`, send Ctrl+L (form feed) first and wait
        briefly so claude's TUI redraws — that strips out the box-drawing
        / styling artifacts left in iTerm's grid from previous frames,
        giving a cleaner capture. Don't use refresh on the auto-poll
        path: every 5s of Ctrl+L would be visible flicker for the user.

        `strip_input` (default True) removes the Ink input box + footer at
        the bottom — wanted for attach scoring, but the "iTerm screen"
        viewer passes False to show the full screen including those lines.

        `scrollback` (default False) reads only the CURRENT visible grid
        (~30-50 rows) — what you want for the live screen view and pending-
        choice detection. Attach matching passes True to also read up to
        `max_lines` of SCROLLBACK: when a menu/long output fills the screen
        the conversation scrolls out of the grid, so fingerprint scoring
        would otherwise see none of it and score 0.

        `with_cursor` → return (text, cursor) where cursor is (row, col, vis):
        the terminal cursor in the CURRENT grid (same snapshot, no extra RPC),
        row 0-based from the top of the visible grid. iTerm has no cheap
        cursor-visibility flag, so vis is always 1 here.
        """
        _fail = (None, None) if with_cursor else None
        if not self.app:
            return _fail
        session = self.app.get_session_by_id(iterm_session_id)
        if session is None:
            return _fail
        if refresh:
            try:
                await session.async_send_text("\x0c")  # Ctrl+L
                await asyncio.sleep(0.25)
            except Exception:
                pass
        # Scrollback read (attach only): async_get_contents() reads an
        # arbitrary absolute line range; async_get_line_info() gives the
        # bounds (oldest available line = overflow; total = history + grid).
        # Falls back to the visible-only read if the range read fails.
        lines = None
        cursor = None
        if scrollback:
            try:
                info = await asyncio.wait_for(session.async_get_line_info(), _RPC_TIMEOUT)
                grid = info.mutable_area_height
                history = info.scrollback_buffer_height
                overflow = info.overflow
                avail = history + grid
                want = min(max_lines, avail)
                first = overflow + avail - want
                line_contents = await asyncio.wait_for(
                    session.async_get_contents(first, want), _RPC_TIMEOUT)
                lines = [lc.string.replace("\x00", " ").rstrip() for lc in line_contents]
            except Exception:
                lines = None
        if lines is None:
            try:
                contents = await asyncio.wait_for(
                    session.async_get_screen_contents(), _RPC_TIMEOUT)
            except Exception:
                return _fail
            lines = []
            for y in range(contents.number_of_lines):
                line = contents.line(y)
                # iTerm pads wide chars (CJK, emoji) and some rendered text
                # with NULL bytes between glyphs. They render as nothing, so
                # screen text appears with words mashed together. Fold NULLs
                # to space here so every caller of this method gets readable
                # output without each having to remember the workaround.
                s = line.string.replace("\x00", " ").rstrip()
                lines.append(s)
            if with_cursor:
                try:
                    cc = contents.cursor_coord            # same snapshot as the grid above
                    # cursor_coord.y is ABSOLUTE (scrollback-inclusive); subtract the
                    # first visible line's absolute row (windowed_coord_range.start.y,
                    # in the same snapshot — no extra RPC) to get a grid-relative row.
                    base = contents.windowed_coord_range.start.y
                    if cc is not None:
                        cursor = (int(cc.y) - int(base), int(cc.x), 1)
                except Exception:
                    cursor = None
        if strip_input:
            lines = _strip_input_area(lines)
        while lines and not lines[-1]:
            lines.pop()
        text = "\n".join(lines[-max_lines:])
        return (text, cursor) if with_cursor else text


def _is_dash_bar(s: str) -> bool:
    s = s.strip()
    return len(s) >= 20 and all(c == "─" for c in s)


def _is_prompt_line(s: str) -> bool:
    s = s.lstrip()
    return s.startswith("❯") or s.startswith(">")


def _strip_input_area(lines: list[str]) -> list[str]:
    """Remove the Ink input box (and footer below it) from the bottom of the screen.

    Pattern: a `─────...` bar, a `❯ ...` prompt line, zero+ continuation lines,
    a closing `─────...` bar, then footer (auto mode hint, token counter, etc).
    Walk from the bottom up: find the closing bar, find the opening bar above
    it with a prompt line between them, drop everything from the opening bar on.
    """
    n = len(lines)
    # closing bar = lowest dash-bar (skip trailing footer/empty lines)
    bottom_bar = None
    for i in range(n - 1, -1, -1):
        if _is_dash_bar(lines[i]):
            bottom_bar = i
            break
    if bottom_bar is None:
        return lines
    # opening bar = next dash-bar above; require a prompt line between them
    top_bar = None
    saw_prompt = False
    for i in range(bottom_bar - 1, -1, -1):
        if _is_dash_bar(lines[i]):
            top_bar = i
            break
        if _is_prompt_line(lines[i]):
            saw_prompt = True
    if top_bar is None or not saw_prompt:
        return lines
    return lines[:top_bar]


_UI_NOISE_TOKENS = (
    "auto mode", "shift+tab", "esc to interrupt", "Welcome to", "Tips for",
    "Tip:", "Try \"", "claude is", "no conversation found",
    "running in", "▶▶", "⏵⏵", "│", "─",
)


async def _read_screen_lines(session) -> list[str]:
    try:
        contents = await asyncio.wait_for(session.async_get_screen_contents(), _RPC_TIMEOUT)
    except Exception:
        return []
    out = []
    for y in range(contents.number_of_lines):
        try:
            # NULL → space (see get_screen_for for rationale).
            s = contents.line(y).string.replace("\x00", " ").strip()
        except Exception:
            continue
        if s:
            out.append(s)
    return out


def _pick_fingerprint(lines: list[str]) -> Optional[str]:
    """A distinctive substring likely to appear in the JSONL but not in
    other sessions' JSONLs. Pick the longest non-noisy line, then take a
    middle slice so stray box-drawing chars on the edges don't ruin matching."""
    candidates = []
    for line in lines:
        if len(line) < 25:
            continue
        if any(tok in line for tok in _UI_NOISE_TOKENS):
            continue
        candidates.append(line)
    if not candidates:
        return None
    best = max(candidates, key=len)
    # Take a 30-char chunk from the middle (avoids leading prefixes / trailing UI)
    if len(best) <= 30:
        return best
    mid = len(best) // 2
    return best[max(0, mid - 15):mid + 15]


async def _verify_session_id(session, cwd: str, current_sid: str) -> Optional[str]:
    """Read the iTerm2 screen, pick a fingerprint line, and locate which
    JSONL in cwd's project dir contains it. Returns the matching session_id
    (filename stem) or None when unverifiable (empty screen, ambiguous)."""
    lines = await _read_screen_lines(session)
    fp = _pick_fingerprint(lines)
    if not fp:
        return None
    encoded = cwd.replace("/", "-").replace("_", "-")
    proj = Path.home() / ".claude" / "projects" / encoded
    if not proj.exists():
        return None
    matches: list[Path] = []
    for p in proj.glob("*.jsonl"):
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 200_000))  # tail only — recent activity
                tail = f.read()
        except OSError:
            continue
        if fp in tail:
            matches.append(p)
    if len(matches) == 1:
        return matches[0].stem
    if len(matches) > 1:
        # Multiple JSONLs contain the fingerprint — prefer the most recently
        # modified (it's actively being written, more likely the live one).
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return matches[0].stem
    return None  # not found in any → cannot verify (e.g. screen too generic)


_RESUME_RE = re.compile(
    r"(?:--resume|--continue|-r)[=\s]+([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def _resume_sid_from_cmd(cmd: str) -> str:
    """If the claude process was launched as `claude --resume <uuid>`, return
    that session_id. This is GROUND TRUTH: the tab is running exactly that
    session, no screen-content guessing needed."""
    m = _RESUME_RE.search(cmd)
    return m.group(1) if m else ""


def _norm_tty(tty: str) -> str:
    """Normalize a tty to a comparable id: '/dev/ttys005' → 's005', 'ttys005'
    → 's005', 's005' → 's005'. (macOS `ps -o tty` prints 's005'; iTerm's tty
    variable is '/dev/ttys005'.)"""
    t = tty.rsplit("/", 1)[-1]
    return t[3:] if t.startswith("tty") else t


def _claude_ttys() -> set[str]:
    """ONE `ps` over all processes → set of normalized ttys that have a
    FOREGROUND claude. Replaces N per-tab `ps -t` calls in list_all_tabs."""
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "tty=,stat=,command="],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:
        return set()
    ttys: set[str] = set()
    for line in out.splitlines():
        parts = line.split(None, 2)   # tty, stat, command
        if len(parts) != 3:
            continue
        tty, stat, cmd = parts
        if "+" not in stat:           # foreground process group only
            continue
        if _is_claude_cmd(cmd):
            ttys.add(_norm_tty(tty))
    return ttys


def _claude_procs_by_tty() -> dict[str, tuple[int, str]]:
    """ONE `ps` over all processes → {normalized_tty: (pid, resume_sid)} for
    FOREGROUND claudes. Replaces the N per-tty `ps -t` calls in
    list_claude_tabs (cwd is still resolved per hit by the caller)."""
    return _ps_scan()[0]


def _ps_scan() -> tuple[dict[str, tuple[int, str]], dict[int, int], dict[int, str]]:
    """One `ps -A` → (foreground claudes by tty, pid→ppid, pid→tty) for everything.

    The last two both exist to get from iTerm2's `jobPid` back to claude. jobPid is the
    tab's FOREGROUND JOB, which while claude works is usually a child it spawned
    (`caffeinate -i -t 300`): the parent map walks up to claude, and the tty map asks the
    other way round — which terminal is that job on, and is claude on it. See
    claude_above / claude_on_job_tty.
    """
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "tty=,pid=,ppid=,stat=,command="],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:
        return {}, {}, {}
    res: dict[str, tuple[int, str]] = {}
    parent: dict[int, int] = {}
    pid_tty: dict[int, str] = {}
    for line in out.splitlines():
        parts = line.split(None, 4)   # tty, pid, ppid, stat, command
        if len(parts) != 5:
            continue
        tty, pid_str, ppid_str, stat, cmd = parts
        try:
            pid, ppid = int(pid_str), int(ppid_str)
        except ValueError:
            continue
        parent[pid] = ppid
        if tty and tty != "??":
            pid_tty[pid] = _norm_tty(tty)
        if "+" not in stat:           # foreground process group only
            continue
        if not _is_claude_cmd(cmd):
            continue
        res.setdefault(_norm_tty(tty), (pid, _resume_sid_from_cmd(cmd)))
    return res, parent, pid_tty


def _claude_on_tty(tty_path: str) -> Optional[tuple[int, str, str]]:
    """If a foreground `claude` process is running on this TTY, return
    (pid, cwd, resume_sid). resume_sid is the session_id from a
    `--resume <uuid>` argv (ground truth), or "" if launched without it.

    Only the foreground process group counts (its `stat` has '+'). This lets
    us ignore Ctrl-Z suspended claudes and other backgrounded copies in the
    same tab — only the one currently receiving keyboard input matters."""
    tty_name = os.path.basename(tty_path)
    try:
        out = subprocess.run(
            ["ps", "-t", tty_name, "-o", "pid=,stat=,command="],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        parts = s.split(None, 2)  # pid, stat, command
        if len(parts) != 3:
            continue
        pid_str, stat, cmd = parts
        if "+" not in stat:
            continue  # not in foreground process group
        if not _is_claude_cmd(cmd):
            continue
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        cwd = _pid_cwd(pid)
        if cwd:
            return pid, cwd, _resume_sid_from_cmd(cmd)
    return None


def _is_claude_cmd(cmd: str) -> bool:
    """True only when `claude` is the EXECUTABLE (argv[0]) or the script run by a
    node/bun/deno interpreter (argv[1]). NOT merely a path ARGUMENT that happens
    to contain a `claude` component (e.g. `tail -f /var/log/claude`, `cd
    /work/claude`), which the old any-token check wrongly flagged as a claude tab."""
    tokens = cmd.split()
    if not tokens:
        return False
    if os.path.basename(tokens[0]) == "claude":
        return True
    if (len(tokens) >= 2 and os.path.basename(tokens[0]) in ("node", "bun", "deno")
            and os.path.basename(tokens[1]) == "claude"):
        return True
    return False


def _pid_start_time(pid: int) -> float:
    """Unix timestamp when the process started. 0.0 on failure.

    `ps -o lstart=` prints LOCAL time. Force LC_ALL=C so the weekday is English
    (else Chinese-locale "六" breaks strptime) and use time.mktime which treats
    the parsed struct as local time (calendar.timegm would treat as UTC and be
    off by the timezone offset)."""
    try:
        env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2, env=env,
        ).stdout.strip()
        if not out:
            return 0.0
        import time
        # e.g. "Sat Apr 25 20:01:06 2026"  (always local time)
        tm = time.strptime(out, "%a %b %d %H:%M:%S %Y")
        return time.mktime(tm)
    except Exception:
        return 0.0


def _assign_session_ids(refs: list["ClaudeSessionRef"]) -> None:
    """Pair each iTerm2 ref to its JSONL.

    Two-pass heuristic per cwd:
      1. Fresh-session match: for each pid, find a JSONL whose ctime is within
         ~5 min of pid_start AND mtime > pid_start. ctime doesn't update on
         appends, so it identifies "the file created during this pid's
         lifetime" — the JSONL that THIS pid is writing.
      2. Fallback: any remaining pids (likely --resumed an old session) paired
         to remaining JSONLs by mtime desc.

    Without pass 1, when multiple claudes share a cwd (e.g., one in iTerm2 +
    one in Terminal.app), an iTerm2 pid would wrongly be paired with the most-
    recently-modified JSONL — which might belong to the Terminal.app claude."""
    from collections import defaultdict
    by_cwd: dict[str, list] = defaultdict(list)
    for r in refs:
        by_cwd[r.cwd].append(r)

    for cwd, group in by_cwd.items():
        encoded = cwd.replace("/", "-").replace("_", "-")
        proj = Path.home() / ".claude" / "projects" / encoded
        if not proj.exists():
            continue
        meta: list[tuple[Path, float, float]] = []
        for p in proj.glob("*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            # On macOS, st_ctime updates on writes (size changes qualify as
            # metadata change). True file-creation time is st_birthtime.
            birth = getattr(st, "st_birthtime", st.st_ctime)
            meta.append((p, birth, st.st_mtime))
        if not meta:
            continue

        # Pass 1: fresh-session match by ctime ≈ pid_start.
        used: set[Path] = set()
        FRESH_WINDOW = 300.0  # seconds
        # Try newest-pid first so a chain of fresh starts gets paired in order.
        group.sort(key=lambda r: _pid_start_time(r.pid), reverse=True)
        for ref in group:
            pid_start = _pid_start_time(ref.pid)
            if pid_start <= 0:
                continue
            best: Optional[Path] = None
            best_diff: Optional[float] = None
            for p, ctime, mtime in meta:
                if p in used:
                    continue
                if mtime <= pid_start:
                    continue  # file hasn't been written during this pid's lifetime
                diff = abs(ctime - pid_start)
                if diff > FRESH_WINDOW:
                    continue
                if best_diff is None or diff < best_diff:
                    best, best_diff = p, diff
            if best is not None:
                ref.claude_session_id = best.stem
                used.add(best)

        # Pass 2: remaining pids → remaining JSONLs by mtime desc.
        remaining_jsonls = [p for (p, _c, _m) in sorted(meta, key=lambda x: -x[2]) if p not in used]
        remaining_pids = [r for r in group if not r.claude_session_id]
        for ref, jsonl in zip(remaining_pids, remaining_jsonls):
            ref.claude_session_id = jsonl.stem


def _claude_session_id_for_pid(pid: int, cwd: str) -> Optional[str]:
    """Which JSONL is this claude process writing to?

    Claude Code doesn't hold the file descriptor open (writes are short bursts),
    so lsof is unreliable. Fall back to the most-recently-modified JSONL in the
    project directory for this cwd — which is what Claude Code itself writes to.
    """
    try:
        out = subprocess.run(
            ["lsof", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout
    except Exception:
        out = ""
    for line in out.splitlines():
        if "/.claude/projects/" in line and line.rstrip().endswith(".jsonl"):
            parts = line.split()
            if parts:
                return os.path.basename(parts[-1])[:-len(".jsonl")]

    # Fallback: newest JSONL in the project dir (Claude's encoding is / and _ → -).
    encoded = cwd.replace("/", "-").replace("_", "-")
    proj = Path.home() / ".claude" / "projects" / encoded
    if not proj.exists():
        return None
    files = list(proj.glob("*.jsonl"))
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_mtime)
    return files[-1].stem


def _pid_cwd(pid: int) -> Optional[str]:
    try:
        out = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


if __name__ == "__main__":
    async def main():
        bridge = ItermBridge()
        await bridge.connect()
        refs = await bridge.list_claude_sessions()
        for r in refs:
            print(r)

    asyncio.run(main())
