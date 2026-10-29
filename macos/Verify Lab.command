#!/bin/bash
# Double-click this file in Finder (or run it) to verify the Lab on this Mac.
#
# What it does, in order, and nothing else:
#   1. moves to the repository this file lives in and pulls the current branch (fast-forward only);
#   2. runs scripts/verify_mac.py with the real checks on: a Docker session, the self-improvement
#      loop with the first installed coder model, and a read-only ssh probe of "nexus" if ~/.ssh/config knows it;
#      pass a ChatGPT/Claude export zip as the first argument to also check the import parsers;
#   3. writes verify-mac-<stamp>.json and .md into the repository folder and opens that folder in Finder.
# It never reads an owner key, never writes into a Lab's state folder and never installs anything.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || { echo "Could not enter $REPO"; exit 1; }
echo "MainAIger Lab verification · $(date)"
echo "Repository: $REPO"
echo
echo "== 1. git pull --ff-only"
git pull --ff-only || echo "(pull failed; continuing with the checkout as it is)"
echo
echo "== 2. verify"
PY=python3
if [ -x .venv/bin/python3 ]; then PY=.venv/bin/python3; echo "(using the checkout's .venv Python, where Playwright is installed)"; fi
FLAGS=(--docker --model auto)
if ssh -G nexus 2>/dev/null | grep -qi '^hostname nexus$'; then
  echo "(no ssh alias named nexus in ~/.ssh/config; skipping the ssh probe)"
else
  FLAGS+=(--ssh-host nexus)
fi
if [ "${1:-}" != "" ]; then FLAGS+=(--export "$1"); fi
echo "$PY scripts/verify_mac.py ${FLAGS[*]}"
"$PY" scripts/verify_mac.py "${FLAGS[@]}"
STATUS=$?
echo
echo "== 3. report"
ls -1t verify-mac-*.md 2>/dev/null | head -1
open "$REPO" 2>/dev/null
echo
echo "Done (exit $STATUS). Upload the newest verify-mac-*.json (or .md) to the chat."
read -r -p "Press Return to close this window. "
exit $STATUS
