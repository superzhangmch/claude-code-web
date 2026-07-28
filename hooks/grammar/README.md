# grammar hook — live English-polish bar

A Claude Code **UserPromptSubmit hook** that quietly polishes the English of
the prompt you just typed and shows a native-sounding rewrite in a slim
floating bar at the top of the screen. It's a *learning aid*, not a rewriter:
your prompt is submitted exactly as typed — the bar just shows how it could
read better. Chinese-dominant prompts are skipped (it's for English practice).

macOS only (the bar is a tiny Cocoa/Swift app).

## Pieces

| File | Role |
|---|---|
| `grammar_check.sh` | the hook. Reads the prompt from stdin, gates (length / awake-display / English-dominant), calls an OpenAI-compatible endpoint to polish it, writes the result to `/tmp/grammar_last_correction.txt`. Never blocks or edits your prompt. |
| `grammar_system_prompt.txt` | the polishing instructions (treat input as a quoted string to polish, never answer it; translate stray Chinese; keep technical tokens; append a short parenthetical noting the fixes). |
| `grammar_bar.swift` | the floating bar. Watches the `/tmp` file (kqueue, zero idle wakeups), shows the latest correction, follows the mouse across displays, auto-hides after 5 min, click / hover / ⌥⌘G to dismiss-recall. |

The hook and the bar talk only through `/tmp/grammar_last_correction.txt` — no
ports, no state.

## Install

```sh
# 1. copy the hook dir into your Claude config
cp -R hooks/grammar ~/.claude/hooks/grammar

# 2. build the bar (produces the gitignored `grammar_bar` binary next to the sources)
swiftc ~/.claude/hooks/grammar/grammar_bar.swift -o ~/.claude/hooks/grammar/grammar_bar

# 3. register the hook in ~/.claude/settings.json
```

`settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "~/.claude/hooks/grammar/grammar_check.sh" } ] }
    ]
  }
}
```

## Configure the LLM

The hook posts to an OpenAI-compatible chat-completions endpoint. Defaults suit
a local [LiteLLM](https://github.com/BerriAI/litellm) proxy; override via env
(e.g. exported in your shell profile, which hooks inherit):

| env var | default | meaning |
|---|---|---|
| `GRAMMAR_LLM_URL` | `http://localhost:4000/v1/chat/completions` | endpoint |
| `GRAMMAR_LLM_MODEL` | `claude-haiku-4-5` | model (pick something fast/cheap) |
| `GRAMMAR_LLM_KEY` | *(unset)* | bearer token, only if your endpoint needs auth |

Debug log: `/tmp/grammar_hook.log`. Hotkey to recall the last bar: **⌥⌘G**.
