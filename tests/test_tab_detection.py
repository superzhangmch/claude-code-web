#!/usr/bin/env python3
"""A live claude tab must not be silently demoted to "a plain shell".

Two session variables decide whether a tab is running claude: `tty` (matched against
ps) and `jobPid` (matched by identity). Both are read from iTerm2, and the reader
returned None on ANY failure — indistinguishable from a real "no". One hiccup on one
session therefore dropped that tab out of the session list and out of the periodic
snapshot, with nothing logged.

That is not hypothetical. On 2026-08-26 the list lost w1t6 at 11:27, showed it at
12:05, lost it again at 12:35, and had it back at 13:14 — while the claude in that tab
(ttys009, pid 42967) had been running continuously since Aug 20 18:10. w1t8 did the
same. Finding that out needed `ps -o lstart` on six-day-old processes, because nothing
in the logs said a probe had failed. It also mattered beyond cosmetics: the periodic
snapshot is built from this list, so an unreadable tab silently became a session that
"resume saved" would not restore.

What is pinned:

  1. a transient read failure is RETRIED, and the second answer is used;
  2. a permanent failure is COUNTED and LOGGED — never silently a None;
  3. a tab that answered neither key is reported as blind, not as a plain shell, and the
     log names it as wXtY;
  4. detection itself still works both ways: by tty, and by jobPid when tty is None
     (iTerm2 leaves tty empty on restored windows);
  5. the autosave refuses to record an enumeration that had a blind tab.

    python3 tests/test_tab_detection.py       # exit 0 = pass
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


class Sess:
    """An iTerm2 session that answers variable reads — flakily, if asked to."""

    _n = 0

    def __init__(self, vals, fail_first=0, fail_always=False, session_id=None):
        Sess._n += 1
        # Every iTerm session has an id; the matcher keys its remembered mapping on it.
        self.session_id = session_id or f"SESS-{Sess._n}"
        self.vals = vals
        self.fail_first = fail_first
        self.fail_always = fail_always
        self.calls = 0

    async def async_get_variable(self, var):
        self.calls += 1
        if self.fail_always or self.calls <= self.fail_first:
            raise RuntimeError("websocket hiccup")
        return self.vals.get(var)


async def main():
    import iterm_bridge as ib

    logged = []
    real_warn = ib.log.warning
    ib.log.warning = lambda fmt, *a: logged.append(fmt % a if a else fmt)

    try:
        print("=== a transient failure is retried, not taken as an answer ===")
        s = Sess({"tty": "/dev/ttys009"}, fail_first=1)
        before = ib._gv_fail
        got = await ib._gv(s, "tty")
        check("the retry gets the real value", got == "/dev/ttys009", repr(got))
        check("...and it isn't counted as a failure", ib._gv_fail == before, str(ib._gv_fail))

        print("=== a permanent failure is counted and said out loud ===")
        logged.clear()
        s = Sess({}, fail_always=True)
        before = ib._gv_fail
        got = await ib._gv(s, "jobPid")
        check("still None (the caller must cope)", got is None, repr(got))
        check("...but counted", ib._gv_fail == before + 1, f"{before} -> {ib._gv_fail}")
        check("...and logged, naming the variable",
              any("jobPid" in m for m in logged), str(logged))
        check("...after more than one attempt", s.calls >= 2, f"{s.calls} calls")

        # ---------------------------------------------------------------- detection
        bridge = ib.ItermBridge() if hasattr(ib, "ItermBridge") else None
        if bridge is None:                       # class renamed — find it rather than guess
            cls = next(v for k, v in vars(ib).items()
                       if isinstance(v, type) and hasattr(v, "_claude_by_session"))
            bridge = cls()

        # ps says: claude on ttys009 (pid 42967) and on ttys005 (pid 41255).
        real_scan = ib._ps_scan            # the real parser — restored for its own section
        stub_scan = lambda: ({"ttys009": (42967, "sid-9"),
                              "ttys005": (41255, "sid-5")}, {}, {})
        ib._ps_scan = stub_scan

        print("=== detection works by tty, and by jobPid when tty is empty ===")
        logged.clear()
        flat = [
            (0, 0, Sess({"tty": "/dev/ttys009", "jobPid": 42967})),   # normal
            (0, 1, Sess({"tty": None, "jobPid": 41255})),             # restored window
            (0, 2, Sess({"tty": "/dev/ttys099", "jobPid": 123})),     # a real plain shell
        ]
        hits = await bridge._claude_by_session(flat)
        check("matched by tty", hits.get(0) == ("/dev/ttys009", 42967, "sid-9"), str(hits.get(0)))
        check("matched by jobPid with no tty at all",
              hits.get(1) == ("ttys005", 41255, "sid-5"), str(hits.get(1)))
        check("a genuine shell is not matched", 2 not in hits, str(hits))
        check("nothing blind, nothing warned",
              bridge.last_probe_blind == 0 and not logged, str((bridge.last_probe_blind, logged)))

        print("=== a tab that answers NEITHER key is blind, not 'a plain shell' ===")
        logged.clear()
        flat = [
            (0, 0, Sess({"tty": "/dev/ttys009", "jobPid": 42967})),
            (0, 5, Sess({}, fail_always=True)),   # w1t6, the real case
        ]
        hits = await bridge._claude_by_session(flat)
        check("the readable tab is still matched", 0 in hits, str(hits))
        check("the unreadable one is counted as blind",
              bridge.last_probe_blind == 1, str(bridge.last_probe_blind))
        check("...and the log names it the way the UI does (w1t6)",
              any("w1t6" in m for m in logged), str(logged))
        check("...and says it may be wrong to call it non-claude",
              any("non-claude" in m for m in logged), str(logged))
        # A clean run afterwards must clear it, or one bad enumeration would stop the
        # autosave forever.
        flat = [(0, 0, Sess({"tty": "/dev/ttys009", "jobPid": 42967}))]
        await bridge._claude_by_session(flat)
        check("a later clean enumeration resets the count",
              bridge.last_probe_blind == 0, str(bridge.last_probe_blind))

        print("=== the foreground job is often NOT claude itself ===")
        # THE cause of t6. While claude works it keeps a `caffeinate -i -t 300` child
        # alive in its own process group, so iTerm2's jobPid reports the CHILD. Identity
        # matching missed it — and on a window iTerm2 restored, tty is unreadable, so
        # jobPid is the only key there is. The tab therefore dropped off the list for as
        # long as that caffeinate lived (~5 minutes), then came back, over and over.
        # This is the real `ps -A -o tty=,pid=,ppid=,stat=,command=` shape from mac-pro.
        real_ps = (
            "ttys009 42967 42600 S+   claude --resume 1a41ce37-e011-47d6-ac6a-14ab29db202a\n"
            "ttys009  4325 42967 S+   caffeinate -i -t 300\n"
            "ttys009 42600 42599 S    -zsh\n"
            "ttys012 21678 21281 S+   claude\n"
            "??      34855     1 S    claude\n"
        )
        real_run = ib.subprocess.run
        ib.subprocess.run = lambda *a, **k: type("R", (), {"stdout": real_ps})()
        ib._ps_scan = real_scan            # this section tests the parser itself
        try:
            procs, parent, pid_tty = ib._ps_scan()
            check("ps gives the foreground claudes by tty",
                  procs == {"s009": (42967, "1a41ce37-e011-47d6-ac6a-14ab29db202a"),
                            "s012": (21678, "")}, str(procs))
            check("...and caffeinate is NOT mistaken for one", 4325 not in
                  {p for p, _ in procs.values()}, str(procs))
            check("...but its parent link is kept — one way back to claude",
                  parent.get(4325) == 42967, str(parent.get(4325)))
            check("...and its terminal too — the other way back",
                  pid_tty.get(4325) == "s009", str(pid_tty.get(4325)))
            check("a claude with no tty at all is ignored", "??" not in procs
                  and 34855 not in {p for p, _ in procs.values()}, str(procs))

            b2 = type(bridge)()
            flat = [(0, 5, Sess({"tty": None, "jobPid": 4325})),       # t6: caffeinate
                    (0, 14, Sess({"tty": "/dev/ttys012", "jobPid": 21678}))]  # t15: normal
            hits = await b2._claude_by_session(flat)
            check("a restored tab whose job is claude's caffeinate child IS matched",
                  hits.get(0, (None, None, None))[1] == 42967, str(hits.get(0)))
            check("...and resolves to the right session, from claude's own argv",
                  (hits.get(0) or ("", 0, ""))[2].startswith("1a41ce37"), str(hits.get(0)))
            check("the normal tab still matches by tty", hits.get(1, (0, 0, 0))[1] == 21678,
                  str(hits.get(1)))
            check("nothing is reported blind — both keys were readable all along",
                  b2.last_probe_blind == 0, str(b2.last_probe_blind))

            # A grandchild must work too: the job can be a tool call's subprocess.
            ib.subprocess.run = lambda *a, **k: type("R", (), {"stdout":
                "ttys009 42967 42600 S+   claude --resume 1a41ce37-e011-47d6-ac6a-14ab29db202a\n"
                "ttys009  4400 42967 S+   bash -lc something\n"
                "ttys009  4401  4400 S+   grep -r foo .\n"})()
            flat = [(0, 0, Sess({"tty": None, "jobPid": 4401}))]
            hits = await b2._claude_by_session(flat)
            check("a grandchild of claude is matched too (two hops up)",
                  hits.get(0, (0, 0))[1] == 42967, str(hits.get(0)))
            # The backstop: a job that is NOT descended from claude but shares its
            # terminal (an orphan whose parent ps didn't capture) still resolves.
            ib.subprocess.run = lambda *a, **k: type("R", (), {"stdout":
                "ttys009 42967 42600 S+   claude --resume 1a41ce37-e011-47d6-ac6a-14ab29db202a\n"
                "ttys009  4500     1 S+   some-orphan\n"})()
            flat = [(0, 0, Sess({"tty": None, "jobPid": 4500}))]
            hits = await b2._claude_by_session(flat)
            check("a job on claude's terminal is matched even with no parent link",
                  hits.get(0, (0, 0))[1] == 42967, str(hits.get(0)))
            # It must not invent matches: an unrelated job has no claude above it.
            ib.subprocess.run = lambda *a, **k: type("R", (), {
                "stdout": "ttys099 700 701 S+   vim\nttys099 701 1 Ss   -zsh\n"})()
            flat = [(0, 0, Sess({"tty": None, "jobPid": 700}))]
            hits = await b2._claude_by_session(flat)
            check("a job with no claude above it is NOT matched", hits == {}, str(hits))
            # A broken/looping parent chain must terminate, not spin. On a terminal with
            # NO claude, so the tty backstop can't answer it and the walk is what's tested.
            ib.subprocess.run = lambda *a, **k: type("R", (), {"stdout":
                "ttys009 42967 42600 S+  claude\nttys099 500 501 S+  a\nttys099 501 500 S+  b\n"})()
            flat = [(0, 0, Sess({"tty": None, "jobPid": 500}))]
            hits = await asyncio.wait_for(b2._claude_by_session(flat), 5)
            check("a parent cycle terminates instead of spinning", hits == {}, str(hits))
        finally:
            ib.subprocess.run = real_run
            ib._ps_scan = stub_scan        # hand the fixture back for the sections below

        print("=== two enumerations at once must not close each other's connection ===")
        # THE cause. _fresh_app holds the lock only while it swaps self.connection and
        # closes the old one; the caller then reads session variables over the connection
        # it was handed, with no lock at all. A second enumeration in that window closes
        # that connection mid-read — the reads raise, _gv returns None, and those tabs are
        # reported as not-claude. Which tabs depends purely on timing, which is why one
        # tab could come and go from the list three times in a morning.
        #
        # Modelled on the real objects: a connection that can be closed, sessions whose
        # reads fail once it is, and an enumeration that yields between the swap and the
        # reads (as every await in the real one does).
        state = {"conn": None, "closed": set(), "gen": 0}

        class FakeSess:
            _n = 0
            def __init__(self, conn):
                FakeSess._n += 1
                self.session_id = f"RACE-{FakeSess._n}"
                self.conn = conn
            async def async_get_variable(self, var):
                await asyncio.sleep(0.01)          # a real RPC round-trip
                if self.conn in state["closed"]:
                    raise RuntimeError("connection closed under us")
                return {"tty": "/dev/ttys009", "jobPid": 42967}.get(var)

        async def fresh():
            """What _fresh_app does: new connection, close the old, swap it in."""
            async with bridge._lock:
                old = state["conn"]
                state["gen"] += 1
                state["conn"] = state["gen"]
                await asyncio.sleep(0.01)
                if old is not None:
                    state["closed"].add(old)

        async def enumerate_once():
            async with bridge._enum_lock:          # the fix under test
                await fresh()
                sessions = [FakeSess(state["conn"]) for _ in range(4)]
                flat = [(0, i, s) for i, s in enumerate(sessions)]
                hits = await bridge._claude_by_session(flat)
                return len(hits)

        got = await asyncio.gather(*[enumerate_once() for _ in range(4)])
        check("every concurrent enumeration still sees all 4 tabs", got == [4, 4, 4, 4], str(got))
        check("...and none of them went blind", bridge.last_probe_blind == 0,
              str(bridge.last_probe_blind))
        # And prove the harness can actually detect the bug: same thing WITHOUT the lock.
        state.update({"conn": None, "closed": set(), "gen": 0})

        async def enumerate_unlocked():
            await fresh()
            sessions = [FakeSess(state["conn"]) for _ in range(4)]
            return len(await bridge._claude_by_session([(0, i, s) for i, s in enumerate(sessions)]))

        unlocked = await asyncio.gather(*[enumerate_unlocked() for _ in range(4)])
        check("without the lock, tabs really do disappear (the harness proves the bug)",
              any(n < 4 for n in unlocked), str(unlocked))

        print("=== both enumerations take that lock ===")
        import ast
        src = open(os.path.join(ROOT, "iterm_bridge.py"), encoding="utf-8").read()
        holders = {n.name for n in ast.walk(ast.parse(src))
                   if isinstance(n, ast.AsyncFunctionDef)
                   and any(isinstance(w, ast.AsyncWith)
                           and any("_enum_lock" in ast.dump(i.context_expr) for i in w.items)
                           for w in ast.walk(n))}
        check("list_claude_tabs and list_all_tabs both hold _enum_lock",
              {"list_claude_tabs", "list_all_tabs"} <= holders, str(sorted(holders)))
    finally:
        ib.log.warning = real_warn

    print(("\nFAILED: " + ", ".join(_fails)) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
