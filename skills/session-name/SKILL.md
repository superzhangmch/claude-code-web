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
    "last_visit": "2026-04-15 10:58",
    "last_user_msg": "the last thing the user said...",
    "first_user_msg": "the first thing the user said..."
  }
]
```

## How to find the current session ID

**Important**: Multiple Claude sessions may be running concurrently in the same project. The approach below handles this.

### Step 1: Find the project directory

The project directory in `~/.claude/projects/` is derived from `$PWD` by replacing `/` and `_` with `-`:

```bash
PROJECT_DIR=$(echo "$PWD" | sed 's|[/_]|-|g')
```

### Step 2: Identify the current session file

Plant a **unique marker** into the session, then grep for it. This is 100% reliable even with multiple concurrent sessions:

```bash
# Generate and echo a unique marker (this gets written into the current session's JSONL)
MARKER="SESSION_MARKER_$(uuidgen)"
echo "$MARKER"

# Then grep for it — only the current session will contain it
SESSION_FILE=$(grep -l "$MARKER" ~/.claude/projects/${PROJECT_DIR}/*.jsonl 2>/dev/null)
```

Run these as **two separate bash commands** (the marker must be written to the JSONL before grepping).

### Step 3: Extract session info

```bash
python3.11 -c "
import json, os, sys
session_file = sys.argv[1]
first_msg = last_msg = None
first_ts = last_ts = None
sid = None
with open(session_file) as f:
    for line in f:
        try:
            obj = json.loads(line.strip())
            if obj.get('type') == 'permission-mode' and obj.get('sessionId'):
                sid = obj['sessionId']
            if obj.get('type') == 'user' and obj.get('message', {}).get('role') == 'user':
                content = obj['message']['content']
                if isinstance(content, str) and content.strip():
                    if first_msg is None:
                        first_msg = content[:150]
                        first_ts = obj.get('timestamp', '')
                    last_msg = content[:150]
                    last_ts = obj.get('timestamp', '')
        except: pass
print(json.dumps({
    'session_id': sid or os.path.basename(session_file).replace('.jsonl',''),
    'first_user_msg': first_msg or '',
    'last_user_msg': last_msg or '',
    'last_visit': (last_ts or '')[:16].replace('T',' ')
}))
" "\$SESSION_FILE"
```

## Operations

### 1. Name / Rename / Remember a session

Triggered by: "remember this session as XXX", "name this session XXX", "rename this session to XXX", "save this session as XXX"

Steps:
1. If no name/title is provided by the user, ask for one.
2. Find the current session file and extract info using the method above.
3. Read `~/.claude/session_index.json` (create with `[]` if missing).
4. Check if an entry with the same `session_id` already exists:
   - If yes: update `title`, `last_visit`, `last_user_msg` (keep `first_user_msg`).
   - If no: append a new entry with all fields including `project_path` from `$PWD`.
5. Write the updated JSON back to `~/.claude/session_index.json`.
6. Confirm: "Session saved as **<title>**. Resume with: `claude --resume <session_id>`"

### 2. Find / Search sessions

Triggered by: "find session about XXX", "which session was about XXX", "search sessions for XXX"

Steps:
1. Read `~/.claude/session_index.json`.
2. Search all fields (title, first_user_msg, last_user_msg, project_path) for the keyword (case-insensitive).
3. Display matching results:
   ```
   **<title>** (<last_visit>)
   Project: <project_path>
   First msg: <first_user_msg>
   Resume: claude --resume <session_id>
   ```

### 3. List sessions

Triggered by: "list sessions", "show my sessions", "list named sessions"

Steps:
1. Read `~/.claude/session_index.json`.
2. Display all entries sorted by `last_visit` descending, in a readable table or list format.
