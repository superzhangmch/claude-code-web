#!/bin/bash
# Claude Code UserPromptSubmit hook: takes the prompt you just typed, polishes
# its English via an LLM, and writes the result to a /tmp file that grammar_bar
# (a tiny floating macOS status bar) displays. Non-blocking, best-effort — it
# never alters or delays your prompt; it just shows a nicer phrasing you can
# learn from. English-dominant prompts only (Chinese-heavy input is skipped).
#
# Install: copy this dir to ~/.claude/hooks/ and register the hook in
# ~/.claude/settings.json (see README.md). Configurable via env:
#   GRAMMAR_LLM_URL   OpenAI-compatible chat-completions endpoint
#                     (default: http://localhost:4000/v1/chat/completions)
#   GRAMMAR_LLM_MODEL model name       (default: claude-haiku-4-5)
#   GRAMMAR_LLM_KEY   bearer token, only if your endpoint requires auth (optional)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="/tmp/grammar_hook.log"
OUT="/tmp/grammar_last_correction.txt"

LLM_URL="${GRAMMAR_LLM_URL:-http://localhost:4000/v1/chat/completions}"
LLM_MODEL="${GRAMMAR_LLM_MODEL:-claude-haiku-4-5}"

# python for helper snippets (Homebrew installs 3.11 under /opt/homebrew/bin on
# Apple Silicon, which isn't always on a hook's PATH).
for PY in python3.11 /opt/homebrew/bin/python3.11 python3; do
  command -v "$PY" >/dev/null 2>&1 && break
done

input=$(cat)
prompt=$(echo "$input" | jq -r '.prompt // empty')

echo "[$(date '+%H:%M:%S')] prompt(${#prompt}): ${prompt:0:80}" >> "$LOG"

# Skip too short or too long (likely skill-expanded, not user-typed)
if [ -z "$prompt" ] || [ ${#prompt} -lt 10 ]; then
  echo "[$(date '+%H:%M:%S')] SKIP: too short" >> "$LOG"
  exit 0
fi

# Machine-generated prompts are not user typing: harness-injected XML blocks
# (<task-notification> etc.) and peer-bridge messages ([⇄ from peer ...]).
if [[ "$prompt" =~ ^[[:space:]]*\<[a-zA-Z_-]+\> ]] || [[ "$prompt" == "[⇄"* ]]; then
  echo "[$(date '+%H:%M:%S')] SKIP: machine-generated (xml/peer prefix)" >> "$LOG"
  echo "" > "$OUT"
  exit 0
fi

# Dedup: cron/loop/watcher prompts replay the same text verbatim — correcting
# it again is redundant. Skip if this exact prompt was already corrected once.
# (Hash is recorded only AFTER a successful correction, so a prompt skipped for
# other reasons — e.g. display asleep — still gets corrected when retyped.)
SEEN=/tmp/grammar_seen_hashes
prompt_hash=$(printf '%s' "$prompt" | md5)
if grep -qxF "$prompt_hash" "$SEEN" 2>/dev/null; then
  echo "[$(date '+%H:%M:%S')] SKIP: duplicate of an already-corrected prompt" >> "$LOG"
  exit 0
fi

# Long input (>500 chars): truncate to head 200 + tail 200 with a skipped-count marker
if [ ${#prompt} -gt 500 ]; then
  prompt=$(printf '%s' "$prompt" | "$PY" -c 'import sys; s=sys.stdin.read(); print(f"{s[:200]} [..{len(s)-400} chars skipped..] {s[-200:]}")')
  echo "[$(date '+%H:%M:%S')] TRUNCATED to head+tail (${#prompt} chars sent)" >> "$LOG"
fi

# Skip when no display is awake — bar would be invisible (lid closed / headless
# / remote session), so don't burn an LLM call. Counts online displays that are
# not asleep; CG errors (no WindowServer access, e.g. pure SSH) count as 0.
awake_displays=$("$PY" - <<'PY' 2>/dev/null
import ctypes
CG = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
ids = (ctypes.c_uint32 * 16)()
n = ctypes.c_uint32(0)
if CG.CGGetActiveDisplayList(16, ids, ctypes.byref(n)) != 0:
    print(0)
else:
    print(sum(0 if CG.CGDisplayIsAsleep(ids[i]) else 1 for i in range(n.value)))
PY
)
if [ "${awake_displays:-0}" -eq 0 ]; then
  echo "[$(date '+%H:%M:%S')] SKIP: no awake display" >> "$LOG"
  echo "" > "$OUT"
  exit 0
fi

# Language gate, decided in code (no LLM call wasted on Chinese-dominant text).
# Weight: 1 letter = 1/2 zh-char. Skip when letters are under 1/3 of the total:
# 0.5L/(0.5L+C) < 1/3  ⇔  L < C.
letter_count=$(printf '%s' "$prompt" | LC_ALL=C tr -cd 'A-Za-z' | wc -c | tr -d ' ')
cjk_count=$(printf '%s' "$prompt" | "$PY" -c 'import sys; s=sys.stdin.read(); print(sum(1 for c in s if "一" <= c <= "鿿"))')
if [ "$letter_count" -le 2 ] || [ "$letter_count" -lt "$cjk_count" ]; then
  echo "[$(date '+%H:%M:%S')] SKIP: ${letter_count} letters vs ${cjk_count} cjk — not english-dominant" >> "$LOG"
  echo "" > "$OUT"
  exit 0
fi

# Launch grammar bar if not running
if ! pgrep -x grammar_bar > /dev/null 2>&1; then
  "$SCRIPT_DIR/grammar_bar" &
  disown
fi

SYSTEM_PROMPT=$(cat "$SCRIPT_DIR/grammar_system_prompt.txt")

# Wrap the prompt in [[ ]] so the LLM treats it as a quoted string to correct,
# not as an instruction directed at itself.
wrapped_prompt="[[ $prompt ]]"

auth_header=()
[ -n "$GRAMMAR_LLM_KEY" ] && auth_header=(-H "Authorization: Bearer $GRAMMAR_LLM_KEY")

response=$(curl -s --max-time 10 "$LLM_URL" \
  -H "Content-Type: application/json" \
  "${auth_header[@]}" \
  -d "$(jq -n --arg msg "$wrapped_prompt" --arg sys "$SYSTEM_PROMPT" --arg model "$LLM_MODEL" '{
    model: $model,
    messages: [
      {role: "system", content: $sys},
      {role: "user", content: $msg}
    ],
    max_tokens: 1024
  }')" 2>> "$LOG")

if [ -z "$response" ]; then
  echo "[$(date '+%H:%M:%S')] ERROR: empty response from LLM (timeout or connection failed)" >> "$LOG"
  exit 0
fi

# Check for API error
error=$(echo "$response" | jq -r '.error.message // empty' 2>/dev/null)
if [ -n "$error" ]; then
  echo "[$(date '+%H:%M:%S')] API ERROR: $error" >> "$LOG"
  exit 0
fi

corrected=$(echo "$response" | jq -r '.choices[0].message.content // empty')
corrected=$(echo "$corrected" | tr '\n' ' ' | sed 's/  */ /g; s/^ *//; s/ *$//')

echo "[$(date '+%H:%M:%S')] corrected: ${corrected:0:80}" >> "$LOG"

if [ -n "$corrected" ] && [ "$corrected" != "$prompt" ]; then
  echo "$corrected" > "$OUT"
  # Mark as corrected so verbatim replays (cron/watcher ticks) skip the LLM.
  printf '%s\n' "$prompt_hash" >> "$SEEN"
  tail -n 300 "$SEEN" > "$SEEN.tmp" && mv "$SEEN.tmp" "$SEEN"
else
  echo "" > "$OUT"
fi

exit 0
