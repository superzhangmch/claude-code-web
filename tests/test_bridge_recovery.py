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
    def __init__(self, sid, i):
        self.claude_session_id = sid
        self.name = f"tab{i}"
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
    check("through all of that, the manual 15 were never touched", len(manual_now()) == 15)

    cc_web._resume_ended_mono = cc_web._time.monotonic() - (cc_web.SNAPSHOT_QUIET_AFTER_RESUME + 5)
    hist = await tick()
    check("once quiet, the live list IS recorded", len(hist) == 1 and hist[0]["count"] == 3, str(hist))
    check("...in the auto directory, leaving the manual file alone", len(manual_now()) == 15)
    hist = await tick()
    check("unchanged → NO second file (diff, not a file per hour)", len(hist) == 1, str(len(hist)))
    fake.n = 5
    hist = await tick()
    check("changed → a new entry, newest first",
          len(hist) == 2 and hist[0]["count"] == 5 and hist[1]["count"] == 3,
          str([h["count"] for h in hist]))

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
    print(("\nFAILED: " + ", ".join(_fails)) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
