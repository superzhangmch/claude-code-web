#!/usr/bin/env python3
"""A session with no transcript yet is still attachable.

claude writes `<sid>.jsonl` on the first exchange, so a tab that was opened and
left sitting at the prompt has none. The tab LIST is built from a different
source — claude's own pid↔session store — so such a tab shows up in the UI with
its cwd and its default title ("Claude Code"), and then `/api/attach` refused it
with `unknown session_id` because attach began by demanding the transcript. From
a phone that is a dead end: you cannot type the first message into a tab you
cannot open, and the transcript only appears after that first message.

The transcript was never needed to BIND. Everything after that gate exists to
guess which tab a sid belongs to by matching transcript fingerprints against
screens; the store answers the same question directly, which is what
_try_autobind already uses. So:

  1. no transcript + the store knows the pid  -> bound (this is the bug);
  2. the binding carries jsonl_path=None rather than inventing a path;
  3. no transcript + no live tab             -> still 404 (a truly unknown sid);
  4. a session WITH a transcript keeps the old fingerprint-scoring path.

    python3 tests/test_attach_no_transcript.py       # exit 0 = pass
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_fails = []


def check(label, ok, extra=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"   [{extra}]" if extra and not ok else ""))
    if not ok:
        _fails.append(label)


class Ref:
    """The subset of ClaudeSessionRef that binding reads."""

    def __init__(self, pid, cwd="/tmp/proj", iterm_session_id="x1"):
        self.pid = pid
        self.cwd = cwd
        self.iterm_session_id = iterm_session_id
        self.window_index = 0
        self.tab_index = 3
        self.claude_session_id = ""


class StubBridge:
    def __init__(self, refs):
        self.refs = refs

    async def ensure_connected(self):
        pass

    async def list_claude_tabs(self):
        return self.refs


async def main():
    os.environ.setdefault("CC_WEB_TOKEN", "t")
    import cc_web
    from fastapi import HTTPException

    SID = "cbbe99d1-8b2c-4488-be92-de30bb32630a"
    PID = 98851

    async def attach(*, jsonl, refs, store_pids):
        """Call the real endpoint with the three inputs it consults stubbed."""
        real = (cc_web.bridge, cc_web.find_jsonl_for_session, cc_web._pids_for_session,
                cc_web._pid_start_time_hook if hasattr(cc_web, "_pid_start_time_hook") else None)
        cc_web.bridge = StubBridge(refs)
        cc_web.find_jsonl_for_session = lambda s: jsonl
        cc_web._pids_for_session = lambda s: store_pids
        import iterm_bridge
        real_start = iterm_bridge._pid_start_time
        iterm_bridge._pid_start_time = lambda pid: 1234.0     # "the pid is alive"
        try:
            return await cc_web.post_attach(
                cc_web.AttachPayload(claude_session_id=SID))
        finally:
            cc_web.bridge, cc_web.find_jsonl_for_session, cc_web._pids_for_session = real[:3]
            iterm_bridge._pid_start_time = real_start
            cc_web.bindings.remove_session(SID)

    print("=== 1. no transcript, but the store knows the pid -> bound ===")
    # Caught, not propagated: before the fix this raised 404 out of the endpoint, and a
    # test that dies with a traceback reports "crashed", not "the behaviour is wrong".
    try:
        r = await attach(jsonl=None, refs=[Ref(PID)], store_pids=[PID])
    except HTTPException as e:
        r = {"result": f"HTTP {e.status_code}: {e.detail}"}
    check("attach returns bound instead of 404",
          r.get("result") == "bound", str(r))
    check("...bound to the pid the store named",
          (r.get("binding") or {}).get("pid") == PID, str(r.get("binding")))

    print("=== 2. the binding admits it has no transcript ===")
    cc_web.find_jsonl_for_session_real = cc_web.find_jsonl_for_session
    b = None
    real_insert = cc_web.bindings.insert
    captured = []
    cc_web.bindings.insert = lambda x: (captured.append(x), real_insert(x))[1]
    try:
        await attach(jsonl=None, refs=[Ref(PID)], store_pids=[PID])
    finally:
        cc_web.bindings.insert = real_insert
    b = captured[-1] if captured else None
    check("jsonl_path is None, not a fabricated path",
          b is not None and b.jsonl_path is None, repr(getattr(b, "jsonl_path", "no binding")))
    check("a None jsonl reads as an empty transcript (not a crash)",
          cc_web.jsonl_cache.entries(None) == [])

    print("=== 3. no transcript AND no live tab -> still 404 ===")
    code = None
    try:
        await attach(jsonl=None, refs=[], store_pids=[])
    except HTTPException as e:
        code = e.status_code
    check("an unknown sid is still rejected", code == 404, str(code))

    print("=== 4. with a transcript, the old path is untouched ===")
    # Reaching _project_path_from_jsonl at all proves the fingerprint-scoring branch
    # still runs when a transcript exists (it is the first call after the gate).
    seen = []
    real_ppf = cc_web._project_path_from_jsonl
    cc_web._project_path_from_jsonl = lambda p: (seen.append(p), "/tmp/proj")[1]
    try:
        await attach(jsonl="/tmp/fake.jsonl", refs=[Ref(PID)], store_pids=[PID])
    except Exception:
        pass                                  # the rest of that path needs a real file
    finally:
        cc_web._project_path_from_jsonl = real_ppf
    check("the transcript branch still runs when a transcript exists",
          seen == ["/tmp/fake.jsonl"], str(seen))

    print()
    if _fails:
        print(f"FAILED ({len(_fails)}): " + "; ".join(_fails))
        return 1
    print("all good")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
