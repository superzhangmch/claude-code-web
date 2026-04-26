# claude-code-web

View and reply to your local **Claude Code** sessions (running in iTerm2)
from a remote browser, including your phone.

## What it does

A small FastAPI server runs on the Mac where `claude` runs. It uses
iTerm2's Python API to discover tabs running `claude`, reads their JSONL
transcripts, matches them to your picked session via screen-content +
LLM, and forwards what you type in the browser into the live tab.

You can also open a new `claude` / `claude --resume` tab from a
predefined cwd allowlist — useful when you're away from the Mac.

The browser side is a single static page. iOS-friendly: install as a
PWA via Safari → Add to Home Screen.

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

## Optional

`macos_helpers/` has two launchd plists for keeping the Mac awake on AC
and locking the screen on lid-close (so the server stays reachable with
the lid down). Edit the paths inside before installing — neither is
required.
