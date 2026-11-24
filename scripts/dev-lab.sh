#!/usr/bin/env bash
# Starts a real JupyterLab with Blaym for trying recording by hand. Opens no browser.
#
# Usage:  scripts/dev-lab.sh            (BLAYM_DEV_FRESH=1 wipes the dev folder first)
#
# What it does:
#   1. builds the extension (`tsc`, then `jupyter-builder build --development True .`) when
#      the bundle is missing or older than src/, schema/, style/ or package.json;
#   2. starts `jupyter lab` from the repo .venv (bound to BLAYM_DEV_IP; default 0.0.0.0 under WSL,
#      else 127.0.0.1) on a free port with a random token, with the
#      blaym_jupyter server extension enabled and the built labextension linked;
#   3. keeps everything in ~/.cache/blaym-dev/ so history survives between runs:
#        notebooks/  the Jupyter root folder (sample.ipynb is created if it is empty)
#        data/       the Jupyter data dir
#        data/blaym/ the Blaym store (history, blobs)
#      Stop with Ctrl-C.
#
# Dependency on the S1 probe: `jupyter-builder` (and so the build) comes from
# probes/s1-capture/.venv because `jlpm` and the npm registry are not available offline.
# The script stops with a message if it is missing.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROBE="$ROOT/probes/s1-capture"
DEV="$HOME/.cache/blaym-dev"
EXT="$ROOT/blaym_jupyter/labextension"

[ -x "$ROOT/.venv/bin/jupyter" ] || { echo "dev-lab.sh: missing $ROOT/.venv/bin/jupyter (repo .venv with jupyterlab)" >&2; exit 2; }
if [ ! -f "$EXT/package.json" ] || [ -n "$(find "$ROOT/src" "$ROOT/schema" "$ROOT/style" "$ROOT/package.json" -newer "$EXT/package.json" -type f 2>/dev/null | head -1)" ]; then
  [ -x "$PROBE/.venv/bin/jupyter-builder" ] || { echo "dev-lab.sh: missing $PROBE/.venv/bin/jupyter-builder (needed to build the extension offline)" >&2; exit 2; }
  echo "Building the extension..."
  (cd "$ROOT" && npx tsc --sourceMap && PATH="$PROBE/.venv/bin:$PATH" jupyter-builder build --development True .) || exit 1
fi

[ -n "${BLAYM_DEV_FRESH:-}" ] && rm -rf "$DEV"
mkdir -p "$DEV/notebooks" "$DEV/data/labextensions" "$DEV/config"
ln -sfn "$EXT" "$DEV/data/labextensions/blaym-jupyter"
PY="$ROOT/.venv/bin/python"
if [ -z "$(ls -A "$DEV/notebooks")" ]; then
  "$PY" - "$DEV/notebooks/sample.ipynb" <<'PYEOF'
import json, sys
cells = ["import math\nx = 3", "print(math.sqrt(x))", "# change me\ny = x * 2\ny"]
nb = {"cells": [{"cell_type": "code", "id": f"sample-{i}", "metadata": {}, "source": s,
                 "execution_count": None, "outputs": []} for i, s in enumerate(cells)],
      "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                   "language_info": {"name": "python"}},
      "nbformat": 4, "nbformat_minor": 5}
json.dump(nb, open(sys.argv[1], "w"), indent=1)
PYEOF
fi

PORT="$("$PY" -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')"
TOKEN="$("$PY" -c 'import secrets;print(secrets.token_hex(24))')"
if [ -n "${BLAYM_DEV_IP:-}" ]; then IP="$BLAYM_DEV_IP"
elif grep -qi microsoft /proc/version 2>/dev/null; then IP=0.0.0.0
else IP=127.0.0.1; fi
API="http://127.0.0.1:$PORT/blaym-jupyter/api"
URLS="  1. Open:  http://127.0.0.1:$PORT/lab/tree/sample.ipynb?token=$TOKEN"
if [ "$IP" = "0.0.0.0" ]; then
  HOSTIP="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | grep -v '^127\.' | head -1)"
  if [ -n "$HOSTIP" ]; then
    URLS="$URLS
     or:    http://$HOSTIP:$PORT/lab/tree/sample.ipynb?token=$TOKEN"
  fi
  URLS="$URLS
     The server is then reachable from other machines on the network that can reach this
     address, protected by the token only; use BLAYM_DEV_IP=127.0.0.1 to keep it local."
fi
cat <<MSG

Blaym dev JupyterLab
$URLS
  2. In the notebook toolbar click the "Blaym: off" button to switch recording on
     (the status bar at the bottom shows the same state).
  3. Edit, add, move and delete cells, run them. Nothing is written into the notebook file.
  Recorded data: $DEV/data/blaym/   (notebooks are in $DEV/notebooks/)
  Read it back (the routes need the token). The history id is the "notebookId" in the answer
  of the page's call to .../notebooks/register (browser DevTools, Network tab, filter "register").
  Then, with ID set to that id:
    ID=<explorationId>
    curl -s -H "Authorization: token $TOKEN" "$API/outline?explorationId=\$ID"
    curl -s -H "Authorization: token $TOKEN" "$API/state?explorationId=\$ID&branchId=<rootBranchId from the outline>&tick=<highestTick from the outline>"
  Stop with Ctrl-C.

MSG
export JUPYTER_DATA_DIR="$DEV/data" JUPYTER_CONFIG_DIR="$DEV/config"
exec "$ROOT/.venv/bin/jupyter" lab --no-browser --ip="$IP" --port="$PORT" --port-retries=0 \
  --ServerApp.token="$TOKEN" --ServerApp.root_dir="$DEV/notebooks" --ServerApp.password='' \
  --ServerApp.jpserver_extensions="{'blaym_jupyter': True}"
