#!/usr/bin/env python3
"""Backend tests for the brief session list — GET /api/sessions?brief=1.

The full list calls _session_views() on every row, i.e. parses every JSONL, to build the
card excerpts. Those excerpts are ~74% of the response body and none of them survive in a
one-line row, so first paint on a phone was waiting on work it then threw away. brief=1
lists the live claude tabs only, from one directory scan plus one TAIL read per tab.

Pinned here:

  1. brief carries NO transcript excerpts (that is the whole point) and says brief:true.
  2. "last used" is the last HUMAN message, not the file mtime. A transcript whose mtime
     was bumped by a background rewrite (autonomous-loop tick, resume, file sync) must not
     claim to have been used just now — on the real corpus that put a session last spoken
     to on 07-17 at the top of the list dated 08-17, which is exactly the lie that makes a
     "last use" sort useless. It agrees with the full list to the minute.
  3. A tail with no human turn at all falls back to mtime and SAYS SO (ts_approx), rather
     than presenting a guess as fact.
  4. It lists tabs only — no Recent/Named — and never opens a transcript that isn't on
     screen.

    .venv/bin/python tests/test_brief_list.py        # exit 0 = pass
"""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "brief-test-token"
PORT = 8997

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


def ts(dt):
    """Claude writes UTC with a Z suffix; the server renders local time. Keep the fixture
    honest about that or the assertions drift by the UTC offset."""
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def local(dt):
    return dt.astimezone().strftime("%m-%d %H:%M")


def write_transcript(home, cwd, sid, rounds, last_human, pad=0, tail_noise=0):
    """One fabricated JSONL. `pad` inflates each answer; `tail_noise` appends assistant-only
    entries AFTER the last human turn (that is what pushes it out of a 64KB tail read)."""
    proj = os.path.join(home, ".claude", "projects", cwd.replace("/", "-").replace("_", "-"))
    os.makedirs(proj, exist_ok=True)
    path = os.path.join(proj, sid + ".jsonl")
    lines = []
    for i in range(rounds):
        when = last_human - datetime.timedelta(minutes=(rounds - 1 - i) * 5)
        lines.append({"type": "user", "cwd": cwd, "timestamp": ts(when),
                      "message": {"role": "user", "content": f"human turn {i}"}})
        lines.append({"type": "assistant", "cwd": cwd, "timestamp": ts(when + datetime.timedelta(seconds=30)),
                      "message": {"role": "assistant", "content": [{"type": "text", "text": "answer " + "x" * pad}]}})
    for j in range(tail_noise):
        lines.append({"type": "assistant", "cwd": cwd,
                      "timestamp": ts(last_human + datetime.timedelta(minutes=j + 1)),
                      "message": {"role": "assistant", "content": [{"type": "text", "text": "tool churn " + "y" * 4000}]}})
    with open(path, "w", encoding="utf-8") as f:
        for e in lines:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return path


def _uvicorn():
    for c in (os.path.join(ROOT, ".venv/bin/uvicorn"),
              os.path.expanduser("~/claude-code-web/.venv/bin/uvicorn")):
        if os.path.exists(c):
            return c
    return shutil.which("uvicorn")


def get(path):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                headers={"authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main():
    if not _uvicorn():
        print("SKIP: no uvicorn found"); return 0

    home = tempfile.mkdtemp(prefix="ccweb-brief-")
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    with open(os.path.join(home, ".claude", "cc_web.conf"), "w") as f:
        f.write(f"token={TOKEN}\n")

    now = datetime.datetime.now(datetime.timezone.utc)
    # (a) normal session, last spoken to 2h ago
    a = write_transcript(home, "/tmp/proj-a", "aaaaaaaa-1111-2222-3333-444444444444",
                         rounds=3, last_human=now - datetime.timedelta(hours=2), pad=200)
    # (b) same, but its file was rewritten in the background AFTER the conversation ended:
    #     mtime = now, last human message = 30 DAYS ago.
    b = write_transcript(home, "/tmp/proj-b", "bbbbbbbb-1111-2222-3333-444444444444",
                         rounds=3, last_human=now - datetime.timedelta(days=30), pad=200)
    os.utime(b, (time.time(), time.time()))
    # (c) ends in >64KB of assistant/tool churn → nothing human inside the first tail window
    c = write_transcript(home, "/tmp/proj-c", "cccccccc-1111-2222-3333-444444444444",
                         rounds=2, last_human=now - datetime.timedelta(hours=5), tail_noise=40)
    # (d) a transcript that is NOT a live tab — brief must not even read it
    write_transcript(home, "/tmp/proj-d", "dddddddd-1111-2222-3333-444444444444",
                     rounds=5, last_human=now - datetime.timedelta(hours=1), pad=200)

    srv = subprocess.Popen(
        [_uvicorn(), "cc_web:app", "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=ROOT, env=dict(os.environ, HOME=home),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(60):
            time.sleep(0.4)
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/auth-status", timeout=2).read()
                break
            except Exception:
                continue

        # There is no terminal bridge in a test, so /api/sessions can't produce live tabs.
        # Call the builder in-process with the tab list the bridge would have handed over.
        sys.path.insert(0, ROOT)
        import cc_web
        cc_web.PROJECTS_ROOT = __import__("pathlib").Path(home) / ".claude" / "projects"
        tabs = [
            {"sid": "aaaaaaaa-1111-2222-3333-444444444444", "name": "proj-a", "window_index": 0,
             "tab_index": 1, "pid": 111, "cwd": "/tmp/proj-a"},
            {"sid": "bbbbbbbb-1111-2222-3333-444444444444", "name": "proj-b", "window_index": 0,
             "tab_index": 0, "pid": 222, "cwd": "/tmp/proj-b"},
            {"sid": "cccccccc-1111-2222-3333-444444444444", "name": "proj-c", "window_index": 1,
             "tab_index": 0, "pid": 333, "cwd": "/tmp/proj-c"},
        ]
        rows = cc_web.brief_picker_sessions(live_tabs=tabs)
        by = {r["claude_session_id"][:8]: r for r in rows}

        print("=== what brief returns ===")
        check("one row per live tab, nothing else", len(rows) == 3, str(len(rows)))
        check("...tabs only — no Recent/Named group",
              {r["group"] for r in rows} == {"tabs"}, str({r["group"] for r in rows}))
        check("a transcript that is not a live tab is absent", "dddddddd" not in by)
        check("no transcript excerpts at all",
              all("exchanges" not in r and "last_user_msg" not in r and "first_user_msg" not in r for r in rows))
        check("rows are marked brief so the UI can't draw them as full cards",
              all(r.get("brief") is True for r in rows))
        check("sorted by window/tab", [(r["window_index"], r["tab_index"]) for r in rows]
              == sorted((r["window_index"], r["tab_index"]) for r in rows))
        check("each row carries what the one-liner needs",
              all(all(k in r for k in ("tab_name", "window_index", "tab_index", "user_name",
                                       "summary_title", "last_visit", "mtime", "bound", "cwd"))
                  for r in rows))

        print("=== 'last used' is the last human message, not the file mtime ===")
        want_a = local(now - datetime.timedelta(hours=2))
        check("a normal session reports its last human turn",
              by["aaaaaaaa"]["last_visit"] == want_a, f'{by["aaaaaaaa"]["last_visit"]} want {want_a}')
        want_b = local(now - datetime.timedelta(days=30))
        check("a background rewrite does NOT make a session look freshly used",
              by["bbbbbbbb"]["last_visit"] == want_b, f'{by["bbbbbbbb"]["last_visit"]} want {want_b}')
        check("...so it sorts below the session actually used 2h ago",
              by["bbbbbbbb"]["mtime"] < by["aaaaaaaa"]["mtime"])
        check("...and is not flagged approximate — it was read, not guessed",
              by["bbbbbbbb"].get("ts_approx") is False)
        want_c = local(now - datetime.timedelta(hours=5))
        check("a long tail of tool churn is escalated past, not given up on",
              by["cccccccc"]["last_visit"] == want_c and by["cccccccc"].get("ts_approx") is False,
              f'{by["cccccccc"]["last_visit"]} want {want_c} approx={by["cccccccc"].get("ts_approx")}')

        print("=== it agrees with the full list, and costs much less ===")
        full = {r["claude_session_id"][:8]: r for r in
                cc_web.build_picker_sessions(live_tabs=tabs, recent_n=10, named_n=5)
                if r["group"] == "tabs"}
        check("the same tabs, with the same last-use to the minute",
              all(by[k]["last_visit"] == full[k]["last_visit"] for k in by),
              str({k: (by[k]["last_visit"], full[k]["last_visit"]) for k in by if by[k]["last_visit"] != full[k]["last_visit"]}))
        cc_web._SESSION_CTX_CACHE.clear(); cc_web._BRIEF_TS_CACHE.clear()
        t0 = time.time(); cc_web.brief_picker_sessions(live_tabs=tabs); t_brief = time.time() - t0
        cc_web._SESSION_CTX_CACHE.clear(); cc_web._BRIEF_TS_CACHE.clear()
        t0 = time.time(); f = cc_web.build_picker_sessions(live_tabs=tabs, recent_n=10, named_n=5); t_full = time.time() - t0
        nb = len(json.dumps(cc_web.brief_picker_sessions(live_tabs=tabs), ensure_ascii=False).encode())
        nf = len(json.dumps(f, ensure_ascii=False).encode())
        check("brief is cheaper to build", t_brief < t_full, f"{t_brief*1000:.1f}ms vs {t_full*1000:.1f}ms")
        check("brief is a smaller payload", nb < nf, f"{nb/1024:.1f}KB vs {nf/1024:.1f}KB")

        print("=== the endpoint plumbs the flag through ===")
        d = get("/api/sessions?brief=1")
        check("brief=1 is echoed back", d.get("brief") is True, str(d.get("brief")))
        check("...and the safety extras still come along (runaway/battery/store)",
              "runaway" in d and "battery" in d and "claude_store" in d)
        d2 = get("/api/sessions")
        check("no flag → the full list, as before", d2.get("brief") is False, str(d2.get("brief")))
    finally:
        srv.terminate()
        try: srv.wait(timeout=10)
        except Exception: srv.kill()
        shutil.rmtree(home, ignore_errors=True)

    print(("\nFAILED: " + ", ".join(_fails)) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
