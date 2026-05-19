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

## 给 session 起名(强烈推荐)

把仓库里的 `session-name` Claude Code skill 装到 `~/.claude/skills/`，
之后在任何 claude session 里说"remember this session as XXX"就能给
当前 session 起个标题。`cc_web` picker 会优先显示有标题的 session，
没装这个 skill 的话 picker 里全是 `(unnamed) <第一条消息>`，只能靠
时间排序找。

```sh
cp -R skills/session-name ~/.claude/skills/
```

详见 `skills/README.md`。

## 让 Mac 合盖不睡

`macos_helpers/` 里两个 launchd job 配合解决"合盖 → Mac 不睡 + 屏幕
锁了"。详见 `macos_helpers/README.md`。本仓库不依赖它，但实际场景里
强烈推荐。

## 必要时操作几下电脑

`/remote/` (`remote_mac_ctrl.py`) 是个挫版 VNC: 不流式、截图按需拉、键盘
就 10 个键。场景很窄: **有时候想知道 mac 上发生了啥**, **有时候需要远程点几个
按钮** — 不至于打开 VNC, 又懒得开笔电。

### macOS TCC 权限

`/remote/` (`remote_mac_ctrl.py`) 让手机看屏 + 点击 + 打字。macOS 把这些功能
分到**三个独立的隐私授权**里, 互不通用。少一个 → 静默失败 (报"could not
create image from display"、点击没反应、打字被当 shortcut 吃掉), 而且 launchd
起的进程不一定会**弹授权框**, 得手动去 System Settings 添。

| 权限 (中文菜单名) | 用在哪 | 为啥要 |
|---|---|---|
| **录屏与系统录音** (Screen & System Audio Recording) | `/remote/api/screenshot`, `/remote/api/cursor_strip` (所有 `screencapture` 调用) | macOS 默认禁止读屏像素。不给 → 截不出图, 手机看不到 mac。 |
| **辅助功能** (Accessibility) | `/remote/api/key` (modifier shortcut), `/remote/api/unlock` 的 lock-first 那步 (Ctrl+Cmd+Q via AppleScript System Events) | System Events 模拟按键吃这个。不给 → lock-first 无效。 |
| **输入监控** (Input Monitoring) | `/remote/api/type`, `/remote/api/click`, `/remote/api/scroll` (Quartz `CGEventPost` HID 层) | HID 层注入 (能穿透锁屏, 让 unlock 工作) 在现代 macOS 上要这个。 |

**怎么给** (新机器只搞一次):

1. **System Settings → 隐私与安全性**
2. 上表三个分类挨个进去, 点 `+`, 把跑 server 的 Python 解释器加进去, 通常是:
   `/opt/homebrew/Cellar/python@3.11/<版本>/Frameworks/Python.framework/Versions/3.11/Resources/Python.app`
3. 拨到 on (会问你重启服务: `launchctl kickstart -k gui/$(id -u)/com.zmc.cc-web`)
4. 如果还是报 `could not create image from display`, 看屏幕是不是**睡着了** —
   screencapture 在熄屏状态本来就抓不了, 跟权限无关

**重置授权** (之前误点了 Don't Allow 导致再也不弹): 在 mac terminal 跑
`tccutil reset ScreenCapture` (Accessibility / ListenEvent 类似),
然后再触发一次功能, 会重新弹授权框。

## 其它语言

- English: [README.md](README.md)
