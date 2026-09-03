#!/usr/bin/env python3
"""selfcheck: the report shapes it must REFUSE.

A self-check is a self-graded exam, so the value is not in what the model writes — it
is in what cannot be written down. Each case here is one of the ways such a report
rots, made impossible mechanically rather than asked for politely in a prompt.

    python3 tests/test_selfcheck.py      # exit 0 = pass
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SC = os.path.join(ROOT, "skills", "self-check", "selfcheck.py")

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


def main():
    if not os.path.exists(SC):
        print("FAIL: selfcheck.py missing"); return 1
    home = tempfile.mkdtemp(prefix="ccweb-sc-")
    env = dict(os.environ, HOME=home, CC_SESSION_ID="aa0faa53-722e-44b3-99ea-d01f40013bcf")
    SID = env["CC_SESSION_ID"]
    memo_d = os.path.join(home, ".claude", "cc_web_memo.d")
    check_f = os.path.join(home, ".claude", "cc_web_check.d", SID + ".json")
    os.makedirs(memo_d)
    work = tempfile.mkdtemp(prefix="ccweb-sc-work-")

    def run(*args, cwd=work):
        return subprocess.run([sys.executable, SC, *args], capture_output=True, text=True,
                              env=env, cwd=cwd)

    def save(rep):
        p = os.path.join(work, "r.json")
        open(p, "w").write(json.dumps(rep, ensure_ascii=False))
        return run("save", "--file", p)

    def write_memo(task, notes, ver="2026-09-03T10:00:00"):
        open(os.path.join(memo_d, SID + ".json"), "w").write(json.dumps({
            "task": {"text": task, "updated_at": ver, "sent_at": "", "sent_count": 0},
            "notes": {"text": notes, "updated_at": ver, "sent_at": "", "sent_count": 0},
            "supervisor": None}, ensure_ascii=False))

    print("=== facts: what to check, and the state of the world ===")
    r = run("facts")
    check("runs before any memo exists", r.returncode == 0, r.stderr[:80])
    f = json.loads(r.stdout)
    check("...and says plainly that there is nothing to check", f["has_task"] is False, str(f["has_task"]))
    write_memo("把进度条修好, 别动 UI 布局", "部署前先跑测试套件")
    f = json.loads(run("facts").stdout)
    check("reads the human's two boxes", f["task"].startswith("把进度条") and "测试套件" in f["notes"],
          f["task"][:20])
    check("...binds a task VERSION, so a report can be tied to it", bool(f["memo_ver"]), f["memo_ver"])
    check("...and no checklist is pinned on the first run", f["pinned"] is None, str(f["pinned"]))
    check("...git state comes from git, not from recollection", "repo" in f["git"], str(f["git"])[:60])

    print("=== a verdict with no evidence cannot be stored ===")
    r = save({"verdict": "ok", "summary": "都好",
              "items": [{"claim": "进度条修好了", "status": "done"}]})
    check("rejected", r.returncode != 0 and "no evidence" in r.stderr, r.stderr.strip()[:90])
    check("...and nothing was written", not os.path.exists(check_f))
    r = save({"verdict": "ok", "summary": "都好",
              "items": [{"claim": "进度条修好了", "status": "done",
                         "evidence": [{"cmd": "git log --oneline -1", "out": ""}]}]})
    check("an evidence entry with empty output is also rejected",
          r.returncode != 0 and "real" in r.stderr, r.stderr.strip()[:70])
    r = save({"verdict": "ok", "summary": "无法判断",
              "items": [{"claim": "布局没被改动", "status": "unverifiable"}]})
    check("'unverifiable' without saying what would make it checkable is rejected",
          r.returncode != 0, r.stderr.strip()[:70])

    print("=== a report cannot claim to be about a different task version ===")
    ok_item = {"claim": "进度条修好了", "status": "done",
               "evidence": [{"cmd": "git log --oneline -1", "out": "abc123 fix progress bar"}]}
    r = save({"verdict": "ok", "summary": "符合", "items": [ok_item],
              "memo_ver": "我说我是针对最新版本的", "checked_at": "2001-01-01"})
    check("stored", r.returncode == 0, r.stderr[:80])
    rep = json.load(open(check_f))
    check("...with the version stamped by the script, not by the report",
          rep["memo_ver"] == "2026-09-03T10:00:00|2026-09-03T10:00:00", rep["memo_ver"])
    check("...and checked_at likewise", not rep["checked_at"].startswith("2001"), rep["checked_at"])
    check("...the checklist is now pinned", rep["pinned"]["claims"] == ["进度条修好了"],
          str(rep["pinned"]))

    print("=== the checklist cannot be quietly rewritten ===")
    # The failure this prevents: on the day an item is inconvenient, drop it and report
    # green on the remaining easy ones.
    r = save({"verdict": "ok", "summary": "符合",
              "items": [{"claim": "顺手改了个别的, 这个通过", "status": "done",
                         "evidence": [{"cmd": "true", "out": "ok"}]}]})
    check("rejected while the task text is unchanged", r.returncode != 0 and "dropped" in r.stderr,
          r.stderr.strip().splitlines()[0][:80] if r.stderr else "")
    check("...and it names what was dropped and what was added", "进度条修好了" in r.stderr)
    check("...the stored report is untouched", json.load(open(check_f))["pinned"]["claims"] == ["进度条修好了"])
    write_memo("换个任务: 查缓存", "部署前先跑测试套件", ver="2026-09-03T12:00:00")
    r = save({"verdict": "ok", "summary": "符合",
              "items": [{"claim": "缓存问题定位到了", "status": "done",
                         "evidence": [{"cmd": "true", "out": "ok"}]}]})
    check("but a NEW task re-derives the list on purpose", r.returncode == 0, r.stderr[:80])
    check("...and re-pins it", json.load(open(check_f))["pinned"]["claims"] == ["缓存问题定位到了"])

    print("=== the verdict must match the items ===")
    r = save({"verdict": "ok", "summary": "都好",
              "items": [{"claim": "缓存问题定位到了", "status": "not_done",
                         "evidence": [{"cmd": "true", "out": "ok"}]}]})
    check("not_done items with verdict ok → rejected", r.returncode != 0 and "cannot be ok" in r.stderr,
          r.stderr.strip()[:70])
    r = save({"verdict": "deviations", "summary": "都好",
              "items": [{"claim": "缓存问题定位到了", "status": "done",
                         "evidence": [{"cmd": "true", "out": "ok"}]}]})
    check("...and 'deviations' with nothing deviating → rejected", r.returncode != 0,
          r.stderr.strip()[:70])

    print("=== 'this task is not mine' must be argued, not asserted ===")
    # The case the human asked for by name: a task written into the wrong session must
    # be REPORTED, not executed. It is also the most convenient verdict available, so
    # it costs a reason and evidence.
    r = save({"verdict": "not_mine", "summary": "不是我的"})
    check("bare not_mine → rejected", r.returncode != 0 and "not_mine_reason" in r.stderr,
          r.stderr.strip()[:70])
    r = save({"verdict": "not_mine", "summary": "不是我的", "not_mine_reason": "任务讲的是另一个仓库"})
    check("...a reason alone is still not enough", r.returncode != 0 and "evidence" in r.stderr,
          r.stderr.strip()[:70])
    r = save({"verdict": "not_mine", "summary": "不是我的",
              "not_mine_reason": "任务讲的是 audio_player 项目, 这个 session 在 cc-web 仓库里",
              "not_mine_evidence": [{"cmd": "pwd", "out": work}]})
    check("with a reason AND evidence it is stored", r.returncode == 0, r.stderr[:80])
    check("...and it says so, loudly, in the summary output",
          "不是给这个 session" in r.stdout and "拒绝执行" in r.stdout, r.stdout.strip()[-60:])

    print("=== an empty session, and a disputed task ===")
    os.remove(os.path.join(memo_d, SID + ".json"))
    r = save({"verdict": "ok", "summary": "x", "items": [ok_item]})
    check("with no task at all, only no_task is honest", r.returncode != 0 and "no_task" in r.stderr,
          r.stderr.strip()[:70])
    r = save({"verdict": "no_task", "summary": "没有任务"})
    check("...and no_task needs no items", r.returncode == 0, r.stderr[:70])
    write_memo("任务假设 X 成立", "n")
    r = save({"verdict": "disputed", "summary": "任务前提不成立"})
    check("'the task itself is wrong' needs the dispute spelled out",
          r.returncode != 0 and "disputes" in r.stderr, r.stderr.strip()[:70])
    r = save({"verdict": "disputed", "summary": "任务前提不成立",
              "disputes": [{"about": "task", "reason": "实测 X 不成立"}]})
    check("...then it is stored", r.returncode == 0, r.stderr[:70])

    print("=== a stale report announces itself ===")
    write_memo("任务又改了", "n", ver="2026-09-04T09:00:00")
    r = run("show")
    check("show() warns the report predates the current task",
          "更早版本" in r.stdout, r.stdout.strip()[:60])

    print("=== green is one line ===")
    write_memo("单一任务", "n", ver="2026-09-05T09:00:00")
    save({"verdict": "ok", "summary": "全部符合",
          "items": [{"claim": "a", "status": "done", "evidence": [{"cmd": "true", "out": "ok"}]},
                    {"claim": "b", "status": "done", "evidence": [{"cmd": "true", "out": "ok"}]}]})
    out = run("show").stdout.strip().splitlines()
    check("an all-clear prints two lines at most, not a wall of ticks",
          len(out) <= 2, str(len(out)) + " lines: " + " / ".join(out)[:80])

    shutil.rmtree(home, ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)
    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
