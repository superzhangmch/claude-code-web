#!/bin/bash
# Sets pmset disablesleep=1 on AC power, 0 on battery.
# Run as root via LaunchDaemon (polls every 10s by default).
src=$(/usr/bin/pmset -g batt | /usr/bin/head -1)
if [[ "$src" == *"AC Power"* ]]; then
    /usr/bin/pmset -a disablesleep 1
else
    /usr/bin/pmset -a disablesleep 0
fi
