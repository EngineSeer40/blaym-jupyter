"""Entry point: `python -m blaym_vscode --root <folder>`, token on the first line of standard input.

Prints one JSON line: `{"ready": true, "port": n, "apiVersion": 1}`, or
`{"ready": false, "error": ...}` with a non-zero exit. Ends when standard input closes.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

from .auth import MIN_TOKEN_BYTES


def _say(value: dict) -> None:
    print(json.dumps(value), flush=True)


def _fail(error: str, code: int = 1) -> int:
    _say({"ready": False, "error": error})
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blaym_vscode")
    parser.add_argument("--root", required=True, help="the store folder")
    args = parser.parse_args(argv)
    token = sys.stdin.readline().strip()
    if len(token.encode("utf-8")) < MIN_TOKEN_BYTES:
        return _fail("token_missing_or_too_short")
    from .host import StoreInUse, VscodeHost, take_store_lock
    from .server import make_server

    try:
        lock = take_store_lock(Path(args.root))
    except StoreInUse:
        return _fail("store_in_use", 2)
    except OSError as exc:
        return _fail(f"cannot_open_store: {type(exc).__name__}")
    host = None
    try:
        host = VscodeHost(args.root, token)
        server = make_server(host.recording, host.authorizer)
        _say({"ready": True, "port": server.server_address[1], "apiVersion": 1})
        # stop when the starter goes away (standard input closes), so no process is left behind
        threading.Thread(target=lambda: (sys.stdin.read(), server.shutdown()), daemon=True).start()
        server.serve_forever()
        server.server_close()
        return 0
    except Exception as exc:  # noqa: BLE001
        return _fail(f"{type(exc).__name__}: {str(exc)[:200]}")
    finally:
        if host is not None:
            host.close()
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
