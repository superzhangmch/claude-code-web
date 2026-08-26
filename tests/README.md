# tests

No pytest, no framework: each file is a script that prints `ok` / `FAIL` lines and exits
`0` when everything passed. Run one, or run them all:

```sh
tests/run_all.sh                     # all of them, with a port check first
.venv/bin/python tests/test_x.py     # just one
```

Use the venv's python for anything that starts a server (`fastapi`, `uvicorn`,
`websockets`); the node-only ones run under plain `python3`.

## Conventions

- **`$HOME` is always a throwaway dir** for tests that start a server. The real
  `~/.claude` holds your config, bindings, session snapshots and the single-instance
  lock — a test must never be able to touch it.
- **Never print a secret.** `test_grammar_api.py` asserts the api_key and api_base never
  appear in a response body.
- **Test the failing direction.** When a test is added for a fix, re-break the fix and
  confirm the test goes red. Several tests in here caught real bugs only because of that.
- **The front-end tests extract the REAL function** out of `static/index.html` and run it
  under node, rather than keeping a copy that can drift.

## Gotcha: fixed ports

Most server tests bind fixed ports in the 8993–8999 range. **A local dev/stub server
sitting on one of those makes them fail in ways that look like real bugs** (this cost me
three false alarms in one day). `run_all.sh` checks first and tells you. `test_ui_smoke.py`
uses ephemeral ports and has no such problem.

## What each one covers

| file | needs | covers |
|---|---|---|
| `test_ui_smoke.py` | geckodriver + firefox | The real page in a real browser: brief list rows, brief/full toggle, the resume-source chooser and its preview, and the "can't reach the terminal" banner + reconnect. Skips if the browser isn't installed. |
| `test_bridge_recovery.py` | venv | Terminal-bridge failure handling (reported, not swallowed; readable reasons; connect retries) and the session snapshots: two stores, one auto entry per session-SET, the 100 cap, resume source selection, cancel, and idempotent re-runs. |
| `test_brief_list.py` | venv | `GET /api/sessions?brief=1`: tabs only, no transcript excerpts, and "last used" read from a tail rather than the file mtime (which lies). |
| `test_tab_detection.py` | venv | A live claude tab must not be silently demoted to a plain shell: an unreadable session variable is retried, counted and logged, a tab that answers neither key is reported as blind, and the autosave refuses to record an incomplete enumeration. |
| `test_exit_paths.py` | venv (+ node) | Ending a tab: `/exit` first only when claude is actually in there (not "when we have a session id"), jobs still veto the close, and all four UI triggers — composer `/exit`, ⚙ menu, tab list ⏏, picker card Close — go through the one path. |
| `test_gear_menu_ui.py` | node | The shipped `renderAsrMenu()`: one labelled row for the mode and one for the model (not one line per option), the shared vendor prefix dropped only when they really all share it, and "not configured" still naming the config keys. |
| `test_brief_row_ui.py` | node | The shipped `briefRow()`: column order, name fallbacks, the `~` on an approximate time, click → enter/attach, and brief hiding the search chrome. |
| `test_voice_unified_path.py` | node | One recording path for both ⚙ modes: segmented batch (⏸ transcribes), retry of a failed segment, the 5-min cap, and realtime still streaming. |
| `test_voice_batch_fallback.py` | node | A recording that falls back to batch stays batch until it ends, and ⏸ during a dead stream doesn't kill the session. |
| `test_asr_stream_e2e.py` | venv | Real cc_web + a fake soniox: pre-connect buffering, the empty FINISH frame, drain on stop. |
| `test_grammar_api.py` | venv | `/api/grammar` always carries a `status`, so "not configured" and "call failed" can't look like "your text is fine". Also: no secrets in responses. |
| `test_grammar_ui.py` | node | What each `status` renders, and that a failed call is not cached (one flake used to pin the message until a page reload). |
| `test_review_fixes.py` | venv | Assorted earlier fixes; kept as a regression net. |
