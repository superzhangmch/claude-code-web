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

    print("=== versions: editing is not a version, forking is ===")
    cc_web.post_session_memo(P(claude_session_id=SID, task="第一版任务", notes="长期规矩"))
    got = cc_web.get_session_memo(claude_session_id=SID)
    check("one version to begin with", len(got["versions"]) == 1 and got["current"] == 1,
          json.dumps(got["versions"])[:60])
    cc_web.post_session_memo(P(claude_session_id=SID, task="第一版任务(改了措辞)"))
    got = cc_web.get_session_memo(claude_session_id=SID)
    check("...and editing does NOT make another one — that is the whole split",
          len(got["versions"]) == 1 and got["task"]["text"] == "第一版任务(改了措辞)",
          str(len(got["versions"])))
    r2 = cc_web.post_session_memo(P(claude_session_id=SID, fork=True))
    check("fork makes one and switches to it", len(r2["versions"]) == 2 and r2["current"] == 2,
          f'{len(r2["versions"])} / v{r2["current"]}')
    # A fork starts from the current content, not from blank: it is "this task, but
    # going a different way", and an empty start means retyping the half that has not
    # changed — which is how the standing notes stop being kept up to date.
    check("...copied from the one you were on", r2["task"]["text"] == "第一版任务(改了措辞)"
          and r2["notes"]["text"] == "长期规矩", r2["task"]["text"])
    check("...and it has sent nothing yet, whatever the old one had sent",
          r2["task"]["sent_count"] == 0)
    cc_web.post_session_memo(P(claude_session_id=SID, task="第二版: 换个方向"))
    texts = [v["task"]["text"] for v in cc_web.get_session_memo(claude_session_id=SID)["versions"]]
    check("editing the fork leaves the older version alone",
          texts == ["第一版任务(改了措辞)", "第二版: 换个方向"], str(texts))

    print("=== ...and one of them is current ===")
    r3 = cc_web.post_session_memo(P(claude_session_id=SID, set_current=1))
    check("set_current switches which boxes everything else sees",
          r3["current"] == 1 and r3["task"]["text"] == "第一版任务(改了措辞)", r3["task"]["text"])
    check("...and that counts as a change of intent, so a self-check re-derives",
          cc_web._memo_ver_str(SID) == f"r{r3['rev']}" and r3["rev"] > r2["rev"],
          f"{r2['rev']} -> {r3['rev']}")
    got = None
    try:
        cc_web.post_session_memo(P(claude_session_id=SID, delete=1))
    except HTTPException as e:
        got = e.detail
    check("deleting the CURRENT version is refused — a delete must not silently change "
          "what the session is working to", got and "current" in str(got), str(got)[:60])
    cc_web.post_session_memo(P(claude_session_id=SID, delete=2))
    check("...a non-current one goes",
          len(cc_web.get_session_memo(claude_session_id=SID)["versions"]) == 1)
    got = None
    try:
        cc_web.post_session_memo(P(claude_session_id=SID, delete=1))
    except HTTPException as e:
        got = e.detail
    check("...and the last one cannot be deleted at all", got and "only version" in str(got), str(got)[:50])
    got = None
    try:
        cc_web.post_session_memo(P(claude_session_id=SID, set_current=99))
    except HTTPException as e:
        got = e.status_code
    check("a version that does not exist is a 404, not a new blank one", got == 404, str(got))

    print("=== an OLD version can be opened, edited and saved — without going current ===")
    # A list you can only switch to is a list of things you cannot fix. And fixing a
    # typo in an old version must not mean telling the session, even for a moment,
    # that it is now working to it.
    cc_web.post_session_memo(P(claude_session_id=SID, task="v1 的任务", notes="规矩"))
    cc_web.post_session_memo(P(claude_session_id=SID, fork=True, task="v2 的任务"))
    before = cc_web.get_session_memo(claude_session_id=SID)
    r = cc_web.post_session_memo(P(claude_session_id=SID, version=1, task="v1 被修好了"))
    check("the write lands in the version named", 
          [v["task"]["text"] for v in r["versions"]] == ["v1 被修好了", "v2 的任务"],
          str([v["task"]["text"] for v in r["versions"]]))
    check("...current is untouched", r["current"] == before["current"] == 2, str(r["current"]))
    check("...and so are the boxes everything downstream reads",
          r["task"]["text"] == "v2 的任务", r["task"]["text"])
    # rev is the EFFECTIVE intent. Editing a version nobody is working to changes none.
    check("...and rev does not move, so a self-check report stays valid",
          r["rev"] == before["rev"], f"{before['rev']} -> {r['rev']}")
    check("editing the current one DOES move rev",
          cc_web.post_session_memo(P(claude_session_id=SID, version=2, task="v2 改了"))["rev"]
          > before["rev"])
    got = None
    try:
        cc_web.post_session_memo(P(claude_session_id=SID, version=99, task="x"))
    except HTTPException as e:
        got = e.status_code
    check("a version that does not exist is a 404, not a new one", got == 404, str(got))
    r = cc_web.post_session_memo(P(claude_session_id=SID, fork=True, version=1, task="进了新版本"))
    check("fork ignores `version` — a fork writes into the one it just made",
          r["current"] == 3 and r["task"]["text"] == "进了新版本", f'v{r["current"]} {r["task"]["text"]}')
    check("...and every version comes back IN FULL, or it could not be edited",
          all(isinstance(v["task"], dict) and "text" in v["task"] for v in r["versions"]),
          str(type(r["versions"][0]["task"])))

    print("=== ...and the panel loads it without the guard fighting the click ===")
    src1 = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    check("opening a version forces the boxes to redraw",
          "memoRender(true); memoVerRender(); memoMark(false);" in src1)
    # The don't-stomp-what-you-are-typing guard is for a poll landing mid-sentence; it
    # must not refuse the load you just asked for. It did: after loading v1 the box had
    # focus, so clicking back to v2 left v1's text under a "editing v2" label.
    check("...over the mid-typing guard, which is what force is for",
          "(force || document.activeElement !== memoTA[f])" in src1)
    check("saving names the version being edited", "version: memoEditing || undefined" in src1)
    check("the bar says when the boxes are NOT the live version",
          "不是当前版本" in src1)

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

    print("=== the composer route makes a version, the boxes are edited in place ===")
    src0 = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    menu0 = re.search(r"const setBox = \(field\) => async \(\) => \{.*?\n    \};", src0, re.S)
    check("set task desc/constrain forks",
          menu0 and "fork: true" in menu0.group(0), (menu0.group(0)[:60] if menu0 else "?"))
    check("...while 保存 does not — it writes the version being edited",
          "version: memoEditing || undefined" in src0 and "fork" not in
          src0[src0.index("async function memoSaveAll"):src0.index("async function memoSaveAll") + 400])
    check("fork takes what is in the boxes right now, saved or not",
          "memoPost({ fork: true, task: memoTA.task.value, notes: memoTA.notes.value })" in src0)
    check("moving to another version with unsaved edits warns instead of dropping them",
          "未保存的改动 —— 离开会丢弃它" in src0)

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
    check("...it writes into the composer instead", "inputEl.value =" in body)
    check("...with the tag leading", 'MEMO_TAG[f] + " " + text' in body)
    check("...and a half-written draft kept, not overwritten",
          'cur.trim() ? line + "\\n" + cur : line' in body, body[body.find("const cur"):][:120])
    # It deliberately does NOT save: saving is the button and only the button, so a
    # one-off variation can be sent without committing it to the memo. The box keeps
    # saying 未保存… while its text sits in the composer, which is the honest state.
    check("...and does NOT quietly save on the way (no autosave anywhere)",
          "memoPost" not in body, body[:60])
    check("...then closes the modal", 'memoModal.classList.remove("show")' in body)

    print("=== the prompt builders paste the CONTENT, and still never send ===")
    ask = re.search(r"const MEMO_ASK = \{(.*?)\};", src, re.S)
    abody = ask.group(1) if ask else ""
    check("run check asks about the task", "请据此检查当前任务." in abody, abody.strip()[:50])
    check("set periodic check asks for a 30-minute watcher",
          "每 30分钟执行一次的watcher" in abody and "任务完成后" in abody and "替换它" in abody,
          abody.strip()[-60:])
    fn = re.search(r"function memoAsk\(kind, scope\) \{.*?\n  \}", src, re.S)
    fb = fn.group(0) if fn else ""
    check("it fences the box's own text rather than pointing at a file",
          '"```\\n" + body + "\\n```\\n"' in fb, fb[fb.find("const line"):][:70])
    check("...fills the composer and does not send", "/api/input" not in fb and "inputEl.value =" in fb)
    check("...and an empty box asks nothing", "if (!attachedSid || !body) return;" in fb)
    # Both boxes at once has to say which is which, or "these two things" is unreadable.
    check("the both-boxes prompt labels the two", "当前任务:" in src and "注意事项(与当前任务无关" in src)
    check("a button with nothing to ask about is disabled, not silently inert",
          "function memoAskBtns()" in src and "b.disabled = !on" in src)

    print("=== the composer can fill the boxes (long-press on send) ===")
    menu = re.search(r"const setBox = \(field\) => async \(\) => \{.*?\n    \};", src, re.S)
    mb = menu.group(0) if menu else ""
    check("both items exist", '"set task desc"' in src and '"set task constrain"' in src)
    # One field per request: the other box may not even be loaded in this view, and
    # posting both would write whatever stale value happens to be in the DOM.
    # Names ONE field: the other box may not even be loaded in this view, so sending
    # both would write whatever stale value is sitting in the DOM. The fork copies the
    # other one server-side, from the stored record.
    check("it names ONE field, so the other box cannot be clobbered",
          "{ fork: true, task: text }" in mb and "{ fork: true, notes: text }" in mb,
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
