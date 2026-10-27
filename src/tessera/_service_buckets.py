"""HTTP routes for queryable noun stores, persistent named file buckets, and remote key management.

Mounted by `tessera.service.create_app` behind the same `Gate` (`store` scope for stores/buckets,
`admin` scope for key management) and POST-only write rules.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from tessera._service_intelligence import slice_bytes_response
from tessera.access import Gate, bearer_secret
from tessera.errors import InvalidRequest
from tessera.grants import EFFECTS, KeyGrant
from tessera.models import BucketConfig
from tessera.store import Store


class IssueKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=128)
    queues: list[str]
    kinds: list[str] = Field(default_factory=lambda: ["*"])
    effect_classes: list[str] = Field(default_factory=lambda: list(EFFECTS))
    scopes: list[str] = Field(default_factory=list)
    days: float = Field(default=14.0, gt=0.0, le=365.0)


def mount_storage_and_admin_routes(app: FastAPI, store: Store, gate: Gate) -> None:
    def require_key(request: Request) -> KeyGrant:
        return gate.grant(bearer_secret(request))

    key: Any = Depends(require_key)

    @app.get("/v1/stores")
    def list_stores(grant: KeyGrant = key) -> dict[str, Any]:
        gate.store_scope(grant)
        return {"stores": [s.model_dump(mode="json") for s in store.stores()]}

    @app.get("/v1/stores/{store_name}/records")
    def list_records(
        store_name: str,
        prefix: str = "",
        limit: int = 100,
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        records = store.list_records(store_name, prefix=prefix, limit=limit)
        return {"records": [r.model_dump(mode="json") for r in records]}

    @app.post("/v1/buckets/define")
    def define_bucket(config: BucketConfig, grant: KeyGrant = key) -> dict[str, Any]:
        gate.store_scope(grant)
        return store.define_bucket(config).model_dump(mode="json")

    @app.get("/v1/buckets")
    def list_buckets(grant: KeyGrant = key) -> dict[str, Any]:
        gate.store_scope(grant)
        return {"buckets": [b.model_dump(mode="json") for b in store.buckets()]}

    @app.get("/v1/buckets/{bucket_name}/objects")
    def list_objects(
        bucket_name: str,
        prefix: str = "",
        limit: int = 100,
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        objs = store.list_objects(bucket_name, prefix=prefix, limit=limit)
        return {"objects": [o.model_dump(mode="json") for o in objs]}

    @app.post("/v1/buckets/{bucket_name}/objects/{object_key:path}", status_code=201)
    async def put_object(
        bucket_name: str,
        object_key: str,
        request: Request,
        expected_version: int | None = None,
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        if expected_version is None and (hdr := request.headers.get("x-expected-version")):
            try:
                expected_version = int(hdr)
            except ValueError as exc:
                raise InvalidRequest("x-expected-version must be an integer") from exc
        metadata: dict[str, Any] = {}
        if raw_meta := request.headers.get("x-tessera-metadata"):
            try:
                parsed = json.loads(raw_meta)
            except json.JSONDecodeError as exc:
                raise InvalidRequest(f"x-tessera-metadata is not valid JSON: {exc.msg}") from exc
            if not isinstance(parsed, dict):
                raise InvalidRequest("x-tessera-metadata must be a JSON object")
            metadata = parsed
        content_type = request.headers.get("content-type") or "application/octet-stream"
        data = await request.body()
        saved = await run_in_threadpool(
            lambda: store.put_object(
                bucket_name,
                object_key,
                data,
                content_type=content_type,
                metadata=metadata,
                expected_version=expected_version,
            )
        )
        return saved.model_dump(mode="json")

    @app.get("/v1/buckets/{bucket_name}/objects/{object_key:path}", response_model=None)
    def get_object(
        bucket_name: str,
        object_key: str,
        request: Request,
        version: int | None = None,
        meta: bool = False,
        history: bool = False,
        grant: KeyGrant = key,
    ) -> Response | dict[str, Any]:
        gate.store_scope(grant)
        if history:
            versions = store.object_history(bucket_name, object_key)
            return {"versions": [v.model_dump(mode="json") for v in versions]}
        if meta:
            return store.get_object_meta(bucket_name, object_key, version).model_dump(mode="json")
        obj_meta, data = store.get_object(bucket_name, object_key, version)
        meta_json = json.dumps(obj_meta.model_dump(mode="json"), separators=(",", ":"))
        return slice_bytes_response(
            data,
            request.headers.get("range"),
            media_type=obj_meta.content_type,
            headers={
                "etag": f'"{obj_meta.sha256}"',
                "x-tessera-version": str(obj_meta.version),
                "x-tessera-sha256": obj_meta.sha256,
                "x-tessera-object-meta": meta_json,
            },
        )

    @app.post("/v1/admin/keys", status_code=201)
    def issue_key(body: IssueKeyBody, grant: KeyGrant = key) -> dict[str, Any]:
        gate.admin(grant)
        secret, issued = store.issue_key(
            body.label,
            queues=body.queues,
            kinds=body.kinds,
            effect_classes=body.effect_classes,
            scopes=body.scopes,
            days=body.days,
        )
        return {"key": secret, **issued.model_dump(mode="json")}

    @app.get("/v1/admin/keys")
    def list_keys(grant: KeyGrant = key) -> dict[str, Any]:
        gate.admin(grant)
        return {"keys": [k.model_dump(mode="json") for k in store.list_keys()]}

    @app.post("/v1/admin/keys/{key_id}/revoke")
    def revoke_key(key_id: str, grant: KeyGrant = key) -> dict[str, Any]:
        gate.admin(grant)
        return store.revoke_key(key_id).model_dump(mode="json")
