#!/bin/bash
set -e

# launchd's KeepAlive respawns this script on ANY exit, throttled only by
# ThrottleInterval. Every failure path must sleep first, or a hopeless respin
# floods the log (the HTTP agent did exactly that: 36 MB / ~45k failed binds).
RETRY_SLEEP=${CC_WEB_RETRY_SLEEP:-60}
die() {
    echo "$(date '+%F %T') ERROR: $* — sleeping ${RETRY_SLEEP}s before launchd retries" >&2
    sleep "$RETRY_SLEEP"
    exit 1
}

for d in "$HOME/claude-code-web" "$HOME/Desktop/my_code/claude-code-web" "$HOME/code/claude-code-web"; do
  if [ -d "$d/.venv" ] && [ -f "$d/cc_web.py" ]; then PROJECT_DIR="$d"; break; fi
done
[ -n "$PROJECT_DIR" ] || die "no project dir"

# One cc_web per machine — two of them share (and clobber) all state under
# ~/.claude. Checked before the cert refresh below so a rejected start stays
# cheap and doesn't hammer `tailscale cert`. cc_web.py holds an flock too.
#
# Deliberately NOT `pgrep -f "uvicorn cc_web:app"`: that matches any process
# whose command line merely mentions the string — an ssh command, a grep, this
# very script's error message — and one such observer already caused a false
# "already running" that left the service down. Match the real thing instead:
# argv is "<python> <...>/uvicorn cc_web:app ...", so require field 2 to end in
# /uvicorn and field 3 to be exactly cc_web:app.
cc_web_pids() {
    ps -Ao pid=,args= | awk '$3 ~ /\/uvicorn$/ && $4 == "cc_web:app" { print $1 }'
}
RUNNING=$(cc_web_pids)
[ -z "$RUNNING" ] || die "another cc_web is already running (pid $(echo $RUNNING))"

for t in /usr/local/bin/tailscale /opt/homebrew/bin/tailscale /Applications/Tailscale.app/Contents/MacOS/Tailscale "$(command -v tailscale)"; do [ -x "$t" ] && TS="$t" && break; done
[ -n "$TS" ] || die "tailscale CLI not found"
IP=$("$TS" ip -4 2>/dev/null | head -1)
[ -n "$IP" ] || die "tailscale not connected (no v4 IP)"
D="$HOME/cc_https"; mkdir -p "$D"
# refresh cert on every (re)start; tailscale only issues a new one near expiry.
# on failure keep the existing file so startup still works.
"$TS" cert --cert-file "$D/tls.crt" --key-file "$D/tls.key" YOUR-HOST.YOUR-TAILNET.ts.net >/dev/null 2>&1 || echo "$(date) cert refresh skipped (kept existing)"
cd "$PROJECT_DIR"
echo "$(date "+%F %T") cc-web-https DIRECT $IP:8443 (trusted cert) from $PROJECT_DIR"
exec ./.venv/bin/uvicorn cc_web:app --host "$IP" --port 8443 \
  --ssl-certfile "$D/tls.crt" --ssl-keyfile "$D/tls.key" --log-level warning
