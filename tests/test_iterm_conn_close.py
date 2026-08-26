#!/usr/bin/env python3
"""Regression test for the iTerm2 connection leak that deadlocked the app twice.

list_claude_tabs() opens a FRESH iterm2 connection on every call — deliberately, because
a long-lived one's App singleton goes stale and async_refresh() doesn't un-stick it. That
is only safe if the superseded connection is actually closed, and it wasn't:

    close = getattr(old, "async_close", None)
    if close: ...

iterm2 2.19's Connection has no close method of any kind, so that probe found nothing and
skipped. Every enumeration leaked a connection plus its forever-dispatcher task. ~48/day
from the snapshot timer alone, more from every picker load / ⇆ click / attach: after five
days, ~500 live unix sockets on each side (measured: 513 in cc_web, 574 in iTerm2), enough
per-connection state that iTerm2's API server wedged and took the whole app's main thread
with it — twice, the second time costing every open claude session.

So the close path must be verified by test, not by reading it. What is pinned:

  1. the websocket IS closed, and the dispatcher tasks ARE cancelled (cancel first — the
     ConnectionClosedError traceback spam in the logs came from dispatchers still running
     on a socket that had been closed under them);
  2. a Connection shaped like iterm2 2.19's — no close method anywhere — is still closed
     via its websocket. This is the exact case the old code silently skipped;
  3. futures are found by TYPE, not by attribute name, so name mangling or a version
     rename cannot turn this back into a no-op;
  4. it never raises: a close that throws must not break the enumeration that called it;
  5. the leak counter notices when connections stop being closed.

    python3 tests/test_iterm_conn_close.py       # exit 0 = pass
"""
import asyncio
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


class FakeWS:
    def __init__(self, raises=False):
        self.closed = 0
        self.raises = raises
        self.closed_while_dispatching = None

    async def close(self):
        if self.raises:
            raise RuntimeError("socket already gone")
        self.closed += 1


class Conn2_19:
    """Shaped like iterm2 2.19's Connection: a websocket, a name-mangled dispatch future,
    a list of helper tasks — and NO close method of any kind."""

    def __init__(self, loop, ws=None):
        self.websocket = ws or FakeWS()
        self.loop = loop
        self._Connection__dispatch_forever_future = loop.create_future()
        self._Connection__tasks = [loop.create_future(), loop.create_future()]
        self._Connection__receivers = []


async def main():
    import iterm_bridge as ib
    loop = asyncio.get_running_loop()

    print("=== a 2.19-shaped connection (no close method) is still closed ===")
    check("it really has no close API — the case the old code skipped",
          not [n for n in dir(Conn2_19(loop)) if "clos" in n.lower() and n != "websocket"],
          str([n for n in dir(Conn2_19(loop)) if "clos" in n.lower()]))
    c = Conn2_19(loop)
    ws = c.websocket
    fut = c._Connection__dispatch_forever_future
    helpers = list(c._Connection__tasks)
    await ib._close_conn(c, None)
    check("the websocket was closed", ws.closed == 1, f"close() calls: {ws.closed}")
    check("the forever-dispatcher was cancelled", fut.cancelled())
    check("...and so were the helper tasks", all(t.cancelled() for t in helpers))

    print("=== the dispatcher is cancelled BEFORE the socket closes ===")
    # Checked by STATE at the moment close() runs, not by callback order: cancel() marks
    # the future cancelled immediately but schedules its callback, so ordering callbacks
    # would measure the event loop rather than the code.
    seen = {}
    class OrderWS(FakeWS):
        def __init__(self, fut_getter):
            super().__init__()
            self._get = fut_getter
        async def close(self):
            seen["cancelled_when_closing"] = self._get().cancelled()
            await super().close()
    holder = {}
    c = Conn2_19(loop, ws=OrderWS(lambda: holder["f"]))
    holder["f"] = c._Connection__dispatch_forever_future
    await ib._close_conn(c, None)
    check("the dispatcher is already cancelled when the socket closes "
          "(else it screams ConnectionClosed)",
          seen.get("cancelled_when_closing") is True, str(seen))

    print("=== found by type, not by name ===")
    class Renamed:
        """Same thing after a version rename: different attribute names entirely."""
        def __init__(self, loop):
            self.websocket = FakeWS()
            self.some_new_name_for_the_task = loop.create_future()
    r = Renamed(loop)
    t = r.some_new_name_for_the_task
    await ib._close_conn(r, None)
    check("a renamed task attribute is still cancelled", t.cancelled())
    check("...and the socket still closed", r.websocket.closed == 1)

    print("=== it never breaks its caller ===")
    c = Conn2_19(loop, ws=FakeWS(raises=True))
    try:
        await ib._close_conn(c, None)
        raised = None
    except Exception as e:
        raised = repr(e)
    check("a throwing close is swallowed", raised is None, str(raised))
    check("...but the tasks were still cancelled",
          c._Connection__dispatch_forever_future.cancelled())

    class NoWS:
        def __init__(self, loop):
            self.f = loop.create_future()
    n = NoWS(loop)
    try:
        await ib._close_conn(n, None)
        raised = None
    except Exception as e:
        raised = repr(e)
    check("a connection with no websocket at all doesn't raise either", raised is None, str(raised))

    print("=== nothing to do cases ===")
    before = ib._conn_closed
    await ib._close_conn(None, None)
    c = Conn2_19(loop)
    await ib._close_conn(c, c)          # same object = still current
    check("None and still-current are no-ops",
          ib._conn_closed == before and c.websocket.closed == 0)

    print("=== the leak counter notices ===")
    ib._conn_open = ib._conn_closed + 20
    logged = []
    real_warn = ib.log.warning
    ib.log.warning = lambda fmt, *a: logged.append(fmt % a if a else fmt)
    try:
        await ib._close_conn(Conn2_19(loop), None)
    finally:
        ib.log.warning = real_warn
    check("a growing open-minus-closed count is warned about",
          any("not closed" in m for m in logged), str(logged))

    print("=== the cached App must be invalidated when the connection is replaced ===")
    # Both enumerations must go through that one helper rather than inlining it again.
    users = [n.name for n in ast.walk(ast.parse(open(os.path.join(ROOT, "iterm_bridge.py"),
                                                     encoding="utf-8").read()))
             if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
             and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                     and c.func.attr == "_fresh_app" for c in ast.walk(n))]
    # Each public enumeration is now a thin wrapper that takes _enum_lock and calls a
    # `_<name>_locked` body — a second enumeration used to close the connection the first
    # was still reading over, which silently dropped tabs from the list. So accept either
    # the entry point or the body it delegates to.
    norm = {n[1:-len("_locked")] if n.startswith("_") and n.endswith("_locked") else n
            for n in users}
    check("both list_claude_tabs and list_all_tabs use the shared helper",
          {"list_claude_tabs", "list_all_tabs"} <= norm, str(sorted(set(users))))
    # Closing the old connection is only half of it. iterm2 keeps App as a global
    # singleton bound to the connection it was constructed on:
    #     if App.instance is None: App.instance = await App.async_construct(conn)
    #     else:                    await App.instance.async_refresh()
    # so unless the singleton is dropped first, async_get_app() returns the App built on
    # the connection we just closed and refreshes it over a dead socket — enumeration
    # answers correctly ONCE and then returns zero forever. Observed exactly that:
    # tabs=15, then 0, 0, 0, 0, 0 on a machine with 15 working tabs.
    # Checked with the AST, by call node and line number. Substring searches kept
    # matching the DOCSTRING (which necessarily discusses both functions), so the
    # assertion passed with the real call deleted — twice, while writing this.
    src = open(os.path.join(ROOT, "iterm_bridge.py"), encoding="utf-8").read()
    # _fresh_app is where the three steps live now — both enumerations share it, which
    # is the point: the second copy of this dance is how the bug survived its first fix.
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
              and n.name == "_fresh_app")
    def call_lines(dotted):
        out = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                parts, f = [], n.func
                while isinstance(f, ast.Attribute):
                    parts.append(f.attr); f = f.value
                if isinstance(f, ast.Name):
                    parts.append(f.id)
                if ".".join(reversed(parts)) == dotted:
                    out.append(n.lineno)
        return sorted(out)
    inval = call_lines("iterm2.app.invalidate_app")
    get_app = call_lines("iterm2.async_get_app")
    closes = call_lines("_close_conn")
    check("_fresh_app calls invalidate_app()", bool(inval), str(inval))
    check("...before async_get_app() (order matters)",
          bool(inval) and bool(get_app) and inval[0] < get_app[0], f"{inval} vs {get_app}")
    check("...and still closes the superseded connection", bool(closes), str(closes))

    print(("\nFAILED: " + ", ".join(_fails)) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
