"""File specifics of the blob store: layout, damage and concurrency."""

from __future__ import annotations

import gzip
import hashlib
import os
import threading

import pytest

from blaym_jupyter.ports.blobs import BlobCorrupt
from blaym_jupyter.store.blobs import FileBlobStore

EX = "exp-1"


def files(tmp_path, kind="blobs"):
    base = tmp_path / "explorations" / EX / kind
    return [p for p in base.rglob("*") if p.is_file()] if base.exists() else []


def rows(store):
    return store._conn.execute("SELECT hash FROM blobs").fetchall()


def test_a_blob_is_a_gzip_file_named_by_its_hash_and_damage_or_a_lost_file_is_detected(tmp_path):
    store = FileBlobStore(tmp_path)
    try:
        data = os.urandom(4000)
        r = store.put(EX, data)
        (path,) = files(tmp_path)
        assert path.name == hashlib.sha256(data).hexdigest() == r.contentHash.split(":")[1]
        assert gzip.decompress(path.read_bytes()) == data and files(tmp_path, "staging") == []
        for _ in range(3):
            store.put(EX, data)
        assert len(files(tmp_path)) == 1 and len(rows(store)) == 1
        raw = path.read_bytes()
        for damaged in (raw[: len(raw) // 2] + bytes([raw[len(raw) // 2] ^ 0xFF]) + raw[len(raw) // 2 + 1:], raw[: len(raw) // 2], b"not gzip",
                        gzip.compress(b"someone else's bytes")):
            path.write_bytes(damaged)
            with pytest.raises(BlobCorrupt):
                store.read(EX, r.contentHash)
        path.unlink()  # a row without a file is reported on read and repaired by the next put
        assert not store.exists(EX, r.contentHash)
        with pytest.raises(BlobCorrupt):
            store.read(EX, r.contentHash)
        store.put(EX, data)
        assert store.read(EX, r.contentHash) == data
    finally:
        store.close()
    store = FileBlobStore(tmp_path)  # and it survives a restart
    try:
        assert store.read(EX, r.contentHash) == data
    finally:
        store.close()


def test_two_store_instances_putting_the_same_content_at_once_keep_one_file_and_row_each(tmp_path):
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
    try:
        assert not errors and len(files(tmp_path)) == 3 and len(rows(a)) == 3
        for data, r in results:
            assert a.read(EX, r.contentHash) == data
    finally:
        a.close()
        b.close()
