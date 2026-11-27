#!/usr/bin/env bash
# End-to-end scenario in real VS Code (slow; the only way to run it). Two launches on one temporary store:
# 1 records the scripted session and is killed; 2 reopens, checks the start-up gap and compares checkpoints.
set -euo pipefail
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"; . "$NVM_DIR/nvm.sh"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
S3C="$REPO/probes/s3c"
export DISPLAY="${DISPLAY:-:0}"
[ -x "$S3C/.venv-kernel/bin/python" ] || { echo "missing $S3C/.venv-kernel"; exit 1; }
cd "$REPO"
npx tsc -p vscode
rm -rf "$S3C/vscode/portable/user-data/logs"
DIR="$(mktemp -d)"
mkdir -p "$DIR/workspace" "$DIR/store"
export BLAYM_E2E_DIR="$DIR"
trap 'rm -rf "$DIR"' EXIT
LOG="$DIR/run.log"
fail() { echo "FAILED, see $KEEP"; grep -E "PHASE|FAILED|Error|MISMATCH" "$LOG" | tail -20; exit 1; }
KEEP="${BLAYM_E2E_LOG:-${TMPDIR:-/tmp}/blaym-e2e.log}"
if [ "${BLAYM_EXPORT:-}" = 1 ]; then
  # export mode: phase 1 only, writes the cross-editor fixture
  export BLAYM_EXPORT_OUT="$REPO/fixtures/recorder/cross-editor/vscode.json"
  mkdir -p "$(dirname "$BLAYM_EXPORT_OUT")"
  PHASES="1"
else PHASES="1 2"; fi
for PHASE in $PHASES; do
  BLAYM_E2E_PHASE=$PHASE timeout 400 node "$REPO/vscode/out/vscode/test/runTest.js" >> "$LOG" 2>&1 || { cp "$LOG" "$KEEP"; fail; }
  [ "$PHASE" = 1 ] && sleep 3
done
cp "$LOG" "$KEEP"
sleep 5
if pgrep -f "$S3C/vscode/app|$S3C/.venv-kernel|blaym_vscode --root $DIR" >/dev/null; then
  echo "LEFTOVER PROCESSES"; pgrep -af "$S3C/vscode/app|$S3C/.venv-kernel|blaym_vscode --root $DIR"; exit 1
fi
grep -E "^\[e2e\]" "$LOG" || true
echo "no leftover processes"
