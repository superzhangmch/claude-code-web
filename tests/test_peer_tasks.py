#!/usr/bin/env python3
"""ask_peer --task / --tasks: reading what a session is SUPPOSED to be doing.

A supervisor's question is not "what did this session say" — it is "what was it asked
to do, and what did its own last check say about that". Neither is in the transcript,
which is exactly why the Task memo exists. This is the read side of it, and it lives in
ask-peer because ask-peer already knows how to find a session across hosts; a second
skill would be a second copy of that.

Drives the real functions with a stubbed HTTP layer.

    python3 tests/test_peer_tasks.py      # exit 0 = pass
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "skills", "ask-peer-claude-code", "ask_peer.py")

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


def load():
    spec = importlib.util.spec_from_file_location("ask_peer", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    if not os.path.exists(SCRIPT):
        print("SKIP: ask_peer.py not found"); return 0
    ap = load()

    # The whole HTTP layer, replaced. Two hosts: one answers, one is down — a
    # supervisor that silently drops an unreachable machine is worse than useless,
    # since "nobody is watching over there" is the thing it exists to notice.
    calls = []
    TABS = {"https://a:8443": [{"sid": "1111aaaa", "session_name": "有任务的"},
                               {"sid": "2222bbbb", "session_name": "没任务的"}]}
    MEMO = {"1111aaaa": {"task": {"text": "把进度条修好"}, "notes": {"text": "别加依赖"},
                         "current": 3, "versions": [1, 2, 3]}}
    CHECK = {"1111aaaa": {"exists": True, "stale": True,
                          "report": {"verdict": "deviations", "checked_at": "2026-09-04T09:00:00",
                                     "summary": "两项没做", "deviations": ["测试没跑", "没部署"],
                                     "disputes": [{"about": "notes", "reason": "这条该变成门禁"}]}}}

    def fake_req(method, url, token, body=None, timeout=30):
        calls.append(url)
        base = url.split("/api/")[0]
        if "/api/tabs" in url:
            if base not in TABS:
                raise OSError("connection refused")
            return {"tabs": TABS[base]}
        sid = url.split("claude_session_id=")[-1]
        if "/api/session-memo" in url:
            if sid not in MEMO:
                return {"task": {"text": ""}, "notes": {"text": ""}, "current": 1, "versions": [1]}
            return MEMO[sid]
        if "/api/session-check" in url:
            return CHECK.get(sid, {"exists": False})
        raise AssertionError("unexpected url " + url)

    ap._req = fake_req
    ap._local_ip = lambda: "a"
    ap._known_hosts = lambda: ["a", "b"]
    ap._bases = lambda h: [f"https://{h}:8443"]

    print("=== one session: intent beside outcome ===")
    got = ap._one_task("https://a:8443", "t", "a", "1111aaaa")
    check("the human-written task comes back", got["task"] == "把进度条修好", got["task"])
    check("...and the standing notes with it", got["notes"] == "别加依赖", got["notes"])
    check("...and which version is current", got["version"] == 3 and got["versions"] == 3,
          f'v{got["version"]} of {got["versions"]}')
    c = got["check"]
    check("the last self-check verdict", c["verdict"] == "deviations", str(c["verdict"]))
    check("...its deviations, which are the part worth reading",
          c["deviations"] == ["测试没跑", "没部署"], str(c["deviations"]))
    check("...its disputes about the task itself", c["disputes"] == ["这条该变成门禁"], str(c["disputes"]))
    # A report about a rewritten task certifies the wrong thing, so this must travel
    # WITH the verdict — never be left for the reader to go and ask about separately.
    check("...and staleness rides along with it", c["stale"] is True, str(c["stale"]))

    print("=== the supervisor view: every session, both hosts ===")
    got = ap._all_tasks(None, "t")
    check("both sessions listed", got["count"] == 2, str(got["count"]))
    check("...including the one nobody set a task for — that is worth seeing",
          got["with_task"] == 1 and any(not r["task"] and not r["notes"] for r in got["sessions"]),
          str(got["with_task"]))
    check("...with the session's name, so a human can tell which is which",
          [r["name"] for r in got["sessions"]] == ["有任务的", "没任务的"],
          str([r["name"] for r in got["sessions"]]))
    check("an unreachable host is REPORTED, not quietly skipped",
          got["unreachable"] and "b:8443" in got["unreachable"][0], str(got["unreachable"]))
    check("a session with no report says so rather than inventing one",
          got["sessions"][1]["check"] is None, str(got["sessions"][1]["check"]))
    check("nothing was sent — no /api/input anywhere",
          not any("/api/input" in u for u in calls), str([u for u in calls if "input" in u]))
    check("...and no transcript was read either: intent and verdict only",
          not any("/api/state" in u for u in calls), str([u for u in calls if "state" in u]))

    print("=== a memo that cannot be read is said so, not guessed ===")
    def broken(method, url, token, body=None, timeout=30):
        if "/api/session-memo" in url:
            raise OSError("boom")
        return fake_req(method, url, token, body, timeout)
    ap._req = broken
    got = ap._one_task("https://a:8443", "t", "a", "1111aaaa")
    check("it reports the failure instead of showing an empty task",
          "note" in got and "unreadable" in got["note"], str(got.get("note")))

    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
