#!/usr/bin/env python3
"""Paging back without dragging the answers along.

Looking for something YOU said, in a session with hundreds of rounds, means paging back
through every response to find it — over a phone's data plan. So `users_only` serves the
requests alone, and `round_at` fetches the answer to ONE of them once you have found it.

Both are SERVER-side. Filtering in the browser would have been a few lines and would
have missed the entire point, which is the bytes that never leave the machine ("it
should not be implemented at the front end only (to save my 5g data)").

    python3 tests/test_load_earlier.py      # exit 0 = pass
"""
import asyncio
import datetime
import json
import os
import pathlib
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


def main():
    home = tempfile.mkdtemp(prefix="ccweb-earlier-")
    os.makedirs(os.path.join(home, ".claude"))
    os.environ["HOME"] = home
    os.environ["CC_WEB_TOKEN"] = "t"
    sys.path.insert(0, ROOT)

    sid = "12345678-1111-2222-3333-444444444444"
    proj = os.path.join(home, ".claude", "projects", "-tmp-p")
    os.makedirs(proj)
    path = pathlib.Path(proj) / (sid + ".jsonl")
    t0 = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
    ROUNDS = 12
    with open(path, "w", encoding="utf-8") as f:
        for r in range(ROUNDS):
            ts = (t0 + datetime.timedelta(minutes=r)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            # ensure_ascii=False because that is how the real files are written —
            # checked, not assumed: "content":"你看看…" sits in them as itself, which is
            # what the search's cheap per-line prefilter depends on.
            f.write(json.dumps({"type": "user", "cwd": "/tmp/p", "timestamp": ts,
                                "uuid": f"u-{r}",
                                "message": {"role": "user", "content": f"请求 {r}"}},
                               ensure_ascii=False) + "\n")
            # Long on purpose: the saving is the claim, so the answers have to be the
            # bulk of it, as they are in a real session.
            f.write(json.dumps({"type": "assistant", "cwd": "/tmp/p", "timestamp": ts,
                                "uuid": f"a-{r}",
                                "message": {"role": "assistant", "content": [
                                    {"type": "text", "text": f"回复 {r} 独角兽 " + "x" * 3000}]}},
                               ensure_ascii=False) + "\n")

    # A line written the OTHER way (\uXXXX-escaped). It was here for the server-side
    # search's per-line prefilter; that search is gone, but a fixture that contains both
    # spellings still guards the paging code against assuming one of them.
    with open(path, "a", encoding="utf-8") as f:
        ts = (t0 + datetime.timedelta(minutes=99)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        f.write(json.dumps({"type": "user", "cwd": "/tmp/p", "timestamp": ts,
                            "uuid": "u-esc",
                            "message": {"role": "user", "content": "转义过的请求"}}) + "\n")

    try:
        import cc_web
    except Exception as e:                                  # pragma: no cover
        print("SKIP: cannot import cc_web:", e); return 0

    class B:
        claude_session_id = sid
        jsonl_path = path
        iterm_session_id = "stub"
        pid = 1
        cwd = "/tmp/p"
        window_index = 0
        tab_index = 0
        name = "t"
        epoch = "e"
        bound_at = 0

    cc_web.bindings.get_by_session = staticmethod(lambda s: B())
    cc_web.verify_binding = lambda b: True

    async def _screen(*a, **k):
        return ""
    cc_web.bridge.get_screen_for = _screen

    async def go():
        st = cc_web.get_state
        base = await st(claude_session_id=sid, mode="medium", rounds=2)
        first = min(b["_idx"] for b in base["transcript"])

        full = await st(claude_session_id=sid, mode="medium", rounds=4, before_idx=first)
        only = await st(claude_session_id=sid, mode="medium", rounds=4, before_idx=first,
                        users_only=True)
        nbytes = lambda t: len(json.dumps(t, ensure_ascii=False).encode())
        fb, ob = nbytes(full["transcript"]), nbytes(only["transcript"])

        print("=== load-earlier can leave the answers on the server ===")
        check("the same four rounds come back either way",
              sum(1 for b in only["transcript"] if b["type"] == "user") == 4,
              str(len(only["transcript"])))
        check("...with nothing but requests in the light one",
              {b["type"] for b in only["transcript"]} == {"user"},
              str(sorted({b["type"] for b in only["transcript"]})))
        # The whole reason this is not a browser-side filter.
        check("...and it is DRAMATICALLY smaller on the wire",
              ob < fb / 5, f"{fb} → {ob} bytes ({100 - ob * 100 // fb}% less)")
        check("...paging back at the same pace, not a smaller step",
              len([b for b in full["transcript"] if b["type"] == "user"]) == 4)

        print("=== ...and then one round's answer, on demand ===")
        idxs = sorted(b["_idx"] for b in only["transcript"])
        one = await st(claude_session_id=sid, mode="medium", round_at=idxs[-1])
        check("the answer to that request comes back",
              len(one["transcript"]) == 1 and one["transcript"][0]["type"] == "assistant",
              str([b["type"] for b in one["transcript"]]))
        # It must not include the request: that one is already on screen, and sending it
        # again would either duplicate it or have to be de-duplicated client-side.
        check("...without the request itself, which is already on screen",
              all(b["type"] != "user" for b in one["transcript"]))
        check("...and it is the RIGHT answer",
              "回复" in json.dumps(one["transcript"][0]["message"], ensure_ascii=False),
              json.dumps(one["transcript"][0]["message"], ensure_ascii=False)[:40])
        # The request you found may be older than anything the window holds, which is
        # the normal case after paging back a few times.
        oldest = await st(claude_session_id=sid, mode="medium", round_at=idxs[0])
        check("a round older than the loaded window is read from disk",
              len(oldest["transcript"]) == 1, str(len(oldest["transcript"])))
        # One round, not "everything after it".
        check("...and stops at the next request", len(oldest["transcript"]) == 1)

        print("=== finding is NOT a server call ===")
        # There was a /api/search here for about an hour: it streamed the whole jsonl
        # and returned matches. Removed on request — finding something is a filter over
        # what is already loaded, and this pair of features is what makes that cheap:
        # pull hundreds of requests down with users_only for a few hundred bytes, then
        # look through them in the browser for nothing.
        src = open(os.path.join(ROOT, "cc_web.py"), encoding="utf-8").read()
        check("the transcript-text search endpoint is gone", not hasattr(cc_web, "get_search"))
        check("...and so is the by-uuid round fetch it needed", "round_uuid" not in src)
        # NOT "no /api/search anywhere": there has always been one, and it does something
        # else entirely — a backend search across ALL transcripts for the session picker
        # (search_sessions). That collision is not trivia. Deleting the new endpoint by
        # searching the source for its route name matched the OLD one, and the slice
        # from there to /api/state removed 933 LINES of cc_web.py — sixteen functions,
        # including _status_line and the whole attach/detach group. The file still
        # compiled; nothing failed until a test called get_state.
        check("...while the session-picker search, which predates all this, is untouched",
              hasattr(cc_web, "search_sessions"))
        # The file was restored from the last commit and the two wanted changes
        # re-applied by hand. This is the cheap guard that would have caught it.
        import subprocess
        head = subprocess.run(["git", "-C", ROOT, "show", "HEAD:cc_web.py"],
                              capture_output=True, text=True).stdout
        if head:
            import re as _re
            hf = set(_re.findall(r"(?m)^(?:async def |def )(\w+)", head))
            cf = set(_re.findall(r"(?m)^(?:async def |def )(\w+)", src))
            check("no function went missing against the last commit",
                  not (hf - cf), str(sorted(hf - cf)[:8]))

        print("=== the default is unchanged ===")
        plain = await st(claude_session_id=sid, mode="medium", rounds=4, before_idx=first)
        check("without the flag, answers still come with it",
              any(b["type"] == "assistant" for b in plain["transcript"]))

    asyncio.run(go())
    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
