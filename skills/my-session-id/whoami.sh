#!/bin/bash
# Find THIS claude-code session's own pid + session id — authoritatively.
#
# How: walk UP the process tree from this script's shell until we hit the claude
# process that owns a  ~/.claude/sessions/<pid>.json  store file. That file is
# claude-code's own (undocumented) pid↔sessionId record — the same one cc-web
# uses as its PRIMARY binding resolver — so it's ground truth, not a guess.
#
# Usage:
#   whoami.sh            # human-readable: session_id / pid / cwd / status
#   whoami.sh --id-only  # print just the session id (for scripting, e.g.
#                        #   ask_peer.py --from "$(whoami.sh --id-only)")
id_only=0; [ "$1" = "--id-only" ] && id_only=1
pid=$$
for _ in $(seq 1 12); do
  f="$HOME/.claude/sessions/$pid.json"
  if [ -f "$f" ]; then
    ID_ONLY=$id_only /usr/bin/python3 - "$f" <<'PY'
import json, os, sys
d = json.load(open(sys.argv[1]))
if os.environ.get("ID_ONLY") == "1":
    print(d.get("sessionId", ""))
else:
    print("session_id :", d.get("sessionId", ""))
    print("pid        :", d.get("pid", ""))
    print("cwd        :", d.get("cwd", ""))
    print("status     :", d.get("status", ""))
    print("store      :", sys.argv[1])
PY
    exit 0
  fi
  ppid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
  [ -z "$ppid" ] || [ "$ppid" = 0 ] || [ "$ppid" = 1 ] && break
  pid=$ppid
done
echo "ERROR: no ~/.claude/sessions/<pid>.json found up the process tree" >&2
echo "  (old claude without the store? or not launched as a normal claude session)" >&2
exit 1
