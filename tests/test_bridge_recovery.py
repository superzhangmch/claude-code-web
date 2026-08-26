#!/usr/bin/env python3
"""Terminal-bridge failure handling + the periodic session snapshot.

Both exist because of one afternoon: iTerm2 deadlocked, its Python API stopped
answering, and cc_web reported "no tabs" for hours while 15 claude sessions were alive.
Restarting iTerm2 then killed them, "Resume saved" failed with a raw websockets error
("no close frame received or sent"), and only a full cc_web restart recovered.

What is pinned:

  1. A bridge failure is REPORTED (`bridge_error`), not swallowed into an empty list.
     An unreachable terminal and an idle machine must never look the same.
  2. The reason is a sentence, not a websockets internal.
  3. Connecting RETRIES — a just-relaunched iTerm2 whose API isn't listening yet was the
     actual cause of the failed resume.
  4. Two separate stores. Save owns the manual file; the timer owns a history directory
     and never touches the manual one, so a curated list can't be overwritten by a timer.
  5. The autosave never records a worse picture than it has: it skips when the bridge is
     down, when there are no tabs, while a resume is running, and for a quiet period
     after one finishes (mid-resume the live list is 3-of-15 — recording that, and then
     resuming FROM it, is the worst possible outcome).
  6. The history is diffed (a file per CHANGE, not per hour) and capped at 100, newest
     kept — the snapshot you want is usually the one from before things went wrong.
  7. Resume takes an explicit source, and an older history entry can be picked.
  8. Manual save refuses to write an empty snapshot, and keeps a .prev.json.

    .venv/bin/python tests/test_bridge_recovery.py       # exit 0 = pass
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


class Ref:
    def __init__(self, sid, i, name=None):
        self.claude_session_id = sid
        # iTerm hands over a DECORATED title: a status glyph while claude is working and
        # a " (claude)" suffix for the running process. Neither is part of the name.
        self.name = f"✳ {name or ('tab' + str(i))} (claude)"
        self.window_index = 0
        self.tab_index = i
        self.pid = 5000 + i
        self.cwd = f"/tmp/p{i}"
        self.iterm_session_id = f"x{i}"


class FakeBridge:
    """Stands in for the iTerm/tmux bridge. `mode` picks the failure being simulated."""
    def __init__(self):
        self.mode = "ok"
        self.n = 3
        self.last_error = ""
        self.connects = 0
        self.dropped = 0

    async def ensure_connected(self):
        if self.mode == "closed":
            import websockets.exceptions as we
            raise we.ConnectionClosedError(None, None)
        if self.mode == "timeout":
            raise asyncio.TimeoutError()

    async def list_claude_tabs(self):
        self.connects += 1
        if getattr(self, "degrade", False):      # what a window restoration leaves behind
            self.last_error = ""
            return [Ref(f"{chr(97+i)*8}-1111-2222-3333-444444444444", i, "claude")
                    for i in range(self.n)]
        if getattr(self, "decorate", False):     # spinner glyph / process suffix only
            self.last_error = ""
            return [Ref(f"{chr(97+i)*8}-1111-2222-3333-444444444444", i, "✳ tab%d" % i)
                    for i in range(self.n)]
        if getattr(self, "rename", False):
            self.last_error = ""
            return [Ref(f"{chr(97+i)*8}-1111-2222-3333-444444444444", i, "renamed%d" % i)
                    for i in range(self.n)]
        if self.mode == "closed":
            self.last_error = "与 iTerm2 的连接已断开(iTerm2 被重启过?) — 用 ⚙ 里的 reconnect 重连"
            return []
        if self.mode == "timeout":
            self.last_error = "iTerm2 的 Python API 超时未响应(iTerm2 可能卡死)"
            return []
        self.last_error = ""
        return [Ref(f"{chr(97+i)*8}-1111-2222-3333-444444444444", i) for i in range(self.n)]

    def drop(self):
        self.dropped += 1

    async def wait_ready(self, timeout=20.0):
        return self.mode == "ok"


async def main():
    home = tempfile.mkdtemp(prefix="ccweb-bridge-")
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    open(os.path.join(home, ".claude", "cc_web.conf"), "w").write("token=t\n")
    os.environ["HOME"] = home
    import cc_web
    cc_web.SNAPSHOT_FILE = Path(home) / ".claude" / "cc_web_session_snapshot.json"
    fake = FakeBridge()
    cc_web.bridge = fake
    cc_web._claude_session_meta = lambda pid: {"sessionId": f"{chr(97 + pid - 5000)*8}-1111-2222-3333-444444444444",
                                               "startedAt": 1}

    print("=== a failing bridge is reported, not swallowed ===")
    fake.mode = "closed"
    d = await cc_web.get_tabs()
    check("/api/tabs returns no tabs...", d["tabs"] == [])
    check("...but says why", "reconnect" in d["bridge_error"], d["bridge_error"][:60])
    check("...in words, not a websockets internal",
          "close frame" not in d["bridge_error"] and "ConnectionClosed" not in d["bridge_error"])
    d = await cc_web.get_sessions(brief=1)
    check("/api/sessions reports it too", bool(d["bridge_error"]), d["bridge_error"][:50])
    fake.mode = "timeout"
    d = await cc_web.get_tabs()
    check("a wedged (not closed) API gets its own wording",
          "超时" in d["bridge_error"] or "卡死" in d["bridge_error"], d["bridge_error"][:60])
    fake.mode = "ok"
    d = await cc_web.get_tabs()
    check("a healthy bridge reports no error", d["bridge_error"] == "" and len(d["tabs"]) == 3)

    print("=== the reason helper turns library noise into instructions ===")
    import websockets.exceptions as we
    from iterm_bridge import bridge_reason
    msgs = [bridge_reason(we.ConnectionClosedError(None, None)),
            bridge_reason(asyncio.TimeoutError()),
            bridge_reason(ConnectionRefusedError())]
    check("each failure mode gets a distinct, actionable line",
          len(set(msgs)) == 3 and all(len(m) > 15 for m in msgs))
    check("...and none of them leaks the raw text the user saw",
          not any("close frame" in m for m in msgs), " | ".join(m[:28] for m in msgs))
    # A tmux host must not be told its iTerm2 connection dropped.
    tm = [bridge_reason(e, "tmux") for e in (we.ConnectionClosedError(None, None),
                                             asyncio.TimeoutError(), ConnectionRefusedError())]
    check("the message names THIS host's terminal", all("tmux" in m for m in tm), tm[0][:40])
    check("...and never the wrong one", not any("iTerm2" in m for m in tm), " | ".join(m[:30] for m in tm))
    check("cc_web passes its own terminal name through",
          cc_web.TERM_NAME in cc_web._bridge_reason(we.ConnectionClosedError(None, None)),
          cc_web._bridge_reason(we.ConnectionClosedError(None, None))[:50])

    print("=== connecting retries (a relaunched iTerm2 needs a moment) ===")
    import iterm_bridge
    b = iterm_bridge.ItermBridge()
    tries = {"n": 0}
    async def flaky():
        tries["n"] += 1
        if tries["n"] < 3:
            raise we.ConnectionClosedError(None, None)
        b.app = object()
    b.connect = flaky
    await b._connect_retry()
    check("it keeps trying instead of dying on the first failure", tries["n"] == 3, f"{tries['n']} attempts")
    check("...and clears the error once it works", b.last_error == "")
    tries["n"] = 0
    async def never():
        tries["n"] += 1
        raise we.ConnectionClosedError(None, None)
    b.connect = never
    try:
        await b._connect_retry()
        raised = ""
    except Exception as e:
        raised = type(e).__name__
    check("gives up as BridgeUnavailable, not a websockets error", raised == "BridgeUnavailable", raised)
    check("...having tried more than once", tries["n"] > 1, f"{tries['n']} attempts")
    check("...and remembers why for the endpoints", bool(b.last_error))

    print("=== two stores: Save owns the manual file, the timer owns the history ===")
    cc_web.AUTO_SNAP_DIR = Path(home) / ".claude" / "cc_web_snapshots"
    fake.mode = "ok"; fake.n = 15
    fifteen = await cc_web._live_tab_entries()
    cc_web._write_snapshot(fifteen)
    check("a manual save writes all 15 to the manual file",
          len(json.loads(cc_web.SNAPSHOT_FILE.read_text())["sessions"]) == 15)
    names = [x["name"] for x in json.loads(cc_web.SNAPSHOT_FILE.read_text())["sessions"]]
    check("...storing the real names, not iTerm's live decorations",
          names[:2] == ["tab0", "tab1"], str(names[:2]))

    async def tick():
        """One autosave pass, without waiting out the interval."""
        task = asyncio.create_task(cc_web._snapshot_autosave(0.01))
        await asyncio.sleep(0.2)
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        return cc_web._auto_snap_list()

    manual_now = lambda: json.loads(cc_web.SNAPSHOT_FILE.read_text())["sessions"]

    fake.mode = "closed"
    hist = await tick()
    check("bridge down → nothing recorded", hist == [], str(hist))
    check("...and it says why", "reconnect" in cc_web._snapshot_auto["skipped"],
          cc_web._snapshot_auto["skipped"][:36])
    fake.mode = "ok"; fake.n = 0
    check("zero tabs → nothing recorded (idle machine ≠ permission to write junk)",
          await tick() == [])
    fake.n = 3
    cc_web._resume_progress["running"] = True
    check("mid-resume (3 of 15 back) → nothing recorded", await tick() == [])
    check("...for the stated reason", "resume" in cc_web._snapshot_auto["skipped"],
          cc_web._snapshot_auto["skipped"])
    cc_web._resume_progress["running"] = False
    cc_web._resume_ended_mono = cc_web._time.monotonic()
    check("right after a resume → still nothing (claude is still starting in each tab)",
          await tick() == [])
    cc_web._resume_ended_mono = -1e9          # long enough ago to be irrelevant again
    # A tab whose tty AND jobPid both came back unreadable is missing from the list, and
    # the list is what gets recorded. On 2026-08-26 that dropped w1t6 and w1t8 from three
    # of the day's snapshots while both claudes had been running since Aug 20 — resuming
    # from one of those would have restored 13 of 15 and said nothing about the other two.
    fake.last_probe_blind = 2
    check("an incomplete enumeration → nothing recorded", await tick() == [])
    check("...and it says the tabs were unreadable, not that they were absent",
          "unreadable" in cc_web._snapshot_auto["skipped"], cc_web._snapshot_auto["skipped"])
    fake.last_probe_blind = 0
    check("a clean enumeration right after → recorded normally (the skip isn't sticky)",
          len(await tick()) == 1)
    # Hand the history back empty: the section below counts entries from zero, and a
    # borrowed fixture that isn't returned has already broken assertions here once.
    for f in cc_web.AUTO_SNAP_DIR.glob("auto-*.json"):
        f.unlink()
    check("through all of that, the manual 15 were never touched", len(manual_now()) == 15)

    cc_web._resume_ended_mono = cc_web._time.monotonic() - (cc_web.SNAPSHOT_QUIET_AFTER_RESUME + 5)
    hist = await tick()
    check("once quiet, the live list IS recorded", len(hist) == 1 and hist[0]["count"] == 3, str(hist))
    check("...in the auto directory, leaving the manual file alone", len(manual_now()) == 15)
    was = hist[0]
    hist = await tick()
    check("nothing changed → NOTHING is written (no file per period)",
          len(hist) == 1 and hist[0]["file"] == was["file"], str([h["file"] for h in hist]))

    # THE incident this rule exists for: iTerm2 restores a window and every tab comes back
    # titled a bare "claude". The session set is unchanged, so an earlier version replaced
    # the good entry with the degraded one — and the only copy of 15 tab titles was gone.
    # The real names are carried forward now, which makes it a no-op instead.
    fake.degrade = True
    hist = await tick()
    check("names degrading to 'claude' → still nothing written",
          len(hist) == 1 and hist[0]["file"] == was["file"], str([h["file"] for h in hist]))
    kept = cc_web._auto_snap_read(hist[0]["file"])
    check("...and the real names survive",
          all(n and n != "claude" for n in [x["name"] for x in kept["sessions"]]),
          str([x["name"] for x in kept["sessions"]][:2]))
    fake.degrade = False

    # Decoration is not a change either: the spinner glyph and the " (claude)" suffix come
    # and go on their own.
    fake.decorate = True
    check("decoration-only differences → nothing written", len(await tick()) == 1)
    fake.decorate = False

    # A real rename IS a change — and the old entry is KEPT. Deleting it is what cost us
    # the titles.
    fake.rename = True
    hist = await tick()
    check("a real rename → a new entry, the old one KEPT",
          len(hist) == 2 and hist[1]["file"] == was["file"], str([h["file"] for h in hist]))
    newest = cc_web._auto_snap_read(hist[0]["file"])
    check("...and the new name is what got stored",
          newest["sessions"][0]["name"].startswith("renamed"), newest["sessions"][0]["name"])
    fake.rename = False

    fake.n = 5
    hist = await tick()
    check("a DIFFERENT session set → another entry on top, older ones still there",
          len(hist) == 3 and hist[0]["count"] == 5, str([h["count"] for h in hist]))
    # Compared against its OWN saved_at, not against the other entry: both files can be
    # written inside the same second, and these timestamps only have second resolution.
    check("...and the new set's first_seen starts fresh rather than being inherited",
          hist[0]["first_seen"] == hist[0]["saved_at"],
          f'{hist[0]["first_seen"]} vs {hist[0]["saved_at"]}')

    print("=== the history is capped, oldest dropped ===")
    for i in range(cc_web.AUTO_SNAP_MAX + 5):
        cc_web._auto_snap_save([{"sid": f"{i:04d}", "cwd": "/tmp", "name": f"n{i}",
                                 "window_index": 0, "tab_index": 0}])
    hist = cc_web._auto_snap_list()
    check(f"kept at most {cc_web.AUTO_SNAP_MAX}", len(hist) == cc_web.AUTO_SNAP_MAX, str(len(hist)))
    check("...and it is the NEWEST ones that survived",
          hist[0]["count"] == 1 and json.loads((cc_web.AUTO_SNAP_DIR / hist[0]["file"]).read_text())
          ["sessions"][0]["name"] == f"n{cc_web.AUTO_SNAP_MAX + 4}")

    print("=== resume asks which store, and both are reachable ===")
    real_resume = cc_web._run_resume          # keep the genuine one for the tests below
    real_pids = cc_web._pids_for_session      # the resume tests stub this one out
    async def _noop(*a, **k):
        return None
    called = {}
    async def fake_resume(sessions):
        called["n"] = len(sessions)
        called["first"] = (sessions[0] or {}).get("name", "")
    cc_web._run_resume = fake_resume
    cc_web._resume_progress["running"] = False
    r = await cc_web.post_snapshot_resume(None)                    # no body at all
    await asyncio.sleep(0.05)
    check("no body → the manual snapshot (15)", called.get("n") == 15, str(called))
    cc_web._resume_progress["running"] = False
    r = await cc_web.post_snapshot_resume(cc_web.ResumePayload(source="auto"))
    await asyncio.sleep(0.05)
    check("source=auto → the NEWEST auto entry", called.get("n") == 1
          and called.get("first") == f"n{cc_web.AUTO_SNAP_MAX + 4}", str(called))
    cc_web._resume_progress["running"] = False
    older = cc_web._auto_snap_list()[3]
    r = await cc_web.post_snapshot_resume(cc_web.ResumePayload(source="auto", file=older["file"]))
    await asyncio.sleep(0.05)
    check("an OLDER auto entry can be picked (the point of keeping history)",
          called.get("n") == older["count"], f"{called} want {older['count']}")
    cc_web._resume_progress["running"] = False
    try:
        await cc_web.post_snapshot_resume(cc_web.ResumePayload(source="auto", file="../../etc/passwd"))
        code = 0
    except Exception as e:
        code = getattr(e, "status_code", -1)
    check("a bogus file name is refused, not read", code == 404, f"status {code}")

    print("=== resume: skips what is already running, and can be stopped ===")
    # Drive the REAL _run_resume this time (the stub above only proved source selection).
    cc_web._run_resume = real_resume
    opened, alive = [], {"aaaa1111-1111-2222-3333-444444444444"}   # pretend this one is up
    async def fake_open(cwd, sid, label):
        opened.append((sid, label))
        return "iterm-" + sid[:4]
    fake.open_resume_claude_tab = fake_open
    cc_web._pids_for_session = lambda sid: [999] if sid in alive else []
    cc_web._ensure_iterm2_running = _noop
    cc_web._clean_tab_name = lambda n: n
    want = [{"sid": "aaaa1111-1111-2222-3333-444444444444", "cwd": "/tmp/a", "name": "already-up"},
            {"sid": "bbbb2222-1111-2222-3333-444444444444", "cwd": "/tmp/b", "name": "second"},
            {"sid": "cccc3333-1111-2222-3333-444444444444", "cwd": "/tmp/c", "name": "third"}]
    cc_web._resume_progress.update({"running": True, "total": 3, "done": 0, "results": [],
                                    "resumed": 0, "cancel": False, "cancelled": False})
    await cc_web._run_resume(want)
    st = cc_web._resume_progress
    check("a session that is already running is NOT reopened",
          [o[0][:4] for o in opened] == ["bbbb", "cccc"], str([o[0][:4] for o in opened]))
    check("...it is reported as already running, not as a failure",
          any(r["status"] == "already running" for r in st["results"]),
          str([r["status"] for r in st["results"]]))
    check("...so the count reflects only what it actually opened", st["resumed"] == 2, str(st["resumed"]))
    check("the saved tab name is used as the label", opened[0][1] == "second", str(opened[0]))

    # Running it again is a no-op: whatever the first pass opened is now alive, so the
    # second pass has nothing left to do. That makes Resume safe to click twice (or to
    # re-run after a partial/cancelled attempt) without ending up with duplicate tabs.
    alive |= {sid for sid, _ in opened}
    opened.clear()
    cc_web._resume_progress.update({"running": True, "total": 3, "done": 0, "results": [],
                                    "resumed": 0, "cancel": False, "cancelled": False})
    await cc_web._run_resume(want)
    st = cc_web._resume_progress
    check("resuming the same snapshot again opens NOTHING (idempotent per session)",
          opened == [], str(opened))
    check("...and says so for every entry",
          all(r["status"] == "already running" for r in st["results"]),
          str([r["status"] for r in st["results"]]))
    # A cancelled run can simply be re-run: the ones it managed to open are skipped and
    # it picks up the rest.
    alive -= {"cccc3333-1111-2222-3333-444444444444"}
    opened.clear()
    cc_web._resume_progress.update({"running": True, "total": 3, "done": 0, "results": [],
                                    "resumed": 0, "cancel": False, "cancelled": False})
    await cc_web._run_resume(want)
    check("re-running after a partial attempt only opens what is missing",
          [o[0][:4] for o in opened] == ["cccc"], str([o[0][:4] for o in opened]))

    alive = {"aaaa1111-1111-2222-3333-444444444444"}
    cc_web._pids_for_session = lambda sid: [999] if sid in alive else []
    opened.clear()
    cc_web._resume_progress.update({"running": True, "total": 3, "done": 0, "results": [],
                                    "resumed": 0, "cancel": False, "cancelled": False})
    async def cancel_soon():
        while cc_web._resume_progress["done"] < 1:
            await asyncio.sleep(0.02)
        await cc_web.post_resume_cancel()
    await asyncio.gather(cc_web._run_resume(want), cancel_soon())
    st = cc_web._resume_progress
    check("cancel stops it part-way", st["cancelled"] is True and st["done"] < 3,
          f'done={st["done"]} cancelled={st["cancelled"]}')
    check("...leaving the tabs it already opened alone (stop ≠ kill)", len(opened) <= 2, str(len(opened)))
    check("...and it is no longer marked running", st["running"] is False)
    r = await cc_web.post_resume_cancel()
    check("cancelling when nothing is running just says so", r["ok"] is False, str(r))

    print("=== 'already running' must mean RUNNING, not 'the store still has a file' ===")
    cc_web._pids_for_session = real_pids      # the resume section above stubbed it
    # Resume reads this as "skip", so a stale entry means a session silently never comes
    # back — and a claude that was killed (which is when you reach for resume) leaves its
    # store file behind.
    store = Path(home) / ".claude" / "sessions"
    store.mkdir(parents=True, exist_ok=True)
    cc_web.CLAUDE_SESSIONS_DIR = store
    (store / "999999.json").write_text(json.dumps(
        {"pid": 999999, "sessionId": "dead-session", "startedAt": 1}))
    check("a store entry for a dead pid is NOT 'running'",
          cc_web._pids_for_session("dead-session") == [],
          str(cc_web._pids_for_session("dead-session")))
    (store / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "sessionId": "live-session"}))
    check("...and a live one still is",
          cc_web._pids_for_session("live-session") == [os.getpid()],
          str(cc_web._pids_for_session("live-session")))
    # Pid reuse: right pid, wrong process — the start time gives it away.
    (store / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "sessionId": "recycled", "startedAt": 1000}))
    check("...and a recycled pid isn't mistaken for the session",
          cc_web._pids_for_session("recycled") == [],
          str(cc_web._pids_for_session("recycled")))

    print("=== the resume list says which entries will be skipped ===")
    # Borrow the manual snapshot file, then put it back: later assertions still expect
    # the 15 sessions that were saved into it earlier.
    manual_backup = cc_web.SNAPSHOT_FILE.read_text()
    cc_web.SNAPSHOT_FILE.write_text(json.dumps({"saved_at": "x", "sessions": [
        {"sid": "live-session", "cwd": "/tmp", "name": "up", "window_index": 0, "tab_index": 0},
        {"sid": "dead-session", "cwd": "/tmp", "name": "down", "window_index": 0, "tab_index": 1}]}))
    (store / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "sessionId": "live-session"}))
    d = await cc_web.get_snapshot()
    flags = {x["sid"]: x.get("running") for x in d["sessions"]}
    check("a running session is flagged", flags.get("live-session") is True, str(flags))
    check("...and a dead one is not", flags.get("dead-session") is False, str(flags))
    cc_web.SNAPSHOT_FILE.write_text(manual_backup)

    print("=== the picker's data: both stores in one payload ===")
    d = await cc_web.get_snapshot()
    check("manual sessions at the top level", len(d["sessions"]) == 15)
    check("...auto history alongside it, newest first",
          len(d["auto"]) > 1 and d["auto"][0]["saved_at"] >= d["auto"][1]["saved_at"], str(d["auto"][:2]))
    check("...with the total and the cap, so the UI can say '40 of 100'",
          d["auto_total"] == cc_web.AUTO_SNAP_MAX and d["auto_max"] == cc_web.AUTO_SNAP_MAX,
          f'{d["auto_total"]}/{d["auto_max"]}')
    pv = await cc_web.get_snapshot_preview(d["auto"][0]["file"])
    check("preview returns that snapshot's sessions (so resume can list them)",
          pv["ok"] and len(pv["sessions"]) == d["auto"][0]["count"])
    try:
        await cc_web.get_snapshot_preview("auto-../x.json")
        code = 0
    except Exception as e:
        code = getattr(e, "status_code", -1)
    check("preview refuses a bogus name too", code == 404, f"status {code}")

    print("=== manual save refuses to write nothing ===")
    fake.mode = "closed"
    try:
        await cc_web.post_snapshot_save()
        code = 0
    except Exception as e:
        code = getattr(e, "status_code", -1)
    check("Save with an unreachable bridge is an error, not an empty file",
          code in (409, 503), f"status {code}")
    check("...and the manual 15 are untouched", len(manual_now()) == 15)

    print("=== the manual reconnect ===")
    fake.mode = "ok"; fake.n = 2
    r = await cc_web.post_bridge_reset()
    check("it drops the cached connection first", fake.dropped >= 1)
    check("...and reports what it can see", r["ok"] is True and r["tabs"] == 2, str(r))
    fake.mode = "closed"
    r = await cc_web.post_bridge_reset()
    check("a failing reconnect explains itself", r["ok"] is False and bool(r["error"]), str(r)[:70])

    shutil.rmtree(home, ignore_errors=True)
    print("=== a stored handle is a HINT, verified at the point of use ===")
    # What the bindings file stores: the session id (durable), plus an iTerm session id,
    # a pid and a tab index — all three of which perish. The handle dies when iTerm2
    # recreates the session (restored window); the pid dies when the session /exits and
    # is RESUMED, which is the same session at a new pid in possibly a new tab. It is
    # persisted so bindings survive a cc_web restart, which is fine — trusting it
    # afterwards is not. Both paths must fall back to resolving by SESSION ID.
    import ast as _ast
    src = open(os.path.join(ROOT, "cc_web.py"), encoding="utf-8").read()
    tree = _ast.parse(src)
    def fn(name):
        return next(n for n in _ast.walk(tree)
                    if isinstance(n, (_ast.AsyncFunctionDef, _ast.FunctionDef)) and n.name == name)
    def calls(node, target):
        return [c for c in _ast.walk(node) if isinstance(c, _ast.Call)
                and ((isinstance(c.func, _ast.Name) and c.func.id == target)
                     or (isinstance(c.func, _ast.Attribute) and c.func.attr == target))]
    for h in ("post_input", "get_screen"):
        node = fn(h)
        check(f"{h} re-resolves by session id rather than reporting it gone",
              len(calls(node, "_try_autobind")) >= 2,
              f"{len(calls(node, '_try_autobind'))} call(s)")
    # _try_autobind is the by-session-id resolver: claude's own pid↔session store first,
    # then --resume argv. Neither is the stored handle, which is the point.
    ab = fn("_try_autobind")
    check("...and that resolver uses claude's store, not the stored handle",
          bool(calls(ab, "_pids_for_session")) and bool(calls(ab, "list_claude_tabs")),
          "")
    check("...it never reads iterm_session_id from the old binding",
          "iterm_session_id" not in _ast.dump(ab), "")

    print("=== the bindings file stores NOTHING perishable ===")
    # It used to store the iTerm session id, the pid, its start time and the tab index.
    # Each of those dies while the session lives: the handle when iTerm2 recreates the
    # session (any restored window), the pid the moment a session /exits and is RESUMED,
    # the tab index whenever an earlier tab closes. That design is from before claude
    # published pid↔session itself; it now does, so the handle is derivable and storing
    # it only creates a way to be wrong — five sessions answered 404 for six days from an
    # id that hadn't existed since a window restore.
    bf = Path(home) / ".claude" / "cc_web_bindings.json"
    bf.parent.mkdir(parents=True, exist_ok=True)   # an earlier section removes this dir
    cc_web.BINDINGS_FILE = bf
    tbl = cc_web.BindingTable()
    from pathlib import Path as _P2
    tbl.insert(cc_web.Binding(claude_session_id="sid-keep", iterm_session_id="HANDLE-1",
                              pid=4242, pid_start=1.0, cwd="/tmp",
                              jsonl_path=_P2("/tmp/k.jsonl"), window_index=0, tab_index=7))
    on_disk = json.loads(bf.read_text())
    blob = json.dumps(on_disk)
    for perishable in ("HANDLE-1", "4242", "iterm_session_id", "pid", "tab_index"):
        check(f"{perishable!r} is not written to disk", perishable not in blob, blob[:120])
    check("the session id IS", on_disk == {"sessions": ["sid-keep"]}, blob[:120])

    print("=== a restart keeps 'attached', re-derives everything else ===")
    fresh = cc_web.BindingTable()
    check("the attached set survives", fresh.load_persisted() == 1 and fresh.attached() == {"sid-keep"},
          str(fresh.attached()))
    check("...but no handle is resurrected — it is resolved on first use",
          fresh.get_by_session("sid-keep") is None, str(fresh.get_by_session("sid-keep")))

    print("=== the legacy whole-record file is read and migrated ===")
    bf.write_text(json.dumps([{"claude_session_id": "sid-old", "iterm_session_id": "OLD",
                               "pid": 1, "pid_start": 1.0, "cwd": "/tmp",
                               "jsonl_path": "/tmp/o.jsonl"}]))
    legacy = cc_web.BindingTable()
    check("old files still say who was attached", legacy.load_persisted() == 1
          and legacy.attached() == {"sid-old"}, str(legacy.attached()))
    check("...and the file is rewritten without the perishable fields",
          json.loads(bf.read_text()) == {"sessions": ["sid-old"]}, bf.read_text()[:120])

    print("=== dropping a stale handle is not detaching ===")
    tbl2 = cc_web.BindingTable()
    tbl2.insert(cc_web.Binding(claude_session_id="sid-x", iterm_session_id="H", pid=7,
                               pid_start=1.0, cwd="/tmp", jsonl_path=_P2("/tmp/x.jsonl")))
    tbl2.remove_session("sid-x")
    check("a session stays yours when only its handle went bad",
          tbl2.attached() == {"sid-x"} and tbl2.get_by_session("sid-x") is None,
          str(tbl2.attached()))
    tbl2.forget("sid-x")
    check("an explicit detach really detaches", tbl2.attached() == set(), str(tbl2.attached()))

    print("=== re-resolving is rate-limited (it costs a whole enumeration) ===")
    # /api/screen is POLLED. Re-resolving costs an enumeration — a fresh iTerm2
    # connection — so a tab whose screen legitimately comes back empty must not turn a
    # polling endpoint into an enumeration loop. That is the same load pattern that had
    # concurrent enumerations closing each other's sockets this morning.
    calls = []
    real_ab = cc_web._try_autobind
    class NB:
        def __init__(self, h): self.iterm_session_id = h; self.claude_session_id = "sid-rr"
    async def fake_ab(sid):
        calls.append(sid)
        return NB("NEW")
    cc_web._try_autobind = fake_ab
    cc_web._last_reresolve.clear()
    try:
        first = await cc_web._reresolve_handle("sid-rr", "OLD", "test")
        second = await cc_web._reresolve_handle("sid-rr", "OLD", "test")
        check("the first attempt resolves", first is not None and first.iterm_session_id == "NEW",
              str(first))
        check("...an immediate second one does NOT enumerate again",
              second is None and len(calls) == 1, f"{len(calls)} enumeration(s)")
        # ...and it lets go once the gap has passed.
        cc_web._last_reresolve["sid-rr"] = cc_web._time.monotonic() - (cc_web.RERESOLVE_MIN_GAP + 1)
        third = await cc_web._reresolve_handle("sid-rr", "OLD", "test")
        check("...but a later one is allowed", third is not None and len(calls) == 2,
              f"{len(calls)} enumeration(s)")
        # A resolve that lands on the SAME handle is not reported as a fix.
        cc_web._last_reresolve.clear()
        async def same_ab(sid):
            calls.append(sid); return NB("OLD")
        cc_web._try_autobind = same_ab
        check("resolving to the same handle reports nothing to retry",
              await cc_web._reresolve_handle("sid-rr", "OLD", "test") is None, "")
    finally:
        cc_web._try_autobind = real_ab

    print("=== the attached list is bounded ===")
    from pathlib import Path as _P3
    bf2 = Path(home) / ".claude" / "cc_web_bindings.json"
    bf2.parent.mkdir(parents=True, exist_ok=True)
    cc_web.BINDINGS_FILE = bf2
    big = cc_web.BindingTable()
    n = cc_web.ATTACHED_MAX + 5
    for i in range(n):
        big.insert(cc_web.Binding(claude_session_id=f"s{i:04d}", iterm_session_id=f"h{i}",
                                  pid=10000 + i, pid_start=1.0, cwd="/tmp",
                                  jsonl_path=_P3("/tmp/b.jsonl")))
    check("it stops growing at the cap", big.attached_count() == cc_web.ATTACHED_MAX,
          str(big.attached_count()))
    check("...dropping the OLDEST, keeping the newest",
          "s0000" not in big.attached() and f"s{n-1:04d}" in big.attached(), "")
    check("...and the file agrees",
          len(json.loads(bf2.read_text())["sessions"]) == cc_web.ATTACHED_MAX,
          str(len(json.loads(bf2.read_text())["sessions"])))

    print(("\nFAILED: " + ", ".join(_fails)) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
