#!/usr/bin/env python3
"""Per-session memo: the current task, and the standing notes that outlive it.

Two boxes the HUMAN writes, one file per session, each sendable into the session as a
tagged message. Server side is exercised against a temp HOME by calling the real
endpoint functions; the client's half (which tag, which door) is asserted by
extracting it from static/index.html.

    python3 tests/test_session_memo.py      # exit 0 = pass
"""
import json
import os
import re
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
    home = tempfile.mkdtemp(prefix="ccweb-memo-")
    os.makedirs(os.path.join(home, ".claude"))
    os.environ["HOME"] = home
    os.environ["CC_WEB_TOKEN"] = "t"
    try:
        import cc_web
    except Exception as e:                                  # pragma: no cover
        print("SKIP: cannot import cc_web:", e); return 0
    from fastapi import HTTPException

    P = cc_web.MemoPayload
    SID = "aa0faa53-722e-44b3-99ea-d01f40013bcf"

    print("=== an untouched session costs nothing ===")
    rec = cc_web.get_session_memo(claude_session_id=SID)
    check("reads as a blank record, not an error",
          rec["task"]["text"] == "" and rec["notes"]["text"] == "", json.dumps(rec)[:60])
    check("...with the supervisor slot present but unused (reserved)",
          "supervisor" in rec and rec["supervisor"] is None, str(rec.get("supervisor")))
    check("...and no file written just for looking",
          not os.path.exists(os.path.join(home, ".claude", "cc_web_memo.d", SID + ".json")))
    check("...so its polls carry no memo_ver at all", cc_web._memo_ver(SID) is None,
          str(cc_web._memo_ver(SID)))

    print("=== the two fields are independent ===")
    cc_web.post_session_memo(P(claude_session_id=SID, field="task", text="  修进度条, 别动布局  "))
    cc_web.post_session_memo(P(claude_session_id=SID, field="notes", text="不要引入新依赖"))
    rec = cc_web.get_session_memo(claude_session_id=SID)
    check("the task is stored, trimmed", rec["task"]["text"] == "修进度条, 别动布局", rec["task"]["text"])
    check("the standing notes are stored separately", rec["notes"]["text"] == "不要引入新依赖", rec["notes"]["text"])
    check("...each with its own updated_at", bool(rec["task"]["updated_at"]) and bool(rec["notes"]["updated_at"]))
    # This is the whole reason for two boxes: rewriting the task must not cost you the
    # standing rules, or you stop keeping them.
    cc_web.post_session_memo(P(claude_session_id=SID, field="task", text="改成: 查缓存问题"))
    rec = cc_web.get_session_memo(claude_session_id=SID)
    check("rewriting the task leaves the notes alone",
          rec["task"]["text"] == "改成: 查缓存问题" and rec["notes"]["text"] == "不要引入新依赖",
          rec["task"]["text"] + " | " + rec["notes"]["text"])

    print("=== 'I sent this' is recorded, per field ===")
    cc_web.post_session_memo(P(claude_session_id=SID, field="task", mark_sent=True))
    cc_web.post_session_memo(P(claude_session_id=SID, field="task", mark_sent=True))
    rec = cc_web.get_session_memo(claude_session_id=SID)
    check("counted", rec["task"]["sent_count"] == 2, str(rec["task"]["sent_count"]))
    check("...and stamped", bool(rec["task"]["sent_at"]), rec["task"]["sent_at"])
    check("...without touching the other field's count", rec["notes"]["sent_count"] == 0)
    check("mark_sent alone does not alter the text", rec["task"]["text"] == "改成: 查缓存问题")

    print("=== the poll only carries a version, not the strings ===")
    v1 = cc_web._memo_ver(SID)
    check("memo_ver is an int once there is a file", isinstance(v1, int), str(v1))
    os.utime(os.path.join(home, ".claude", "cc_web_memo.d", SID + ".json"), (v1 + 30, v1 + 30))
    check("...and it moves when the file does", cc_web._memo_ver(SID) == v1 + 30, str(cc_web._memo_ver(SID)))

    print("=== a session id is a filename, so it is checked ===")
    for bad in ("../../etc/passwd", "a/b", "", "x", "a\\b"):
        got = None
        try:
            cc_web.post_session_memo(P(claude_session_id=bad, field="task", text="x"))
        except HTTPException as e:
            got = e.status_code
        except Exception as e:
            got = type(e).__name__
        check(f"refused: {bad!r}", got == 400, str(got))
    # codex's pre-binding aliases must still work — they are real session keys
    cc_web.post_session_memo(P(claude_session_id="pending-pane-%35", field="task", text="ok"))
    check("a codex pending-pane alias is accepted",
          cc_web.get_session_memo(claude_session_id="pending-pane-%35")["task"]["text"] == "ok")
    check("...and nothing escaped the memo dir",
          sorted(os.listdir(os.path.join(home, ".claude", "cc_web_memo.d")))
          == sorted([SID + ".json", "pending-pane-%35.json"]),
          str(os.listdir(os.path.join(home, ".claude", "cc_web_memo.d"))))

    print("=== an unknown field is refused, so a typo cannot invent one ===")
    got = None
    try:
        cc_web.post_session_memo(P(claude_session_id=SID, field="supervisor", text="x"))
    except HTTPException as e:
        got = e.status_code
    check("field must be task or notes", got == 400, str(got))

    print("=== emptied completely → the file goes away ===")
    cc_web.post_session_memo(P(claude_session_id="pending-pane-%35", field="task", text=""))
    check("no text and never sent → nothing left on disk",
          not os.path.exists(os.path.join(home, ".claude", "cc_web_memo.d", "pending-pane-%35.json")))

    print("=== per agent, like every other state file ===")
    check("the directory is agent-scoped (a codex instance gets its own)",
          cc_web._state_path("cc_web_memo.d").name == "cc_web_memo.d" if not cc_web.IS_CODEX
          else cc_web._state_path("cc_web_memo.d").name == "cc_web_memo.codex.d",
          cc_web._state_path("cc_web_memo.d").name)

    print("=== the button fills the composer; it does not send ===")
    src = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    tag = re.search(r"const MEMO_TAG = \{ (.*?) \};", src)
    check("each field has its own tag, so the model can tell them apart",
          bool(tag) and 'task: "[当前任务]"' in tag.group(1) and 'notes: "[注意事项]"' in tag.group(1),
          tag.group(1) if tag else "not found")
    fill = re.search(r"function memoToInput\(f\) \{.*?\n  \}", src, re.S)
    body = fill.group(0) if fill else ""
    check("memoToInput exists", bool(body), "not found")
    # The whole point of the change: one button press must not reach the session. You
    # look at it in the box, add the sentence that made you reach for it, then send.
    check("it does NOT post to /api/input — nothing is sent by pressing it",
          "/api/input" not in body, body[:80])
    check("...it writes into the composer instead", "inputEl.value =" in body)
    check("...with the tag leading", 'MEMO_TAG[f] + " " + text' in body)
    check("...and a half-written draft kept, not overwritten",
          'cur.trim() ? line + "\\n" + cur : line' in body, body[body.find("const cur"):][:120])
    check("...and it saves the box before filling, so the two cannot disagree",
          "memoPost({ field: f, text })" in body)
    check("...then closes the modal", 'memoModal.classList.remove("show")' in body)

    print("=== 'sent' is counted on the real send, wherever the text came from ===")
    send = re.search(r"async function send\(\) \{.*?\n  \}", src, re.S)
    sbody = send.group(0) if send else ""
    check("send() recognises a memo by its leading tag",
          "body.startsWith(MEMO_TAG[k])" in sbody, "found" if sbody else "send() not found")
    check("...and stamps mark_sent then — so a reminder typed by hand counts too",
          "mark_sent: true" in sbody)
    check("a refresh landing mid-typing does not overwrite the box",
          "document.activeElement !== memoTA[f]" in src)
    check("the poll drives the refresh off memo_ver", "memoSync(m.memo_ver)" in src)

    print("=== the popup is full screen, not a dialog ===")
    css = re.search(r"#memo-modal \.memo-card \{(.*?)\}", src, re.S)
    cbody = css.group(1) if css else ""
    check("the card fills the viewport", "100vw" in cbody and "max-width: 100vw" in cbody, cbody.strip()[:70])
    # Without min-height:0 a textarea will not shrink under its rows= and shoves the
    # second box (and its button) off the bottom — measured before this was added.
    check("...and both boxes split the height rather than the card scrolling",
          "flex: 1; min-height: 0" in src and "min-height: 64px" in src)

    print("" if not _fails else "")
    print("FAILED: " + ", ".join(_fails) if _fails else "all pass")
    shutil.rmtree(home, ignore_errors=True)
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
