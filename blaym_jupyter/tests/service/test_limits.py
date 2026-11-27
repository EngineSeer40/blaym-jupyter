"""The size limits: chat bodies, previews, searchText, the indexing limit."""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from blaym_jupyter_types import BlaymEvent, GetBlobRequest, StoreLimits
from blaym_jupyter.service.blobrefs import is_inline, items_of
from blaym_jupyter.tests.history.scenario import Scenario, output_item
from blaym_jupyter.tests.service.helpers import TOKEN, Rig

SECRET = "ghp_aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5"


def rig_with(**kw):
    return Rig(limits=StoreLimits(**kw))


def start():
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    return s


def chat_body(rig):
    return [e.payload.body for e in rig.stored() if e.type == "chat_message_added"][0]


def send_chat(rig, body, mid="m1"):
    s = start()
    s.chat(mid, body)
    return s, rig.outcomes(s.events)


def test_chat_over_the_limit_is_cut_with_a_marker():
    rig = rig_with(chatMessageMaxBytes=100)
    _, out = send_chat(rig, "a" * 500)
    assert [o.status for o in out] == ["accepted", "accepted"]
    body = chat_body(rig)
    assert len(body.encode()) <= 100 and body.endswith("bytes not stored]")
    removed = int(body.rsplit("[truncated: ", 1)[1].split(" ")[0])
    assert removed + len(body.rsplit("[truncated", 1)[0]) == 500


def inline_output(rig, text, **kw):
    s = start()
    run = s.run("c1")
    s.output(run, text)
    return s, rig.outcomes(s.events)


def output_item_of(rig):
    return [i for e in rig.stored() for i in items_of(e) if e.type == "output_added"][0]


def test_big_inline_output_moves_to_a_blob():
    rig = Rig()
    text = ("line of output\n" * 14000) + " token=" + SECRET + "\n"  # secret beyond the 16 KB cut
    s, out = inline_output(rig, text)
    assert all(o.status == "accepted" for o in out), out
    item = output_item_of(rig)
    assert len(item.preview.encode()) <= 16384 and not is_inline(item)
    blob = rig.service.get_blob(TOKEN, GetBlobRequest(explorationId=rig.exp, contentHash=item.contentHash))
    assert SECRET not in blob.decode() and "[hidden" in blob.decode()
    assert item.sizeBytes == len(blob) and item.truncated is False
    assert blob.decode().startswith(item.preview)
    assert SECRET not in item.model_dump_json()
    assert rig.blobs.exists(rig.exp, item.contentHash)
    for e in rig.stored():
        BlaymEvent.model_validate(e.model_dump(mode="json"))
    assert rig.state(rig.root, rig.stored()[-1].tick).status == "ok"
    assert [o.duplicate for o in rig.outcomes(s.events)] == [True] * len(s.events)


@settings(max_examples=8, deadline=None)
# SECRET only standalone: a key glued to word characters is a documented false negative (docs/secrets.md)
@given(text=st.lists(st.sampled_from(["a", "b", " ", "\n", "日", "😀", "é", " " + SECRET + " "]), max_size=60).map("".join),
       inline=st.integers(1, 120), chat=st.integers(1, 120), index=st.integers(1, 120))
def test_generated_texts_never_exceed_limits(text, inline, chat, index):
    rig = Rig(limits=StoreLimits(inlineOutputBytes=inline, chatMessageMaxBytes=chat, indexedTextBytesPerItem=index))
    s = start()
    run = s.run("c1")
    item = output_item(text)
    item["searchText"] = text
    s._emit("output_added", {"runId": run, "cellId": "c1", "outputIndex": 0, "item": item},
            revs=s._current_revisions())
    s.chat("m1", text)
    s.chat_update("m1", text + text)
    out = rig.outcomes(s.events)
    assert all(o.status == "accepted" for o in out), out
    for e in rig.stored():
        BlaymEvent.model_validate(e.model_dump(mode="json"))
        if e.type.startswith("chat_message"):
            assert len(e.payload.body.encode()) <= chat
            assert SECRET not in e.payload.body
        for it in items_of(e):
            assert len(it.preview.encode()) <= inline
            assert it.searchText is None or len(it.searchText.encode()) <= index
            if not is_inline(it):
                assert rig.blobs.exists(rig.exp, it.contentHash)
