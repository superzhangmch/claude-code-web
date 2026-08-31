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

## peer-relay-responder

Tells a session **how to answer a message that was relayed into it** from another
session. Two kinds, told apart by the tag the relay puts at the start:

- **internal** — `[⇄ from peer claude <id> (name)]`, one of the owner's own
  sessions (what `ask-peer-claude-code` sends). Just answer in your terminal;
  the asker reads your transcript.
- **external** — `[⇄ from external peer session — <who>]`, someone who is *not*
  the owner, relayed in through some bridge. The reply goes back by POSTing it to
  a **pre-agreed local** destination; the message carries only a one-time `req` id.

```sh
cp -R skills/peer-relay-responder ~/.claude/skills/
```

Deliberately bridge- and product-agnostic: it depends only on the tags, and the
helper holds no keys — the message gives it a `req` id and nothing else.

Where the reply goes is **hardcoded** in `reply_to_bridge.py` (the local bridge)
and cannot be specified at all — not by the message, not by env, not by a flag. So
there is no input through which a forged message could redirect a reply or use
this session to POST at services only this machine can reach. Moving the bridge
means editing that one line.

Redirects are still refused: urllib turns a 302 POST into a GET, so following one
would drop the reply body while the call still looked like it succeeded.

What no check can decide is whether an answer should be given at all, which is why
the skill keeps a short list of what not to do on a stranger's request.

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

`ccsid` prints one line naming this session, where to reach it and what it is
called, in the same shape cc-web uses, and copies it to the clipboard (`pbcopy`
on macOS, `wl-copy`/`xclip`/`xsel` on Linux). Run it inside any Bash tool call,
or as `! ccsid` at the claude-code prompt.

Put it on PATH as a **symlink**, not a copy. A copy is how one host silently kept
printing the old bare-uuid line long after every `skills/` tree had been updated:
the file being maintained and the file being run were not the same file.

```sh
mkdir -p ~/.local/bin
ln -sfn ~/.claude/skills/my-session-id/ccsid ~/.local/bin/ccsid
# then, inside a claude-code session:
ccsid
# -> claude_code_session_id=d4289270-...-5866b0237486 at host.tailnet.ts.net:8443, tab_name=cc-web
#    (no reply to this)
```

Two lines, and only the first goes to the clipboard.

Everything after the id is best-effort and printed only when true: the tailnet
**DNSName** (not the admin-console display name — they differ), and the port read
from a running cc-web's own argv. `tab_name` is asked of the **local cc-web**, so
it is the name the human is looking at. The store file also carries a `name`, but
it is a startup snapshot and usually an auto-slug — on mac-pro 14 of 15 sessions
disagreed (`tmp-6d` in the store vs `Hello` on the tab) — so it serves only as
the fallback for when cc-web is down. No cc-web or no tailscale → no address, since
the id alone still resolves via `ask_peer`'s host search.

That last line is aimed at the model, not at you. Typed as `! ccsid`, this stdout
is the only thing the model sees — and a `!` command **always** costs a turn
(measured: `!` bypasses `UserPromptSubmit` hooks entirely, and even a zero-output
one draws a reply), so the turn cannot be prevented, only kept short.

It depends on the `my-session-id` skill being installed at
`~/.claude/skills/my-session-id/`.
