# skills

Claude Code skills used by `cc_web`. Install one by copying its
directory into `~/.claude/skills/`.

## session-name

Lets you name the current Claude Code session ("remember this session
as the LTV refactor", "rename this session to ...") and search by
title later. The skill writes to `~/.claude/session_index.json`, which
the `cc_web` picker reads to show human-friendly titles instead of
"(unnamed) <first user msg>".

```sh
cp -R skills/session-name ~/.claude/skills/
# then in any claude session:
#   "remember this session as <title>"
```

Without it the web UI still works, but every session in the picker
shows up as `(unnamed)` and is sorted only by recency.
