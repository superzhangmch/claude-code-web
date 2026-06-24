---
name: session-name
description: "Name, rename, or remember the current Claude Code session with a title for easy retrieval later. Also find/search past named sessions across all projects. Triggers when user says: 'remember this session as', 'name this session', 'rename this session', 'save this session as', 'find session about', 'search session', 'list sessions', 'show my sessions', 'which session was about'."
user_invocable: true
---

# Session Naming Skill

Allows the user to name sessions and find them later. All data is stored in a single global file: `~/.claude/session_index.json`.

The `cc_web` browser picker reads this same file to show titled sessions
in its top list. Without this skill, sessions still show up in the
picker but only by their first user message and timestamp — naming
them makes them findable by title.

## Storage format

`~/.claude/session_index.json` is a JSON array of objects:

```json
[
  {
    "session_id": "7f283905-1a06-4d86-8f2b-995b9a4f8133",
    "title": "user given title",
    "project_path": "/Users/you/projects/example",
    "first_user_msg": "the first thing the user said..."
  }
]
```

## How to find the current session ID

**Primary method (fast, no marker injection):** Claude Code writes a
`~/.claude/sessions/<pid>.json` file for every live session, mapping the
Claude process PID to `{sessionId, cwd, status, ...}`. The Bash tool runs
as a *descendant* of the Claude process, so we walk up the parent-PID chain
and the first ancestor PID that has a matching `sessions/<pid>.json` file
**is** the current session. We use file existence (not process-name matching)
as the signal, and cross-check `cwd == $PWD` for safety. This handles
multiple concurrent sessions correctly because each Bash tool only ever
walks up to its own Claude process.

Run this single command — it prints `session_id`, `first_user_msg`, and `cwd`:

```bash
python3.11 - <<'PYEOF'
import os, json, glob, subprocess

def ppid_of(pid):
    out = subprocess.run(["ps","-o","ppid=","-p",str(pid)],
                         capture_output=True, text=True).stdout.strip()
    return int(out) if out else None

# 1. Collect this process's ancestor PIDs (self -> shell -> claude -> ...)
pid, ancestors = os.getpid(), []
for _ in range(30):
    ancestors.append(pid)
    p = ppid_of(pid)
    if not p or p <= 1: break
    pid = p

# 2. The current session = the ancestor PID that owns a sessions/<pid>.json
sess_dir = os.path.expanduser("~/.claude/sessions")
cwd = os.getcwd()
sid = jsonl = None
candidates = []
for a in ancestors:
    f = os.path.join(sess_dir, f"{a}.json")
    if os.path.exists(f):
        try: candidates.append(json.load(open(f)))
        except Exception: pass
# prefer the candidate whose cwd matches $PWD; else first found
pick = next((c for c in candidates if c.get("cwd") == cwd), candidates[0] if candidates else None)
if pick:
    sid = pick.get("sessionId")

# 3. Read first_user_msg from that session's JSONL (path derived from cwd)
first_msg = ""
if sid:
    projdir = cwd.replace("/", "-").replace("_", "-")
    jsonl = os.path.expanduser(f"~/.claude/projects/{projdir}/{sid}.jsonl")
    if os.path.exists(jsonl):
        for line in open(jsonl):
            try:
                obj = json.loads(line)
                if obj.get("type") == "user" and obj.get("message",{}).get("role") == "user":
                    c = obj["message"]["content"]
                    if isinstance(c, str) and c.strip():
                        first_msg = c[:150]; break
            except Exception: pass

print(json.dumps({"session_id": sid, "first_user_msg": first_msg, "cwd": cwd}))
PYEOF
```

If `session_id` comes back `null` (e.g. the `sessions/` file is missing on
an older client), fall back to the marker method below.

### Fallback method (marker injection)

Plant a unique marker, then grep the project's JSONLs for it:

```bash
# Command 1 — echo a marker (gets written into the current session's JSONL)
MARKER="SESSION_MARKER_$(uuidgen)"; echo "$MARKER"
```
```bash
# Command 2 (separate call, so the marker is already persisted)
PROJECT_DIR=$(echo "$PWD" | sed 's|[/_]|-|g')
SESSION_FILE=$(grep -l "$MARKER" ~/.claude/projects/${PROJECT_DIR}/*.jsonl 2>/dev/null)
echo "$SESSION_FILE"   # basename (minus .jsonl) is the session_id
```

## Operations

### 1. Name / Rename / Remember a session

Triggered by: "remember this session as XXX", "name this session XXX", "rename this session to XXX", "save this session as XXX"

Steps:
1. If no name/title is provided by the user, ask for one.
2. Find the current session file and extract info using the method above.
3. Read `~/.claude/session_index.json` (create with `[]` if missing).
4. Check if an entry with the same `session_id` already exists:
   - If yes: update `title` (keep `first_user_msg` and `project_path`).
   - If no: append a new entry with all fields including `project_path` from `$PWD`.
5. Write the updated JSON back to `~/.claude/session_index.json`.
6. Confirm: "Session saved as **<title>**. Resume with: `claude --resume <session_id>`"

### 2. Find / Search sessions

Triggered by: "find session about XXX", "which session was about XXX", "search sessions for XXX"

Steps:
1. Read `~/.claude/session_index.json`.
2. Search all fields (title, first_user_msg, project_path) for the keyword (case-insensitive).
3. For activity timestamps, stat the corresponding JSONL file under
   `~/.claude/projects/` and use its mtime — the index intentionally
   does not store activity time so it never goes stale.
4. Display matching results:
   ```
   **<title>** (<jsonl mtime>)
   Project: <project_path>
   First msg: <first_user_msg>
   Resume: claude --resume <session_id>
   ```

### 3. List sessions

Triggered by: "list sessions", "show my sessions", "list named sessions"

Steps:
1. Read `~/.claude/session_index.json`.
2. For each entry, stat the corresponding JSONL under
   `~/.claude/projects/` for its mtime; sort entries by mtime
   descending and display in a readable table or list format.
