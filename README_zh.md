# claude-code-web

让你**离开 Mac 也能继续操作 iTerm2 里跑了一半的 Claude Code 任务**。
地铁上掏出手机看一眼 Claude 回复了啥，回个消息，再走开。

## 想做什么

你在公司 iTerm2 启动了一个 Claude Code 长任务，下班想路上手机继续。
对话上下文、TODO、运行中的状态，全都在公司那台 Mac 的那个 terminal
session 里。我们要**远程驱动同一个 session**，而不是新开一个从头来。

要把这事做通，需要三样东西配合:

1. **Mac 不睡** — 哪怕合盖也得保持 server 运行。`macos_helpers/`
   里两个 launchd 脚本解决这个 (见 `macos_helpers/README.md`)。
2. **网络穿透** — Mac 一般在 NAT/防火墙后面，没公网 IP。
   **[Tailscale](https://tailscale.com/)** 给每台设备分配稳定的
   `100.x.x.x` IP，加进 Tailnet 后从任何网络都能直连。
3. **桥接进程** — 浏览器和 iTerm2 里那个活的 `claude` 进程之间的
   桥。**就是本仓库**。

## 这个仓库做啥

`cc_web.py` 是 Mac 上跑的一个小 FastAPI server。用 iTerm2 Python API:

- 找到所有以 `claude` 为前台进程的 iTerm2 tab。
- 读每个 tab 的 JSONL transcript (`~/.claude/projects/<…>.jsonl`)。
- 当你在浏览器选了一个 session，用屏幕内容评分 + LLM 仲裁来匹配
  到正确的 tab (claude-code 不直接暴露 pid → session\_id，只能推断)。
- 你在浏览器里打字 → 通过 iTerm2 `send_text` 直接喂进 live tab。
  多行消息用 bracketed paste 发，Claude 看到的是一条消息而不是多条。
- 从预设的 cwd 白名单里开新 tab 跑 `claude` / `claude --resume` ——
  人不在 Mac 前也能新启任务。

浏览器端是一个 static page。iOS 可以 Safari → 添加到主屏幕，当 PWA 用。

## 安装

```sh
git clone https://github.com/superzhangmch/claude-code-web.git
cd claude-code-web
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

启用 iTerm2 的 Python API: Settings → General → Magic → *Enable
Python API*，然后重启 iTerm2。

## 配置

复制模板，填上你的真值:

```sh
mkdir -p ~/.claude
cp config.example/cc_web.conf ~/.claude/cc_web.conf
chmod 600 ~/.claude/cc_web.conf
$EDITOR ~/.claude/cc_web.conf
```

三段配置:
- `token` — 鉴权 token，浏览器请求时放在 `Authorization: Bearer …`。
- LLM 三件套 — `api_base` / `api_key` / `model`，要求是 OpenAI 兼容的
  chat-completions 端点 (LiteLLM / Ollama / vLLM 等都可以)。
- `cwd=` 行(可多行) — *New session* 按钮允许在哪几个目录下启 `claude`。

## 运行

**绑特定 IP**，推荐 Tailscale 接口 (`100.x.x.x`) — 这样 server 只能
通过你的 VPN 访问。**不要用 `0.0.0.0`**(除非你信任 Mac 所在的每个
Wi-Fi)，那会绑所有网卡，公共 Wi-Fi 也包括。

```sh
# 看你的 Tailscale IP
tailscale ip -4
# 绑到它
.venv/bin/uvicorn cc_web:app --host 100.x.x.x --port 8765
```

只想内网用就绑 LAN IP (`192.168.x.x` / `10.x.x.x`)；只想本地测就
`--host 127.0.0.1`。

浏览器访问 `http://<那个 IP>:8765/`，输入 token，picker 里就能看到
当前活跃的 sessions。点 `Attach` (已绑过则显示 `Enter`) 进入。

## 让 Mac 合盖不睡

`macos_helpers/` 里两个 launchd job 配合解决"合盖 → Mac 不睡 + 屏幕
锁了"。详见 `macos_helpers/README.md`。本仓库不依赖它，但实际场景里
强烈推荐。

## 其它语言

- English: [README.md](README.md)
