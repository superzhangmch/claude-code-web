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
    # The reserved slot now holds the whip's policy for this session. Reserved became
    # used, and "used" means: present, empty, and NOT registered until a human writes
    # a policy and turns the switch on.
    check("...with the supervisor slot present but unarmed",
          isinstance(rec.get("supervisor"), dict) and rec["supervisor"]["enabled"] is False
          and rec["supervisor"]["policy"] == "" and rec.get("watched") is False,
          str(rec.get("supervisor")))
    check("...and no file written just for looking",
          not os.path.exists(os.path.join(home, ".claude", "cc_web_memo.d", SID + ".json")))
    check("...so its polls carry no memo_ver at all", cc_web._memo_ver(SID) is None,
          str(cc_web._memo_ver(SID)))

    print("=== the two fields are independent ===")
    cc_web.post_session_memo(P(claude_session_id=SID, task="  修进度条, 别动布局  "))
    cc_web.post_session_memo(P(claude_session_id=SID, notes="不要引入新依赖"))
    rec = cc_web.get_session_memo(claude_session_id=SID)
    check("the task is stored, trimmed", rec["task"]["text"] == "修进度条, 别动布局", rec["task"]["text"])
    check("the standing notes are stored separately", rec["notes"]["text"] == "不要引入新依赖", rec["notes"]["text"])
    check("...each with its own updated_at", bool(rec["task"]["updated_at"]) and bool(rec["notes"]["updated_at"]))
    # This is the whole reason for two boxes: rewriting the task must not cost you the
    # standing rules, or you stop keeping them.
    cc_web.post_session_memo(P(claude_session_id=SID, task="改成: 查缓存问题"))
    rec = cc_web.get_session_memo(claude_session_id=SID)
    check("rewriting the task leaves the notes alone",
          rec["task"]["text"] == "改成: 查缓存问题" and rec["notes"]["text"] == "不要引入新依赖",
          rec["task"]["text"] + " | " + rec["notes"]["text"])

    print("=== 'I sent this' is recorded, per field ===")
    cc_web.post_session_memo(P(claude_session_id=SID, mark_sent="task"))
    cc_web.post_session_memo(P(claude_session_id=SID, mark_sent="task"))
    rec = cc_web.get_session_memo(claude_session_id=SID)
    check("counted", rec["task"]["sent_count"] == 2, str(rec["task"]["sent_count"]))
    check("...and stamped", bool(rec["task"]["sent_at"]), rec["task"]["sent_at"])
    check("...without touching the other field's count", rec["notes"]["sent_count"] == 0)
    check("mark_sent alone does not alter the text", rec["task"]["text"] == "改成: 查缓存问题")

    print("=== one version, and editing rewrites it ===")
    # There WERE versions here: a list, fork, set-current, delete, and editing an old
    # one without making it current. Retired on request — "只要能维护好一个版本不就行了" —
    # and it was the right call: the task is a thing you rewrite as the work moves, not
    # a thing you keep a history of, and the history came with its own way to go wrong
    # (editing an old version believing it was the live one).
    #
    # The storage kept its shape with a single entry, so files written by the versions
    # build still load. What had to survive is `rev`.
    cc_web.post_session_memo(P(claude_session_id=SID, task="第一版"))
    cc_web.post_session_memo(P(claude_session_id=SID, task="改过的"))
    got = cc_web.get_session_memo(claude_session_id=SID)
    check("still exactly one version", len(got["versions"]) == 1, str(len(got["versions"])))
    check("...and the text is the latest", got["task"]["text"] == "改过的", got["task"]["text"])
    # rev is the load-bearing survivor: the self-check's "对不上现在的任务" and the whip's
    # "I already have a confirmation for this task" both compare against it.
    r1 = got["rev"]
    cc_web.post_session_memo(P(claude_session_id=SID, task="又改了"))
    check("rev moves when the text moves",
          cc_web.get_session_memo(claude_session_id=SID)["rev"] == r1 + 1, str(r1))
    cc_web.post_session_memo(P(claude_session_id=SID, task="又改了"))
    check("...and not when the same text is posted again",
          cc_web.get_session_memo(claude_session_id=SID)["rev"] == r1 + 1)
    # The actions are gone from the API, not just from the buttons: a stale client
    # posting `fork: true` must not quietly get a second version.
    for gone in ("fork", "set_current", "delete", "label", "version"):
        check(f"the `{gone}` action is gone from the API", gone not in cc_web.MemoPayload.model_fields)

    print("=== a file written by the pre-versions build still opens ===")
    # Those files were written by an earlier build of this same panel; "please re-type
    # it" would be an odd thing to say about a memo.
    flat = {"task": {"text": "旧格式任务", "updated_at": "2026-09-01T10:00:00",
                     "sent_at": "", "sent_count": 3},
            "notes": {"text": "旧格式注意事项", "updated_at": "2026-09-01T10:00:00",
                      "sent_at": "", "sent_count": 0},
            "rev": 7, "supervisor": None}
    open(os.path.join(home, ".claude", "cc_web_memo.d", SID + ".json"), "w").write(
        json.dumps(flat, ensure_ascii=False))
    got = cc_web.get_session_memo(claude_session_id=SID)
    check("it becomes version 1, with its text and its counts intact",
          got["current"] == 1 and got["task"]["text"] == "旧格式任务"
          and got["task"]["sent_count"] == 3 and got["notes"]["text"] == "旧格式注意事项",
          json.dumps(got["task"], ensure_ascii=False)[:70])
    check("...and its rev is kept, so an existing self-check report is not made stale "
          "by the migration alone", got["rev"] == 7, str(got["rev"]))

    print("=== the composer route writes the box, like every other edit ===")
    src0 = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    menu0 = re.search(r"const setBox = \(field\) => async \(\) => \{.*?\n    \};", src0, re.S)
    check("set task desc/constrain posts the one field", bool(menu0)
          and "{ task: text }" in menu0.group(0), (menu0.group(0)[:60] if menu0 else "?"))
    # It used to fork a new version when the text arrived from outside the panel. There
    # is nothing to fork into now.
    check("...and no version machinery is left in the page",
          "fork: true" not in src0 and "memoEditing" not in src0
          and "memo-vers" not in src0)

    print("=== the poll only carries a version, not the strings ===")
    v1 = cc_web._memo_ver(SID)
    check("memo_ver is an int once there is a file", isinstance(v1, int), str(v1))
    os.utime(os.path.join(home, ".claude", "cc_web_memo.d", SID + ".json"), (v1 + 30, v1 + 30))
    check("...and it moves when the file does", cc_web._memo_ver(SID) == v1 + 30, str(cc_web._memo_ver(SID)))

    print("=== a session id is a filename, so it is checked ===")
    for bad in ("../../etc/passwd", "a/b", "", "x", "a\\b"):
        got = None
        try:
            cc_web.post_session_memo(P(claude_session_id=bad, task="x"))
        except HTTPException as e:
            got = e.status_code
        except Exception as e:
            got = type(e).__name__
        check(f"refused: {bad!r}", got == 400, str(got))
    # codex's pre-binding aliases must still work — they are real session keys
    cc_web.post_session_memo(P(claude_session_id="pending-pane-%35", task="ok"))
    check("a codex pending-pane alias is accepted",
          cc_web.get_session_memo(claude_session_id="pending-pane-%35")["task"]["text"] == "ok")
    check("...and nothing escaped the memo dir",
          sorted(os.listdir(os.path.join(home, ".claude", "cc_web_memo.d")))
          == sorted([SID + ".json", "pending-pane-%35.json"]),
          str(os.listdir(os.path.join(home, ".claude", "cc_web_memo.d"))))

    print("=== only the two boxes are writable ===")
    # The reserved supervisor slot is not a third notes field: there is no way to put
    # anything in it through this door, by typo or otherwise.
    check("the payload has no supervisor field at all",
          "supervisor" not in P.model_fields, str(list(P.model_fields)))
    got = None
    try:
        cc_web.post_session_memo(P(claude_session_id=SID, mark_sent="supervisor"))
    except HTTPException as e:
        got = e.status_code
    check("mark_sent must name one of the two", got == 400, str(got))
    got = None
    try:
        cc_web.post_session_memo(P(claude_session_id=SID))
    except HTTPException as e:
        got = e.status_code
    check("a request that would do nothing is refused rather than rewriting the file",
          got == 400, str(got))

    print("=== two writes at once must not eat each other ===")
    # THE bug: the save button posted its two fields concurrently. Both writers used
    # the same <sid>.json.tmp, the second replace() died FileNotFoundError, and the
    # loser had read a half-replaced file, concluded both boxes were empty and deleted
    # the record. What the human had typed was gone.
    import threading as _th
    cc_web.post_session_memo(P(claude_session_id=SID, task="并发前的任务", notes="并发前的注意事项"))
    errs = []

    def _hammer(which, n):
        for i in range(n):
            try:
                cc_web.post_session_memo(P(claude_session_id=SID, **{which: f"{which}-{i}"}))
            except Exception as e:                       # noqa: BLE001 — any escape is a fail
                errs.append(f"{which}: {type(e).__name__}: {e}")

    ts = [_th.Thread(target=_hammer, args=("task", 25)), _th.Thread(target=_hammer, args=("notes", 25))]
    [t.start() for t in ts]; [t.join() for t in ts]
    check("no writer blew up", not errs, "; ".join(errs[:2]))
    rec = cc_web.get_session_memo(claude_session_id=SID)
    check("...the record still exists", bool(rec["task"]["text"] or rec["notes"]["text"]),
          json.dumps(rec)[:60])
    check("...and NEITHER field was wiped by the other",
          rec["task"]["text"].startswith("task-") and rec["notes"]["text"].startswith("notes-"),
          f'{rec["task"]["text"]!r} / {rec["notes"]["text"]!r}')
    check("...one request can carry both, which is why the button needs no concurrency",
          set(["task", "notes"]).issubset(P.model_fields))

    print("=== a file that exists but will not parse is not overwritten ===")
    # One bad read must not become "the boxes were empty, so I deleted them".
    open(os.path.join(home, ".claude", "cc_web_memo.d", SID + ".json"), "w").write("{ this is not json")
    got = None
    try:
        cc_web.post_session_memo(P(claude_session_id=SID, task="踩上去"))
    except HTTPException as e:
        got = e.status_code
    check("refused, loudly", got == 500, str(got))
    check("...and the file is untouched, for a human to look at",
          open(os.path.join(home, ".claude", "cc_web_memo.d", SID + ".json")).read().startswith("{ this"))
    os.remove(os.path.join(home, ".claude", "cc_web_memo.d", SID + ".json"))

    print("=== emptied completely → the file goes away ===")
    cc_web.post_session_memo(P(claude_session_id="pending-pane-%35", task=""))
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
    check("...with the tag leading", 'MEMO_TAG[f] + " " + text' in body)
    # The insert itself moved into memoPutInInput when the fill-both button arrived —
    # one copy of "never eat a half-written draft", shared by both entry points, rather
    # than two that can drift.
    put = re.search(r"function memoPutInInput\(line\) \{.*?\n  \}", src, re.S)
    pb = put.group(0) if put else ""
    check("...it writes into the composer instead", bool(pb) and "inputEl.value =" in pb, pb[:60])
    check("...and a half-written draft kept, not overwritten",
          'cur.trim() ? line + "\\n" + cur : line' in pb, pb[pb.find("const cur"):][:120])
    check("...and it is the ONE place that does so",
          src.count('cur.trim() ? line + "\\n" + cur : line') == 1)
    # It deliberately does NOT save: saving is the button and only the button, so a
    # one-off variation can be sent without committing it to the memo. The box keeps
    # saying 未保存… while its text sits in the composer, which is the honest state.
    check("...and does NOT quietly save on the way (no autosave anywhere)",
          "memoPost" not in body, body[:60])
    check("...then closes the modal", 'memoModal.classList.remove("show")' in pb)

    print("=== filling the composer: one button, and it never sends ===")
    # There were four buttons here: three all saying `run check`, distinguished only by
    # which row they stood in, plus `set periodic check`. Asked about directly ("这四个
    # 啥意思?"), and the answer was that the design was the problem. Now: one 填入输入框
    # per box, one for both, and no appended instruction — you write the sentence you
    # actually meant.
    # Looked for in the MARKUP, not the whole file: the comment explaining why they
    # went mentions the old label, and a check that trips over its own rationale is a
    # check nobody keeps.
    body_html = src[:src.index("<script>")]
    check("no `run check` buttons remain",
          "run check" not in body_html and "check 本框" not in body_html)
    fn = re.search(r"function memoToInputBoth\(\) \{.*?\n  \}", src, re.S)
    fb = fn.group(0) if fn else ""
    check("both boxes go in, each tagged as itself",
          "MEMO_TAG.task" in fb and "MEMO_TAG.notes" in fb, fb[:60])
    check("...with neither box required", "(!t && !n)" in fb, fb[fb.find("if (!attached"):][:50])
    check("...and it fills the composer rather than sending",
          "/api/input" not in fb and "memoPutInInput" in fb)
    # One implementation of "put it in the composer": the "never eat a half-written
    # draft" rule must not exist in two copies that can drift apart.
    check("the per-box and both buttons share one insert path",
          src.count("function memoPutInInput") == 1 and src.count("memoPutInInput(") >= 3)
    # `set periodic check` became a CHECKBOX, because the button read as though cc-web
    # would run the watcher. It never did — it typed a request at the session, and the
    # real periodic check is ⚙ → Watch → 检查周期.
    check("the watcher request is now an opt-in checkbox",
          'id="memo-watcher"' in src and 'type="checkbox"' in src)
    check("...appended only when it is ticked", "cb.checked" in fb, fb[fb.find("const cb"):][:60])
    check("...and its wording says WHO runs it",
          "不是 cc-web 起" in src and "Watch → 检查周期" in src)
    check("...while the real period lives server-side",
          "period_min" in open(os.path.join(ROOT, "cc_web.py"), encoding="utf-8").read())
    check("a button with nothing to fill in is disabled, not silently inert",
          "function memoAskBtns()" in src and "b.disabled = !(t || n)" in src)

    print("=== the composer can fill the boxes (long-press on send) ===")
    menu = re.search(r"const setBox = \(field\) => async \(\) => \{.*?\n    \};", src, re.S)
    mb = menu.group(0) if menu else ""
    check("both items exist", '"set task desc"' in src and '"set task constrain"' in src)
    # One field per request: the other box may not even be loaded in this view, and
    # posting both would write whatever stale value happens to be in the DOM.
    # Names ONE field: the other box may not even be loaded in this view, so sending
    # both would write whatever stale value is sitting in the DOM. The endpoint leaves
    # an omitted field alone, which is what makes that safe.
    check("it names ONE field, so the other box cannot be clobbered",
          "{ task: text }" in mb and "{ notes: text }" in mb,
          mb[mb.find("const ok"):][:90])
    check("...clears the composer (the box owns the text now)", 'inputEl.value = ""' in mb)
    check("...then re-reads the stored copy and opens the modal, so 'saved' is visible",
          "memoLoad(attachedSid" in mb and 'memoModal.classList.add("show")' in mb)

    print("=== 'sent' is counted on the real send, wherever the text came from ===")
    send = re.search(r"async function send\(\) \{.*?\n  \}", src, re.S)
    sbody = send.group(0) if send else ""
    check("send() recognises a memo by its leading tag",
          "body.startsWith(MEMO_TAG[k])" in sbody, "found" if sbody else "send() not found")
    check("...and stamps mark_sent for THAT field — so a reminder typed by hand counts too",
          "mark_sent: mtag" in sbody)
    # The intent, not the literal body: typing marks state (and refreshes which
    # prompt buttons are usable) but must not schedule a write.
    typed = src[src.index("function memoTyped()"):src.index("async function memoSaveAll")]
    check("nothing saves on a timer — typing only marks the state",
          "memoMark(true)" in typed and "setTimeout" not in typed and "memoPost" not in typed,
          typed.splitlines()[0][:70])
    check("...and closing with unsaved text asks instead of discarding silently",
          "有未保存的改动" in src)
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
