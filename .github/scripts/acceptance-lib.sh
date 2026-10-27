#!/bin/bash
# Helpers for the macOS acceptance workflow. Sourced by each step.
# Results accumulate in $RESULTS so the final step can summarise them.
RESULTS="${RUNNER_TEMP:-/tmp}/acceptance-results.txt"
JAR="${RUNNER_TEMP:-/tmp}/lab-cookies.txt"
CODEFILE="${RUNNER_TEMP:-/tmp}/lab-last-http-code"
BASE="http://127.0.0.1:8794"
STATE_DIR="$HOME/Library/Application Support/MainAIger Lab"
APP="/Applications/MainAIger Lab.app"

pass() { echo "PASS $1" >> "$RESULTS"; echo "::notice::PASS $1"; }
info() { echo "INFO $1" >> "$RESULTS"; echo "::notice::INFO $1"; }
fail() { echo "FAIL $1" >> "$RESULTS"; echo "::error::FAIL $1"; return 1; }
unverified() { echo "UNVERIFIED $1" >> "$RESULTS"; echo "::warning::UNVERIFIED $1"; }

# api METHOD PATH [JSON-BODY] -> prints the response body; `code` returns the HTTP status.
# (The status is kept in a file because callers use command substitution.)
api() {
  local method=$1 path=$2 body=${3:-}
  if [ "$method" = POST ]; then
    curl -sS -b "$JAR" -c "$JAR" -H "Host: 127.0.0.1:8794" -H "Origin: $BASE" \
      -H 'Content-Type: application/json' -H "X-CSRF-Token: ${CSRF:-}" \
      -X POST --data "$body" -o - -w '%{http_code}' "$BASE$path" > "$CODEFILE.raw"
  else
    curl -sS -b "$JAR" -c "$JAR" -H "Host: 127.0.0.1:8794" -o - -w '%{http_code}' "$BASE$path" > "$CODEFILE.raw"
  fi
  # BSD head has no negative byte count, so split with Python (portable).
  python3 -c '
import sys
raw = open(sys.argv[1], "rb").read()
open(sys.argv[2], "w").write(raw[-3:].decode())
sys.stdout.buffer.write(raw[:-3])' "$CODEFILE.raw" "$CODEFILE"
}
code() { cat "$CODEFILE"; }

# field KEY.PATH <<< JSON  (dot path; prints scalar or JSON for containers)
field() {
  python3 -c '
import json, sys
d = json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d = d[int(k)] if isinstance(d, list) else (d.get(k) if isinstance(d, dict) else None)
print(d if not isinstance(d, (dict, list)) else json.dumps(d))' "$1"
}

lab_up() { curl -fsS -m 3 -H "Host: 127.0.0.1:8794" "$BASE/" -o /dev/null 2>/dev/null; }
