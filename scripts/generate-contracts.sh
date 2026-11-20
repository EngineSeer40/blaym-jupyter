#!/usr/bin/env bash
# Usage: generate-contracts.sh <output.py> [schema.json]
# Generates pydantic models from the blaym-jupyter-types envelope schema.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="$1"
SCHEMA="${2:-node_modules/blaym-jupyter-types/schema/blaym-event-envelope.schema.json}"
uv run --no-sync datamodel-codegen \
  --input "$SCHEMA" \
  --input-file-type jsonschema \
  --output-model-type pydantic_v2.BaseModel \
  --target-python-version 3.13 \
  --class-name BlaymEventEnvelope \
  --extra-fields forbid \
  --field-constraints \
  --use-annotated \
  --disable-timestamp \
  --formatters black isort \
  --custom-file-header "# GENERATED FILE - DO NOT EDIT.
# Source schema: blaym-event-envelope.schema.json (blaym-jupyter-types).
# Regenerate with: jlpm contracts:generate" \
  --output "$OUT"
