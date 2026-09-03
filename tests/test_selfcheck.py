#!/usr/bin/env python3
"""The self-check gate: the report shapes cc-web must REFUSE.

A self-check is a self-graded exam, so the value is not in what the model writes — it
is in what cannot be stored. Each case here is one of the ways such a report rots.

These checks live in cc-web, not in the skill's script, and that is the point: a
validator inside a script the agent runs is advice — the agent can write the report
file itself and skip it. Behind the endpoint that owns the file it is a gate, and both
agents get the same one. So the tests drive the ENDPOINT.

    python3 tests/test_selfcheck.py      # exit 0 = pass
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


def main():
    home = tempfile.mkdtemp(prefix="ccweb-sc-")
    os.makedirs(os.path.join(home, ".claude"))
    os.environ["HOME"] = home
    os.environ["CC_WEB_TOKEN"] = "t"
    try:
        import cc_web
    except Exception as e:                                  # pragma: no cover
        print("SKIP: cannot import cc_web:", e); return 0
    from fastapi import HTTPException

    SID = "aa0faa53-722e-44b3-99ea-d01f40013bcf"
    MP, CP = cc_web.MemoPayload, cc_web.CheckPayload

    def save(**kw):
        """Returns (ok, detail). 422 is the gate talking."""
        try:
            return True, cc_web.post_session_check(CP(claude_session_id=kw.pop("sid", SID), **kw))
        except HTTPException as e:
            return False, str(e.detail)

    def memo(task="修进度条, 别动 UI 布局", notes="部署前先跑测试套件"):
        cc_web.post_session_memo(MP(claude_session_id=SID, task=task, notes=notes))

    ev = [{"cmd": "git log --oneline -1", "out": "abc123 fix progress bar"}]

    print("=== nothing to check, and the report knows it ===")
    ok, d = save(verdict="ok", summary="x", items=[{"claim": "a", "status": "done", "evidence": ev}])
    check("with no task at all, only no_task is honest", not ok and "no_task" in d, str(d)[:70])
    ok, d = save(verdict="no_task", summary="没有任务")
    check("...and no_task needs no items", ok, str(d)[:70])
    got = cc_web.get_session_check(claude_session_id=SID)
    check("it reads back", got["exists"] and got["report"]["verdict"] == "no_task")

    print("=== a verdict with no evidence cannot be stored ===")
    memo()
    ok, d = save(verdict="ok", summary="都好", items=[{"claim": "进度条修好了", "status": "done"}])
    check("rejected", not ok and "no evidence" in d, str(d)[:80])
    ok, d = save(verdict="ok", summary="都好",
                 items=[{"claim": "x", "status": "done", "evidence": [{"cmd": "git log", "out": ""}]}])
    check("...and so is evidence with no real output", not ok and "real output" in d, str(d)[:70])
    ok, d = save(verdict="ok", summary="无法判断",
                 items=[{"claim": "布局没被改动", "status": "unverifiable"}])
    check("'unverifiable' must say what would make it checkable", not ok, str(d)[:70])
    ok, d = save(verdict="ok", summary="无法判断",
                 items=[{"claim": "布局没被改动", "status": "unverifiable",
                         "note": "需要一条断言结构不变量的测试"}])
    check("...with that said, it is accepted (and needs no evidence)", ok, str(d)[:70])

    print("=== the report cannot date itself ===")
    # New task text, so the pinned list from the previous section is re-derived on
    # purpose rather than colliding with it (which the section after this one tests).
    memo(task="修进度条")
    ok, rep = save(verdict="ok", summary="符合", items=[{"claim": "进度条修好了", "status": "done", "evidence": ev}])
    check("stored", ok, str(rep)[:70])
    ver = rep["memo_ver"]
    # A counter, not a timestamp: second-resolution stamps made two edits inside one
    # second indistinguishable, and the pinning rule then rejected an honest re-derive.
    check("the task version is stamped by the server", bool(ver) and ver.startswith("r"), ver)
    check("...as is checked_at", rep["checked_at"][:2] == "20", rep["checked_at"])
    check("...and the checklist is now pinned", rep["pinned"]["claims"] == ["进度条修好了"], str(rep["pinned"]))

    print("=== the checklist cannot be quietly rewritten ===")
    # The failure this prevents: on the day an item is inconvenient, drop it and report
    # green on the remaining easy ones.
    ok, d = save(verdict="ok", summary="符合",
                 items=[{"claim": "换了个容易的", "status": "done", "evidence": ev}])
    check("rejected while the task text is unchanged", not ok and "must not either" in d, str(d)[:80])
    check("...and it names what was dropped", "进度条修好了" in str(d))
    check("...the stored report is untouched",
          cc_web.get_session_check(claude_session_id=SID)["report"]["pinned"]["claims"] == ["进度条修好了"])
    memo(task="换个任务: 查缓存")
    ok, rep = save(verdict="ok", summary="符合",
                   items=[{"claim": "缓存问题定位到了", "status": "done", "evidence": ev}])
    check("but a NEW task re-derives the list on purpose", ok, str(rep)[:70])
    check("...and re-pins it", rep["pinned"]["claims"] == ["缓存问题定位到了"])
    # Rewriting only the STANDING NOTES must invalidate it too — the checklist is
    # derived from both boxes.
    memo(task="换个任务: 查缓存", notes="另外: 不要引入新依赖")
    ok, rep = save(verdict="ok", summary="符合",
                   items=[{"claim": "没有引入新依赖", "status": "done", "evidence": ev}])
    check("changing only the notes also re-derives it", ok, str(rep)[:60])

    print("=== the verdict must match the items ===")
    ok, d = save(verdict="ok", summary="都好",
                 items=[{"claim": "没有引入新依赖", "status": "not_done", "evidence": ev}])
    check("not_done with verdict ok → rejected", not ok and "cannot be ok" in d, str(d)[:70])
    ok, d = save(verdict="deviations", summary="都好",
                 items=[{"claim": "没有引入新依赖", "status": "done", "evidence": ev}])
    check("...and 'deviations' with nothing deviating → rejected", not ok, str(d)[:70])
    ok, rep = save(verdict="deviations", summary="有一项没做",
                   items=[{"claim": "没有引入新依赖", "status": "not_done", "evidence": ev}])
    check("a real deviation is stored, and listed for reading", ok and rep["deviations"] == ["没有引入新依赖"],
          str(rep.get("deviations")))

    print("=== 'this task is not mine' must be argued, not asserted ===")
    # The case asked for by name: a task written into the wrong session must be
    # REPORTED, not executed. It is also the most convenient verdict available.
    ok, d = save(verdict="not_mine", summary="不是我的")
    check("bare not_mine → rejected", not ok and "not_mine_reason" in d, str(d)[:70])
    ok, d = save(verdict="not_mine", summary="不是我的", not_mine_reason="讲的是另一个仓库")
    check("...a reason alone is not enough", not ok and "evidence" in d, str(d)[:70])
    ok, rep = save(verdict="not_mine", summary="不是我的", not_mine_reason="讲的是另一个仓库",
                   not_mine_evidence=[{"cmd": "pwd", "out": "/home/x/other"}])
    check("with a reason AND evidence it is stored", ok, str(rep)[:70])
    check("...and it needs no checked items (there was nothing to check)", ok and rep["items"] == [])

    print("=== 'the task itself is wrong' has somewhere to go ===")
    ok, d = save(verdict="disputed", summary="前提不成立")
    check("disputed must spell out the dispute", not ok and "disputes" in d, str(d)[:70])
    ok, rep = save(verdict="disputed", summary="前提不成立",
                   disputes=[{"about": "task", "reason": "实测 X 不成立"}])
    check("...then it is stored", ok and rep["disputes"][0]["about"] == "task", str(rep.get("disputes")))

    print("=== a stale report announces itself ===")
    memo(task="任务又改了")
    got = cc_web.get_session_check(claude_session_id=SID)
    check("the server marks it stale rather than leaving it to the reader",
          got["stale"] is True, str(got["stale"]))
    check("...and the poll carries a version so a viewer notices",
          isinstance(cc_web._check_ver(SID), int), str(cc_web._check_ver(SID)))

    print("=== a session id is a filename here too ===")
    for bad in ("../../etc/passwd", "a/b", ""):
        ok, d = save(sid=bad, verdict="no_task", summary="x")
        check(f"refused: {bad!r}", not ok, str(d)[:50])
    check("...and nothing escaped the report dir",
          os.listdir(os.path.join(home, ".claude", "cc_web_check.d")) == [SID + ".json"],
          str(os.listdir(os.path.join(home, ".claude", "cc_web_check.d"))))

    print("=== the skill is a thin client: config in ONE place ===")
    sc = open(os.path.join(ROOT, "skills", "self-check", "selfcheck.py"), encoding="utf-8").read()
    check("it reads the project's single config file for the token",
          'CONF = HOME / ".claude" / "cc_web.conf"' in sc)
    check("...and does NOT re-derive cc-web's state directory names",
          "cc_web_memo" not in sc and "cc_web_check.d" not in sc)
    check("...asking cc-web which agent this is instead of guessing",
          "/api/server-info" in sc and "/api/session-memo" in sc)
    check("...and storing the report through the gate",
          "/api/session-check" in sc)
    check("the port comes from the running process, not a constant",
          "cc_web:app" in sc and "--port" in sc)

    shutil.rmtree(home, ignore_errors=True)
    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
