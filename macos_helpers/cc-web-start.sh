#!/bin/bash
# Wrapper that the cc-web LaunchAgent runs. Picks up the Tailscale IP at
# launch time (so the plist itself doesn't need to be machine-specific) and
# execs uvicorn from this project's .venv.

set -e

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
if [ -z "$PROJECT_DIR" ]; then
    echo "$(date '+%F %T') ERROR: project dir not found" >&2
    exit 1
fi

# Find tailscale CLI.
for t in /usr/local/bin/tailscale /opt/homebrew/bin/tailscale "$(command -v tailscale)"; do
    [ -x "$t" ] && TAILSCALE="$t" && break
done
if [ -z "$TAILSCALE" ]; then
    echo "$(date '+%F %T') ERROR: tailscale CLI not found" >&2
    exit 1
fi

HOST=$("$TAILSCALE" ip -4 2>/dev/null | head -1)
if [ -z "$HOST" ]; then
    echo "$(date '+%F %T') ERROR: tailscale not connected (no v4 IP)" >&2
    exit 1
fi

cd "$PROJECT_DIR"
echo "$(date '+%F %T') starting cc_web from $PROJECT_DIR on $HOST:8765"
exec ./.venv/bin/uvicorn cc_web:app --host "$HOST" --port 8765 --log-level info
