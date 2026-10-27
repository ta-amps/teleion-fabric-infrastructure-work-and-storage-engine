"""HTTP client for a Tessera service (SPEC §7). It meets the same Backend protocol as Store, so an app
switches between the in-process store and the service by configuration alone."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from typing import Any

import httpx

from tessera._version import __version__
from tessera.errors import (
    BY_CODE,
    RateLimited,
    TesseraError,
    Unavailable,
    VersionConflict,
)
from tessera.models import (
    BucketConfig,
    BucketInfo,
    ChildSpec,
    Event,
    FeedEntry,
    FeedSummary,
    Health,
    Job,
    JobStatus,
    Lease,
    NounScent,
    ObjectMeta,
    QueueConfig,
    QueueCounts,
    Receipt,
    RecordVersion,
    RecordWrite,
    ReflexMatch,
    ResultView,
    StoreSummary,
    TaskSpec,
    TaskView,
)

STATUS_ERRORS: dict[int, str] = {
    400: "invalid_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "stale_lease",
    429: "rate_limited",
    503: "unavailable",
}
MAX_RETRY_AFTER = 30.0


def error_from_response(resp: httpx.Response) -> TesseraError:
    """Map a problem+json response (SPEC §8) to the matching TesseraError."""
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    code = str(body.get("code") or STATUS_ERRORS.get(resp.status_code, "internal"))
    detail = str(body.get("detail") or body.get("title") or resp.reason_phrase)
    cls = BY_CODE.get(code, TesseraError)
    if cls is VersionConflict:
        current = body.get("current_version")
        return VersionConflict(
            detail, current_version=current if isinstance(current, int) else None
        )
    if cls is RateLimited:
        return RateLimited(detail, retry_after=_retry_after(resp))
    if cls is Unavailable:
        return Unavailable(detail, retry_after=_retry_after(resp))
    return cls(detail)


def _retry_after(resp: httpx.Response) -> float:
    try:
        return min(max(float(resp.headers.get("retry-after", "1")), 0.0), MAX_RETRY_AFTER)
    except ValueError:
        return 1.0


class Client:
    """Talks to a Tessera service with one bearer key. The key is never logged or repr'd."""

    def __init__(
        self,
        base_url: str,
        key: str | None = None,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"user-agent": f"tessera-client/{__version__}"}
        if key:
            headers["authorization"] = f"Bearer {key}"
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport
        )
        self._max_retries = max_retries
        self.base_url = base_url.rstrip("/")

    def __repr__(self) -> str:
        return f"Client(base_url={self.base_url!r})"

    def close(self) -> None:
        self._http.close()

    def _call(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            resp = self._http.request(
                method, path, json=body, params=params, headers=headers, content=content
            )
            if resp.status_code in (429, 503) and attempt < self._max_retries:
                time.sleep(_retry_after(resp))
                continue
            if resp.status_code >= 400:
                raise error_from_response(resp)
            return resp
        raise Unavailable("retries exhausted")

    # queues and tasks ---------------------------------------------------------------------------

    def define_queue(self, config: QueueConfig) -> QueueConfig:
        resp = self._call("POST", "/v1/queues/define", body=config.model_dump())
        return QueueConfig.model_validate(resp.json())

    def queues(self) -> list[QueueConfig]:
        resp = self._call("GET", "/v1/queues")
        return [QueueConfig.model_validate(q) for q in resp.json()["queues"]]

    def enqueue(self, spec: TaskSpec) -> str:
        resp = self._call("POST", "/v1/tasks", body=spec.model_dump(exclude_none=True))
        return str(resp.json()["task_id"])

    # the five verbs -----------------------------------------------------------------------------

    def claim(
        self,
        queue: str,
        worker: str,
        *,
        kinds: Sequence[str] | None = None,
        effect_classes: Sequence[str] | None = None,
    ) -> Lease | None:
        body: dict[str, Any] = {"queue": queue, "worker": worker}
        if kinds:
            body["kinds"] = list(kinds)
        if effect_classes:
            body["effect_classes"] = list(effect_classes)
        resp = self._call("POST", "/v1/tasks/claim", body=body)
        if resp.status_code == 204:
            return None
        return Lease.model_validate(resp.json())

    def heartbeat(self, task_id: str, fencing_token: int) -> Lease:
        resp = self._call(
            "POST", "/v1/tasks/heartbeat", body={"task_id": task_id, "fencing_token": fencing_token}
        )
        return Lease.model_validate(resp.json())

    def split(self, task_id: str, fencing_token: int, children: Sequence[ChildSpec]) -> list[str]:
        resp = self._call(
            "POST",
            "/v1/tasks/split",
            body={
                "task_id": task_id,
                "fencing_token": fencing_token,
                "children": [c.model_dump(exclude_none=True) for c in children],
            },
        )
        return [str(i) for i in resp.json()["children"]]

    def submit(
        self,
        task_id: str,
        fencing_token: int,
        output: dict[str, Any],
        *,
        idempotency_key: str,
        ok: bool = True,
        error: str | None = None,
    ) -> Receipt:
        resp = self._call(
            "POST",
            "/v1/tasks/submit",
            body={
                "task_id": task_id,
                "fencing_token": fencing_token,
                "output": output,
                "ok": ok,
                "error": error,
            },
            headers={"idempotency-key": idempotency_key},
        )
        return Receipt.model_validate(resp.json())

    def cancel(self, task_id: str, reason: str = "") -> TaskView:
        resp = self._call("POST", "/v1/tasks/cancel", body={"task_id": task_id, "reason": reason})
        return TaskView.model_validate(resp.json())

    def park(
        self, task_id: str, fencing_token: int, *, reason: str, resume_within: float | None = None
    ) -> TaskView:
        body: dict[str, Any] = {
            "task_id": task_id,
            "fencing_token": fencing_token,
            "reason": reason,
        }
        if resume_within is not None:
            body["resume_within"] = resume_within
        return TaskView.model_validate(self._call("POST", "/v1/tasks/park", body=body).json())

    def resume(self, task_id: str, value: dict[str, Any], *, park: int | None = None) -> TaskView:
        body: dict[str, Any] = {"task_id": task_id, "input": value}
        if park is not None:
            body["park"] = park
        resp = self._call("POST", "/v1/tasks/resume", body=body)
        return TaskView.model_validate(resp.json())

    # operations and observation -----------------------------------------------------------------

    def reap(self) -> int:
        return int(self._call("POST", "/v1/admin/reap").json()["reaped"])

    def task(self, task_id: str) -> TaskView:
        return TaskView.model_validate(self._call("GET", f"/v1/tasks/{task_id}").json())

    def results(self, task_id: str) -> list[ResultView]:
        resp = self._call("GET", f"/v1/tasks/{task_id}/results")
        return [ResultView.model_validate(r) for r in resp.json()["results"]]

    def counts(self) -> dict[str, QueueCounts]:
        resp = self._call("GET", "/v1/counts")
        return {q: QueueCounts.model_validate(c) for q, c in resp.json()["queues"].items()}

    def recount(self) -> dict[str, QueueCounts]:
        resp = self._call("GET", "/v1/counts", params={"recount": "true"})
        return {q: QueueCounts.model_validate(c) for q, c in resp.json()["queues"].items()}

    def health(self) -> Health:
        return Health.model_validate(self._call("GET", "/v1/health").json())

    def events(self, after: int = 0, limit: int = 1000) -> list[Event]:
        resp = self._call("GET", "/v1/events", params={"after": after, "limit": limit})
        return [Event.model_validate(e) for e in resp.json()["events"]]

    # model jobs ---------------------------------------------------------------------------------

    def register_job(self, task_id: str, fencing_token: int, job_id: str, model: str) -> Job:
        resp = self._call(
            "POST",
            "/v1/jobs",
            body={
                "task_id": task_id,
                "fencing_token": fencing_token,
                "job_id": job_id,
                "model": model,
            },
        )
        return Job.model_validate(resp.json())

    def update_job(
        self,
        job_id: str,
        *,
        status: JobStatus,
        input_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
    ) -> Job:
        body = {
            "status": status,
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
        }
        resp = self._call(
            "POST", f"/v1/jobs/{job_id}", body={k: v for k, v in body.items() if v is not None}
        )
        return Job.model_validate(resp.json())

    def jobs(self, task_id: str | None = None) -> list[Job]:
        params = {"task_id": task_id} if task_id else None
        resp = self._call("GET", "/v1/jobs", params=params)
        return [Job.model_validate(j) for j in resp.json()["jobs"]]

    # atomic storage -----------------------------------------------------------------------------

    def put_record(
        self, store: str, key: str, value: Any, *, expected_version: int | None
    ) -> RecordVersion:
        resp = self._call(
            "POST",
            f"/v1/stores/{store}/records/{key}",
            body={"value": value, "expected_version": expected_version},
        )
        return RecordVersion.model_validate(resp.json())

    def put_records(self, writes: Sequence[RecordWrite]) -> list[RecordVersion]:
        resp = self._call(
            "POST", "/v1/stores/batch", body={"writes": [w.model_dump() for w in writes]}
        )
        return [RecordVersion.model_validate(r) for r in resp.json()["records"]]

    def get_record(self, store: str, key: str, version: int | None = None) -> RecordVersion:
        params = {"version": version} if version is not None else None
        resp = self._call("GET", f"/v1/stores/{store}/records/{key}", params=params)
        return RecordVersion.model_validate(resp.json())

    def record_history(self, store: str, key: str) -> list[RecordVersion]:
        resp = self._call("GET", f"/v1/stores/{store}/records/{key}/history")
        return [RecordVersion.model_validate(r) for r in resp.json()["versions"]]

    def put_blob(self, data: bytes) -> str:
        resp = self._call(
            "POST", "/v1/blobs", content=data, headers={"content-type": "application/octet-stream"}
        )
        return str(resp.json()["sha256"])

    def get_blob(self, sha256: str) -> bytes:
        data = self._call("GET", f"/v1/blobs/{sha256}").content
        if hashlib.sha256(data).hexdigest() != sha256:
            raise TesseraError(f"blob {sha256} failed its hash check")
        return data

    def stores(self) -> list[StoreSummary]:
        resp = self._call("GET", "/v1/stores")
        return [StoreSummary.model_validate(s) for s in resp.json()["stores"]]

    def list_records(
        self, store: str, *, prefix: str = "", limit: int = 100
    ) -> list[RecordVersion]:
        params: dict[str, Any] = {"limit": limit}
        if prefix:
            params["prefix"] = prefix
        resp = self._call("GET", f"/v1/stores/{store}/records", params=params)
        return [RecordVersion.model_validate(r) for r in resp.json()["records"]]

    def define_bucket(self, config: BucketConfig) -> BucketInfo:
        resp = self._call("POST", "/v1/buckets/define", body=config.model_dump())
        return BucketInfo.model_validate(resp.json())

    def buckets(self) -> list[BucketInfo]:
        resp = self._call("GET", "/v1/buckets")
        return [BucketInfo.model_validate(b) for b in resp.json()["buckets"]]

    def put_object(
        self,
        bucket: str,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> ObjectMeta:
        headers = {"content-type": content_type}
        if metadata is not None:
            headers["x-tessera-metadata"] = json.dumps(metadata, separators=(",", ":"))
        params: dict[str, Any] = {}
        if expected_version is not None:
            params["expected_version"] = expected_version
        resp = self._call(
            "POST",
            f"/v1/buckets/{bucket}/objects/{key}",
            content=data,
            headers=headers,
            params=params or None,
        )
        return ObjectMeta.model_validate(resp.json())

    def get_object_meta(self, bucket: str, key: str, version: int | None = None) -> ObjectMeta:
        params: dict[str, Any] = {"meta": "true"}
        if version is not None:
            params["version"] = version
        resp = self._call("GET", f"/v1/buckets/{bucket}/objects/{key}", params=params)
        return ObjectMeta.model_validate(resp.json())

    def get_object(
        self, bucket: str, key: str, version: int | None = None
    ) -> tuple[ObjectMeta, bytes]:
        params: dict[str, Any] | None = {"version": version} if version is not None else None
        resp = self._call("GET", f"/v1/buckets/{bucket}/objects/{key}", params=params)
        envelope = resp.headers.get("x-tessera-object-meta")
        meta = (
            ObjectMeta.model_validate(json.loads(envelope))
            if envelope
            else self.get_object_meta(bucket, key, version)
        )
        if hashlib.sha256(resp.content).hexdigest() != meta.sha256:
            raise TesseraError(f"object {bucket}/{key} failed its hash check")
        return meta, resp.content

    def list_objects(self, bucket: str, *, prefix: str = "", limit: int = 100) -> list[ObjectMeta]:
        params: dict[str, Any] = {"limit": limit}
        if prefix:
            params["prefix"] = prefix
        resp = self._call("GET", f"/v1/buckets/{bucket}/objects", params=params)
        return [ObjectMeta.model_validate(o) for o in resp.json()["objects"]]

    def object_history(self, bucket: str, key: str) -> list[ObjectMeta]:
        resp = self._call("GET", f"/v1/buckets/{bucket}/objects/{key}", params={"history": "true"})
        return [ObjectMeta.model_validate(o) for o in resp.json()["versions"]]

    def get_object_slice(
        self, bucket: str, key: str, *, start: int, end: int | None = None
    ) -> bytes:
        range_val = f"bytes={start}-" if end is None else f"bytes={start}-{end}"
        return self._call(
            "GET", f"/v1/buckets/{bucket}/objects/{key}", headers={"range": range_val}
        ).content

    def get_blob_slice(self, sha256: str, *, start: int, end: int | None = None) -> bytes:
        range_val = f"bytes={start}-" if end is None else f"bytes={start}-{end}"
        return self._call("GET", f"/v1/blobs/{sha256}", headers={"range": range_val}).content

    def search_nouns(
        self,
        query: str = "",
        *,
        store: str | None = None,
        bucket: str | None = None,
        limit: int = 20,
    ) -> list[NounScent]:
        params: dict[str, Any] = {"q": query, "limit": limit}
        if store is not None:
            params["store"] = store
        if bucket is not None:
            params["bucket"] = bucket
        resp = self._call("GET", "/v1/search", params=params)
        return [NounScent.model_validate(s) for s in resp.json()["scents"]]

    def feeds(self) -> list[FeedSummary]:
        resp = self._call("GET", "/v1/feeds")
        return [FeedSummary.model_validate(f) for f in resp.json()["feeds"]]

    def publish_feed(
        self,
        feed: str,
        payload: dict[str, Any],
        *,
        kind: str = "delta",
        summary: str = "",
    ) -> FeedEntry:
        resp = self._call(
            "POST",
            f"/v1/feeds/{feed}",
            body={"payload": payload, "kind": kind, "summary": summary},
        )
        return FeedEntry.model_validate(resp.json())

    def poll_feed(self, feed: str, *, after: int = 0, limit: int = 100) -> list[FeedEntry]:
        resp = self._call("GET", f"/v1/feeds/{feed}", params={"after": after, "limit": limit})
        return [FeedEntry.model_validate(e) for e in resp.json()["entries"]]

    def match_reflex(self, query: str, *, limit: int = 5) -> ReflexMatch:
        resp = self._call("GET", "/v1/reflexes/match", params={"q": query, "limit": limit})
        return ReflexMatch.model_validate(resp.json())
