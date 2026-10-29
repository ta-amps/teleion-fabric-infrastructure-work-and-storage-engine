"""HTTP routes for queryable noun stores, persistent named file buckets, and remote key management.

Mounted by `tessera.service.create_app` behind the same `Gate` (`store` scope for stores/buckets,
`admin` scope for key management), Layer 1/2 governance filters, and POST-only write rules.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from tessera._service_intelligence import slice_bytes_response
from tessera.access import Gate
from tessera.errors import InvalidRequest
from tessera.grants import EFFECTS, KeyGrant
from tessera.models import VISIBILITY_SCOPES, BucketConfig
from tessera.store import Store


class IssueKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=128)
    queues: list[str]
    kinds: list[str] = Field(default_factory=lambda: ["*"])
    effect_classes: list[str] = Field(default_factory=lambda: list(EFFECTS))
    scopes: list[str] = Field(default_factory=list)
    days: float = Field(default=14.0, gt=0.0, le=365.0)


def parse_governance_header(request: Request) -> dict[str, Any]:
    """Parse optional `x-tessera-governance` JSON header + query overrides for binary blob/object uploads."""
    out: dict[str, Any] = {}
    if raw_gov := request.headers.get("x-tessera-governance"):
        try:
            parsed = json.loads(raw_gov)
        except json.JSONDecodeError as exc:
            raise InvalidRequest(f"x-tessera-governance is not valid JSON: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise InvalidRequest("x-tessera-governance must be a JSON object")
        if "producer_surface" in parsed and parsed["producer_surface"] is not None:
            out["producer_surface"] = str(parsed["producer_surface"])
        if "visibility_scope" in parsed and parsed["visibility_scope"] is not None:
            vs = str(parsed["visibility_scope"])
            if vs not in VISIBILITY_SCOPES:
                raise InvalidRequest(f"invalid visibility_scope '{vs}'")
            out["visibility_scope"] = vs
        if "allowed_surfaces" in parsed and parsed["allowed_surfaces"] is not None:
            if not isinstance(parsed["allowed_surfaces"], list):
                raise InvalidRequest("allowed_surfaces must be a list")
            out["allowed_surfaces"] = [str(x) for x in parsed["allowed_surfaces"]]
        if "target_cohorts" in parsed and parsed["target_cohorts"] is not None:
            if not isinstance(parsed["target_cohorts"], list):
                raise InvalidRequest("target_cohorts must be a list")
            out["target_cohorts"] = [str(x) for x in parsed["target_cohorts"]]
        if "tags" in parsed and parsed["tags"] is not None:
            if not isinstance(parsed["tags"], list):
                raise InvalidRequest("tags must be a list")
            out["tags"] = [str(x) for x in parsed["tags"]]

    qp = request.query_params
    if q_vis := qp.get("visibility_scope"):
        if q_vis not in VISIBILITY_SCOPES:
            raise InvalidRequest(f"invalid visibility_scope '{q_vis}'")
        out["visibility_scope"] = q_vis
    if q_surf := qp.get("producer_surface"):
        out["producer_surface"] = q_surf
    if q_tags := qp.getlist("tag"):
        existing = list(out.get("tags", []))
        existing.extend(t for t in q_tags if t.strip())
        out["tags"] = existing
    if q_cohorts := qp.getlist("cohort"):
        out["target_cohorts"] = [c for c in q_cohorts if c.strip()]
    return out


def check_claimed_content(request: Request, data: bytes) -> None:
    """Reject a claimed `sha256` or `size` in `x-tessera-governance` that the bytes do not match.

    Content identity always comes from the received bytes. A claim is only an integrity check,
    matching the Worker edge, so a caller cannot bind another owner's digest to its upload.
    """
    raw_gov = request.headers.get("x-tessera-governance")
    if not raw_gov:
        return
    parsed = json.loads(raw_gov)  # parse_governance_header already validated the JSON object
    size = parsed.get("size")
    if size is not None and (
        isinstance(size, bool) or not isinstance(size, int) or size != len(data)
    ):
        raise InvalidRequest("the claimed size does not match the uploaded bytes")
    digest = parsed.get("sha256")
    if digest is not None and (
        not isinstance(digest, str) or digest.lower() != hashlib.sha256(data).hexdigest()
    ):
        raise InvalidRequest("the claimed sha256 does not match the uploaded bytes")


def mount_storage_and_admin_routes(
    app: FastAPI,
    store: Store,
    gate: Gate,
    require_key_dep: Callable[..., Any],
) -> None:
    key: Any = Depends(require_key_dep)

    @app.get("/v1/stores")
    def list_stores(grant: KeyGrant = key) -> dict[str, Any]:
        gate.store_scope(grant)
        return {"stores": [s.model_dump(mode="json") for s in store.stores()]}

    @app.get("/v1/stores/{store_name}/records")
    def list_records(
        store_name: str,
        prefix: str = "",
        limit: int = 100,
        q: str = "",
        tag: str | None = None,
        category: str | None = None,
        cohort: str | None = None,
        surface: str | None = None,
        include_restricted: bool = False,
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        records = store.list_records(
            store_name,
            prefix=prefix,
            limit=limit,
            q=q,
            tag=tag,
            category=category,
            cohort=cohort,
            surface=surface,
            include_restricted=include_restricted,
        )
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
        q: str = "",
        tag: str | None = None,
        category: str | None = None,
        cohort: str | None = None,
        surface: str | None = None,
        include_restricted: bool = False,
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        objs = store.list_objects(
            bucket_name,
            prefix=prefix,
            limit=limit,
            q=q,
            tag=tag,
            category=category,
            cohort=cohort,
            surface=surface,
            include_restricted=include_restricted,
        )
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
        gov = parse_governance_header(request)
        content_type = request.headers.get("content-type") or "application/octet-stream"
        data = await request.body()
        check_claimed_content(request, data)
        saved = await run_in_threadpool(
            lambda: store.put_object(
                bucket_name,
                object_key,
                data,
                content_type=content_type,
                metadata=metadata,
                expected_version=expected_version,
                **gov,
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
