#!/bin/bash
# Run every test, in order, and report. Exit 0 only if all of them passed.
#
# Checks the fixed test ports first: a stray dev/stub server on one of them makes a
# server test fail in a way that looks exactly like a real bug (it has fooled me more
# than once), so it is worth naming up front rather than debugging twice.
set -u
cd "$(dirname "$0")/.."

PY=""
for c in .venv/bin/python "$HOME/claude-code-web/.venv/bin/python"; do
  [ -x "$c" ] && { PY="$c"; break; }
done
[ -n "$PY" ] || { echo "no venv python found (tried .venv and ~/claude-code-web/.venv)"; exit 2; }

# ---- port check -------------------------------------------------------------
busy=""
for p in 8993 8994 8995 8996 8997 8998 8999; do
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -q "127.0.0.1:$p " && busy="$busy $p"
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1 && busy="$busy $p"
  fi
done
if [ -n "$busy" ]; then
  echo "!! something is already listening on:$busy"
  echo "   the server tests bind those ports — stop it first, or they will fail"
  echo "   for reasons that have nothing to do with the code."
  echo
fi

# ---- run --------------------------------------------------------------------
fail=0
for t in tests/test_*.py; do
  printf '%-40s ' "$t"
  out=$(timeout 600 "$PY" "$t" 2>&1)
  rc=$?
  if [ $rc -eq 0 ]; then
    case "$out" in
      SKIP*) echo "SKIP  (${out#SKIP: })" ;;
      *)     echo "PASS" ;;
    esac
  else
    echo "FAIL"
    printf '%s\n' "$out" | grep -E "^  FAIL|FAILED|Error|Traceback" | head -8 | sed 's/^/    /'
    fail=1
  fi
done

# ---- stamp ------------------------------------------------------------------
# On a green run, record WHICH bytes were green. The deploy refuses to ship a tree
# whose fingerprint has no stamp — so "run the suite before deploying" stops being a
# sentence someone has to remember. Only on success, and the stamp is removed on
# failure so a stale green cannot vouch for a red tree.
STAMP=".suite-stamp"
if [ $fail -eq 0 ]; then
  fp=$("$PY" tests/tree_fingerprint.py | tail -1)
  printf '{"fingerprint":"%s","when":"%s","head":"%s","dirty":%s}\n' \
    "$fp" "$(date '+%F %T')" "$(git rev-parse --short HEAD 2>/dev/null || echo none)" \
    "$([ -n "$(git status --porcelain 2>/dev/null)" ] && echo true || echo false)" > "$STAMP"
else
  rm -f "$STAMP"
fi

echo
[ $fail -eq 0 ] && echo "all good" || echo "SOMETHING FAILED (see above)"
exit $fail
