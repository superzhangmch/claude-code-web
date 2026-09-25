#!/usr/bin/env python3
"""★ on a request: what gets stored, and why it is the uuid rather than the index.

A long session needs somewhere to come back to. The button is the easy half; the
address is the part worth testing.

`_idx` cannot be it. It is numbered per WINDOW — the first entry the cache happens to
read gets _JSONL_BASE and earlier rounds count DOWN from there — so the same message
answers to a different _idx depending on how much history had been paged in when the
window was built, and the numbering restarts whenever the cache is rebuilt. A bookmark
stored against it would drift onto another message.

The uuid is claude's own per-record id: stable, unique, and already on every entry the
client renders. The byte offset is recorded beside it by one scan at save time — the
address a future "open the window at this round" needs, free while the file is open.

    python3 tests/test_bookmarks.py      # exit 0 = pass
"""
import datetime
import json
import os
import pathlib
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
    home = tempfile.mkdtemp(prefix="ccweb-bm-")
    os.makedirs(os.path.join(home, ".claude"))
    os.environ["HOME"] = home
    os.environ["CC_WEB_TOKEN"] = "t"
    try:
        import cc_web
    except Exception as e:                                  # pragma: no cover
        print("SKIP: cannot import cc_web:", e); return 0

    sid = "22222222-3333-4444-5555-666666666666"
    proj = os.path.join(home, ".claude", "projects", "-tmp-bm")
    os.makedirs(proj)
    path = pathlib.Path(proj) / (sid + ".jsonl")
    t0 = datetime.datetime(2026, 9, 22, tzinfo=datetime.timezone.utc)
    with open(path, "w", encoding="utf-8") as f:
        for r in range(6):
            ts = (t0 + datetime.timedelta(minutes=r)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            f.write(json.dumps({"type": "user", "cwd": "/tmp/bm", "timestamp": ts,
                                "uuid": f"u-{r}",
                                "message": {"role": "user", "content": f"请求 {r}"}},
                               ensure_ascii=False) + "\n")
            f.write(json.dumps({"type": "assistant", "cwd": "/tmp/bm", "timestamp": ts,
                                "uuid": f"a-{r}",
                                "message": {"role": "assistant", "content": [
                                    {"type": "text", "text": "回复 " + str(r)}]}},
                               ensure_ascii=False) + "\n")

    P = cc_web.BookmarkPayload

    print("=== saving one ===")
    out = cc_web.post_bookmark(P(claude_session_id=sid, uuid="u-3", round=4,
                                 ts="2026-09-22T00:03:00.000Z",
                                 text="  请求  3\n（换行和多余空白应该被收掉） "))
    items = out["items"]
    check("it is stored", len(items) == 1 and items[0]["uuid"] == "u-3", str(items))
    it = items[0]
    # The point of the whole exercise: a durable file position, found by scanning.
    lines = path.read_bytes().split(b"\n")
    want_off = sum(len(x) + 1 for x in lines[:6])          # u-3 is the 7th line (0-based 6)
    check("...with the byte offset of its line", it["off"] == want_off,
          f'{it["off"]} vs {want_off}')
    check("...and its ordinal among the records", it["ordinal"] == 6, str(it["ordinal"]))
    # Reading it back at that offset must land exactly on the record, which is what
    # makes the offset worth storing at all.
    with open(path, "rb") as fh:
        fh.seek(it["off"])
        first = json.loads(fh.readline().decode())
    check("...so seeking there lands on that record", first["uuid"] == "u-3", str(first.get("uuid")))
    check("...the label is one line, whitespace collapsed",
          it["text"] == "请求 3 （换行和多余空白应该被收掉）", repr(it["text"]))
    check("...and what the client already knew is kept as sent",
          it["round"] == 4 and it["ts"].startswith("2026-09-22"), str([it["round"], it["ts"]]))

    print("=== a GET returns them ===")
    check("the list comes back", len(cc_web.get_bookmarks(sid)["items"]) == 1)

    print("=== saving more, and the order they come back in ===")
    cc_web.post_bookmark(P(claude_session_id=sid, uuid="u-1", round=2, text="请求 1"))
    got = cc_web.post_bookmark(P(claude_session_id=sid, uuid="u-5", round=6, text="请求 5"))
    # Oldest first: the list should read in the order the session happened, not in the
    # order you happened to star things.
    check("they are ordered by position in the session",
          [x["uuid"] for x in got["items"]] == ["u-1", "u-3", "u-5"],
          str([x["uuid"] for x in got["items"]]))

    print("=== starring the same one twice is not two bookmarks ===")
    again = cc_web.post_bookmark(P(claude_session_id=sid, uuid="u-3", round=4, text="请求 3 改了标签"))
    check("still one entry for it",
          sum(1 for x in again["items"] if x["uuid"] == "u-3") == 1,
          str([x["uuid"] for x in again["items"]]))
    check("...and the newer label won",
          next(x for x in again["items"] if x["uuid"] == "u-3")["text"] == "请求 3 改了标签")

    print("=== un-starring ===")
    left = cc_web.post_bookmark(P(claude_session_id=sid, uuid="u-3", add=False))
    check("it is gone", [x["uuid"] for x in left["items"]] == ["u-1", "u-5"],
          str([x["uuid"] for x in left["items"]]))
    for u in ("u-1", "u-5"):
        cc_web.post_bookmark(P(claude_session_id=sid, uuid=u, add=False))
    # No file at all when nothing is starred: an untouched session costs nothing, same
    # rule the memo follows.
    check("the file goes away when the last one does",
          not cc_web._bm_file(sid).exists() and cc_web.get_bookmarks(sid)["items"] == [])

    print("=== a uuid that is not in the file ===")
    # Stored anyway, with off=-1: refusing would lose the bookmark over a file that has
    # been rotated or moved, and the list can still show the label.
    ghost = cc_web.post_bookmark(P(claude_session_id=sid, uuid="not-there", text="?"))
    check("it is kept, flagged as not located",
          ghost["items"][0]["off"] == -1 and ghost["items"][0]["ordinal"] == -1,
          str(ghost["items"][0]))
    cc_web.post_bookmark(P(claude_session_id=sid, uuid="not-there", add=False))

    print("=== the state file is per session, and named like the others ===")
    cc_web.post_bookmark(P(claude_session_id=sid, uuid="u-0", text="x"))
    d = cc_web._bm_dir()
    check("one file per session under ~/.claude", (d / (sid + ".json")).exists(), str(d))
    check("...and a bad session id is refused, not turned into a path",
          _raises(lambda: cc_web._bm_file("../../etc/passwd")))

    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


def _raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
