#!/bin/bash
# Counts the user's messages per session and asks Claude to checkpoint on every 10th.
# Run with "reset" to zero the count (used after /clear).
input=$(cat)
sid=$(printf '%s' "$input" | sed -n 's/.*"session_id" *: *"\([^"]*\)".*/\1/p' | head -n 1)
f="${TMPDIR:-/tmp}/claude-cp-count-${sid:-default}"
if [ "$1" = "reset" ]; then rm -f "$f"; exit 0; fi
n=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$f"
if [ $((n % 10)) -eq 0 ]; then
  echo "This is user message $n in this session. After fully completing this request, run the cp skill."
fi
exit 0
