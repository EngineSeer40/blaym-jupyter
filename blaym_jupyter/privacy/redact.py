"""Hide likely secrets in text, and in every text field of an event.

Common patterns only, not everything (see docs/secrets.md). The module is pure: it
imports the standard library and the contracts, and knows nothing of stores or services.

Rules the code keeps:

- Deterministic and idempotent: hiding already hidden text changes nothing. A marker
  names the kind (`[hidden: api key]`), never the value, and is never matched again.
- Linear time. Every pattern is anchored on a fixed word or a word boundary, every
  variable-length part is either bounded or cannot be re-scanned from the next start
  position, and nothing nests a repeat inside a repeat.
- Only the secret part is replaced: a name, quotes and punctuation stay.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from blaym_jupyter_types import BlaymEvent

MARKER_RE = re.compile(r"\[hidden: [a-z ]{3,40}\]")

_API = "api key"
_TOKEN = "token"
_PASSWORD = "password"
_SECRET = "secret"
_PRIVATE = "private key"
_CONN = "connection string password"


def _marker(kind: str) -> str:
    return f"[hidden: {kind}]"


# --- known key formats ---------------------------------------------------------------
# Runs are unbounded (a bounded run would leave a tail that matches on a second pass, and
# idempotence would break). Each starts with a lookbehind that forbids a word character before it. That keeps the
# scan linear (a run such as `eyJeyJeyJ...` is tried once, not once per letter).

_NB = r"(?<![A-Za-z0-9_\-])"
_KEY_FORMATS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(_NB + r"(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"), _API),
    (re.compile(_NB + r"AIza[0-9A-Za-z_\-]{35}(?![A-Za-z0-9_\-])"), _API),
    (re.compile(_NB + r"github_pat_[A-Za-z0-9_]{22,}"), _TOKEN),
    (re.compile(_NB + r"gh[pousr]_[A-Za-z0-9]{20,}"), _TOKEN),
    (re.compile(_NB + r"xox[baprs]-[A-Za-z0-9\-]{10,}"), _TOKEN),
    (re.compile(_NB + r"[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"), _API),
    (re.compile(_NB + r"sk-[A-Za-z0-9_\-]{20,}"), _API),
    (re.compile(_NB + r"eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}"), _TOKEN),
]

_PEM = re.compile(
    r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----.*?(?:-----END [A-Z ]{0,30}PRIVATE KEY-----|\Z)",
    re.DOTALL,
)

# `scheme://user:password@host`: only the password goes.
_URL_CRED = re.compile(
    r"(?P<pre>[A-Za-z][A-Za-z0-9+.\-]{1,20}://[^\s:/@\"'<>\\]{0,100}:)(?P<pw>[^\s/@\"'<>\\]+)(?=@)"
)

# `Authorization: Bearer <token>` and `Basic <b64>`, also inside a header dict or JSON.
_AUTH = re.compile(
    r"(?P<pre>authorization[\"']?[ \t]*[:=][ \t]*\\?[\"']?(?:bearer|basic)[ \t]+)(?P<v>[^\s\"'\\]{1,})",
    re.IGNORECASE,
)

# --- assignments -------------------------------------------------------------------------

_KW = (
    r"(?P<kw>pass(?:word|wd)|pwd|secret|token|api[_\-]?key|access[_\-]?key|private[_\-]?key|client[_\-]?secret)"
)
_SUFFIX = r"(?P<suf>[A-Za-z0-9_\-]{0,24})"

# A value in quotes, anywhere: `password = "x"`, `"token": "x"`, `os.environ["TOKEN"] = "x"`,
# JSON escaped inside a string (`\"password\": \"x\"`).
_ASSIGN_QUOTED = re.compile(
    _KW + _SUFFIX
    + r"(?P<kq>\\?[\"'])?\]?"
    + r"(?P<sep>[ \t]*(?:(?::[ \t]*[A-Za-z_][A-Za-z0-9_.]{0,40}[ \t]*)?=(?!=)|:(?!:))[ \t]*)"
    + r"(?P<q>\\?[\"'])(?P<v>[^\"'\n\\]*)",
    re.IGNORECASE,
)

# An unquoted value that runs to the end of the line, for env files, YAML, INI, `export`
# and `%env` lines. Only at the start of a line: inside code `f(password=pw)` is a
# variable and stays.
_ASSIGN_LINE = re.compile(
    r"^(?P<pre>[ \t]*(?:-[ \t]+)?(?:[!%][ \t]*)?(?:(?:export|env|set)[ \t]+)?"
    r"[A-Za-z0-9_.\-]{0,40}?)" + _KW + _SUFFIX
    + r"(?:\\?[\"'])?"
    + r"(?P<sep>[ \t]*(?:=|:(?=[ \t])|:(?=$))[ \t]*)"
    + r"(?P<v>[^\s\"'\\=][^\n\"'\\]*)(?=$)",
    re.IGNORECASE | re.MULTILINE,
)

# `--password=hunter2` and `--password hunter2` on a command line.
_ASSIGN_FLAG = re.compile(
    r"(?<![A-Za-z0-9_])(?P<pre>--[A-Za-z0-9_\-]{0,24}" + _KW + _SUFFIX + r"(?:=|[ \t]+))"
    r"(?P<v>[^\s\"'\\\-][^\s\"'\\]*)",
    re.IGNORECASE,
)

_KIND = {
    "password": _PASSWORD, "passwd": _PASSWORD, "pwd": _PASSWORD,
    "secret": _SECRET, "client_secret": _SECRET, "client-secret": _SECRET, "clientsecret": _SECRET,
    "token": _TOKEN,
    "private_key": _PRIVATE, "private-key": _PRIVATE, "privatekey": _PRIVATE,
}


def _kind_of(kw: str) -> str:
    k = kw.lower()
    return _KIND.get(k, _API)  # api_key, apikey, access_key and spellings


# Names that only look like secrets: a count, a type, a file, a label, a tokenizer.
_DENY_SUFFIX = {
    "s", "count", "counts", "len", "length", "limit", "max", "min", "id", "ids", "type", "types",
    "num", "number", "size", "file", "path", "name", "names", "url", "uri", "field", "column", "col", "label",
    "prompt", "hint", "help", "text", "message", "msg", "placeholder", "input", "expiry", "expires",
    "expiration", "ttl", "endpoint", "header", "headers", "kind", "policy", "manager", "store",
    "provider", "env", "var", "variable", "auth", "dir", "pattern", "regex", "ratio", "index", "idx",
}
_CAMEL = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


def _suffix_denied(suf: str) -> bool:
    s = suf.strip("_-")
    if not s:
        return False
    first = _CAMEL.findall(s.replace("-", "_").split("_")[0])
    seg = (first[0] if first else s).lower()
    return seg in _DENY_SUFFIX or seg.startswith("iz")  # tokenizer, tokenize


_PLACEHOLDER_FULL = re.compile(r"[xX*._\-#~]{3,}|\.\.\.|…|-+")
_PLACEHOLDER_WORD = {
    "none", "null", "nil", "true", "false", "undefined", "nan", "changeme", "change-me", "placeholder",
    "redacted", "example", "todo", "tbd", "yes", "no", "on", "off", "str", "int", "float", "bool",
    "bytes", "any", "secretstr", "dict", "list",
}
_REFS = ("os.environ", "environ[", "environ.get", "os.getenv", "getenv(", "getpass.", "getpass(", "keyring.", "input(")


def _is_placeholder(v: str) -> bool:
    s = v.strip()
    if not s or s.startswith("[hidden:") or s[0] in "<${%":
        return True
    low = s.lower()
    if low in _PLACEHOLDER_WORD or low.startswith("your"):
        return True
    if _PLACEHOLDER_FULL.fullmatch(s):
        return True
    return low.startswith(_REFS)


_IDENT_CHAIN = re.compile(r"[A-Za-z_][A-Za-z_.]*")


def _hide_quoted(m: re.Match[str]) -> str:
    if _suffix_denied(m["suf"]) or _is_placeholder(m["v"]):
        return m[0]
    if m["sep"] == ":" and not m["kq"]:
        return m[0]  # `...password:')`: the quote closes a string, it does not open a value
    return m[0][: m.start("v") - m.start()] + _marker(_kind_of(m["kw"]))


def _hide_line(m: re.Match[str]) -> str:
    if _suffix_denied(m["suf"]):
        return m[0]
    value = m["v"]
    cut = value.find(" #")  # a trailing comment
    if cut >= 0:
        value = value[:cut]
    core = value.rstrip()
    if not core or _is_placeholder(core) or any(c in core for c in "()[]{}`"):
        return m[0]
    spaced = m["sep"].startswith((" ", "\t")) and "=" in m["sep"]
    if spaced and _IDENT_CHAIN.fullmatch(core):
        return m[0]  # `password = my_password` in code: a variable, not a value
    start = m.start("v") - m.start()
    return m[0][:start] + _marker(_kind_of(m["kw"])) + m[0][start + len(core):]


def _hide_flag(m: re.Match[str]) -> str:
    if _suffix_denied(m["suf"]) or _is_placeholder(m["v"]):
        return m[0]
    return m["pre"] + _marker(_kind_of(m["kw"]))


def _hide_auth(m: re.Match[str]) -> str:
    if _is_placeholder(m["v"]):
        return m[0]
    return m["pre"] + _marker(_TOKEN)


def _hide_url(m: re.Match[str]) -> str:
    if _is_placeholder(m["pw"]):
        return m[0]
    return m["pre"] + _marker(_CONN)


def redact(text: str) -> tuple[str, int]:
    """Return the text with likely secrets hidden, and the number of hidden spans."""
    if not text:
        return text, 0
    total = 0
    state = [text]

    def run(pattern: re.Pattern[str], repl: Callable[[re.Match[str]], str]) -> None:
        state[0] = pattern.sub(repl, state[0])

    # Count real changes only: a callback may keep a match (a placeholder, a variable).

    def counted(fn: Callable[[re.Match[str]], str]) -> Callable[[re.Match[str]], str]:
        def inner(m: re.Match[str]) -> str:
            nonlocal total
            out = fn(m)
            if out != m[0]:
                total += 1
            return out

        return inner

    def fixed(kind: str) -> Callable[[re.Match[str]], str]:
        def inner(m: re.Match[str]) -> str:
            nonlocal total
            total += 1
            return _marker(kind)

        return inner

    run(_PEM, fixed(_PRIVATE))
    run(_URL_CRED, counted(_hide_url))
    for pattern, kind in _KEY_FORMATS:
        run(pattern, fixed(kind))
    run(_AUTH, counted(_hide_auth))
    run(_ASSIGN_QUOTED, counted(_hide_quoted))
    run(_ASSIGN_LINE, counted(_hide_line))
    run(_ASSIGN_FLAG, counted(_hide_flag))
    return state[0], total


# --- events -----------------------------------------------------------------------------------


def _walk(value: Any) -> Any:
    """Redact every string VALUE at any depth; keys are never touched."""
    if isinstance(value, str):
        return redact(value)[0]
    if isinstance(value, dict):
        return {k: _walk(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v) for v in value]
    return value


def _item(item: Any) -> None:
    item.preview = redact(item.preview)[0]
    if item.searchText is not None:
        item.searchText = redact(item.searchText)[0]


def _cell(cell: Any) -> None:
    cell.code = redact(cell.code)[0]
    cell.metadata = _walk(cell.metadata)
    for group in cell.outputs:
        for it in group.items:
            _item(it)


def _redact_payload(kind: str, p: Any) -> None:
    if kind in ("notebook_snapshot", "changed_outside_editor"):
        p.notebookMetadata = _walk(p.notebookMetadata)
        for c in p.cells:
            _cell(c)
    elif kind == "cell_inserted":
        p.code = redact(p.code)[0]
        if p.metadata is not None:
            p.metadata = _walk(p.metadata)
        for it in p.outputs or []:
            _item(it)
    elif kind == "cell_source_changed":
        p.code = redact(p.code)[0]
    elif kind in ("cell_metadata_changed", "notebook_metadata_changed"):
        p.metadata = _walk(p.metadata)
    elif kind in ("output_added", "output_replaced"):
        _item(p.item)
    elif kind in ("chat_message_added", "chat_message_updated"):
        p.body = redact(p.body)[0]


def redact_event(event: Any) -> Any:
    """A new event of the same type with every text field hidden; the original is untouched.

    Takes a `BlaymEvent` (root model) or one concrete event model. Plain mappings are not
    accepted: validate them first (`BlaymEvent.model_validate`).
    """
    wrapped = isinstance(event, BlaymEvent)
    concrete = event.root if wrapped else event
    if not hasattr(concrete, "payload") or not hasattr(concrete, "type"):
        raise TypeError(f"redact_event needs a BlaymEvent or an event model, not {type(event).__name__}")
    copy = concrete.model_copy(deep=True)
    _redact_payload(copy.type, copy.payload)
    return BlaymEvent(root=copy) if wrapped else copy
