"""Raw blob governance is one row per namespace and digest, written by the first uploader.

Before the fix, a key that uploaded bytes another key had already stored privately got 201 and
then 404 on reading them back, which told it those bytes existed there; on the Python store a
public bucket object holding such bytes was unreadable to everyone but the first uploader.
docs/raw-blob-governance.md records what is fixed and what still needs a decision. The Worker
has the same checks in tests/workerd_blob_first_uploader.mjs. Synthetic keys and bytes only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tessera import BucketConfig, Store
from tessera.access import ServiceSettings
from tessera.errors import NotFound
from tessera.service import create_app

SHARED = b"SYNTHETIC bytes that two keys both hold"


def _headers(key: str, governance: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"authorization": f"Bearer {key}"}
    if governance is not None:
        headers["x-tessera-governance"] = json.dumps(governance)
    return headers


def test_a_later_uploader_reads_back_bytes_another_key_stored_privately(tmp_path: Path) -> None:
    db_path = tmp_path / "first-uploader.db"
    store = Store(db_path)
    first, _ = store.issue_key("synthetic-first", queues=["none"], scopes=["store"])
    later, _ = store.issue_key("synthetic-later", queues=["none"], scopes=["store"])
    other, _ = store.issue_key("synthetic-other", queues=["none"], scopes=["store"])
    app = create_app(store, ServiceSettings(db_path=str(db_path), reaper_seconds=3600))
    digest = hashlib.sha256(SHARED).hexdigest()
    private = {"visibility_scope": "private"}

    with TestClient(app) as client:
        assert client.post(
            "/v1/blobs", content=SHARED, headers=_headers(first, private)
        ).json() == {
            "sha256": digest,
            "size": len(SHARED),
        }
        stored = client.post("/v1/blobs", content=SHARED, headers=_headers(later, private))
        assert stored.status_code == 201
        assert stored.json() == {"sha256": digest, "size": len(SHARED)}

        # The 201-then-404 pair revealed that another key held these bytes privately.
        read_back = client.get(f"/v1/blobs/{digest}", headers=_headers(later))
        assert read_back.status_code == 200
        assert read_back.content == SHARED
        assert client.get(f"/v1/blobs/{digest}", headers=_headers(first)).content == SHARED

        # Nobody else gains access, and an upload grants no access to other bytes.
        assert client.get(f"/v1/blobs/{digest}", headers=_headers(other)).status_code == 404
        mine = client.post("/v1/blobs", content=b"synthetic other bytes", headers=_headers(other))
        assert mine.status_code == 201
        assert client.get(f"/v1/blobs/{digest}", headers=_headers(other)).status_code == 404


def test_an_object_is_governed_by_its_acl_not_by_who_stored_its_bytes_first(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "objects.db")
    try:
        store.define_bucket(BucketConfig(name="nouns"))
        with store.scope_context(key_id="synthetic-first"):
            digest = store.put_blob(SHARED, visibility_scope="private")
        with store.scope_context(key_id="synthetic-writer"):
            store.put_object("nouns", "shared.txt", SHARED)
            assert store.get_object("nouns", "shared.txt")[1] == SHARED
            assert store.get_blob(digest) == SHARED
        with store.scope_context(key_id="synthetic-reader"):
            meta, data = store.get_object("nouns", "shared.txt")
            assert (meta.sha256, data) == (digest, SHARED)
            # Raw reads still follow the blob's own row: this key uploaded nothing.
            with pytest.raises(NotFound):
                store.get_blob(digest)
    finally:
        store.close()


def test_quarantine_and_private_objects_still_hide_bytes_from_keys_that_never_held_them(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "unchanged.db")
    try:
        store.define_bucket(BucketConfig(name="nouns"))
        with store.scope_context(key_id="synthetic-owner", surface="eranos"):
            store.put_object(
                "nouns", "held.txt", SHARED, visibility_scope="private", tags=["status:quarantined"]
            )
        digest = hashlib.sha256(SHARED).hexdigest()
        with store.scope_context(key_id="synthetic-reader", surface="eranos"):
            with pytest.raises(NotFound):
                store.get_object("nouns", "held.txt")
            with pytest.raises(NotFound):
                store.get_blob(digest)
    finally:
        store.close()
