"""The blob store contract, run against the in-memory and the file implementation (S4, Task 4)."""

from __future__ import annotations

import hashlib

import pytest
from hypothesis import given, strategies as st

import blaym_jupyter.tests.history.conftest as _profiles  # noqa: F401

from blaym_jupyter.ports.blobs import BlobLimits, BlobNotFound
from blaym_jupyter.store.blobs import FileBlobStore
from blaym_jupyter.testing.memory import MemoryBlobStore

EX = "exploration-1"
SMALL = BlobLimits(inline_bytes=4, max_stored_bytes=100, truncated_preview_bytes=10)


def _make(kind, path):
    return MemoryBlobStore() if kind == "memory" else FileBlobStore(path)


@pytest.fixture(params=("memory", "file"))
def blobs(request, tmp_path):
    store = _make(request.param, tmp_path)
    yield store
    store.close()


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_put_and_read_round_trip(blobs):
    r = blobs.put(EX, b"hello")
    assert (r.contentHash, r.storedBytes, r.originalBytes, r.truncated) == (sha(b"hello"), 5, 5, False)
    assert blobs.read(EX, r.contentHash) == b"hello"
    assert blobs.exists(EX, r.contentHash)


def test_empty_content(blobs):
    r = blobs.put(EX, b"")
    assert blobs.read(EX, r.contentHash) == b""
    assert r.storedBytes == 0 and not r.truncated


def test_unknown_hash(blobs):
    assert not blobs.exists(EX, sha(b"nothing"))
    with pytest.raises(BlobNotFound):
        blobs.read(EX, sha(b"nothing"))
    for bad in ("", "sha256:xyz", "../../etc/passwd", sha(b"x").upper()):
        assert not blobs.exists(EX, bad)
        with pytest.raises(BlobNotFound):
            blobs.read(EX, bad)


def test_blobs_belong_to_one_exploration(blobs):
    r = blobs.put(EX, b"mine")
    assert not blobs.exists("other", r.contentHash)
    with pytest.raises(BlobNotFound):
        blobs.read("other", r.contentHash)


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "..", ".", "a\\b", "/abs"])
def test_exploration_id_is_never_a_path(blobs, bad):
    with pytest.raises(ValueError):
        blobs.put(bad, b"x")


def test_same_content_is_one_blob(blobs):
    a = blobs.put(EX, b"same")
    b = blobs.put(EX, [b"sa", b"me"])  # a stream of the same bytes
    assert a.contentHash == b.contentHash
    assert blobs.read(EX, a.contentHash) == b"same"


def test_truncation_marks_and_keeps_preview(blobs):
    data = b"0123456789" * 11  # 110 > 100
    r = blobs.put(EX, data, SMALL)
    assert r.truncated and r.originalBytes == 110 and r.storedBytes == 10
    assert blobs.read(EX, r.contentHash) == data[:10]
    assert r.contentHash == sha(data[:10])  # the hash is of the stored bytes


def test_exactly_at_the_limit_is_not_truncated(blobs):
    r = blobs.put(EX, b"a" * 100, SMALL)
    assert not r.truncated and r.storedBytes == 100
    r = blobs.put(EX, b"a" * 101, SMALL)
    assert r.truncated and r.storedBytes == 10


def test_truncation_does_not_split_a_character(blobs):
    data = ("é" * 60).encode()  # 120 bytes, 2 per character
    r = blobs.put(EX, data, BlobLimits(max_stored_bytes=100, truncated_preview_bytes=11))
    stored = blobs.read(EX, r.contentHash)
    assert r.truncated and stored.decode() == "é" * 5 and r.storedBytes == 10


def test_binary_is_not_redacted_and_is_truncated_by_bytes(blobs):
    seen = []
    data = bytes([0xFF, 0xFE, 0x00]) * 50
    r = blobs.put(EX, data, SMALL, redact=lambda t: seen.append(t) or t)
    assert seen == [] and r.truncated and blobs.read(EX, r.contentHash) == data[:10]


def test_redact_is_applied_before_hashing(blobs):
    r = blobs.put(EX, b"key=SECRET ok", redact=lambda t: t.replace("SECRET", "***"))
    assert r.contentHash == sha(b"key=*** ok")
    assert blobs.read(EX, r.contentHash) == b"key=*** ok"
    # the original never becomes readable by its own hash
    assert not blobs.exists(EX, sha(b"key=SECRET ok"))
    assert r.originalBytes == len(b"key=SECRET ok") and r.storedBytes == len(b"key=*** ok")


def test_redact_is_applied_before_truncation(blobs):
    # the secret straddles the preview boundary: cutting first would leave a partial secret
    data = b"aaaaaaSECRETbbbbbbbbbb" + b"z" * 100
    r = blobs.put(EX, data, SMALL, redact=lambda t: t.replace("SECRET", "#"))
    assert blobs.read(EX, r.contentHash) == b"aaaaaa#bbb"
    assert b"SEC" not in blobs.read(EX, r.contentHash)


def test_redaction_that_grows_text_past_the_limit_truncates(blobs):
    r = blobs.put(EX, b"x" * 50, SMALL, redact=lambda t: t * 3)
    assert r.truncated and r.storedBytes == 10 and r.originalBytes == 50


def test_redact_is_idempotent_for_a_retry(blobs):
    redact = lambda t: t.replace("SECRET", "***")  # noqa: E731
    a = blobs.put(EX, b"SECRET", redact=redact)
    b = blobs.put(EX, blobs.read(EX, a.contentHash), redact=redact)
    assert a.contentHash == b.contentHash


def test_read_returns_the_stored_bytes_not_the_original(blobs):
    r = blobs.put(EX, b"abcdefghij" * 20, SMALL)
    assert blobs.read(EX, r.contentHash) == b"abcdefghij"


@given(
    data=st.binary(max_size=300),
    inline=st.integers(0, 50),
    max_stored=st.integers(0, 200),
    preview=st.integers(0, 120),
)
def test_round_trip_truncation_and_idempotence(data, inline, max_stored, preview):
    import tempfile

    limits = BlobLimits(inline, max_stored, preview)
    with tempfile.TemporaryDirectory() as d:
        for kind in ("memory", "file"):
            store = _make(kind, d)
            try:
                r = store.put(EX, data, limits)
                again = store.put(EX, [data[:7], data[7:]], limits)
                assert again == r  # idempotent, stream or not
                stored = store.read(EX, r.contentHash)
                assert r.originalBytes == len(data) and r.storedBytes == len(stored)
                assert r.contentHash == sha(stored)
                if r.truncated:
                    assert len(data) > max_stored and len(stored) <= preview
                else:
                    assert len(data) <= max_stored and stored == data  # round trip
            finally:
                store.close()


def test_redaction_sees_a_secret_split_across_stream_chunks(blobs):
    chunks = [b"key=SEC", b"RET and more"]
    r = blobs.put(EX, iter(chunks), redact=lambda t: t.replace("SECRET", "***"))
    assert blobs.read(EX, r.contentHash) == b"key=*** and more"


@pytest.mark.parametrize("bad", ["sha256:../../x", "sha256:" + "0" * 63 + "/", "/etc/passwd", "sha256:" + "0" * 64 + "\n"])
def test_malformed_hashes_are_unknown_never_paths(blobs, bad):
    blobs.put(EX, b"x")
    assert not blobs.exists(EX, bad)
    with pytest.raises(BlobNotFound):
        blobs.read(EX, bad)


def test_overlong_exploration_id_is_a_value_error(blobs):
    with pytest.raises(ValueError):
        blobs.put("a" * 300, b"x")


def test_exploration_id_length_boundary(blobs):
    ok = "a" * 128
    r = blobs.put(ok, b"x")
    assert blobs.read(ok, r.contentHash) == b"x"
    with pytest.raises(ValueError):
        blobs.put("a" * 129, b"x")


# -- housekeeping (Task 9) ---------------------------------------------------------


def _plant_debris(store, tmp_path):
    """Plant what a crash can leave; only the file store has any (memory has no files)."""
    if not isinstance(store, FileBlobStore):
        return 0, None
    base = tmp_path / "explorations" / EX
    for name in ("op-1", "op-2"):
        (base / "staging" / name).mkdir(parents=True)
        (base / "staging" / name / "half").write_bytes(b"partial")
    data = b"orphan content"
    stray = base / "blobs" / hashlib.sha256(data).hexdigest()
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"not even gzip")
    return 2, "sha256:" + stray.name


def test_cleanup_staging_removes_leftovers_and_keeps_blobs(blobs, tmp_path):
    kept = blobs.put(EX, b"keep me")
    planted, _ = _plant_debris(blobs, tmp_path)
    assert blobs.cleanup_staging() == planted
    assert blobs.cleanup_staging() == 0
    assert blobs.read(EX, kept.contentHash) == b"keep me"
    if planted:
        assert not list((tmp_path / "explorations" / EX / "staging").iterdir())


def test_orphans_lists_files_without_a_record_and_deletes_nothing(blobs, tmp_path):
    kept = blobs.put(EX, b"has a row")
    _, stray = _plant_debris(blobs, tmp_path)
    assert blobs.orphans(EX) == ([stray] if stray else [])
    assert blobs.orphans(EX) == ([stray] if stray else [])  # reported again: never deleted
    assert kept.contentHash not in blobs.orphans(EX)
    assert blobs.orphans("never-used") == []
    assert blobs.hashes(EX) == [kept.contentHash]
    assert blobs.hashes("never-used") == []
