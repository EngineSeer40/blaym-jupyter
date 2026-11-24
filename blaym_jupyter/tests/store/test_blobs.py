"""File specifics of the blob store (S4, Task 4): write ordering, damage, memory, concurrency."""

from __future__ import annotations

import gzip
import hashlib
import os
import threading
import tracemalloc
from pathlib import Path

import pytest

from blaym_jupyter.ports.blobs import BlobCorrupt, BlobLimits, BlobNotFound
from blaym_jupyter.store import blobs as blobs_module
from blaym_jupyter.store.blobs import FileBlobStore

EX = "exp-1"


@pytest.fixture
def store(tmp_path):
    s = FileBlobStore(tmp_path)
    yield s
    s.close()


def rows(store):
    return store._conn.execute("SELECT hash FROM blobs").fetchall()


def files(tmp_path, kind="blobs"):
    return [p for p in (tmp_path / "explorations" / EX / kind).rglob("*") if p.is_file()] if (
        tmp_path / "explorations" / EX / kind
    ).exists() else []


def hex_of(h: str) -> str:
    return h.split(":", 1)[1]


def test_file_is_named_by_the_hash_and_is_gzip(store, tmp_path):
    r = store.put(EX, b"hello world")
    path = tmp_path / "explorations" / EX / "blobs" / hex_of(r.contentHash)
    assert path.is_file()
    assert gzip.decompress(path.read_bytes()) == b"hello world"
    assert hex_of(r.contentHash) == hashlib.sha256(b"hello world").hexdigest()
    assert files(tmp_path, "staging") == []  # staging is cleaned after a good put


def test_same_content_is_one_file_and_one_row(store, tmp_path):
    for _ in range(3):
        store.put(EX, b"same")
    assert len(files(tmp_path)) == 1 and len(rows(store)) == 1


def test_a_put_before_any_event_is_allowed_with_foreign_keys_on(store):
    assert store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert store.put("brand-new", b"x").storedBytes == 1
    assert len(rows(store)) == 1


def test_event_store_still_enforces_its_foreign_keys(store):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO events(exploration_id, tick, event_id, branch_id, wall_time_ms, type,"
            " user_id, workspace_id, body) VALUES ('nope',1,'e1','main',0,'t','u','w','{}')"
        )


def test_gzip_level_comes_from_limits(tmp_path):
    data = (b"abc def " * 5000)
    sizes = {}
    for level in (1, 9):
        s = FileBlobStore(tmp_path / str(level))
        r = s.put(EX, data, BlobLimits(gzip_level=level))
        sizes[level] = (tmp_path / str(level) / "explorations" / EX / "blobs" / hex_of(r.contentHash)).stat().st_size
        s.close()
    assert sizes[9] <= sizes[1]
    with pytest.raises(ValueError):
        BlobLimits(gzip_level=0)


def test_failure_between_staging_and_replace_leaves_only_staging_debris(store, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk went away")

    monkeypatch.setattr(blobs_module.os, "replace", boom)
    with pytest.raises(OSError):
        store.put(EX, b"data")
    assert rows(store) == []
    assert files(tmp_path) == []
    assert len(files(tmp_path, "staging")) == 1  # debris, for housekeeping (Task 9)
    monkeypatch.undo()
    # a leftover staging folder does not affect a later put
    r = store.put(EX, b"data")
    assert store.read(EX, r.contentHash) == b"data"
    assert len(rows(store)) == 1 and len(files(tmp_path)) == 1


def test_failure_between_replace_and_row_leaves_an_orphan_file_not_a_row(store, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("crash before the row")

    monkeypatch.setattr(store, "_insert_row", boom)
    with pytest.raises(RuntimeError):
        store.put(EX, b"data")
    assert rows(store) == []  # never a row without a file
    assert len(files(tmp_path)) == 1  # an orphan file is allowed
    monkeypatch.undo()
    h = "sha256:" + hashlib.sha256(b"data").hexdigest()
    assert not store.exists(EX, h)  # no row: not a stored blob yet
    with pytest.raises(BlobNotFound):
        store.read(EX, h)
    # a later put of the same content adopts the orphan: one file, one row
    r = store.put(EX, b"data")
    assert r.contentHash == h and store.read(EX, h) == b"data"
    assert len(rows(store)) == 1 and len(files(tmp_path)) == 1


def test_a_damaged_orphan_is_rewritten_by_a_later_put(store, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_insert_row", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        store.put(EX, b"data")
    monkeypatch.undo()
    files(tmp_path)[0].write_bytes(b"garbage")
    r = store.put(EX, b"data")
    assert store.read(EX, r.contentHash) == b"data"


def test_row_without_file_is_reported_on_read_and_repaired_by_put(store, tmp_path):
    r = store.put(EX, b"data")
    files(tmp_path)[0].unlink()
    assert not store.exists(EX, r.contentHash)
    with pytest.raises(BlobCorrupt):
        store.read(EX, r.contentHash)
    store.put(EX, b"data")
    assert store.read(EX, r.contentHash) == b"data"


@pytest.mark.parametrize("damage", ["flip", "truncate", "garbage", "other_content"])
def test_damaged_file_is_detected_on_read(store, tmp_path, damage):
    r = store.put(EX, os.urandom(4000))
    path = files(tmp_path)[0]
    raw = path.read_bytes()
    if damage == "flip":
        mid = len(raw) // 2
        raw = raw[:mid] + bytes([raw[mid] ^ 0xFF]) + raw[mid + 1:]
    elif damage == "truncate":
        raw = raw[: len(raw) // 2]
    elif damage == "garbage":
        raw = b"not gzip at all"
    else:  # a well-formed gzip of different content
        raw = gzip.compress(b"someone else's bytes")
    path.write_bytes(raw)
    with pytest.raises(BlobCorrupt):
        store.read(EX, r.contentHash)


def test_survives_reopen(tmp_path):
    s = FileBlobStore(tmp_path)
    r = s.put(EX, b"kept")
    s.close()
    s = FileBlobStore(tmp_path)
    assert s.read(EX, r.contentHash) == b"kept"
    s.close()


def test_concurrent_puts_of_the_same_content_give_one_file(store, tmp_path):
    data = os.urandom(50_000)
    results, errors = [], []
    barrier = threading.Barrier(8)

    def work():
        try:
            barrier.wait()
            results.append(store.put(EX, data))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len({r.contentHash for r in results}) == 1
    assert len(files(tmp_path)) == 1 and len(rows(store)) == 1
    assert store.read(EX, results[0].contentHash) == data


def _stream(total, chunk=1 << 16):
    block = (b"line of text that is not a secret\n" * 2000)[:chunk]
    sent = 0
    while sent < total:
        n = min(chunk, total - sent)
        yield block[:n]
        sent += n


def _peak(store, total, limits, redact=None):
    tracemalloc.start()
    try:
        r = store.put(EX, _stream(total), limits, redact)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return r, peak


def test_bounded_memory_on_a_100_mb_stream(store):
    limits = BlobLimits(max_stored_bytes=1 << 20, truncated_preview_bytes=1 << 18)
    r, peak = _peak(store, 100 * 1024 * 1024, limits)
    assert r.truncated and r.originalBytes == 100 * 1024 * 1024 and r.storedBytes == 1 << 18
    assert peak < 8 * (1 << 20), peak  # about max_stored plus a few chunks, nowhere near 100 MB


def test_bounded_memory_with_default_limits_and_redaction(store):
    r, peak = _peak(store, 100 * 1024 * 1024, BlobLimits(), redact=lambda t: t.replace("secret", "***"))
    assert r.truncated and r.storedBytes <= 262144
    assert peak < 60 * (1 << 20), peak  # the 10 MB window, a few copies, not the 100 MB input


def test_unsafe_exploration_ids_never_touch_the_disk(store, tmp_path):
    with pytest.raises(ValueError):
        store.put("../escape", b"x")
    assert not (tmp_path / "escape").exists()


def test_staged_file_is_fsynced_and_verified_before_it_is_moved(store, tmp_path, monkeypatch):
    real = os.fsync
    calls = []

    def spy(fd):
        calls.append(fd)
        return real(fd)

    monkeypatch.setattr(blobs_module.os, "fsync", spy)
    store.put(EX, b"data")
    assert len(calls) >= 2  # the staged file and the blobs folder


def test_a_staged_file_that_does_not_verify_is_never_moved_or_referenced(store, tmp_path, monkeypatch):
    real = os.fsync

    def damaging_fsync(fd):  # simulates a bad write: the bytes on disk are not what was written
        real(fd)
        p = Path(os.readlink(f"/proc/self/fd/{fd}")) if os.path.exists(f"/proc/self/fd/{fd}") else None
        if p is not None and p.parent.parent.name == "staging":
            p.write_bytes(b"damaged")

    monkeypatch.setattr(blobs_module.os, "fsync", damaging_fsync)
    with pytest.raises(BlobCorrupt):
        store.put(EX, b"data")
    assert rows(store) == [] and files(tmp_path) == []


def test_a_row_whose_file_is_gone_is_re_inserted_without_error(store, tmp_path):
    r = store.put(EX, b"data")
    files(tmp_path)[0].unlink()
    store.put(EX, b"data")  # the row exists already: no integrity error
    assert len(rows(store)) == 1 and store.read(EX, r.contentHash) == b"data"


def test_two_store_instances_on_one_root_keep_one_file_and_row_per_content(tmp_path):
    a, b = FileBlobStore(tmp_path), FileBlobStore(tmp_path)
    contents = [os.urandom(30_000) for _ in range(3)]
    results, errors = [], []
    barrier = threading.Barrier(12)

    def work(k):
        try:
            barrier.wait()
            results.append((contents[k % 3], (a if k % 2 else b).put(EX, contents[k % 3])))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(k,)) for k in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len(files(tmp_path)) == 3 and len(rows(a)) == 3
    for data, r in results:
        assert a.read(EX, r.contentHash) == data
    a.close()
    b.close()
