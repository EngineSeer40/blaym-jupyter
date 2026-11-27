"""The blob store contract (file implementation). The in-memory store is checked by the tests that use it."""

from __future__ import annotations

import hashlib
import tempfile

import pytest
from hypothesis import given, strategies as st

import blaym_notebook.tests.history.conftest as _profiles  # noqa: F401

from blaym_notebook.ports.blobs import BlobLimits, BlobNotFound
from blaym_notebook.store.blobs import FileBlobStore

EX = "exploration-1"
SMALL = BlobLimits(inline_bytes=4, max_stored_bytes=100, truncated_preview_bytes=10)


@pytest.fixture
def blobs(tmp_path):
    store = FileBlobStore(tmp_path)
    yield store
    store.close()


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_round_trip_dedupe_unknown_hashes_and_ids_that_are_never_paths(blobs):
    r = blobs.put(EX, b"hello")
    assert (r.contentHash, r.storedBytes, r.originalBytes, r.truncated) == (sha(b"hello"), 5, 5, False)
    assert blobs.read(EX, r.contentHash) == b"hello" and blobs.exists(EX, r.contentHash)
    assert blobs.put(EX, [b"he", b"llo"]).contentHash == r.contentHash
    assert blobs.read(EX, blobs.put(EX, b"").contentHash) == b""
    assert not blobs.exists("other", r.contentHash)  # blobs belong to one exploration
    for bad in ("", "sha256:xyz", "../../etc/passwd", sha(b"x").upper(), "sha256:" + "0" * 64 + "\n", sha(b"nothing")):
        assert not blobs.exists(EX, bad)
        with pytest.raises(BlobNotFound):
            blobs.read(EX, bad)
    for bad in ("", "../x", "a/b", "..", "/abs", "a\\b", "a" * 129):
        with pytest.raises(ValueError):
            blobs.put(bad, b"x")
    assert blobs.read("a" * 128, blobs.put("a" * 128, b"x").contentHash) == b"x"


def test_redaction_runs_before_hashing_and_truncation_and_truncation_marks_a_preview(blobs):
    data = b"0123456789" * 10
    assert not blobs.put(EX, data, SMALL).truncated  # exactly at the limit
    r = blobs.put(EX, data + b"!", SMALL)
    assert r.truncated and r.originalBytes == 101 and r.storedBytes == 10
    assert blobs.read(EX, r.contentHash) == data[:10] and r.contentHash == sha(data[:10])
    two_byte = ("é" * 60).encode()  # a character is never split
    r = blobs.put(EX, two_byte, BlobLimits(max_stored_bytes=100, truncated_preview_bytes=11))
    assert blobs.read(EX, r.contentHash).decode() == "é" * 5
    seen = []  # binary is not redacted
    r = blobs.put(EX, bytes([0xFF, 0xFE, 0x00]) * 50, SMALL, redact=lambda t: seen.append(t) or t)
    assert seen == [] and r.truncated
    r = blobs.put(EX, b"key=SECRET ok", redact=lambda t: t.replace("SECRET", "***"))
    assert r.contentHash == sha(b"key=*** ok") and not blobs.exists(EX, sha(b"key=SECRET ok"))
    assert r.originalBytes == len(b"key=SECRET ok")
    # a secret straddling the preview boundary or split across chunks never survives
    r = blobs.put(EX, b"aaaaaaSECRETbbbbbbbbbb" + b"z" * 100, SMALL, redact=lambda t: t.replace("SECRET", "#"))
    assert blobs.read(EX, r.contentHash) == b"aaaaaa#bbb"
    r = blobs.put(EX, iter([b"key=SEC", b"RET and more"]), redact=lambda t: t.replace("SECRET", "***"))
    assert blobs.read(EX, r.contentHash) == b"key=*** and more"


@given(data=st.binary(max_size=300), inline=st.integers(0, 50), max_stored=st.integers(0, 200),
       preview=st.integers(0, 120))
def test_round_trip_truncation_and_idempotence(data, inline, max_stored, preview):
    limits = BlobLimits(inline, max_stored, preview)
    with tempfile.TemporaryDirectory() as d:
        store = FileBlobStore(d)
        try:
            r = store.put(EX, data, limits)
            assert store.put(EX, [data[:7], data[7:]], limits) == r  # idempotent, stream or not
            stored = store.read(EX, r.contentHash)
            assert r.originalBytes == len(data) and r.storedBytes == len(stored) and r.contentHash == sha(stored)
            if r.truncated:
                assert len(data) > max_stored and len(stored) <= preview
            else:
                assert len(data) <= max_stored and stored == data
        finally:
            store.close()


def test_housekeeping_removes_staging_leftovers_and_lists_orphans_without_deleting(blobs, tmp_path):
    kept = blobs.put(EX, b"keep me")
    base = tmp_path / "explorations" / EX
    for name in ("op-1", "op-2"):
        (base / "staging" / name).mkdir(parents=True)
        (base / "staging" / name / "half").write_bytes(b"partial")
    stray = base / "blobs" / hashlib.sha256(b"orphan content").hexdigest()
    stray.write_bytes(b"no row for me")
    assert blobs.cleanup_staging() == 2 and blobs.cleanup_staging() == 0
    assert blobs.read(EX, kept.contentHash) == b"keep me"
    assert blobs.orphans(EX) == ["sha256:" + stray.name] == blobs.orphans(EX) and stray.exists()
    assert blobs.hashes(EX) == [kept.contentHash] and blobs.orphans("never-used") == []
