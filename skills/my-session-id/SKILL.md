---
name: my-session-id
description: 查出"我自己"这个 claude-code 会话的 session id 和 pid（权威、非猜测）。Find THIS claude-code session's own session id + pid. Use when you need to identify yourself — e.g. to tell another session who you are, to fill `--from` for the ask-peer-claude-code skill, or to look yourself up in claude-code-web. Triggers: "我的 session id 是多少", "what's my session id", "我自己是哪个 session", "报出我自己的身份", "my pid".
---

# my-session-id — know thyself

A claude-code agent has **no env var** for its own session id (`CLAUDE_SESSION_ID`
doesn't exist). But claude-code writes an authoritative record at
`~/.claude/sessions/<pid>.json = {pid, sessionId, cwd, status, …}` for each live
session — the same store cc-web uses as its PRIMARY pid↔sessionId resolver.

So the reliable "whoami" = **walk up the process tree from this shell to the
claude process that owns a `~/.claude/sessions/<pid>.json`, and read `sessionId`**.
Ground truth, not a guess (mtime-of-newest-jsonl guessing can pick the wrong
session; this can't).

## Use
```bash
WHO=~/.claude/skills/my-session-id/whoami.sh
bash "$WHO"            # → session_id / pid / cwd / status / store
bash "$WHO" --id-only  # → just the session id (for scripting)
```

Example — introduce yourself to a peer via ask-peer-claude-code:
```bash
MINE=$(bash ~/.claude/skills/my-session-id/whoami.sh --id-only)
echo "问题…" | python3 ~/.claude/skills/ask-peer-claude-code/ask_peer.py \
    --to <PEER_SID> --host <IP> --from "$MINE"
```

## Notes
- Works from inside any Bash tool call: the script's shell is a descendant of
  the claude process, so walking up PPIDs reaches it.
- Needs claude-code's `~/.claude/sessions/` store (present in current versions).
  If missing, it errors instead of guessing — fall back to a unique-marker grep
  (emit a distinctive string, `grep` it in `~/.claude/projects/<proj>/*.jsonl`).
