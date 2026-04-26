# macos_helpers

Optional launchd glue for running `cc_web` on a Mac you walk away from
(close the lid, leave home, browse from your phone).

These are **not required** for claude-code-web to work — they just
solve two macOS-specific annoyances when you want a headless Mac.

## The problem

By default macOS sleeps when the lid closes. If your Mac sleeps, the
web server is unreachable.

The obvious fix — `pmset disablesleep 1` — keeps the Mac awake but
*also* removes lid-close as a screen-lock trigger. Now the lid is
closed, the Mac is awake, and the screen is unlocked. Bad.

## The fix (two LaunchD jobs working together)

### 1. `disablesleep-by-power.sh` + `com.zmc.disablesleep-ac.plist`
**LaunchDaemon (system, root).** Polls AC/battery state every 60s.
- On AC: `pmset -a disablesleep 1` — Mac stays awake even with the lid
  closed, so the web server keeps serving.
- On battery: `pmset -a disablesleep 0` — back to default sleep
  behavior so an unplugged Mac in a bag doesn't cook itself.

### 2. `lock-on-lid.sh` + `com.zmc.lock-on-lid.plist`
**LaunchAgent (per-user).** Polls `AppleClamshellState` every 2s. When
the lid transitions open → closed, calls `pmset displaysleepnow` —
puts the *display* to sleep without sleeping the *Mac*. Combined with
"Require password immediately after sleep or screensaver" in System
Settings → Lock Screen, this gives you lid-close screen lock back, on
top of disablesleep.

## Install

```sh
# 1. Edit paths in both plists. Replace YOUR_USERNAME / install path.
$EDITOR com.zmc.lock-on-lid.plist        # path: /Users/YOUR_USERNAME/bin/lock-on-lid.sh
$EDITOR com.zmc.disablesleep-ac.plist    # path: /usr/local/bin/disablesleep-by-power.sh

# 2. Place the scripts where the plists expect them.
mkdir -p ~/bin
cp lock-on-lid.sh ~/bin/lock-on-lid.sh && chmod +x ~/bin/lock-on-lid.sh
sudo cp disablesleep-by-power.sh /usr/local/bin/disablesleep-by-power.sh
sudo chmod +x /usr/local/bin/disablesleep-by-power.sh

# 3. Install the LaunchDaemon (system-wide, runs as root).
sudo cp com.zmc.disablesleep-ac.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/com.zmc.disablesleep-ac.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.zmc.disablesleep-ac.plist

# 4. Install the LaunchAgent (per-user).
cp com.zmc.lock-on-lid.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.zmc.lock-on-lid.plist

# 5. Enable lock-on-sleep in System Settings → Lock Screen
#    "Require password" → "immediately"
```

## Verify

```sh
# Daemon (root) — check it's loaded and disablesleep follows AC state
sudo launchctl list | grep com.zmc.disablesleep-ac
pmset -g | grep disablesleep      # 1 on AC, 0 on battery

# Agent (user) — check it's running
launchctl list | grep com.zmc.lock-on-lid
tail -f /tmp/lock-on-lid.log      # watch lid-close events
```

## Uninstall

```sh
sudo launchctl bootout system /Library/LaunchDaemons/com.zmc.disablesleep-ac.plist
sudo rm /Library/LaunchDaemons/com.zmc.disablesleep-ac.plist
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.zmc.lock-on-lid.plist
rm ~/Library/LaunchAgents/com.zmc.lock-on-lid.plist
sudo pmset -a disablesleep 0      # restore default
```
