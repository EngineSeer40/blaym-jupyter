#!/usr/bin/env bash
# Usage: check-contracts.sh [schema.json]
# Regenerates to a temp file and fails if it differs from the committed models.
set -euo pipefail
cd "$(dirname "$0")/.."
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
scripts/generate-contracts.sh "$TMP/_generated.py" "$@"
if ! diff -u blaym_jupyter/contracts/_generated.py "$TMP/_generated.py"; then
  echo "contracts drift: run 'jlpm contracts:generate'" >&2
  exit 1
fi
echo "contracts up to date"
