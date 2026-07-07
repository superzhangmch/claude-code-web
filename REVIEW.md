# Code review — findings & resolutions (2026-07)

First full review of `cc_web.py` (~4.5k lines), `static/index.html` (~5.4k, single-file
SPA), and `iterm_bridge.py`. Done with three parallel reviewers (backend / frontend /
bridge), then each finding was verified, fixed or dismissed, self-reviewed, and covered
by regression tests + an end-to-end check on the live server.

Fix commits: `7809cba` (main), `a5eec17` (remaining), `2e5988d` (self-review catch),
`4d843af` (tests). Regression suite: `python3 tests/test_review_fixes.py` (33 checks).

## HIGH — fixed

- **Event-loop blocking → whole server hangs.** `verify_binding` forked `ps` on *every*
  poll of every client; a slow/stalled `ps` froze the entire async loop (observed live).
  → cache the (immutable) pid start-time behind a 4 s TTL (`_pid_start_cached`);
  `os.kill(pid,0)` still detects death instantly. Also offloaded the multi-second
  endpoints to threads: `/api/search` (rg), `/api/session-info` (`_ps_descendants`),
  `/api/sessions` (battery + runaway procs), `/api/cpu-history` (`_sample_top_mem_groups`).
- **iTerm2 RPCs had no timeout.** A hung/half-open websocket await wedged the loop
  forever. → `asyncio.wait_for` on every RPC (connect / get_app / refresh / get_variable /
  get_contents / get_screen_contents); `ps`/`lsof` moved off-loop via `asyncio.to_thread`,
  per-pid `lsof` now concurrent instead of one-at-a-time.
- **Arbitrary file read (security).** `/api/fs/file` + `/api/fs/list` resolved any path with
  no root confinement (and `fs_file` accepts the token in `?token=`). → confined to the
  home tree (`_fs_allowed`); path outside → 403. Verified `/etc/passwd` → 403.
- **Frontend cross-session poll race.** An in-flight `/api/state` (and screen/info/tail/
  tabs) response resolving after the user switched session ingested one session's rows
  into another and desynced `since_idx`. → capture the sid before the fetch, drop the
  response if it changed. Also: stop re-polling forever after a 410 (pid gone); clear the
  stale pid-gone/mismatch banner on enter.
- **`ask_peer` returned truncated replies.** Accepted "turn done" on a transient
  end-turn mid-work. → accept only after seeing the peer go active→idle (or a longer
  stable-idle streak for very fast replies).

## MEDIUM — fixed

- Unbounded `_SCREEN_DELTA_CACHE` → capped. Auth token no longer logged in plaintext.
- `_is_claude_cmd` matched any path arg containing `claude` (e.g. `tail /var/log/claude`)
  → wrong-tab bind; now matches only `claude` as argv[0] or a node/bun/deno script.
- Superseded iTerm2 connections leaked (one per enumeration) → closed via `_close_conn`.
- Screen-delta reconstruction didn't bounds-check `changed` indices (silent corruption)
  → guarded (`0 <= i < n`), server + both client copies.
- `renderMarkdown` passed marked's raw HTML through → potential XSS from transcript /
  peer-relayed content; now sanitized (drops script/style/iframe/…, strips
  `on*`/`javascript:`/`srcdoc`) before it hits the DOM.
- `ask_peer`: `--to` resolves by prefix first (substring fallback); the timeout branch's
  `state()` is guarded so it always emits JSON; `maybe_error` regex tightened to real
  transport failures.

## Self-review catch

- `_pid_start_cached` used `time.monotonic()`, but the module does `import time as _time`,
  so `time` is undefined. Latent because `verify_binding` only runs when a session is
  *bound* — nothing was attached during smoke tests, so it never fired; the moment any
  session binds, every poll would `NameError → 500`. → `_time.monotonic()` (`2e5988d`),
  unit-tested, and E2E-verified by attaching a session and polling (`200`, no traceback).
  Lesson: "endpoint returns 200" isn't enough — state-gated paths (bind-required) need
  their own tests.

## Reviewed and deliberately NOT changed (with reason)

- **`build_picker_sessions` stays on the event loop.** Threading it needs a common lock
  across every accessor of its currently-lockless view caches, or it introduces races;
  the blocking is one-shot on picker load, not the per-poll hot path — risk > reward.
- **No send-time "stale binding → wrong tab" recheck.** iTerm session GUIDs are not
  reused for new sessions, and `verify_binding` already confirms pid liveness + store
  session identity, so wrong-tab delivery isn't realistic.
- **cwd → project-dir encoding is correct**, not a bug — it mirrors Claude Code's own
  scheme (`/` and `_` → `-`), verified against real `~/.claude/projects/` dir names.
- **`ps` column parsing** keeps exact-field-count checks — malformed lines are safely
  skipped (never mis-assigned), which is acceptable for the real `ps` output format.

## One behaviour change to note

`/api/fs/*` now returns 403 for paths outside the home tree. Real uses (uploads,
project files, `~/.claude/image-cache`) are all under home. To serve files elsewhere,
add that root to `_fs_allowed`.
