# claude-code-web

Keep talking to your half-finished **Claude Code** task even when you're
not at the Mac. Pull out your phone on the subway, see what Claude
replied, type a follow-up, walk away again.

## The goal

You start a long Claude Code task in iTerm2, leave the office, want to
check in from your phone. The conversation, the context, the in-flight
TODO — all of it lives in that one terminal session on the office Mac.
We want to **drive that exact session remotely**, not lose it and start
over.

To make that work end-to-end you need three things:

1. **The Mac stays awake** even when its lid is closed, so the remote
   server keeps responding. (`macos_helpers/` solves this — see its
   README.)
2. **Network reach** from wherever you are back to that Mac. The Mac
   typically lives behind NAT/firewalls and has no public IP. **Tailscale**
   gives every device a stable `100.x.x.x` IP that's reachable from any
   network you've joined to your Tailnet.
3. **A bridge** between a browser and the live `claude` process inside
   that iTerm2 tab — discovering the tab, reading its transcript,
   forwarding what you type. **That's what this repo provides.**

## What this repo does

`cc_web.py` runs a small FastAPI server on the Mac. It uses iTerm2's
Python API to:

- Discover every iTerm2 tab where `claude` is the foreground process.
- Read each tab's transcript from `~/.claude/projects/<…>.jsonl`.
- Match the session you pick in the browser to the right tab via
  screen-content scoring + an LLM tie-breaker (claude-code doesn't
  expose pid → session\_id, so the matcher infers it).
- Forward what you type in the browser into the live tab via iTerm2's
  `send_text` API. Multi-line input goes as a bracketed paste so Claude
  sees one message.
- Spawn new tabs for `claude` / `claude --resume` from a cwd allowlist
  when you want to start work on the office Mac while away from it.

The browser side is one static page. iOS users can install it as a PWA
via Safari → Add to Home Screen.

## Install

```sh
git clone https://github.com/superzhangmch/claude-code-web.git
cd claude-code-web
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Enable iTerm2's Python API: Settings → General → Magic → *Enable Python
API*, then restart iTerm2.

## Configure

Copy the template and fill in your values:

```sh
mkdir -p ~/.claude
cp config.example/cc_web.conf ~/.claude/cc_web.conf
chmod 600 ~/.claude/cc_web.conf
$EDITOR ~/.claude/cc_web.conf
```

Three sections — `token` (auth, sent as `Authorization: Bearer …`),
LLM keys (`api_base` / `api_key` / `model`, OpenAI-compatible endpoint
like LiteLLM/Ollama), and one `cwd=` line per allowed working
directory for the *New session* button.

## Run

Bind to a specific IP — the Tailscale interface (`100.x.x.x`) is the
recommended setup so the server is only reachable over your VPN.
**Don't use `0.0.0.0`** unless you trust every network the Mac is on:
that binds to all interfaces, public Wi-Fi included.

```sh
# find your Tailscale IP
tailscale ip -4
# then bind to it
.venv/bin/uvicorn cc_web:app --host 100.x.x.x --port 8765
```

For LAN-only use, bind to your LAN IP (`192.168.x.x` / `10.x.x.x`)
instead. For local-only testing, `--host 127.0.0.1`.

Open `http://<that-ip>:8765/`, enter the token, and your active
sessions show up in the picker. Click `Attach` (or `Enter` if already
bound) to open one.

## Keep the Mac awake (lid closed)

`macos_helpers/` ships two launchd jobs that together let you close the
lid and walk away without the Mac sleeping or the screen staying
unlocked. See `macos_helpers/README.md` for what they do and how to
install them. Optional but strongly recommended for the use case.

## Other languages

- 中文: [README_zh.md](README_zh.md)
