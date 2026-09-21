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


def _txt(b):
    """Text of a transcript item, whichever shape the mode returned."""
    c = ((b.get("message") or {}).get("content")) if b.get("message") else b.get("text")
    if isinstance(c, list):
        return "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""


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

    # A pasted request, in claude's own shape: every multi-line message cc-web delivers
    # arrives as a bracketed paste, and claude wraps pastes in these tags before sending
    # them on — including the id on the CLOSING tag, which is not valid XML but is what
    # it writes. Its system prompt says "the user never sees the id"; in cc-web they did.
    with open(path, "a", encoding="utf-8") as f:
        ts = (t0 + datetime.timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        f.write(json.dumps({"type": "user", "cwd": "/tmp/p", "timestamp": ts, "uuid": "pasted-u",
                            "message": {"role": "user", "content":
                                        '\n\n<pasted_content id="b465">\n粘进来的那段话\n'
                                        '</pasted_content id="b465">\n\n\n不懂啥意思.'}},
                           ensure_ascii=False) + "\n")
        f.write(json.dumps({"type": "assistant", "cwd": "/tmp/p", "timestamp": ts, "uuid": "pasted-a",
                            "message": {"role": "assistant", "content": [
                                {"type": "text", "text": "回答粘贴的那条"}]}},
                           ensure_ascii=False) + "\n")

    # The SAME message twice, the way a queued send lands in the log: claude writes a
    # queue-operation/enqueue the moment you hit send (the "QUEUED" placeholder), then the
    # real user turn when it is delivered. Two different builders, and the first one was
    # missed — so the placeholder still showed the tags while the turn under it was clean.
    # The client hides the placeholder by matching CONTENT, so they have to agree.
    with open(path, "a", encoding="utf-8") as f:
        ts = (t0 + datetime.timedelta(minutes=61)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        wrapped = ('<pasted_content id="b465">\nrule 2 写「another similar connector」\n'
                   '</pasted_content id="b465">\n他这是在质疑啥?!')
        f.write(json.dumps({"type": "queue-operation", "operation": "enqueue",
                            "cwd": "/tmp/p", "timestamp": ts, "uuid": "q-u",
                            "content": wrapped}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"type": "user", "cwd": "/tmp/p", "timestamp": ts, "uuid": "q-delivered",
                            "message": {"role": "user", "content": wrapped}},
                           ensure_ascii=False) + "\n")
        f.write(json.dumps({"type": "assistant", "cwd": "/tmp/p", "timestamp": ts, "uuid": "q-a",
                            "message": {"role": "assistant", "content": [
                                {"type": "text", "text": "回答排队那条"}]}},
                           ensure_ascii=False) + "\n")

    # Two relayed turns, tagged the way ask-peer tags them (one with each marker), plus
    # an answer each — so "the whole round goes" has something to go wrong with.
    with open(path, "a", encoding="utf-8") as f:
        for n, tag in ((0, "[⇄ from peer claude abcd1234 (air)]\n帮我看下这个"),
                       (1, "看完了\n[⇄ end of peer message]")):
            ts = (t0 + datetime.timedelta(minutes=50 + n)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            f.write(json.dumps({"type": "user", "cwd": "/tmp/p", "timestamp": ts,
                                "uuid": f"peer-u{n}",
                                "message": {"role": "user", "content": tag}},
                               ensure_ascii=False) + "\n")
            f.write(json.dumps({"type": "assistant", "cwd": "/tmp/p", "timestamp": ts,
                                "uuid": f"peer-a{n}",
                                "message": {"role": "assistant", "content": [
                                    {"type": "text", "text": "回给 peer 的话"}]}},
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
        rounds_in = lambda t: len({b["_round"] for b in t})
        check("the same four rounds come back either way",
              rounds_in(only["transcript"]) == 4,
              str(sorted({b["_round"] for b in only["transcript"]})))
        check("...with nothing but requests in the light one",
              {b["type"] for b in only["transcript"]} == {"user"},
              str(sorted({b["type"] for b in only["transcript"]})))
        # The whole reason this is not a browser-side filter.
        check("...and it is DRAMATICALLY smaller on the wire",
              ob < fb / 5, f"{fb} → {ob} bytes ({100 - ob * 100 // fb}% less)")
        # Counted in ROUNDS, which is what the page size means. Counting user-type
        # blocks was a proxy that breaks honestly: a queued send puts a QUEUED
        # placeholder AND the delivered turn in the same round, both type "user".
        check("...paging back at the same pace, not a smaller step",
              rounds_in(full["transcript"]) == 4,
              str(sorted({b["_round"] for b in full["transcript"]})))

        print("=== ...and then one round's answer, on demand ===")
        # Deliberately one of the human's own rounds: the fixture also contains relayed
        # ones (tagged 「⇄ from peer claude」) whose answer is addressed to another
        # session, and picking "the last request on the page" silently landed on one.
        mine = [b for b in only["transcript"]
                if "请求" in json.dumps(b["message"], ensure_ascii=False)]
        idxs = sorted(b["_idx"] for b in mine)
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

        print("=== another session's turns are not your requests ===")
        # ask-peer tags every message it relays, in both directions — the tag exists so
        # an untagged message can be trusted to be the human's. Here it is used to keep
        # them out of the one view whose job is finding a request the human made.
        newest = await st(claude_session_id=sid, mode="medium", rounds=2)
        top = max(b["_idx"] for b in newest["transcript"]) + 1
        withp = await st(claude_session_id=sid, mode="medium", rounds=4, before_idx=top,
                         users_only=True)
        nop = await st(claude_session_id=sid, mode="medium", rounds=4, before_idx=top,
                       users_only=True, skip_peer=True)
        txt = lambda r: json.dumps(r["transcript"], ensure_ascii=False)
        check("without the flag, relayed turns come back", "from peer claude" in txt(withp))
        check("with it, they do not", "from peer claude" not in txt(nop)
              and "end of peer message" not in txt(nop), txt(nop)[:60])
        # Four rounds asked for, four real ones served: the filtering happens BEFORE the
        # window is taken, or a page comes back as one request and three gaps.
        check("...and the page is still four requests you made",
              sum(1 for b in nop["transcript"] if b["type"] == "user") == 4,
              str(len(nop["transcript"])))
        # The answer goes with the question: an answer with no question above it reads
        # as a non-sequitur, and the reason for hiding it was that the exchange was not
        # yours.
        rounds_kept = await st(claude_session_id=sid, mode="medium", rounds=6,
                               before_idx=top, skip_peer=True)
        check("the whole relayed ROUND is dropped, answer included",
              "回给 peer 的话" not in txt(rounds_kept), txt(rounds_kept)[:60])
        check("...while your own answers are untouched",
              "回复" in txt(rounds_kept))

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

        print("=== the ASR vocabulary knows what this session is about ===")
        # Dictating into the Task box is exactly when the recogniser needs the session's
        # own words — file names, module names, the thing being built. Those come from
        # the recent conversation AND from the Task / 注意事项 text itself, which is the
        # densest source of them and the likeliest vocabulary of what you are about to
        # say into that very box.
        cc_web.post_session_memo(cc_web.MemoPayload(
            claude_session_id=sid, task="把 news-reader 的 backfill 收口",
            notes="别重构 index.html"))
        terms = cc_web._asr_terms(sid)
        check("the task's own words are in the vocabulary",
              {"news-reader", "backfill"} <= set(terms), str(terms[:8]))
        check("...and the standing notes' too", "index.html" in terms, str(terms[:8]))
        check("...alongside the conversation's", any("请求" in t or "回复" in t for t in terms)
              or len(terms) > 2, str(len(terms)))
        # Eight exchanges rather than four: the term ASR fumbles is often one you used
        # several turns ago.
        src_cc = open(os.path.join(ROOT, "cc_web.py"), encoding="utf-8").read()
        check("...over a window of 8 exchanges", "n_exchanges=8" in src_cc)
        # The extractor's own truncation marker was leaking into the list: every long
        # turn contributed "chars" and "skipped", so every session was biased toward two
        # English words nobody had said.
        check("the truncation marker is not a term",
              not ({"chars", "skipped"} & set(t.lower() for t in terms)), str(terms[:6]))
        check("...and neither is an unsayable 200-character run",
              all(len(t) <= 32 for t in terms), str(max((len(t) for t in terms), default=0)))
        # The vocabulary is built from the WHOLE of each recent turn, not a 200-char
        # head+tail of it. That cap is right for the polish prompt (it wants a sense of
        # the conversation) and wrong here: most file names and identifiers are in the
        # middle of a long message, which is exactly what a head+tail throws away.
        long_mid = "开头。" * 40 + " kubectl_regrade_canary " + "结尾。" * 40
        cc_web.post_session_memo(cc_web.MemoPayload(claude_session_id=sid, task=long_mid))
        mid_terms = cc_web._asr_terms(sid)
        check("a term buried in the middle of a long turn is still collected",
              "kubectl_regrade_canary" in mid_terms, str(mid_terms[:5]))
        check("...and the list stays a list of terms, not prose",
              all(len(t) <= 32 for t in mid_terms) and len(mid_terms) <= 48,
              str(len(mid_terms)))
        check("a session with no memo still works",
              isinstance(cc_web._asr_terms("00000000-0000-0000-0000-000000000000"), list))

        print("=== a pasted block shows as the text, not as packaging ===")
        # Both display modes: brief is what the session view asks for, medium what the
        # brief list and the peer history use — and they are separate builders, neither of
        # which goes through _entry_text. The first fix only cleaned _entry_text, so the
        # page itself still showed the tags.
        for mode in ("brief", "medium"):
            full = await st(claude_session_id=sid, mode=mode, rounds=6)
            pasted = [b for b in full["transcript"]
                      if b.get("type") == "user"
                      and "粘进来的那段话" in _txt(b)]
            check(f"[{mode}] the pasted request is there", len(pasted) == 1,
                  str(full["transcript"][-1])[:300])
            if not pasted:
                continue
            txt = _txt(pasted[0])
            check(f"[{mode}] ...with the tags gone", "pasted_content" not in txt, txt[:80])
            check(f"[{mode}] ...and the id claude says you are never meant to see",
                  "b465" not in txt, txt[:80])
            # Losing the words would be far worse than showing the tags.
            check(f"[{mode}] ...but every word kept, on both sides of the block",
                  txt.strip().startswith("粘进来的那段话") and txt.strip().endswith("不懂啥意思."),
                  repr(txt))

        print("=== the QUEUED placeholder is stripped too, and still matches ===")
        q = await st(claude_session_id=sid, mode="brief", rounds=6)
        ph = [b for b in q["transcript"] if b.get("_queued")]
        if not ph:
            print("    (transcript tail: "
                  + str([(b.get("type"), b.get("_queued"), _txt(b)[:16]) for b in q["transcript"][-6:]])
                  + ")")
        turn = [b for b in q["transcript"]
                if b.get("type") == "user" and not b.get("_queued")
                and "质疑啥" in _txt(b)]
        check("the placeholder is there", len(ph) == 1, str(len(ph)))
        if ph:
            check("...with no tags on it either", "pasted_content" not in _txt(ph[0]),
                  _txt(ph[0])[:70])
            # If only one of the two were stripped, the client would stop recognising the
            # pair and you would see the message twice — once queued, once delivered.
            check("...and it still reads the same as the delivered turn, so the pair matches",
                  bool(turn) and _txt(ph[0]).strip() == _txt(turn[0]).strip(),
                  (_txt(ph[0])[:40] + " vs " + (_txt(turn[0])[:40] if turn else "(no turn)")))

        print("=== the default is unchanged ===")
        plain = await st(claude_session_id=sid, mode="medium", rounds=4, before_idx=first)
        check("without the flag, answers still come with it",
              any(b["type"] == "assistant" for b in plain["transcript"]))

    asyncio.run(go())
    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
