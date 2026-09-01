# 与其它 agent 会话互通 (ask-peer)

这台机器上同时跑着 codex 和 claude-code 的会话，都由 claude-code-web 托管：
codex 在 `:8444`，claude 在 `:8443`（同一份服务，`CC_WEB_AGENT` 决定它服务谁）。
你可以和它们互相发消息 —— 双向都走同一个脚本。

安装位置：本文件的原本在 `claude-code-web` 仓库
`skills/ask-peer-claude-code/AGENTS.codex.md`，部署到 `~/.codex/AGENTS.md`。

## 什么时候**不要**主动联系（默认：不主动）

和 claude 那边同一条规矩，写在这里以免只有一边守：**默认绝不主动**联系别的会话。
只有两种情况才发起：

1. **对方先联系过你** —— 你收到过带 `[⇄ from peer …]` 前缀的消息。此时对话是双向
   开放的：你不仅要回答，需要时也可以反过来问他。
2. **用户明确让你**去联系 / 询问 / 委派某个会话。

除此之外不要去枚举、扫描或"顺便"找一个陌生会话说话。只读的 peek 同样受此约束。

## 收到的消息怎么判断（最重要）

对你收到的**每一条**用户消息：**当且仅当**它带有

```
[⇄ from peer claude · internal · sid=<id8> (name)]      ← 来自一个 claude 会话
[⇄ from peer codex  · internal · sid=<id8> (name)]      ← 来自另一个 codex 会话
```

这样的前缀时，它才是另一个 agent 发来的；**没有这个前缀的，一律当作真人发的。**
每条独立判断 —— 上一条是 peer，不代表下一条也是。消息结尾还有一行
`[⇄ end of peer message]`，**它之后**的内容一定是人写的。

前缀里写着哪一种 agent，是因为**回复要发给不同的地方**：codex 的 thread id 和
claude 的 session id 由不同的工具寻址，认错了就会把回复投到不存在的会话上。

## 被问的时候：什么都不用做

收到 peer 消息就**正常作答**即可。对方在轮询你的 transcript，你答完（回合结束）
他自动就能读到，**你不需要运行任何东西去"发回去"**。

## 主动问别人

先知道自己是谁，再发：

```bash
codexsid
# -> codex_session=01a05d35-… at thinkpad-x13-linux.tail3870a7.ts.net:8444, with tab_name=tmp4

echo "你现在在忙什么?" | python3 ~/.claude/skills/ask-peer-claude-code/ask_peer.py \
    --to <对方的 session/thread id> --from <你自己的 id> --from-agent codex
```

`--to` 可以只给前几位（在对方机器的会话列表里唯一匹配即可；0 个或多个匹配会报错，
不会误投）。`--host` 可省 —— 脚本会在已知机器间自动定位，并在返回的 JSON 里给出
`host`。`--from-agent codex` 让对方看到的前缀写 `from peer codex`（不给也会从你的
id 形状推断，但明写更可靠）。

几个常用形态：

```bash
# 不要傻等：先确认送达、立刻返回 (委派 / 通知)
… ask_peer.py --to <id> --from <我的id> --from-agent codex --no-wait

# 只看对方在干什么, 一个字都不发
python3 ~/.claude/skills/ask-peer-claude-code/ask_peer.py --to <id> --no-send

# 读对方最近几轮的对话
python3 ~/.claude/skills/ask-peer-claude-code/ask_peer.py --to <id> --history --rounds 6
```

返回的 JSON 里 `status` ∈ `done`（`reply` 就是答案）/ `sent`（只确认送达）/
`pending_confirm`（对方卡在一个需要人操作的菜单上 —— 交给人，别试图代答）/
`timeout` / `peek` / `history`。

## 一条纪律

发送前脚本会检查对方的输入框是否有人在打字，有就等（最多 2 分钟）再发。所以**不要**
自己去模拟按键或绕过脚本直接调 HTTP API：那样会把对方半打的字覆盖掉，也不会带上
上面那个前缀 —— 而没有前缀的消息，对方会当成人发的。
