#!/usr/bin/env python3
"""Never open a second claude on a conversation that is already running.

Resume skips a session when `_pids_for_session(sid)` is non-empty, so that answer is the
only thing standing between "restore what was lost" and "two claudes appending to one
transcript".

It said "not running" about a session that was, and this is why: the check compared the
process's real start time to `startedAt` in claude's store with `abs(...) <= 5s`. Those
two are not the same clock. `startedAt` is written when claude INITIALISES the session,
and claude asks "do you trust this folder?" first — on mac-pro (2026-09-23) that dialog
held one session 49s, so its record was stamped 49s after its process began, the check
read that as pid reuse, and resume opened it again. Two `claude --resume d069d324…`
processes, two tabs.

Direction is everything: a process OLDER than its record is the normal case (claude
writes the record later), while a recycled pid can only be NEWER (the record was written
by whoever held the pid then). So the test is one-sided, and `ps` is consulted as a second
opinion that does not depend on claude's store at all.

    python3 tests/test_resume_dedup.py      # exit 0 = pass
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


def main():
    home = tempfile.mkdtemp(prefix="ccweb-resume-")
    os.makedirs(os.path.join(home, ".claude"))
    os.environ["HOME"] = home
    os.environ["CC_WEB_TOKEN"] = "t"
    try:
        import cc_web
    except Exception as e:                                  # pragma: no cover
        print("SKIP: cannot import cc_web:", e); return 0

    store = cc_web.CLAUDE_SESSIONS_DIR
    store.mkdir(parents=True, exist_ok=True)
    SID = "d069d324-de79-4168-b9a5-7dd9faa2a223"
    OTHER = "33fdb662-1111-2222-3333-444444444444"

    # This process is alive and is its own best fixture: os.kill(pid, 0) succeeds and
    # _pid_start_cached returns a real start time.
    me = os.getpid()
    my_start = cc_web._pid_start_cached(me)
    if my_start <= 0:
        print("SKIP: cannot read this process's start time"); return 0

    def record(sid, pid, started_at, updated=1):
        (store / f"{pid}.json").write_text(json.dumps(
            {"sessionId": sid, "pid": pid, "startedAt": int(started_at * 1000),
             "updatedAt": updated}), encoding="utf-8")

    # ps is stubbed so the store half can be tested on its own (and so the suite does not
    # depend on what happens to be running on the machine).
    cc_web._argv_resume_pids = lambda ttl=2.0: {}

    print("=== the session that got resumed twice ===")
    # The record stamped 49s AFTER the process started: claude sat on the trust dialog.
    record(SID, me, my_start + 49.9)
    check("a record written 49s after its process still counts as running",
          cc_web._pids_for_session(SID) == [me], str(cc_web._pids_for_session(SID)))
    for gap in (5.0, 60.0, 3600.0):
        record(SID, me, my_start + gap)
        check(f"...and at +{int(gap)}s too (there is no upper bound on how long a "
              "prompt waits)", cc_web._pids_for_session(SID) == [me])

    print("=== pid reuse is still caught ===")
    # The only case the start time can actually detect: the record predates this process,
    # i.e. it was written by a different one that held the pid earlier.
    record(SID, me, my_start - 600)
    check("a process that started long AFTER its record is not the owner",
          cc_web._pids_for_session(SID) == [], str(cc_web._pids_for_session(SID)))
    record(SID, me, my_start - 2)
    check("...with a few seconds of slack for clock/rounding noise",
          cc_web._pids_for_session(SID) == [me])

    print("=== a dead pid is dead, whatever the store says ===")
    dead = 999_999
    (store / f"{dead}.json").write_text(json.dumps(
        {"sessionId": OTHER, "pid": dead, "startedAt": int(my_start * 1000)}),
        encoding="utf-8")
    check("a leftover record for a pid that is gone counts as not running",
          cc_web._pids_for_session(OTHER) == [], str(cc_web._pids_for_session(OTHER)))

    print("=== ps answers even when the store does not ===")
    # The second opinion: resume's question is "is a claude holding this conversation",
    # and `claude --resume <sid>` in the process table answers it without claude's help.
    for f in store.glob("*.json"):
        f.unlink()
    cc_web._argv_resume_pids = lambda ttl=2.0: {SID: [me]}
    check("a live `claude --resume <sid>` counts with no store record at all",
          cc_web._pids_for_session(SID) == [me], str(cc_web._pids_for_session(SID)))
    record(SID, me, my_start)
    check("...and is not counted twice when the store knows it too",
          cc_web._pids_for_session(SID) == [me], str(cc_web._pids_for_session(SID)))
    cc_web._argv_resume_pids = lambda ttl=2.0: {}

    print("=== the real ps scan ===")
    cc_web._ARGV_PIDS["at"] = 0.0
    real = cc_web._argv_resume_pids(ttl=0.0)
    check("it returns a sid -> pids map without raising", isinstance(real, dict), str(type(real)))
    check("...keyed by session id, never by a flag or a path",
          all(("-" in k and "/" not in k) for k in real), str(list(real)[:3]))
    t0 = time.monotonic()
    for _ in range(50):
        cc_web._argv_resume_pids()
    # 50 sessions × a fork of ps is the hot path this cache exists for.
    check("...and repeat calls are cached, not 50 forks of ps",
          time.monotonic() - t0 < 0.3, f"{time.monotonic() - t0:.2f}s")

    print("=== the resume loop reports a bridge drop honestly ===")
    src = open(os.path.join(ROOT, "cc_web.py"), encoding="utf-8").read()
    i = src.index("async def _run_resume")
    body = src[i:i + 4000]
    # open_resume_claude_tab raised after the tab had opened ("sent 1000 (OK); no close
    # frame received"), resume said "failed", and the retry is what made the second tab.
    check("a failed open re-checks the process table before calling it failed",
          "_pids_for_session(sid)" in body.split("except Exception as ex:")[1][:700],
          "not in the except branch")
    tail = body.split("except Exception as ex:")[1][:1400]
    check("...and the result is still recorded exactly once, on both paths",
          tail.count('st["results"].append') == 1, str(tail.count('st["results"].append')))

    print("=== a resumed tab keeps the name the snapshot saved ===")
    # The other half of "I could not find it": resume set the SESSION name, which is the
    # one claude's OSC title overwrites as soon as it has a summary — `teams` turned into
    # `✳ gen-teams`, `slide_eval` into `◑ slides_eval`. The tab title is a manual
    # override; measured on mac-pro (2026-09-23) by setting it and then having the shell
    # emit `ESC]0;OSC-WON BEL` in that tab: the override survived.
    ib = open(os.path.join(ROOT, "iterm_bridge.py"), encoding="utf-8").read()
    blk = ib[ib.index("async def _open_claude_tab"):][:2600]
    check("the label is set as the TAB title, which OSC cannot overwrite",
          "async_set_title(label)" in blk)
    check("...and as the session name as well (harmless, and what iTerm shows in menus)",
          "async_set_name(label)" in blk)
    check("...through the same call the rename endpoint uses, so they cannot diverge",
          "tab.async_set_title(name)" in ib)

    print("=== the browser counts an already-running session as OK ===")
    idx = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    j = idx.index("const stopped = !!st.cancelled;")
    blk = idx[j:j + 1800]
    # "resumed 1/19" for a run where 18 sessions were up is what prompted "why did 45c0
    # fail to resume" about a session that had been running for two hours.
    check("the headline counts everything that is up, not just the new tabs",
          "rows.length - bad.length" in blk and "${okN}/${st.total}" in blk)
    check("...and failures are listed first", 'bad.map(line).join("\\n")' in blk)

    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
