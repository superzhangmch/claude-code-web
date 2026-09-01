#!/usr/bin/env python3
"""codex as a second agent — and the promise that it cannot hurt the first.

cc_web was written when claude was the only agent. Supporting codex is therefore
bolted on rather than abstracted: a separate module, four new endpoints, no
change to any existing one. The first thing this test pins is that promise, not
the feature — a broken or absent codex must cost the /api/codex/* routes and
NOTHING else, because the alternative is a server that will not start.

The rest pins the reading and writing of codex's own state, against a fixture
tree with the exact shapes measured from codex-cli 0.152.0:

  1. the session list comes from state_<N>.sqlite and picks the HIGHEST N (that
     number is a schema version — codex ships state_5 next to thread_history_1 —
     so a hardcoded name silently reads a dead file after an upgrade);
  2. `live` means a process holds the thread's writer lock, not that a pid file
     exists;
  3. the transcript hides what the model was told and shows what the human said:
     developer-role preambles and the injected <environment_context> user turn
     are dropped, real user text and assistant answers are kept;
  4. turn state comes from codex's own task_started/task_complete events;
  5. a half-written last line (the file is being appended to as we read) is
     skipped, not fatal;
  6. an unknown item type degrades to kind='other' instead of vanishing;
  7. incremental reads by ordinal return only the new entries;
  8. send_message refuses empty input and reports "no rollout yet" as its own
     reason — that is the same trap that made a fresh claude tab unopenable.

    python3 tests/test_codex_backend.py       # exit 0 = pass
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_fails = []


def check(label, ok, extra=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"   [{extra}]" if extra and not ok else ""))
    if not ok:
        _fails.append(label)


def build_fixture(home: Path, rollout_lines):
    """A ~/.codex good enough to exercise every read path."""
    (home / "sessions" / "2026" / "09" / "01").mkdir(parents=True, exist_ok=True)
    (home / "thread-writer-locks").mkdir(parents=True, exist_ok=True)
    rp = home / "sessions" / "2026" / "09" / "01" / "rollout-live.jsonl"
    rp.write_text("\n".join(rollout_lines) + "\n", encoding="utf-8")

    # Two state dbs: an OLD schema version holding a wrong answer, and the
    # current one. Picking by highest N is the whole point.
    for n, rows in ((2, [("stale-thread", "/old", "STALE", 1, 1, 0, "", "", "")]),
                    (5, [("live-thread", "/w", "Live one", 200, 100, 42, "on-request", "openai", str(rp)),
                         ("dead-thread", "/w", "Finished", 100, 90, 7, "on-request", "openai", "")])):
        con = sqlite3.connect(home / f"state_{n}.sqlite")
        con.execute("create table threads (id text, cwd text, title text, updated_at int,"
                    " created_at int, tokens_used int, approval_mode text,"
                    " model_provider text, rollout_path text)")
        con.executemany("insert into threads values (?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        con.close()
    return rp


ROLLOUT = [
    json.dumps({"timestamp": "T0", "ordinal": 0, "type": "session_meta",
                "payload": {"session_id": "live-thread", "id": "live-thread",
                            "cwd": "/w", "originator": "codex-tui", "cli_version": "0.152.0"}}),
    json.dumps({"timestamp": "T1", "ordinal": 1, "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-1"}}),
    json.dumps({"timestamp": "T2", "ordinal": 2, "type": "response_item",
                "payload": {"type": "message", "role": "developer",
                            "content": [{"type": "input_text", "text": "<permissions instructions> ..."}]}}),
    json.dumps({"timestamp": "T3", "ordinal": 3, "type": "response_item",
                "payload": {"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": "<environment_context><cwd>/w</cwd>"}]}}),
    json.dumps({"timestamp": "T4", "ordinal": 4, "type": "world_state", "payload": {}}),
    json.dumps({"timestamp": "T5", "ordinal": 5, "type": "response_item",
                "payload": {"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": "run the tests"}]}}),
    json.dumps({"timestamp": "T6", "ordinal": 6, "type": "response_item",
                "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c1",
                            "input": "pytest -q"}}),
    json.dumps({"timestamp": "T7", "ordinal": 7, "type": "response_item",
                "payload": {"type": "custom_tool_call_output", "call_id": "c1",
                            "output": "3 passed"}}),
    json.dumps({"timestamp": "T8", "ordinal": 8, "type": "response_item",
                "payload": {"type": "brand_new_item_type_from_the_future"}}),
    json.dumps({"timestamp": "T9", "ordinal": 9, "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                            "content": [{"type": "output_text", "text": "all green"}]}}),
    json.dumps({"timestamp": "TA", "ordinal": 10, "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-1",
                            "last_agent_message": "all green"}}),
    '{"timestamp": "TB", "ordinal": 11, "type": "event_ms',      # torn tail line
]


async def main():
    os.environ.setdefault("CC_WEB_TOKEN", "t")

    print("=== the promise: one codebase, one instance per agent ===")
    # Same endpoints, same frontend; which agent an instance serves is
    # $CC_WEB_AGENT. The promise is no longer "cc_web contains no codex code" —
    # it is that a DEFAULT instance behaves exactly as before, which is what the
    # rest of the suite checks, plus the two things it cannot see:
    #
    #   * a claude instance never even imports the codex modules, so a broken
    #     codex_shim cannot cost it anything;
    #   * the two instances do not share a single state file, or they would
    #     clobber each other's bindings the way two claude instances would.
    import cc_web
    check("the default agent is claude", cc_web.AGENT == "claude" and not cc_web.IS_CODEX,
          cc_web.AGENT)
    check("a claude instance has NOT imported the codex shim",
          "codex_shim" not in sys.modules,
          str([m for m in sys.modules if "codex" in m]))
    check("the claude endpoints are all still there",
          {"/api/attach", "/api/state", "/api/input", "/api/tabs"}
          <= {getattr(r, "path", "") for r in cc_web.app.routes})
    for f in ("cc_web_bindings.json", "cc_web_tree.json", "cc_web.lock",
              "session_index.json"):
        check(f"claude keeps its own filename: {f}",
              cc_web._state_path(f).name == f, cc_web._state_path(f).name)

    print("=== a codex instance is separated by every file it writes ===")
    # A subprocess, because AGENT is read once at import: asserting it in-process
    # would only prove that a module-level constant can be monkeypatched.
    import subprocess as sp
    probe = (
        "import sys; sys.path.insert(0, %r)\n"
        "import cc_web\n"
        "print(cc_web.AGENT, cc_web.IS_CODEX)\n"
        "print(cc_web.BINDINGS_FILE.name, cc_web.TREE_FILE.name,"
        " cc_web.INSTANCE_LOCK_FILE.name, cc_web.SESSION_INDEX_PATH.name)\n"
        "print('codex_shim' in sys.modules)\n" % ROOT)
    env = dict(os.environ, CC_WEB_AGENT="codex", CC_WEB_TOKEN="t")
    r = sp.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env)
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if len(lines) < 3:
        check("the codex instance starts", False, r.stderr[-300:])
    else:
        check("CC_WEB_AGENT=codex switches the instance", lines[0] == "codex True", lines[0])
        names = lines[1].split()
        check("every state file is agent-scoped",
              all(".codex." in n for n in names), lines[1])
        check("...and not one of them collides with claude's",
              not {n for n in names} & {"cc_web_bindings.json", "cc_web_tree.json",
                                        "cc_web.lock", "session_index.json"}, lines[1])
        check("even a codex instance defers the shim import until first use",
              lines[2] == "False", lines[2])

    import codex_backend as cx

    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / ".codex"
        home.mkdir()
        os.environ["CODEX_HOME"] = str(home)
        rp = build_fixture(home, ROLLOUT)

        print("=== the session list ===")
        # Process scanning is machine-wide by nature (it reads /proc), so a real
        # codex running on this box would inject itself into a fixture-only list
        # and break the ordering assertions below. Stub it out here and test it
        # deliberately further down.
        _real_procs = cx.live_codex_processes
        cx.live_codex_processes = lambda: []
        check("available() sees the fixture", cx.available() is True)
        ts = cx.list_threads()
        ids = [t["thread_id"] for t in ts]
        check("reads the HIGHEST state_<N>, not the stale one",
              "stale-thread" not in ids and "live-thread" in ids, str(ids))
        check("newest first", ids[0] == "live-thread", str(ids))
        by_id = {t["thread_id"]: t for t in ts}
        check("a thread with nobody holding its lock is not live",
              by_id["live-thread"]["live"] is False and by_id["dead-thread"]["live"] is False)
        check("metadata is carried through",
              by_id["live-thread"]["title"] == "Live one"
              and by_id["live-thread"]["tokens_used"] == 42, str(by_id["live-thread"]))

        print("=== live = someone holds the writer lock ===")
        lock = home / "thread-writer-locks" / "live-thread.lock"
        lock.write_text("")
        fh = open(lock, "r")                     # hold an fd, like codex's flock does
        try:
            live = {t["thread_id"]: t for t in cx.list_threads()}["live-thread"]
            check("holding the lock marks the thread live",
                  live["live"] is True and live["pid"] == os.getpid(),
                  f"live={live['live']} pid={live['pid']} me={os.getpid()}")
        finally:
            fh.close()
        after = {t["thread_id"]: t for t in cx.list_threads()}["live-thread"]
        check("releasing it marks the thread finished", after["live"] is False)

        print("=== the transcript ===")
        r = cx.parse_rollout(rp)
        kinds = [(e["idx"], e["kind"]) for e in r["entries"]]
        texts = {e.get("text") for e in r["entries"]}
        check("the developer preamble is hidden", 2 not in [i for i, _ in kinds], str(kinds))
        check("the injected <environment_context> turn is hidden",
              3 not in [i for i, _ in kinds], str(kinds))
        check("the real user message is kept", "run the tests" in texts)
        check("the assistant's answer is kept", "all green" in texts)
        check("the tool call keeps its input", any(
            e["kind"] == "tool" and e["tool"] == "exec" and e["text"] == "pytest -q"
            for e in r["entries"]), str([e for e in r["entries"] if e["kind"] == "tool"]))
        check("the tool output is attributed to that tool", any(
            e["kind"] == "tool_out" and e["tool"] == "exec" and e["text"] == "3 passed"
            for e in r["entries"]))
        check("an unknown item type survives as 'other', not dropped",
              any(e["kind"] == "other" and e["idx"] == 8 for e in r["entries"]), str(kinds))
        check("the torn last line is skipped, not fatal", r["ordinal"] == 10, str(r["ordinal"]))
        check("session_meta is parsed out of the entries into meta",
              r["meta"].get("cli_version") == "0.152.0" and
              all(e["kind"] != "session_meta" for e in r["entries"]), str(r["meta"]))

        print("=== turn state comes from codex's own events ===")
        check("a completed turn reads idle", cx.turn_state(r["entries"])["idle"] is True)
        check("...carrying the final message",
              cx.turn_state(r["entries"])["last_agent_message"] == "all green")
        mid = [e for e in r["entries"] if e["idx"] < 10]      # drop task_complete
        check("a turn that started and hasn't completed reads busy",
              cx.turn_state(mid)["idle"] is False, str(cx.turn_state(mid)))
        check("a thread with no turn events at all reads idle (nothing to wait for)",
              cx.turn_state([])["idle"] is True)

        print("=== incremental reads ===")
        d = cx.parse_rollout(rp, since_ordinal=8)
        check("only entries after the cursor come back",
              [e["idx"] for e in d["entries"]] == [9, 10], str([e["idx"] for e in d["entries"]]))
        check("...and the cursor still advances to the file's tip", d["ordinal"] == 10)

        print("=== a missing rollout is empty, not an exception ===")
        e = cx.parse_rollout(home / "nope.jsonl")
        check("no file -> empty read", e["entries"] == [] and e["ordinal"] == -1)

        print("=== writing: refusals happen before we shell out ===")
        check("empty thread_id is refused",
              cx.send_message("", "hi")["ok"] is False)
        check("whitespace-only text is refused",
              cx.send_message("live-thread", "   ")["ok"] is False)

        cx.live_codex_processes = _real_procs

        print("=== a session that has no thread yet ===")
        # codex writes a thread row only on the FIRST exchange, so a tab you just
        # opened exists nowhere in its state. Left out of the list, it could not be
        # talked to at all from a phone — you cannot send a first message to a
        # session you cannot see.
        real = {"pane": "%7", "thread_id": "real-thread"}
        cx_list = cx.list_threads
        cx.list_threads = lambda limit=60: [
            {"agent": "codex", "thread_id": cx.PENDING_PREFIX + "9", "cwd": "/w",
             "title": "(new codex session)", "updated_at": 1, "created_at": None,
             "tokens_used": 0, "approval_mode": "", "model_provider": "",
             "rollout_path": "", "pid": 5, "pane": "%9", "live": True, "pending": True},
            {"agent": "codex", "thread_id": "real-thread", "cwd": "/w", "title": "t",
             "updated_at": 2, "created_at": None, "tokens_used": 1, "approval_mode": "",
             "model_provider": "", "rollout_path": "", "pid": 6, "pane": "%7",
             "live": True},
        ]
        try:
            import codex_shim as shim
            check("a pending session resolves by its own id",
                  (shim.find_thread(cx.PENDING_PREFIX + "9") or {}).get("pending") is True)
            check("a pending id whose pane now has a REAL thread follows it there",
                  (shim.find_thread(cx.PENDING_PREFIX + "7") or {}).get("thread_id")
                  == "real-thread",
                  str(shim.find_thread(cx.PENDING_PREFIX + "7")))
            check("a pending session is listed as a tab (so it can be opened)",
                  any(t["sid"].startswith(cx.PENDING_PREFIX) for t in shim.threads_as_tabs()))
            check("queueing a pending id is refused when there is no pane",
                  cx.send_message(cx.PENDING_PREFIX + "9", "hi", pane="")["ok"] is False)
        finally:
            cx.list_threads = cx_list

        print("=== typing into a pane: clear, type, THEN submit ===")
        calls = []
        real_run = cx.subprocess.run
        class R:
            returncode = 0; stdout = ""; stderr = ""
        cx.subprocess.run = lambda args, **kw: (calls.append(args), R())[1]
        real_sleep = cx.time.sleep
        cx.time.sleep = lambda s: None
        try:
            r = cx.type_into_pane("%9", "hello there")
        finally:
            cx.subprocess.run = real_run
            cx.time.sleep = real_sleep
        check("it succeeds", r.get("ok") is True and r.get("method") == "keys", str(r))
        check("three keystrokes: C-u, the literal text, Enter",
              [c[-1] for c in calls] == ["C-u", "hello there", "Enter"], str(calls))
        check("the text goes through -l so it is typed, not interpreted",
              "-l" in calls[1] and "-l" not in calls[0] and "-l" not in calls[2], str(calls))
        check("Enter is its own keystroke, not appended to the text",
              "\n" not in calls[1][-1], repr(calls[1][-1]))

        print("=== the PATH the child gets ===")
        # `codex` is a `#!/usr/bin/env node` script and cc_web runs as a systemd
        # user unit with the minimal PATH, so a node living under nvm is invisible
        # to it: the live endpoint failed with `/usr/bin/env: "node"` while the
        # same call from a shell worked. The child's PATH must be repaired for it.
        real_path = os.environ["PATH"]
        try:
            os.environ["PATH"] = "/usr/local/bin:/usr/bin:/bin"   # a systemd-ish PATH
            nd = cx._node_dir()
            if nd is None:
                check("(no node on this box — nothing to repair)", True)
            else:
                first = cx._exec_env()["PATH"].split(os.pathsep)[0]
                check("a node dir is found even when PATH hides it", True, nd)
                check("...and is prepended to the child's PATH", first == nd,
                      f"{first} != {nd}")
                check("the parent's own PATH is left alone",
                      os.environ["PATH"] == "/usr/local/bin:/usr/bin:/bin")
        finally:
            os.environ["PATH"] = real_path

    os.environ.pop("CODEX_HOME", None)
    print()
    if _fails:
        print(f"FAILED ({len(_fails)}): " + "; ".join(_fails))
        return 1
    print("all good")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
