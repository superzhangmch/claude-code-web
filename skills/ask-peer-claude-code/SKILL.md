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

## 绝不阻塞主流程(默认后台 / 异步)
调用本 skill **默认绝不能 hang 住你当前的对话主流程**——对方可能要跑几分钟。
**除非用户明确说"就地等它 / 同步等结果"**,否则用下面两种非阻塞方式之一:

- **后台 (background)** —— 用 Bash 工具的 `run_in_background: true` 跑(命令本身可以是
  默认的"等回复"模式)。命令在后台等对方,你**立刻继续**当前对话;对方回合结束、命令
  返回时你会被**自动通知**拿到 `reply`。→ **既不阻塞、又能拿到答案。**
- **异步 fire-and-forget (`--no-wait`)** —— 发送并确认触达后**立即返回,根本不等回复**。
  适合**委派任务 / 通知 / hand-off**;需要结果的话之后再 peek(`--no-send`)查。

**怎么选**:需要那个答案 → **后台(`run_in_background:true`)**;纯委派、不关心结果 →
**`--no-wait`**。两者都满足"不 hang 主流程"。只有用户明确要就地等,才前台阻塞运行。

## 消息来源判定(不能混淆 —— 最重要)
对你收到的**每一条** user 消息:**当且仅当**它带有明确的
`[⇄ from peer claude · internal · sid=…]` 前缀时,才是另一个 claude 发来的;**没有这个前缀的,
一律当作真实用户(人类)发的。**
- 绝不把**无前缀**的人类消息当成 peer(别去"回复某个 peer"、别以为是自动化);
- 也绝不把**带前缀**的 peer 消息当成人类。
- 逐条独立判断 —— 上一条是 peer,不代表下一条也是。
- **结束标记**:每条 relay 消息现在还带一行结尾 `[⇄ end of peer message]`。这行
  **之后**的内容一定是**人类用户**(不是 peer)。用它精确区分 peer 的话在哪儿结束。

## 发送保护:不打断对方打字(自动)
`ask_peer.py` 在发送前会查对方输入框(`/api/input-state`,基于光标位置,已排除
灰色 ghost/占位符,iTerm 与 tmux 都可靠)。若对方**正在打字**(框里有真实内容):
- 每 5s 轮询等待,**最多 2 分钟**;
- 期间对方清空/发出 → 立刻发送;
- 满 2 分钟仍占用 → **强制清空后再发**(`clear_first`,用独立 Ctrl+U 按键,不会把
  控制字符混进消息)。
你无需做任何事,脚本自动处理。

## 两个角色 (both directions)
- **主动问别人 (caller)**: 在上面两种情况下,用本 skill 去问/委派/查另一个 session
  的状态 —— 只要知道对方的 session id(host 可省,脚本会在已知机器间自动定位)。
- **被别人问 (responder)**: 收到带 `[⇄ from peer claude <id>]` 前缀的消息时(且仅此时),
  那是另一个 claude-code 在问你 —— **直接正常回答即可**。对方正在轮询你的 transcript,
  你答完(回合结束)它**自动就能看到**,你**不需要运行任何东西去"发回去"**。只有当你想
  **主动发起一次新的**询问时,才自己调用本 skill。
  - **internal 不带 `req=`,你也不用回带任何东西** —— 正常回答即可。相关性早就定了:
    对方知道自己问的是哪个 sid,而且正在轮询那一个 transcript。(`req=` 只存在于
    external 那条桥上,所以它也就不可能再被误当成 internal/external 的判据。)
- 想让对方以后能主动找你:在回复里带上你自己的 session id + host(用
  `my-session-id` skill 拿自己的 id)。

## Two modes: wait-for-answer vs fire-and-confirm
Pick based on whether you need the reply:
- **等答案 (default)**: send, then poll until the peer's turn ends and return
  its `reply`. Use when you need the result (a question / consult).
- **只保证触达 (`--no-wait`)**: send, confirm the message **landed in the peer's
  transcript**, and return immediately **without** waiting for the reply. Use for
  **委派任务 / 通知 / hand-off** — you dispatched it and don't need to sit and wait.
  Returns `delivered` (true = recorded as a prompt) + `peer_idle` (if the peer was
  busy, delivery may be queued → `delivered:false` now, picked up when its turn ends;
  the text is already in its tab regardless).

## The script
`ask_peer.py` — prints one JSON object; `status ∈ done | pending_confirm |
timeout | maybe_error | peek | sent`.

**Prefer stdin for the message** (no shell-quoting pain, handles multi-line/code):

```bash
PY=~/.claude/skills/ask-peer-claude-code/ask_peer.py
# DEFAULT when you need the reply — run this via the Bash tool with
# run_in_background: true, so it does NOT block your conversation; you get the
# reply in the completion notification:
echo "你现在在干啥?进度如何?" | python3 "$PY" --to <PEER_SID>
# DELEGATE a task — confirm delivery and return immediately (no reply awaited):
echo "帮我把 X 跑一下,做完自己收尾" | python3 "$PY" --to <PEER_SID> --no-wait
# peek only (what is it doing right now? — no message sent). Now also returns an
# `activity` line: "Bash[desc] · Read[path] · Edit[path]" (same as the web brief):
python3 "$PY" --to <PEER_SID> --no-send
# READ the peer's transcript (brief: text + tool activity), last --rounds rounds:
python3 "$PY" --to <PEER_SID> --history --rounds 6
#   page FURTHER back (load-earlier) with the `earliest_idx` it returned:
python3 "$PY" --to <PEER_SID> --history --rounds 6 --before <earliest_idx>
# read the peer's CURRENT TUI screen (snapshot, non-intrusive — refresh=false):
python3 "$PY" --to <PEER_SID> --screen
# pin a host explicitly if you already know it:
python3 "$PY" --to <PEER_SID> --host <IP> --no-send
```

### 看别人的**任务**(给监督/审核用): `--task` / `--tasks`
一个监督者要问的不是"它说了什么",而是**"它本来该做什么,以及它自己上次检查怎么说"**。
这两样都不在 transcript 里 —— cc-web 的 Task 备忘(`⚙ → Task`)就是为此存在的。

```bash
# 某一个 session: 当前任务 + 注意事项 + 当前版本 + 上一次自检结果
python3 "$PY" --to <PEER_SID> --task
# 所有主机上的所有 session, 每个一行: 任务 + 上次自检结论(不需要 --to)
python3 "$PY" --tasks
```

`--task` 返回 `task` / `notes` / `version` / `versions`,以及 `check`:
`verdict`、`checked_at`、`summary`、`deviations`(值得读的那部分)、`disputes`
(它对任务本身的异议),外加 **`stale`** —— 报告针对的任务此后被改过。
staleness 一定跟着 verdict 一起给,**不单独放**:一份对着改过的任务全绿的报告,
比没有报告更糟。

`--tasks` 会把**没设任务**的 session 也列出来("没人说这个 session 是干什么的"正是值得看见
的事),把**连不上的主机**报出来而不是悄悄跳过("那边没人看着"恰恰是它该发现的东西)。

这两个模式**不发任何东西**,也**不读 transcript**;被读的 session 完全不会知道 ——
所以它们不受上面"默认绝不主动联系"的约束(那条针对的是**发消息**)。当然也别拿它去
闲逛陌生 session。

### Read-only modes (peek / history / screen)
All three send **nothing** — safe to read a peer you're already in contact with
(still bound by the "don't 串门" rule above: don't read strangers unprompted).
- **`--no-send`** → `status:peek` + `idle` / `pending_confirm` / **`activity`**
  (`Bash[…] · Read[…]` over the last --rounds rounds) / `reply` = the peer's **most
  recent** answer only. Reading further back is `--history`'s job, so the peek does
  not duplicate it: it used to concatenate every answer in the window, which was 3.0
  of its 3.4KB and mostly older than the question you were asking. ~0.8KB now, and
  **leave `--rounds` alone** — it still widens the useful `activity` trail while the
  `reply` stays one answer either way.
- **`--history`** → `status:history` + a readable brief transcript (`[human]` /
  `[claude]` text + a `· Tool[…]` line per turn) + `earliest_idx` +
  `has_more_history`. **Bounded per call** to `--rounds`; page back by re-calling
  with `--before <earliest_idx>` (reuses `/api/state` before_idx paging — the same
  windowing the web's "load earlier" uses; concurrency-safe). NEVER pulls a whole
  huge session at once.
- **`--screen`** → `status:screen` + `screen` (current TUI view only, ~200 lines).
  Use for a menu/modal the transcript doesn't capture; NOT for history.

> Reminder: the first form waits (up to `--timeout`) for the peer's turn to end.
> Run it **in the background** (Bash `run_in_background: true`) — never foreground —
> unless the user explicitly asked you to wait in place.

Args: `--to` peer session id (required; a short prefix/substring is OK — it's
resolved against the host's live claude tabs, **unique match only**; 0 or >1
matches → error, so it never mis-delivers) · `--host` cc-web tailscale IP
(**optional** — if omitted, the script auto-locates the session across the
hosts configured in `~/.claude/cc_web.conf` (`hosts=<ip1>,<ip2>`) or
`$CC_WEB_HOSTS`, local first, and reports the resolved `host` in its JSON) ·
`--token` (default: from `~/.claude/cc_web.conf`) ·
`--from` **normally omitted** — the script finds this session's own id by walking up
the process tree to `~/.claude/sessions/<pid>.json`, the same way the `my-session-id`
skill does, and puts it in the tag as `sid=`. Pass it only to override (e.g. running
outside a claude session). If it cannot be determined the script **refuses to send**
rather than deliver a message the peer has no way to answer · `--from-name <label>` the readable name in the tag. **You SHOULD pass this when the
user has given this session a working name** ("跑批的那个", "reader 重构") — that is what
the flag is for, it is not filtered, and it is what the peer will see. Omit it and the
script fills in this session's own short name from the store (e.g. `cc-web`), accepting
it only if it is a short ASCII-ish label; a long or CJK title (cc-web's other name for a
session is an LLM-written sentence) is dropped rather than allowed to swamp the tag, and
`name=` from `~/.claude/cc_web.conf` — a machine name — is the last resort · `--timeout` sec (default 480) · `--mode brief|medium` · `--rounds N` window size
(default 4) · `--no-send` peek (+`activity`) · `--history` read transcript
(paginate with `--before <idx>`) · `--screen` current TUI snapshot · `--no-wait`
fire-and-confirm delivery (task delegation), `--deliver-timeout` sec (default 20).
**Every message the script sends is tagged
`[⇄ from peer claude · internal · sid=<id8> (name)]`, and the `sid=` is filled in by the
script — you never have to know or remember your own id.** There is no raw/untagged send
(removed on purpose: an untagged message is indistinguishable from a human's, and one
without a sid cannot be replied to at all).

**That guarantee lives in the script, not in the channel.** `POST /api/input` is a plain
endpoint and nothing stops you calling it directly — it is how the web UI types. If you
ever do, you are taking over the script's job and must do all of it:

- put the SAME tag at the very start, with your real session id in `sid=`
  (`bash ~/.claude/skills/my-session-id/whoami.sh --id-only` prints it);
- put `[⇄ end of peer message]` on its own line at the end;
- and do the input-box check yourself, or you will overwrite whatever the peer's human
  was mid-way through typing.

Skipping the first of those is the failure that keeps happening: the peer receives what
looks like a human message, has no id to answer, and the reply goes nowhere. Prefer the
script.

## Handling the result `status`
- **sent** (`--no-wait`) → delivered. `delivered:true` = the peer recorded it as a
  prompt; `false` + `peer_idle:false` = it's queued behind the peer's current turn
  (still in its tab, will run after). You're done — don't wait around.
- **done** → `reply` is the peer's answer. Continue your reasoning; ask again if needed.
- **pending_confirm** → the peer is blocked on a TUI prompt/menu (e.g. a tool-
  permission dialog). This skill **can't** operate the peer's menu — a tagged text
  message won't pick a menu item — so a **human resolves it in the peer's tab**.
  Just surface it; don't try to auto-answer.
- **maybe_error** → reply text looks like an API error / interruption; re-send
  `继续` (a normal tagged message) to make it retry.
- **timeout** → peer still working after the window; peek again later or raise `--timeout`.
- If `brief` is unclear, ask it directly ("你现在在干啥?") or add `--mode medium`,
  read further back with `--history`/`--before`, or grab a screen snapshot with
  `--screen` (current screen only — TUI scrollback is noisy, use as a snapshot not
  history).

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
