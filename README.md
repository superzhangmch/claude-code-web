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

### Dependencies — what each one is for

| Package | Why it's needed |
|---|---|
| `fastapi` | The web framework the whole server (routes, auth, JSON API) is built on. |
| `uvicorn[standard]` | The ASGI server that actually runs the app. The `[standard]` extra pulls in the WebSocket/HTTP speedups **and the TLS support that `--ssl-certfile/--ssl-keyfile` need** — i.e. it's what lets you serve HTTPS (see below). |
| `iterm2` | iTerm2's Python API — discovers the tabs where `claude` is running, reads each tab's on-screen text, and sends your typed input back into the live tab. **macOS only.** |
| `websockets` | Transport the iTerm2 Python API talks over (and used by uvicorn). |
| `pyobjc-framework-Quartz` | Quartz `CGEvent` HID injection + display capture behind the `/remote/` phone-remote — the clicks, typing, scrolling and screenshots. **macOS only.** |
| `pypinyin` | Voice-input polishing: turns the dictation draft into pinyin so the LLM can recover near-sound mis-recognitions (e.g. 「拉铁克」→ LaTeX) from context. |

**On Linux**, install `requirements-linux.txt` instead: it drops the
macOS-only `iterm2` + `pyobjc` packages (the **tmux bridge** replaces the
iTerm2 API there) and adds `python-multipart`. You must also have `tmux`
installed and run your `claude` sessions **inside tmux** — the server
auto-selects the tmux bridge on non-macOS. Skip the iTerm2 Python-API step.

## Configure

Copy the template and fill in your values:

```sh
mkdir -p ~/.claude
cp config.example/cc_web.conf ~/.claude/cc_web.conf
chmod 600 ~/.claude/cc_web.conf
$EDITOR ~/.claude/cc_web.conf
```

Then check the whole setup in one shot — it verifies everything the
[troubleshooting table](#troubleshooting--symptom--cause) covers and prints the
exact fix for anything missing:

```sh
.venv/bin/python doctor.py
```

What's in there:

- **`token`** — auth, sent as `Authorization: Bearer …`. Not optional in
  practice: with no `token=` the server invents a random one at startup, and
  then *no* token you type can log in (see the troubleshooting table).
- **LLM keys** (`api_base` / `api_key` / `model`) — any OpenAI-compatible
  endpoint (LiteLLM, Ollama, …). Used for the attach tie-breaker, to polish
  voice dictation, and for the ✎ English correction. Unset = those features
  report "no LLM configured" rather than pretending to work.
- **`cwd=`** — one line per directory the *New session* button may spawn
  `claude` in.
- **`asr=`** — optional, one line per batch speech-to-text backend; enables the
  🎤 button.
- **`soniox=` / `openai_realtime=`** — optional streaming (realtime) voice, where
  words appear while you talk. With none of the three voice keys set, the ⚙ menu
  says "not configured yet" instead of hiding the feature silently.

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

## Serve over HTTPS (required for 🎤 voice input)

Plain HTTP is fine for reading and typing. But **browsers only allow
microphone capture (`getUserMedia`) in a "secure context"** — `https://`
or `localhost`. Over `http://100.x.x.x:8765` the voice-input button
cannot record at all (a few other niceties — PWA install, clipboard —
also prefer a secure context). So if you want voice input, serve HTTPS.

A self-signed cert means a browser warning on every visit. Tailscale
avoids that entirely: it issues a **real, browser-trusted (Let's Encrypt)
certificate** for your machine's `*.ts.net` name, so there's zero warning
and nothing to click through.

One-time: in the Tailscale admin console enable **MagicDNS** and **HTTPS
Certificates** for your tailnet. Then on the Mac:

```sh
# 1. your machine's tailnet DNS name (…​.ts.net)
tailscale status                       # shows <host>.<tailnet>.ts.net
# 2. fetch a trusted cert for that name (writes <name>.crt / <name>.key)
tailscale cert <host>.<tailnet>.ts.net
# 3. run uvicorn with TLS, bound to the Tailscale IP
.venv/bin/uvicorn cc_web:app \
  --host "$(tailscale ip -4)" --port 8443 \
  --ssl-certfile <host>.<tailnet>.ts.net.crt \
  --ssl-keyfile  <host>.<tailnet>.ts.net.key
```

Open **`https://<host>.<tailnet>.ts.net:8443/`** — use the DNS *name*,
not the `100.x` IP, otherwise the cert won't match and the warning comes
back.

Notes:
- **`tailscale cert` needs root** (on Linux it fails with `Access denied: cert
  access denied`). Grant it once and never again:
  ```sh
  sudo tailscale set --operator=$USER
  ```
  Watch out: `sudo` needs a real terminal — it cannot prompt for a password from
  a non-interactive shell, so run this in an actual tty (or a tmux pane).
  Without a cert there is no HTTPS, and without HTTPS the mic cannot record —
  a three-steps-removed cause for "voice input is broken".
- **Don't front it with `tailscale serve`.** Its HTTP/2 proxy answers
  `curl` fine but browsers reject it (`ERR_CONNECTION_CLOSED`). Terminate
  TLS in uvicorn directly, as above.
- `tailscale cert` certificates expire (~90 days). Re-run `tailscale cert
  <name>` to renew — easiest is to put that line in your start script so
  every (re)start refreshes the cert before launching uvicorn.

## Name your sessions (recommended)

Install the bundled `session-name` Claude Code skill so you can title
sessions ("remember this session as …") and have those titles show up
at the top of the web picker. Without it, sessions appear as
`(unnamed)` and you only have first-user-message + timestamp to
recognize them by.

```sh
cp -R skills/session-name ~/.claude/skills/
```

See `skills/README.md` for details.

## Keep the Mac awake (lid closed)

`macos_helpers/` ships two launchd jobs that together let you close the
lid and walk away without the Mac sleeping or the screen staying
unlocked. See `macos_helpers/README.md` for what they do and how to
install them. Optional but strongly recommended for the use case.

## Tap a few buttons on the Mac when needed

`/remote/` (`remote_mac_ctrl.py`) is a crude VNC: no streaming,
fetch-on-demand screenshots, 10-key on-screen keyboard. The use case is
narrow — sometimes you want to know *what's happening on the Mac right
now*, sometimes you need to *click a few buttons remotely*, not enough to
fire up real VNC and too much hassle to open a laptop.

### macOS TCC permissions

macOS gates each piece of `/remote/` behind a different Privacy &
Security toggle — they don't share. Without them you get silent failures
("could not create image from display", clicks that don't land, typed
chars eaten as shortcuts), and no prompt may appear because the process
is launchd-spawned.

| Permission | Used for | Why we need it |
|---|---|---|
| **Screen & System Audio Recording** | `/remote/api/screenshot`, `/remote/api/cursor_strip` (and any `screencapture` invocation) | macOS blocks pixel readback by default. Without this you can't see the Mac at all. |
| **Accessibility** | `/remote/api/key` (modifier shortcuts), `/remote/api/unlock` lock-first step (Ctrl+Cmd+Q via AppleScript System Events) | System Events keystroke needs this. Lock-first unlock silently no-ops without it. |
| **Input Monitoring** | `/remote/api/type`, `/remote/api/click`, `/remote/api/scroll` (Quartz `CGEventPost` at HID level) | HID-level injection through the lock screen (so unlock works) requires this on modern macOS. |

How to grant (one-time per machine):

1. Open **System Settings → Privacy & Security**.
2. For each of the three sections above, click `+` and add the
   Python interpreter that runs the server. Typically:
   `/opt/homebrew/Cellar/python@3.11/<ver>/Frameworks/Python.framework/Versions/3.11/Resources/Python.app`
3. Toggle it on (you'll be asked to quit + reopen the server — `launchctl kickstart -k gui/$(id -u)/com.zmc.cc-web`).
4. If `screencapture` still errors with *"could not create image from display"*,
   make sure the display is awake — capture fails on sleeping displays
   regardless of TCC.

Resetting (if a prompt was dismissed / denied): `tccutil reset
ScreenCapture` (and the equivalent `Accessibility` / `ListenEvent`), then
trigger the feature again to get a fresh prompt.

### Running it as a launchd auto-start — hard-won gotchas

Two machines, same code: mac-pro "just worked", mac-air broke in three
ways. All three are environment, not code:

1. **Deploy dir must NOT be under `~/Desktop` (or Documents/Downloads).**
   A launchd-spawned process can't read those TCC-protected folders, so
   it dies at startup with
   `PermissionError: Operation not permitted: …/.venv/pyvenv.cfg`.
   Keep the dev checkout in `~/Desktop/my_code/claude-code-web` but
   **deploy a copy to `~/claude-code-web`** (rsync dev → deploy, separate
   `.venv`). `~/bin/cc-web-start.sh` already searches `~/claude-code-web`
   first.

2. **Grant the deploy `python` ALL THREE permissions, then it works
   without a reboot.** They're independent; missing one fails silently:
   - screenshot → HTTP 500 "could not create image" = missing **Screen
     & System Audio Recording**
   - click/scroll/type → HTTP **200 but nothing happens** = missing
     **Accessibility** (Quartz `CGEventPost` needs it; Input Monitoring
     alone is not enough)
   - `--resume`/unlock keystrokes eaten = missing **Accessibility** /
     **Input Monitoring**
   Add the SAME `…/Python.app` to all three lists. `launchctl kickstart
   -k gui/$(id -u)/com.zmc.cc-web` picks the grants up immediately — no
   reboot needed (only add+reboot if a grant refuses to take).

3. **Never restart it with `nohup` from a random shell.** A detached
   nohup process reparents to launchd with no "responsible app", so it
   loses BOTH the iTerm2 API cookie (→ attach gets 401 / "cannot reach
   iTerm2") AND Screen Recording (→ screenshot 500). Restart via
   `launchctl kickstart -k …` (the agent), or — if the agent isn't set
   up yet — from inside an iTerm tab (which lends its own TCC grants).

### Auto-start on Linux (systemd user unit)

There is no launchd on Linux; use a **user** unit (not a system one — it has to
run as you, with your `~/.claude` and your tmux):

```ini
# ~/.config/systemd/user/cc-web-https.service    — see linux_helpers/
[Service]
ExecStart=%h/claude-code-web/cc-web-https-start.sh
Restart=always
RestartSec=15
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now cc-web-https.service
journalctl --user -u cc-web-https -f          # logs
loginctl enable-linger $USER                  # keep running with no login session
```

**Every failure path in a start script must sleep before exiting.**
`Restart=always` (and launchd's `KeepAlive`) restart instantly, so a hopeless
respin — port already taken, tailscale down, no cert — spins forever: one such
loop ran 5 days at ~10s per attempt and wrote a 36 MB log. The scripts under
`macos_helpers/` and `linux_helpers/` sleep 60s on every failure and pre-check
for an already-running instance.

## Only one instance per machine

`cc_web.py` takes an exclusive `flock` on `~/.claude/cc_web.lock` at startup, so a
second instance exits immediately:

```
ERROR ccweb: another cc_web is already running (pid=… argv=…) — refusing to start
ERROR:    Application startup failed. Exiting.        # exit code 3
```

That is deliberate. Two instances (say an HTTP one on 8765 beside an HTTPS one on
8443) don't clash over ports, but they share everything stateful under
`~/.claude`: both run the session-summary worker (double the LLM spend — and each
save rewrites the whole map from its own startup snapshot, so they delete each
other's summaries), both persist their own binding table (one's reaper wipes the
other's bindings), both auto-continue the same API-erroring session, and both
write the same fixed `*.tmp` paths. `CC_WEB_ALLOW_MULTI=1` opts out if you really
want two.

If a restart complains, the old process simply hasn't exited yet: restart through
the supervisor (`launchctl kickstart -k …` / `systemctl --user restart …`), which
stops the old pid and waits for it before starting the new one. Don't `pkill` —
the supervisor races you and respawns mid-start.

## Extras: the grammar hook

`hooks/grammar/` is a **separate**, optional Claude Code `UserPromptSubmit` hook
(macOS only — the bar is a small Swift app): it polishes the English of the prompt
you just typed and shows a native-sounding rewrite in a floating bar, while your
prompt is still submitted exactly as typed. Unrelated to the web UI's own ✎ button
(that one calls `/api/grammar`). See `hooks/grammar/README.md`.

## Troubleshooting — symptom → cause

Run **`.venv/bin/python doctor.py`** first: it checks every row below and prints
the fix. The theme is that the symptom shows up far from the cause.

| Symptom | Cause |
|---|---|
| Typed the token, bounced straight back to the login page | No `token=` in `~/.claude/cc_web.conf`, so the server invented a random one at startup — nothing you type can match, and it changes on every restart. The login page now says so and disables the form. |
| Session picker is empty | macOS: iTerm2's Python API is off, or that first "Allow this script to control iTerm?" dialog was never accepted. Linux: `claude` is not running **inside tmux** — the bridge only sees tmux panes. |
| No 🎤 button; ⚙ says "not configured yet" | No `asr=` / `soniox=` / `openai_realtime=` in the conf. |
| 🎤 is there but recording never starts | Not a secure context. `getUserMedia` needs `https://` (or `localhost`) — see the HTTPS section. |
| "not polished — no LLM configured" after dictating | No `api_base` / `model`; `/api/polish` returns 503. It used to swallow that and hand back the raw dictation, which looked like a successful no-op. |
| ✎ says "no LLM configured" | Same missing `api_base` / `model`. It used to answer "looks natural — no changes" for unconfigured *and* failed calls — a pass mark on text nobody checked. |
| Messages render as raw `**markdown**` | `static/vendor/marked.min.js` didn't load. It's vendored deliberately (no CDN), so this means an incomplete deploy; a banner now says so. |
| ∑ turns into `∑!` | KaTeX couldn't be fetched — it is still CDN-loaded (its webfonts are ~1MB). Offline/blocked network; tap to retry. |
| Startup fails with exit code 3 | Another cc_web holds the lock — see above. |
| Browser cert warning | You opened the `100.x` IP instead of the `*.ts.net` name the cert was issued for. |
| `tailscale cert` → "Access denied" | It needs root: run `sudo tailscale set --operator=$USER` **once**. Note `sudo` needs a real terminal — it cannot prompt from a non-interactive shell. |
| launchd-started server: screenshot 500s, clicks do nothing, attach 401 | TCC permissions, or a `nohup`-detached process. See the launchd gotchas above. |
