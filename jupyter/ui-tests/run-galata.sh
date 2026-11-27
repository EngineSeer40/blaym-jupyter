#!/usr/bin/env bash
# Runs the Galata scenario(s) of ui-tests/tests against a real JupyterLab, fully offline.
#
# Usage:  ui-tests/run-galata.sh [extra playwright args]      (env BLAYM_SPEC=<regex> picks another spec)
#
# What it does:
#   1. builds the extension: `tsc` (lib) and `jupyter-builder build --development True .`
#      (writes blaym_jupyter/labextension, schema included);
#   2. starts `jupyter lab` from the repo's .venv on a free port with a random token, with
#      the blaym_jupyter server extension enabled, in a temporary folder (root dir, Jupyter
#      data dir and Blaym store are all inside it, so nothing of yours is touched), and the
#      built extension linked into that data dir;
#   3. runs `playwright test` (config: ui-tests/playwright.local.config.js) headless;
#   4. stops the server and removes the temporary folder (set BLAYM_KEEP=1 to keep it).
#
# Offline dependencies, taken from the S1 probe because `jlpm` and the npm registry are not
# available here (the script stops with a message if one is missing):
#   - probes/s1-capture/.venv        jupyter-builder (the labextension build)
#   - probes/s1-capture/ui/node_modules   @jupyterlab/galata and @playwright/test
#       ui-tests/node_modules is an UNTRACKED symlink to it, created here if absent
#   - ~/.cache/ms-playwright/chromium_headless_shell-1248   cached headless Chromium
#       (override with BLAYM_CHROMIUM=<path to chrome-headless-shell>)
#   - .venv (repo)                   jupyter lab and the blaym_jupyter package
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PROBE="$ROOT/probes/s1-capture"
CHROMIUM="${BLAYM_CHROMIUM:-$HOME/.cache/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell}"

need() { [ -e "$1" ] || { echo "run-galata.sh: missing $1 ($2)" >&2; exit 2; }; }
need "$ROOT/.venv/bin/jupyter-builder" "jupyter-builder of the main venv"
need "$PROBE/ui/node_modules/@jupyterlab/galata" "galata of the S1 probe ui folder"
need "$PROBE/ui/node_modules/.bin/playwright" "playwright of the S1 probe ui folder"
need "$CHROMIUM" "cached headless Chromium; set BLAYM_CHROMIUM"
need "$ROOT/.venv/bin/jupyter" "the repo .venv with jupyterlab"
[ -e "$HERE/node_modules" ] || ln -s "$PROBE/ui/node_modules" "$HERE/node_modules"

cd "$ROOT/jupyter" || exit 1
(cd "$ROOT/core/ts" && npx tsc) || exit 1
npx tsc --sourceMap || exit 1
PATH="$ROOT/.venv/bin:$PATH" jupyter-builder build --development True . || exit 1

WORK="$(mktemp -d)"
mkdir -p "$WORK/root" "$WORK/data/labextensions" "$WORK/config"
ln -s "$ROOT/jupyter/blaym_jupyter/labextension" "$WORK/data/labextensions/blaym-jupyter"
PY="$ROOT/.venv/bin/python"
export BLAYM_PORT="$("$PY" -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')"
export BLAYM_TOKEN="$("$PY" -c 'import secrets;print(secrets.token_hex(24))')"
export BLAYM_ROOT="$WORK/root" BLAYM_CHROMIUM="$CHROMIUM" BLAYM_RESULTS="$WORK/results"
export JUPYTER_DATA_DIR="$WORK/data" JUPYTER_CONFIG_DIR="$WORK/config"

"$ROOT/.venv/bin/jupyter" lab --no-browser --ip=127.0.0.1 --port="$BLAYM_PORT" --port-retries=0 \
  --ServerApp.token="$BLAYM_TOKEN" --ServerApp.root_dir="$WORK/root" --ServerApp.password='' \
  --LabApp.expose_app_in_browser=True \
  --ServerApp.jpserver_extensions="{'blaym_jupyter': True}" >"$WORK/server.log" 2>&1 &
PID=$!
cleanup() {
  kill "$PID" 2>/dev/null; wait "$PID" 2>/dev/null
  if [ -n "${BLAYM_KEEP:-}" ]; then echo "kept $WORK"; else rm -rf "$WORK"; fi
}
trap cleanup EXIT
for _ in $(seq 1 100); do
  curl -sf "http://127.0.0.1:$BLAYM_PORT/api/status" -H "Authorization: token $BLAYM_TOKEN" >/dev/null 2>&1 && break
  sleep 0.3
done

cd "$HERE" || exit 1
node_modules/.bin/playwright test -c playwright.local.config.js "$@"
