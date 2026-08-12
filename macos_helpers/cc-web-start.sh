#!/bin/bash
# Wrapper that the cc-web LaunchAgent runs. Picks up the Tailscale IP at
# launch time (so the plist itself doesn't need to be machine-specific) and
# execs uvicorn from this project's .venv.

set -e

# launchd's KeepAlive respawns this script on ANY exit, throttled only by
# ThrottleInterval (~10s). So every failure path below must sleep first: a
# hopeless respin otherwise floods the log — that is exactly how
# /tmp/cc-web.log reached 36 MB / ~45k "address already in use" lines after a
# manually-started uvicorn took port 8765 out from under this agent.
RETRY_SLEEP=${CC_WEB_RETRY_SLEEP:-60}
die() {
    echo "$(date '+%F %T') ERROR: $* — sleeping ${RETRY_SLEEP}s before launchd retries" >&2
    sleep "$RETRY_SLEEP"
    exit 1
}

# Find the project directory. Edit / extend this list per machine.
for d in \
    "$HOME/claude-code-web" \
    "$HOME/Desktop/my_code/claude-code-web" \
    "$HOME/code/claude-code-web"; do
    if [ -d "$d/.venv" ] && [ -f "$d/cc_web.py" ]; then
        PROJECT_DIR="$d"
        break
    fi
done
[ -n "$PROJECT_DIR" ] || die "project dir not found"

# One cc_web per machine. Two instances don't collide on ports but they do share
# every stateful file under ~/.claude (cc_web_bindings.json, cc_web_summaries.json)
# and silently overwrite each other. cc_web.py enforces this with an flock as
# well; checking here keeps the rejection cheap and the log readable.
#
# Deliberately NOT `pgrep -f "uvicorn cc_web:app"`: that matches any process
# whose command line merely mentions the string — an ssh command, a grep, this
# script's own error message — and such a false positive already left the service
# down once. argv is "<python> <...>/uvicorn cc_web:app ...", so require field 2
# to end in /uvicorn and field 3 to be exactly cc_web:app.
cc_web_pids() {
    ps -Ao pid=,args= | awk '$3 ~ /\/uvicorn$/ && $4 == "cc_web:app" { print $1 }'
}
RUNNING=$(cc_web_pids)
[ -z "$RUNNING" ] || die "another cc_web is already running (pid $(echo $RUNNING))"

# Find tailscale CLI.
for t in /usr/local/bin/tailscale /opt/homebrew/bin/tailscale "$(command -v tailscale)"; do
    [ -x "$t" ] && TAILSCALE="$t" && break
done
[ -n "$TAILSCALE" ] || die "tailscale CLI not found"

HOST=$("$TAILSCALE" ip -4 2>/dev/null | head -1)
[ -n "$HOST" ] || die "tailscale not connected (no v4 IP)"

cd "$PROJECT_DIR"
echo "$(date '+%F %T') starting cc_web from $PROJECT_DIR on $HOST:8765"
exec ./.venv/bin/uvicorn cc_web:app --host "$HOST" --port 8765 --log-level info
