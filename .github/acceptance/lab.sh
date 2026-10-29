#!/bin/bash
# Shell helpers that drive the local Lab's HTTP API exactly as the browser does:
# same-origin header, cookie session, CSRF token and explicit confirmation on
# every action. Used by the Linux acceptance workflow; no credentials involved
# beyond the Lab's own local owner key read from the state directory.
LAB_URL=${LAB_URL:-http://127.0.0.1:8794}
LAB_DIR=${LAB_DIR:-${RUNNER_TEMP:-/tmp}/lab}
STATE_DIR=${STATE_DIR:-${RUNNER_TEMP:-/tmp}/state}
mkdir -p "$LAB_DIR"
JAR="$LAB_DIR/cookies"
CSRF_FILE="$LAB_DIR/csrf"
RESULTS="$LAB_DIR/results.txt"

# lab_post PATH JSON [MAX_SECONDS]  -> prints the response body; lab_status prints the HTTP code
lab_post() {
  local path=$1 body=$2 maxtime=${3:-30} out
  out=$(curl -sS -m "$maxtime" -o - -w '\n%{http_code}' -b "$JAR" ${LAB_SAVE_COOKIES:+-c "$JAR"} \
        -H "Origin: $LAB_URL" -H 'Content-Type: application/json' \
        -H "X-CSRF-Token: $(cat "$CSRF_FILE" 2>/dev/null)" --data "$body" "$LAB_URL$path")
  printf '%s' "${out##*$'\n'}" > "$LAB_DIR/last_status"
  printf '%s' "${out%$'\n'*}"
}

# lab_get PATH -> prints the response body; lab_status prints the HTTP code of the last call
lab_get() {
  local out
  out=$(curl -sS -m 30 -o - -w '\n%{http_code}' -b "$JAR" "$LAB_URL$1")
  printf '%s' "${out##*$'\n'}" > "$LAB_DIR/last_status"
  printf '%s' "${out%$'\n'*}"
}

# HTTP status of the most recent lab_post/lab_get (functions run in command
# substitution subshells, so a plain variable would not survive).
lab_status() { cat "$LAB_DIR/last_status" 2>/dev/null; }

lab_login() {
  local key body
  key=$(cat "$STATE_DIR/owner.key")
  body=$(LAB_SAVE_COOKIES=1 lab_post /api/login "$(jq -cn --arg k "$key" '{key:$k}')")
  [[ "$(lab_status)" == 200 ]] || { echo "login failed: $(lab_status) $body" >&2; return 1; }
  jq -r .csrf <<<"$body" > "$CSRF_FILE"
}

lab_revision() { lab_get /api/state | jq -r .revision; }

# lab_exec CODE [MAX_SECONDS] -> response body of /api/kernel/execute
lab_exec() { lab_post /api/kernel/execute "$(jq -cn --arg c "$1" '{code:$c,confirm:true}')" "${2:-90}"; }

# lab_exec_file FILE [MAX_SECONDS] -> same, with the cell source read from a file
lab_exec_file() { lab_post /api/kernel/execute "$(jq -cn --rawfile c "$1" '{code:$c,confirm:true}')" "${2:-90}"; }

lab_start_session() { lab_post /api/kernel/start "$(jq -cn --argjson r "$(lab_revision)" '{confirm:true,revision:$r}')" 120; }

containers() { docker ps -a --filter name=mainaiger-lab- --format '{{.Names}}'; }

pass() { echo "::notice::$1: PASS - $2"; echo "$1 PASS - $2" >> "$RESULTS"; }
fail() { echo "::error::$1: FAIL - $2"; echo "$1 FAIL - $2" >> "$RESULTS"; return 1; }
