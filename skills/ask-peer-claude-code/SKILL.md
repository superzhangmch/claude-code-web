---
name: ask-peer-claude-code
description: 和另一个正在运行的 claude-code session 交互（通过 claude-code-web 桥）—— 给它发消息、拿回它的回复。Interact with / consult / delegate to / check the status of ANOTHER claude-code session (identified by its session id or the cc-web URL #s=<id>). Triggers: "和另一个 claude-code 交互", "问一下那个 session", "ask the peer claude-code", "让另一个 claude 帮忙", "问问 mac-pro 那个 claude 在干啥".
---

# ask-peer — interact with another claude-code session

Two claude-code sessions talk **through the claude-code-web (cc-web) HTTP API**,
so everything stays visible in real iTerm tabs and a human can watch / intervene.

- **You (caller)** send a message into the peer's tab and read its reply.
- **The peer** is a normal claude-code session — it needs zero setup; it just
  receives your message and answers.
- Read side is the clean structured transcript (not screen-scraping). Turn-done
  is detected via cc-web's `claude_idle` + `pending_confirm`.

## 什么时候**不要**主动联系(默认:不主动)
本 skill 是**被动 / 按需**工具,**不是后台代理**。默认**绝不主动**联系任何别的
session。只有下面两种情况才发起联系:
1. **已经和某个 session 建立了联系**(对方先带 `[⇄ from peer claude <id>]` 前缀
   联系过你)—— 此时对话是**双向开放**的:你不仅要回答,**如果你有需要,也可以
   主动反过来问他 / 追问 / 请他帮忙**。来回对话完全允许。
2. **用户明确让你**去联系 / 询问 / 委派某个 session。

除此之外(即:**既没人联系过你、用户也没让你联系**),**不要**去枚举 / 扫描别的
session,也**不要"无缘无故"**找一个陌生 session 交流 —— 没有以上理由,就不发任何
消息。(peek `--no-send` 同样受此约束。)

## 两个角色 (both directions)
- **主动问别人 (caller)**: 在上面两种情况下,用本 skill 去问/委派/查另一个 session
  的状态 —— 只要知道对方的 session id(host 可省,脚本会在已知机器间自动定位)。
- **被别人问 (responder)**: 如果你在对话里**收到一条带 `[⇄ from peer claude <id>]`
  前缀的消息**,那是另一个 claude-code 在问你 —— **你直接正常回答就行**。对方正在
  轮询你的 transcript,你答完(回合结束)它**自动就能看到**,你**不需要运行任何东西
  去"发回去"**。只有当你想**主动发起一次新的**询问时,才自己调用本 skill。
- 想让对方以后能主动找你:在回复里带上你自己的 session id + host(用
  `my-session-id` skill 拿自己的 id)。

## The script
`ask_peer.py` — sends, then polls the peer until its turn ends, prints JSON:
`{status, reply, pending_confirm, idle, elapsed, since_idx, note}`
where `status ∈ done | pending_confirm | timeout | maybe_error | peek`.

**Prefer stdin for the message** (no shell-quoting pain, handles multi-line/code):

```bash
PY=~/.claude/skills/ask-peer-claude-code/ask_peer.py
# ask something and wait for the reply (host auto-located from the id):
echo "你现在在干啥?进度如何?" | python3 "$PY" --to <PEER_SID> --from <MY_SID>
# peek only (what is it doing right now? — no message sent):
python3 "$PY" --to <PEER_SID> --no-send
# pin a host explicitly if you already know it:
python3 "$PY" --to <PEER_SID> --host <IP> --no-send
```

Args: `--to` peer session id (required; a short prefix/substring is OK — it's
resolved against the host's live claude tabs, **unique match only**; 0 or >1
matches → error, so it never mis-delivers) · `--host` cc-web tailscale IP
(**optional** — if omitted, the script auto-locates the session across the
hosts configured in `~/.claude/cc_web.conf` (`hosts=<ip1>,<ip2>`) or
`$CC_WEB_HOSTS`, local first, and reports the resolved `host` in its JSON) ·
`--token` (default: from `~/.claude/cc_web.conf`) ·
`--from` your own session id (added as a source prefix so the peer knows it's a
peer, not a human) · `--timeout` sec (default 480) · `--mode brief|medium` ·
`--no-send` peek · `--raw` send verbatim (use to answer a pending prompt/choice).

## Handling the result `status`
- **done** → `reply` is the peer's answer. Continue your reasoning; ask again if needed.
- **pending_confirm** → the peer is asking something (a choice menu or free text).
  Decide the answer, then send it with `--raw` (e.g. the choice number, or text).
- **maybe_error** → reply text looks like an API error / interruption; re-send
  `继续` (with `--raw`) to make it retry.
- **timeout** → peer still working after the window; peek again later or raise `--timeout`.
- If `brief` is unclear, ask it directly ("你现在在干啥?") or add `--mode medium`,
  or grab a screen snapshot: `GET /api/screen?claude_session_id=<SID>` (current
  screen only — TUI scrollback is noisy, use as a snapshot not history).

## Notes
- Each message gets a short tag `[⇄ from peer claude <id8>]` so the peer can tell it's
  a peer relay, not the human — nothing more per-message. On FIRST contact,
  introduce yourself in the body (who you are + your full session id + host) so
  the peer can reach back; after that the short tag is enough.
- Blocking is fine for a caller/consult pattern; for two peers that both initiate,
  give each other's session id + host and each can ask the other.
- Get your own `--from` id with the `my-session-id` skill:
  `--from "$(bash ~/.claude/skills/my-session-id/whoami.sh --id-only)"`.
- cc-web must be running on the peer's machine. Set the auto-search hosts in
  `~/.claude/cc_web.conf` as `hosts=<ip1>,<ip2>` (or export `$CC_WEB_HOSTS`);
  or just pass `--host <ip>`. (Real IPs stay in local config, never in this repo.)
