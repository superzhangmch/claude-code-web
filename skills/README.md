# skills

Claude Code skills used by `cc_web`. Install one by copying its
directory into `~/.claude/skills/`.

## session-name

Lets you name the current Claude Code session ("remember this session
as the LTV refactor", "rename this session to ...") and search by
title later. The skill writes to `~/.claude/session_index.json`, which
the `cc_web` picker reads to show human-friendly titles instead of
"(unnamed) <first user msg>".

```sh
cp -R skills/session-name ~/.claude/skills/
# then in any claude session:
#   "remember this session as <title>"
```

Without it the web UI still works, but every session in the picker
shows up as `(unnamed)` and is sorted only by recency.

## ask-peer-claude-code

Lets one claude-code session **talk to another** through the `cc_web`
bridge: send a message into the peer's iTerm tab and get its reply.
Read side is the clean structured transcript (`/api/state`); turn-done
is detected via cc-web's `claude_idle` + `pending_confirm`. Everything
stays visible in real tabs, so a human can watch and take over.

```sh
cp -R skills/ask-peer-claude-code ~/.claude/skills/
# ask a peer and wait for its reply (message via stdin):
echo "现在进度如何?" | python3 ~/.claude/skills/ask-peer-claude-code/ask_peer.py \
    --to <PEER_SESSION_ID> --host <PEER_IP> --from <MY_SESSION_ID>
```

The peer is a normal session — zero setup; it just answers, and the
caller's poll picks up the reply. `--to` accepts a unique id prefix.

### To use it — what you need / what to change

1. **`cc_web` running on the peer's machine** (this project). `--host` is
   that machine's Tailscale/LAN IP; it defaults to the local machine's own
   IP for a same-machine peer, so cross-machine you must pass `--host`.
2. **Auth token.** The script authenticates with the token in the *local*
   machine's `~/.claude/cc_web.conf` (`token=...`). To reach a peer on
   **another** machine, either give both machines the **same token** in
   their `cc_web.conf`, or pass `--token <peer-machine-token>` explicitly.
3. **Know the peer's session id + IP.** Get the id from the peer via the
   `my-session-id` skill, or from its `cc_web` URL `.../#s=<id>` (a unique
   prefix is enough for `--to`).
4. Nothing to change on the **peer** side — it's a normal session.

### Optional: let a peer reach you PROACTIVELY

An outbound call driven by *another* session is a network action, so
auto-mode blocks it by default. To allow it, **on the machine that will
send** (i.e. where the initiating session runs):

```sh
# 1) stable entrypoint on PATH (so a narrow allow-rule can match reliably):
printf '#!/bin/bash\nexec /usr/bin/python3 "$HOME/.claude/skills/ask-peer-claude-code/ask_peer.py" "$@"\n' \
    > ~/.local/bin/ask-peer && chmod +x ~/.local/bin/ask-peer
```
Then add this line to `~/.claude/settings.json` under `permissions.allow`
(a human must do this — an agent can't self-grant it), and always invoke
the bare `ask-peer` command so the rule matches:
```json
{ "permissions": { "allow": ["Bash(ask-peer:*)"] } }
```
Also: the **receiving** session must be *attached* in its own `cc_web`
(open its `.../#s=<id>` in the web UI) so the message can be delivered,
and the sender must use the **full** session id in `--to`.

## my-session-id

Finds THIS claude-code session's own session id + pid — authoritatively,
by walking up the process tree to the claude process that owns a
`~/.claude/sessions/<pid>.json` (the same store `cc_web` uses as its
primary pid↔sessionId resolver). Handy for filling `--from` above.

```sh
cp -R skills/my-session-id ~/.claude/skills/
bash ~/.claude/skills/my-session-id/whoami.sh            # id / pid / cwd / status
bash ~/.claude/skills/my-session-id/whoami.sh --id-only  # just the id
```

### `ccsid` — one-shot session-id command

`ccsid` is a tiny convenience wrapper around `whoami.sh --id-only`: it prints
`claude_code_session_id=<id>` and copies it to the clipboard (macOS `pbcopy`).
Run it inside any Bash tool call, or as `! ccsid` at the claude-code prompt,
to grab the current session's id (e.g. to open it in `cc_web`).

```sh
cp skills/my-session-id/ccsid ~/.local/bin/ccsid && chmod +x ~/.local/bin/ccsid
# then, inside a claude-code session:
ccsid            # -> claude_code_session_id=<id>  (also copied to clipboard)
```

It depends on the `my-session-id` skill being installed at
`~/.claude/skills/my-session-id/`.
