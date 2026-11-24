"""The size limits of decision K: chat bodies, previews, searchText, the indexing limit (S4-6b)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from blaym_jupyter_types import BlaymEvent, GetBlobRequest, StoreLimits
from blaym_jupyter.ports.blobs import BlobNotFound
from blaym_jupyter.privacy.redact import MARKER_RE
from blaym_jupyter.service.auth import Unauthorized
from blaym_jupyter.service.blobrefs import is_inline, items_of
from blaym_jupyter.service.indexing import truncate_body
from blaym_jupyter.testing.memory import MemorySearchIndex
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


def test_chat_within_the_limit_is_unchanged_and_default_is_one_mib():
    rig = rig_with(chatMessageMaxBytes=100)
    send_chat(rig, "b" * 100)
    assert chat_body(rig) == "b" * 100
    big = Rig()
    send_chat(big, "c" * 1_048_577)
    assert len(chat_body(big).encode()) <= 1_048_576 and "[truncated:" in chat_body(big)


def test_truncation_is_idempotent_and_resend_is_a_duplicate():
    rig = rig_with(chatMessageMaxBytes=100)
    s, _ = send_chat(rig, "é" * 300)
    body = chat_body(rig)
    assert truncate_body(body, 100) == body
    again = rig.outcomes(s.events)
    assert [o.duplicate for o in again] == [True, True]


def test_secret_at_the_cut_is_hidden_and_never_split():
    for pad in range(0, 40):
        rig = rig_with(chatMessageMaxBytes=80)
        send_chat(rig, "x" * pad + " " + SECRET + " tail " * 20)
        body = chat_body(rig)
        assert SECRET not in body and "ghp_" not in body
        assert body.count("[hidden") == len(MARKER_RE.findall(body))
        assert len(body.encode()) <= 80


def test_multibyte_never_split():
    for limit in range(60, 90):
        body = truncate_body("日本語😀" * 40, limit)
        assert len(body.encode()) <= limit
        body.encode().decode()  # valid


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


def test_huge_output_blob_is_truncated_and_flagged():
    rig = rig_with(maxStoredOutputBytes=50_000, truncatedPreviewBytes=30_000)
    inline_output(rig, "z" * 200_000)
    item = output_item_of(rig)
    assert item.truncated and item.sizeBytes == 200_000 and len(item.preview.encode()) <= 16384


def test_output_within_the_limit_stays_inline():
    rig = Rig()
    inline_output(rig, "small")
    item = output_item_of(rig)
    assert is_inline(item) and item.preview == "small" and item.sizeBytes == 5
    assert rig.blobs.hashes(rig.exp) == []


def test_existing_blob_item_keeps_its_hash_and_size_and_gets_a_cut_preview():
    rig = Rig()
    put = rig.service.put_blob(TOKEN, rig.exp, b"q" * 100_000)
    s = start()
    run = s.run("c1")
    item = output_item("q" * 30_000)
    item.update(contentHash=put.contentHash, sizeBytes=put.originalBytes, truncated=False)
    s._emit("output_added", {"runId": run, "cellId": "c1", "outputIndex": 0, "item": item},
            revs=s._current_revisions())
    assert all(o.status == "accepted" for o in rig.outcomes(s.events))
    got = output_item_of(rig)
    assert got.contentHash == put.contentHash and got.sizeBytes == 100_000
    assert len(got.preview.encode()) == 16384


def test_search_text_is_cut():
    rig = rig_with(indexedTextBytesPerItem=100)
    s = start()
    run = s.run("c1")
    item = output_item("short")
    item["searchText"] = "w" * 5000
    s._emit("output_added", {"runId": run, "cellId": "c1", "outputIndex": 0, "item": item},
            revs=s._current_revisions())
    rig.outcomes(s.events)
    assert len(output_item_of(rig).searchText.encode()) <= 100


def test_indexing_limit_is_honoured_and_rebuild_equals_incremental():
    idx = MemorySearchIndex()
    rig = Rig(limits=StoreLimits(indexedTextBytesPerItem=40, chatMessageMaxBytes=500), search_index=idx)
    send_chat(rig, "needle " + "n" * 400)
    texts = lambda: sorted((d.tick, d.kind, d.text) for d in idx._docs)  # noqa: E731
    before = texts()
    assert before and all(len(t.encode()) <= 40 for _, _, t in before)
    assert rig.service.rebuild_search(TOKEN, rig.exp) == len(before)
    assert texts() == before


def test_info_and_get_blob():
    rig = Rig()
    info = rig.service.info(TOKEN)
    assert info.apiVersion.root == 1 if hasattr(info.apiVersion, "root") else info.apiVersion == 1
    assert info.schemaVersion == 2
    put = rig.service.put_blob(TOKEN, rig.exp, b"hello")
    req = GetBlobRequest(explorationId=rig.exp, contentHash=put.contentHash)
    assert rig.service.get_blob(TOKEN, req) == b"hello"
    with pytest.raises(Unauthorized):
        rig.service.get_blob("bad", req)
    with pytest.raises(Unauthorized):
        rig.service.info("bad")
    with pytest.raises(BlobNotFound):
        rig.service.get_blob(TOKEN, GetBlobRequest(explorationId=rig.exp, contentHash="sha256:" + "0" * 64))


@settings(max_examples=40, deadline=None)
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
