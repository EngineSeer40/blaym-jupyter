"""Search text extractors: content, limits, and never raising."""

from __future__ import annotations

import json

from blaym_notebook.recording.searchtext import (
    DATARESOURCE, HTML, PLOTLY, dataresource_text, html_text, plotly_text, search_text, vegalite_text,
)

VEGA = "application/vnd.vegalite.v5+json"


def test_search_text_comes_from_plotly_vega_tables_and_dataresource_and_the_bundle_combines_it():
    fig = {"data": [{"name": "A", "type": "scatter"}, {"type": "bar"}, {"name": "B"}],
           "layout": {"title": "Main", "xaxis": {"title": {"text": "time"}}, "yaxis2": {"title": "rate"},
                      "scene": {"zaxis": {"title": "depth"}}}}
    assert plotly_text(fig) == ["Main", "time", "rate", "depth", "A", "B"]
    assert plotly_text(json.dumps(fig)) == plotly_text(fig)  # a JSON string is read too
    spec = {"title": {"text": "T"}, "layer": [
        {"encoding": {"x": {"field": "a"}, "y": {"field": "b", "title": "B axis"}}},
        {"encoding": {"x": {"field": "a"}}}]}
    assert vegalite_text(spec) == ["T", "a", "b", "B axis"]
    assert vegalite_text({"title": ["line 1", "line 2"]}) == ["line 1", "line 2"]
    spec = {"schema": {"fields": [{"name": "x"}, {"type": "no name"}, {"name": "y"}]}, "data": []}
    assert dataresource_text(spec) == ["x", "y"]
    html = "<table><caption>C</caption><tr><th>a</th><th><b>b</b> c</th></tr><tr><td>cell</td></tr></table>"
    assert html_text(html) == ["C", "a", "b c"]
    assert html_text(["<table><th>x</th>", "</table>"]) == ["x"]
    assert html_text("plain words, no tags") == []
    assert html_text("<th>unclosed") == []
    data = {"text/plain": "ignored", PLOTLY: {"layout": {"title": "P"}}, HTML: "<th>h</th>", "image/png": "AAA"}
    assert search_text(data) == "P\nh"
    assert search_text({"text/plain": "x"}) is None
    out = search_text({DATARESOURCE: {"schema": {"fields": [{"name": "é" * 20}]}}}, limit=7)
    assert out == "é" * 3 and len(out.encode()) <= 7  # cut on a character boundary


def test_bad_input_never_raises():
    bad = ["{not json", "", "null", "[]", "42", None, b"\xff\xfe", {"layout": 3, "data": "x"},
           {"data": [1, None, {"name": 5}], "layout": {"xaxis": 7, "title": {"text": 1}}}]
    for value in bad:
        for mime in (PLOTLY, VEGA, DATARESOURCE, HTML):
            search_text({mime: value})  # no exception
