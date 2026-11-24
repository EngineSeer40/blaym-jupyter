"""Search text for rich outputs (S5, Task 5): pure extractors, never raising.

Plotly: layout title, axis titles, series names. Vega-Lite: title and field names.
Data resource (tables): column names. HTML tables: header cells and captions. The text is
cut at the limit on a character boundary. Anything that cannot be read gives nothing.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any, Mapping

SEARCH_TEXT_LIMIT = 65536

PLOTLY = "application/vnd.plotly.v1+json"
DATARESOURCE = "application/vnd.dataresource+json"
HTML = "text/html"
_VEGALITE = re.compile(r"application/vnd\.vegalite\.v\d+\+json")

_MAX_DEPTH = 40
_MAX_PARTS = 5000


def cut(text: str, limit: int = SEARCH_TEXT_LIMIT) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore")


def _load(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        return json.loads(value)
    return value


def _title(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return None


def _add(parts: list[str], text: str | None) -> None:
    if text and text.strip() and len(parts) < _MAX_PARTS:
        parts.append(text.strip())


def plotly_text(value: Any) -> list[str]:
    spec = _load(value)
    parts: list[str] = []
    if not isinstance(spec, dict):
        return parts
    layout = spec.get("layout")
    if isinstance(layout, dict):
        _add(parts, _title(layout.get("title")))
        axes = [layout] + ([layout["scene"]] if isinstance(layout.get("scene"), dict) else [])
        for holder in axes:
            for key, axis in holder.items():
                if re.fullmatch(r"[xyz]axis\d*", key) and isinstance(axis, dict):
                    _add(parts, _title(axis.get("title")))
    for trace in spec.get("data") or []:
        if isinstance(trace, dict):
            _add(parts, trace.get("name") if isinstance(trace.get("name"), str) else None)
    return parts


def vegalite_text(value: Any) -> list[str]:
    spec = _load(value)
    parts: list[str] = []
    seen: set[str] = set()

    def walk(node: Any, depth: int) -> None:
        if depth > _MAX_DEPTH or len(parts) >= _MAX_PARTS:
            return
        if isinstance(node, dict):
            for key, child in node.items():
                if key == "title":
                    _add(parts, _title(child) if not isinstance(child, list) else None)
                    if isinstance(child, list):
                        for line in child:
                            _add(parts, line if isinstance(line, str) else None)
                elif key == "field" and isinstance(child, str):
                    if child not in seen:
                        seen.add(child)
                        _add(parts, child)
                else:
                    walk(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                walk(child, depth + 1)

    if isinstance(spec, (dict, list)):
        # the title of the whole chart first, then everything else in document order
        walk(spec, 0)
    return parts


def dataresource_text(value: Any) -> list[str]:
    spec = _load(value)
    parts: list[str] = []
    if isinstance(spec, dict) and isinstance(spec.get("schema"), dict):
        for field in spec["schema"].get("fields") or []:
            if isinstance(field, dict) and isinstance(field.get("name"), str):
                _add(parts, field["name"])
    return parts


class _TableText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._in: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("th", "caption"):
            self._in, self._buf = tag, []

    def handle_endtag(self, tag):
        if tag == self._in:
            _add(self.parts, " ".join("".join(self._buf).split()))
            self._in = None

    def handle_data(self, data):
        if self._in:
            self._buf.append(data)


def html_text(value: Any) -> list[str]:
    if isinstance(value, list):
        value = "".join(v for v in value if isinstance(v, str))
    if not isinstance(value, str) or "<" not in value:
        return []
    parser = _TableText()
    parser.feed(value)
    parser.close()
    return parser.parts


def search_text(data: Mapping[str, Any], limit: int = SEARCH_TEXT_LIMIT) -> str | None:
    """Search text for a mime bundle, or None when the bundle has nothing worth indexing."""
    parts: list[str] = []
    try:
        items = list(data.items())
    except Exception:
        return None
    for mime, value in items:
        extractor = None
        if mime == PLOTLY:
            extractor = plotly_text
        elif mime == DATARESOURCE:
            extractor = dataresource_text
        elif mime == HTML:
            extractor = html_text
        elif isinstance(mime, str) and _VEGALITE.fullmatch(mime):
            extractor = vegalite_text
        if extractor is None:
            continue
        try:
            parts.extend(extractor(value))
        except Exception:  # bad JSON, odd shapes, a parser error: never a reason to fail a record
            continue
    text = "\n".join(parts)
    return cut(text, limit) if text.strip() else None
