"""Search text extractors (S5, Task 5): content, limits, and never raising."""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from blaym_jupyter.recording.searchtext import (
    DATARESOURCE, HTML, PLOTLY, dataresource_text, html_text, plotly_text, search_text, vegalite_text,
)

VEGA = "application/vnd.vegalite.v5+json"


def test_plotly_titles_axes_and_series():
    fig = {"data": [{"name": "A", "type": "scatter"}, {"type": "bar"}, {"name": "B"}],
           "layout": {"title": "Main", "xaxis": {"title": {"text": "time"}}, "yaxis2": {"title": "rate"},
                      "scene": {"zaxis": {"title": "depth"}}}}
    assert plotly_text(fig) == ["Main", "time", "rate", "depth", "A", "B"]
    assert plotly_text(json.dumps(fig)) == plotly_text(fig)  # a JSON string is read too


def test_vegalite_title_and_fields_once_each():
    spec = {"title": {"text": "T"}, "layer": [
        {"encoding": {"x": {"field": "a"}, "y": {"field": "b", "title": "B axis"}}},
        {"encoding": {"x": {"field": "a"}}}]}
    assert vegalite_text(spec) == ["T", "a", "b", "B axis"]
    assert vegalite_text({"title": ["line 1", "line 2"]}) == ["line 1", "line 2"]


def test_dataresource_columns():
    spec = {"schema": {"fields": [{"name": "x"}, {"type": "no name"}, {"name": "y"}]}, "data": []}
    assert dataresource_text(spec) == ["x", "y"]


def test_html_tables():
    html = "<table><caption>C</caption><tr><th>a</th><th><b>b</b> c</th></tr><tr><td>cell</td></tr></table>"
    assert html_text(html) == ["C", "a", "b c"]
    assert html_text(["<table><th>x</th>", "</table>"]) == ["x"]
    assert html_text("plain words, no tags") == []
    assert html_text("<th>unclosed") == []


def test_whole_bundle_combines_and_ignores_the_rest():
    data = {"text/plain": "ignored", PLOTLY: {"layout": {"title": "P"}}, HTML: "<th>h</th>", "image/png": "AAA"}
    assert search_text(data) == "P\nh"
    assert search_text({"text/plain": "x"}) is None
    assert search_text({DATARESOURCE: {"schema": {"fields": [{"name": "c"}]}}}) == "c"
    assert search_text({VEGA: {"title": "V"}}) == "V"


def test_limit_cuts_on_a_character_boundary():
    out = search_text({DATARESOURCE: {"schema": {"fields": [{"name": "é" * 20}]}}}, limit=7)
    assert out == "é" * 3 and len(out.encode()) <= 7


@pytest.mark.parametrize("bad", ["{not json", "", "null", "[]", "42", None, b"\xff\xfe", {"layout": 3, "data": "x"},
                                 {"data": [1, None, {"name": 5}], "layout": {"xaxis": 7, "title": {"text": 1}}}])
def test_bad_input_never_raises(bad):
    for mime in (PLOTLY, VEGA, DATARESOURCE, HTML):
        search_text({mime: bad})  # no exception


def test_deeply_nested_vega_does_not_blow_the_stack():
    deep: dict = {}
    node = deep
    for _ in range(5000):
        node["a"] = {}
        node = node["a"]
    node["title"] = "bottom"
    assert vegalite_text(deep) == []  # beyond the depth bound: nothing, no RecursionError


json_leaf = st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=20))
json_any = st.recursive(json_leaf, lambda c: st.one_of(st.lists(c, max_size=4), st.dictionaries(st.text(max_size=8), c, max_size=4)), max_leaves=30)


@settings(max_examples=150, deadline=None)
@given(json_any, st.sampled_from([PLOTLY, VEGA, DATARESOURCE, HTML]))
def test_random_json_never_raises(value, mime):
    out = search_text({mime: value})
    assert out is None or isinstance(out, str)
