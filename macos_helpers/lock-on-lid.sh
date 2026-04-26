#!/bin/bash
# Poll clamshell state. When lid goes from open → closed, put display to sleep.
# Combined with "Require password immediately after sleep or screensaver" in
# System Settings → Lock Screen, this gives you normal lid-close lock without
# needing the Mac to actually sleep.

prev="unknown"
while true; do
    cur=$(/usr/sbin/ioreg -r -k AppleClamshellState 2>/dev/null \
        | /usr/bin/awk '/AppleClamshellState/ {print $NF; exit}')
    if [[ "$cur" == "Yes" && "$prev" != "Yes" ]]; then
        /usr/bin/pmset displaysleepnow
    fi
    prev="$cur"
    sleep 10
done
