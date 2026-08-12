#!/bin/bash
# Wrapper for the cc-web-https user service. HTTPS-only on this box: the browser
# only grants microphone access (getUserMedia) in a secure context, so voice input
# needs TLS. The cert is a real Let's Encrypt one issued through Tailscale, so
# there is no warning interstitial — `tailscale set --operator=$USER` was done
# once, which is why the refresh below works without root.
set -e

# systemd restarts this on ANY exit (Restart=always). Every failure path must
# sleep first, or a hopeless respin floods the journal — the macOS side of this
# project did exactly that once: 44k failed binds / 36MB of log over 5 days
# because a stray process held the port.
RETRY_SLEEP=${CC_WEB_RETRY_SLEEP:-60}
die() {
    echo "$(date '+%F %T') ERROR: $* — sleeping ${RETRY_SLEEP}s before systemd retries" >&2
    sleep "$RETRY_SLEEP"
    exit 1
}

cd "$HOME/claude-code-web" || die "project dir missing"

# One cc_web per machine. Two instances don't clash on ports but they do share
# every stateful file under ~/.claude (cc_web_bindings.json, cc_web_summaries.json)
# and silently overwrite each other; cc_web.py enforces this with an flock too.
# Deliberately NOT `pgrep -f "uvicorn cc_web:app"` — that also matches any shell,
# ssh command or grep whose command line merely mentions the string. argv is
# "<python> <...>/uvicorn cc_web:app ...", so require field 3 to end in /uvicorn
# and field 4 to be exactly cc_web:app.
cc_web_pids() {
    ps -Ao pid=,args= | awk '$3 ~ /\/uvicorn$/ && $4 == "cc_web:app" { print $1 }'
}
RUNNING=$(cc_web_pids)
[ -z "$RUNNING" ] || die "another cc_web is already running (pid $(echo $RUNNING))"

for t in "$(command -v tailscale)" /usr/bin/tailscale /usr/local/bin/tailscale; do
  [ -x "$t" ] && TS="$t" && break
done
[ -n "$TS" ] || die "tailscale CLI not found"
HOST=$("$TS" ip -4 2>/dev/null | head -1)
[ -n "$HOST" ] || die "tailscale not connected (no v4 IP)"

CERT_NAME=YOUR-HOST.YOUR-TAILNET.ts.net
D="$HOME/cc_https"; mkdir -p "$D"
# Refresh on every (re)start; tailscale only issues a new cert near expiry, so
# this is cheap and means the ~90-day renewal needs no attention. On failure keep
# the existing files so a network blip doesn't take the service down.
"$TS" cert --cert-file "$D/tls.crt" --key-file "$D/tls.key" "$CERT_NAME" >/dev/null 2>&1 \
  || echo "$(date '+%F %T') cert refresh skipped (kept existing)"
[ -s "$D/tls.crt" ] && [ -s "$D/tls.key" ] || die "no usable cert at $D"

echo "$(date '+%F %T') starting cc_web (https) on $HOST:8443 as $CERT_NAME"
exec ./.venv/bin/uvicorn cc_web:app --host "$HOST" --port 8443 \
  --ssl-certfile "$D/tls.crt" --ssl-keyfile "$D/tls.key" --log-level info
