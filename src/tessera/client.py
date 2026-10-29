"""HTTP client for a Tessera service (SPEC §7). It meets the same Backend protocol as Store, so an app
switches between the in-process store and the service by configuration alone."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

import httpx

from tessera._version import __version__
from tessera.errors import (
    BY_CODE,
    RateLimited,
    StorageQuotaExhausted,
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
    HandshakeRequest,
    HandshakeResponse,
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
    VisibilityScope,
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
    # The full server value is kept, even beyond MAX_RETRY_AFTER: a provider storage outage
    # can last until the next 00:00 UTC, and the caller decides how to wait.
    if cls is RateLimited:
        return RateLimited(detail, retry_after=_retry_after(resp))
    if cls is StorageQuotaExhausted:
        reset_at = body.get("reset_at")
        return StorageQuotaExhausted(
            detail,
            retry_after=_retry_after(resp),
            reset_at=reset_at if isinstance(reset_at, str) else None,
        )
    if cls is Unavailable:
        return Unavailable(detail, retry_after=_retry_after(resp))
    return cls(detail)


def _retry_after(resp: httpx.Response) -> float:
    try:
        return max(float(resp.headers.get("retry-after", "1")), 0.0)
    except ValueError:
        return 1.0


def _problem_code(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    return str(body.get("code")) if isinstance(body, dict) and body.get("code") else None


def _filter_params(
    *,
    q: str | None = None,
    tag: str | None = None,
    category: str | None = None,
    cohort: str | None = None,
    surface: str | None = None,
    include_restricted: bool = False,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if q is not None and q != "":
        out["q"] = q
    if tag:
        out["tag"] = tag
    if category:
        out["category"] = category
    if cohort:
        out["cohort"] = cohort
    if surface:
        out["surface"] = surface
    if include_restricted:
        out["include_restricted"] = "true"
    return out


class Client:
    """Talks to a Tessera service with one bearer key. The key is never logged or repr'd."""

    def __init__(
        self,
        base_url: str,
        key: str | None = None,
        *,
        surface_id: str | None = None,
        namespace: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"user-agent": f"tessera-client/{__version__}"}
        if key:
            headers["authorization"] = f"Bearer {key}"
        if surface_id:
            headers["x-surface-id"] = surface_id
        if namespace:
            headers["x-namespace"] = namespace
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport
        )
        self._max_retries = max_retries
        self.base_url = base_url.rstrip("/")
        self.surface_id = surface_id
        self.namespace = namespace

    def __repr__(self) -> str:
        return f"Client(base_url={self.base_url!r})"

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def _ns_headers(
        namespace: str | None = None, extra: dict[str, str] | None = None
    ) -> dict[str, str] | None:
        hdrs = dict(extra or {})
        if namespace:
            hdrs["x-namespace"] = namespace
        return hdrs or None

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
                wait = _retry_after(resp)
                # Sleep in-process only for short waits. A storage outage or a longer server
                # hint is raised at once with the full Retry-After instead of being retried
                # early (and repeatedly) within this call.
                if wait <= MAX_RETRY_AFTER and _problem_code(resp) != "storage_quota_exhausted":
                    time.sleep(wait)
                    continue
            if resp.status_code >= 400:
                raise error_from_response(resp)
            return resp
        raise Unavailable("retries exhausted")

    # queues and tasks ---------------------------------------------------------------------------

    def define_queue(self, config: QueueConfig, *, namespace: str | None = None) -> QueueConfig:
        resp = self._call(
            "POST",
            "/v1/queues/define",
            body=config.model_dump(),
            headers=self._ns_headers(namespace),
        )
        return QueueConfig.model_validate(resp.json())

    def queues(self, *, namespace: str | None = None) -> list[QueueConfig]:
        resp = self._call("GET", "/v1/queues", headers=self._ns_headers(namespace))
        return [QueueConfig.model_validate(q) for q in resp.json()["queues"]]

    def enqueue(
        self,
        spec: TaskSpec,
        *,
        idempotency_key: str | None = None,
        namespace: str | None = None,
    ) -> str:
        extra = {"idempotency-key": idempotency_key} if idempotency_key else None
        resp = self._call(
            "POST",
            "/v1/tasks",
            body=spec.model_dump(exclude_none=True),
            headers=self._ns_headers(namespace, extra),
        )
        return str(resp.json()["task_id"])

    # the five verbs -----------------------------------------------------------------------------

    def claim(
        self,
        queue: str,
        worker: str,
        *,
        kinds: Sequence[str] | None = None,
        effect_classes: Sequence[str] | None = None,
        namespace: str | None = None,
    ) -> Lease | None:
        body: dict[str, Any] = {"queue": queue, "worker": worker}
        if kinds:
            body["kinds"] = list(kinds)
        if effect_classes:
            body["effect_classes"] = list(effect_classes)
        resp = self._call("POST", "/v1/tasks/claim", body=body, headers=self._ns_headers(namespace))
        if resp.status_code == 204:
            return None
        return Lease.model_validate(resp.json())

    def heartbeat(self, task_id: str, fencing_token: int, *, namespace: str | None = None) -> Lease:
        resp = self._call(
            "POST",
            "/v1/tasks/heartbeat",
            body={"task_id": task_id, "fencing_token": fencing_token},
            headers=self._ns_headers(namespace),
        )
        return Lease.model_validate(resp.json())

    def split(
        self,
        task_id: str,
        fencing_token: int,
        children: Sequence[ChildSpec],
        *,
        namespace: str | None = None,
    ) -> list[str]:
        resp = self._call(
            "POST",
            "/v1/tasks/split",
            body={
                "task_id": task_id,
                "fencing_token": fencing_token,
                "children": [c.model_dump(exclude_none=True) for c in children],
            },
            headers=self._ns_headers(namespace),
        )
        return [str(i) for i in resp.json()["children"]]

    def submit(
        self,
        task_id: str,
        fencing_token: int,
        output: Any,
        *,
        idempotency_key: str,
        ok: bool = True,
        error: str | None = None,
        execution_report: dict[str, Any] | None = None,
        namespace: str | None = None,
    ) -> Receipt:
        body: dict[str, Any] = {
            "task_id": task_id,
            "fencing_token": fencing_token,
            "output": output,
            "ok": ok,
            "error": error,
        }
        if execution_report is not None:
            body["execution_report"] = execution_report
        resp = self._call(
            "POST",
            "/v1/tasks/submit",
            body=body,
            headers=self._ns_headers(namespace, {"idempotency-key": idempotency_key}),
        )
        return Receipt.model_validate(resp.json())

    def recall_execution(
        self,
        task_id: str,
        fencing_token: int,
        *,
        execution_report: dict[str, Any] | None = None,
        limit: int = 5,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Recall permitted prior runs while holding a live lease; reuse still needs validation."""
        body: dict[str, Any] = {
            "task_id": task_id,
            "fencing_token": fencing_token,
            "limit": limit,
        }
        if execution_report is not None:
            body["execution_report"] = execution_report
        result: dict[str, Any] = self._call(
            "POST", "/v1/executions/recall", body=body, headers=self._ns_headers(namespace)
        ).json()
        return result

    def get_execution(self, run_id: str, *, namespace: str | None = None) -> dict[str, Any]:
        """Read an execution under its source task's current grants and retention policy."""
        result: dict[str, Any] = self._call(
            "GET",
            f"/v1/executions/{quote(run_id, safe='')}",
            headers=self._ns_headers(namespace),
        ).json()
        return result

    def list_executions(
        self,
        task_id: str,
        *,
        after: int = 0,
        limit: int = 20,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """List permitted run references for one task, paginated by fencing token."""
        result: dict[str, Any] = self._call(
            "GET",
            f"/v1/tasks/{quote(task_id, safe='')}/executions",
            params={"after": after, "limit": limit},
            headers=self._ns_headers(namespace),
        ).json()
        return result

    def cancel(self, task_id: str, reason: str = "", *, namespace: str | None = None) -> TaskView:
        resp = self._call(
            "POST",
            "/v1/tasks/cancel",
            body={"task_id": task_id, "reason": reason},
            headers=self._ns_headers(namespace),
        )
        return TaskView.model_validate(resp.json())

    def park(
        self,
        task_id: str,
        fencing_token: int,
        *,
        reason: str,
        resume_within: float | None = None,
        namespace: str | None = None,
    ) -> TaskView:
        body: dict[str, Any] = {
            "task_id": task_id,
            "fencing_token": fencing_token,
            "reason": reason,
        }
        if resume_within is not None:
            body["resume_within"] = resume_within
        return TaskView.model_validate(
            self._call(
                "POST", "/v1/tasks/park", body=body, headers=self._ns_headers(namespace)
            ).json()
        )

    def resume(
        self,
        task_id: str,
        value: dict[str, Any],
        *,
        park: int | None = None,
        namespace: str | None = None,
    ) -> TaskView:
        body: dict[str, Any] = {"task_id": task_id, "input": value}
        if park is not None:
            body["park"] = park
        resp = self._call(
            "POST", "/v1/tasks/resume", body=body, headers=self._ns_headers(namespace)
        )
        return TaskView.model_validate(resp.json())

    # operations and observation -----------------------------------------------------------------

    def reap(self) -> int:
        return int(self._call("POST", "/v1/admin/reap").json()["reaped"])

    def task(self, task_id: str, *, namespace: str | None = None) -> TaskView:
        return TaskView.model_validate(
            self._call("GET", f"/v1/tasks/{task_id}", headers=self._ns_headers(namespace)).json()
        )

    def results(self, task_id: str, *, namespace: str | None = None) -> list[ResultView]:
        resp = self._call(
            "GET", f"/v1/tasks/{task_id}/results", headers=self._ns_headers(namespace)
        )
        return [ResultView.model_validate(r) for r in resp.json()["results"]]

    def counts(self, *, namespace: str | None = None) -> dict[str, QueueCounts]:
        resp = self._call("GET", "/v1/counts", headers=self._ns_headers(namespace))
        return {q: QueueCounts.model_validate(c) for q, c in resp.json()["queues"].items()}

    def recount(self, *, namespace: str | None = None) -> dict[str, QueueCounts]:
        resp = self._call(
            "GET",
            "/v1/counts",
            params={"recount": "true"},
            headers=self._ns_headers(namespace),
        )
        return {q: QueueCounts.model_validate(c) for q, c in resp.json()["queues"].items()}

    def health(self, *, namespace: str | None = None) -> Health:
        return Health.model_validate(
            self._call("GET", "/v1/health", headers=self._ns_headers(namespace)).json()
        )

    def events(
        self, after: int = 0, limit: int = 1000, *, namespace: str | None = None
    ) -> list[Event]:
        resp = self._call(
            "GET",
            "/v1/events",
            params={"after": after, "limit": limit},
            headers=self._ns_headers(namespace),
        )
        return [Event.model_validate(e) for e in resp.json()["events"]]

    # model jobs ---------------------------------------------------------------------------------

    def register_job(
        self,
        task_id: str,
        fencing_token: int,
        job_id: str,
        model: str,
        *,
        namespace: str | None = None,
    ) -> Job:
        resp = self._call(
            "POST",
            "/v1/jobs",
            body={
                "task_id": task_id,
                "fencing_token": fencing_token,
                "job_id": job_id,
                "model": model,
            },
            headers=self._ns_headers(namespace),
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
        namespace: str | None = None,
    ) -> Job:
        body = {
            "status": status,
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
        }
        resp = self._call(
            "POST",
            f"/v1/jobs/{job_id}",
            body={k: v for k, v in body.items() if v is not None},
            headers=self._ns_headers(namespace),
        )
        return Job.model_validate(resp.json())

    def jobs(self, task_id: str | None = None, *, namespace: str | None = None) -> list[Job]:
        params = {"task_id": task_id} if task_id else None
        resp = self._call("GET", "/v1/jobs", params=params, headers=self._ns_headers(namespace))
        return [Job.model_validate(j) for j in resp.json()["jobs"]]

    # atomic storage -----------------------------------------------------------------------------

    def put_record(
        self,
        store: str,
        key: str,
        value: Any,
        *,
        expected_version: int | None,
        namespace: str | None = None,
        producer_surface: str | None = None,
        visibility_scope: VisibilityScope = "public_commons",
        allowed_surfaces: Sequence[str] | None = None,
        target_cohorts: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
    ) -> RecordVersion:
        body: dict[str, Any] = {
            "value": value,
            "expected_version": expected_version,
            "visibility_scope": visibility_scope,
        }
        if producer_surface is not None:
            body["producer_surface"] = producer_surface
        if allowed_surfaces is not None:
            body["allowed_surfaces"] = list(allowed_surfaces)
        if target_cohorts is not None:
            body["target_cohorts"] = list(target_cohorts)
        if tags is not None:
            body["tags"] = list(tags)
        resp = self._call(
            "POST",
            f"/v1/stores/{store}/records/{key}",
            body=body,
            headers=self._ns_headers(namespace),
        )
        return RecordVersion.model_validate(resp.json())

    def put_records(
        self, writes: Sequence[RecordWrite], *, namespace: str | None = None
    ) -> list[RecordVersion]:
        resp = self._call(
            "POST",
            "/v1/stores/batch",
            body={"writes": [w.model_dump() for w in writes]},
            headers=self._ns_headers(namespace),
        )
        return [RecordVersion.model_validate(r) for r in resp.json()["records"]]

    def get_record(
        self,
        store: str,
        key: str,
        version: int | None = None,
        *,
        namespace: str | None = None,
    ) -> RecordVersion:
        params = {"version": version} if version is not None else None
        resp = self._call(
            "GET",
            f"/v1/stores/{store}/records/{key}",
            params=params,
            headers=self._ns_headers(namespace),
        )
        return RecordVersion.model_validate(resp.json())

    def record_history(
        self, store: str, key: str, *, namespace: str | None = None
    ) -> list[RecordVersion]:
        resp = self._call(
            "GET",
            f"/v1/stores/{store}/records/{key}/history",
            headers=self._ns_headers(namespace),
        )
        return [RecordVersion.model_validate(r) for r in resp.json()["versions"]]

    def put_blob(
        self,
        data: bytes,
        *,
        namespace: str | None = None,
        producer_surface: str | None = None,
        visibility_scope: VisibilityScope = "public_commons",
        allowed_surfaces: Sequence[str] | None = None,
        target_cohorts: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
    ) -> str:
        extra: dict[str, str] = {"content-type": "application/octet-stream"}
        gov: dict[str, Any] = {}
        if producer_surface is not None:
            gov["producer_surface"] = producer_surface
        if visibility_scope != "public_commons":
            gov["visibility_scope"] = visibility_scope
        if allowed_surfaces is not None:
            gov["allowed_surfaces"] = list(allowed_surfaces)
        if target_cohorts is not None:
            gov["target_cohorts"] = list(target_cohorts)
        if tags is not None:
            gov["tags"] = list(tags)
        if gov:
            extra["x-tessera-governance"] = json.dumps(gov, separators=(",", ":"))
        resp = self._call(
            "POST",
            "/v1/blobs",
            content=data,
            headers=self._ns_headers(namespace, extra),
        )
        return str(resp.json()["sha256"])

    def get_blob(self, sha256: str, *, namespace: str | None = None) -> bytes:
        data = self._call("GET", f"/v1/blobs/{sha256}", headers=self._ns_headers(namespace)).content
        if hashlib.sha256(data).hexdigest() != sha256:
            raise TesseraError(f"blob {sha256} failed its hash check")
        return data

    def stores(self, *, namespace: str | None = None) -> list[StoreSummary]:
        resp = self._call("GET", "/v1/stores", headers=self._ns_headers(namespace))
        return [StoreSummary.model_validate(s) for s in resp.json()["stores"]]

    def list_records(
        self,
        store: str,
        *,
        prefix: str = "",
        limit: int = 100,
        q: str = "",
        tag: str | None = None,
        category: str | None = None,
        cohort: str | None = None,
        surface: str | None = None,
        include_restricted: bool = False,
        namespace: str | None = None,
    ) -> list[RecordVersion]:
        params: dict[str, Any] = {
            "limit": limit,
            **_filter_params(
                q=q,
                tag=tag,
                category=category,
                cohort=cohort,
                surface=surface,
                include_restricted=include_restricted,
            ),
        }
        if prefix:
            params["prefix"] = prefix
        resp = self._call(
            "GET",
            f"/v1/stores/{store}/records",
            params=params,
            headers=self._ns_headers(namespace),
        )
        return [RecordVersion.model_validate(r) for r in resp.json()["records"]]

    def define_bucket(self, config: BucketConfig, *, namespace: str | None = None) -> BucketInfo:
        resp = self._call(
            "POST",
            "/v1/buckets/define",
            body=config.model_dump(),
            headers=self._ns_headers(namespace),
        )
        return BucketInfo.model_validate(resp.json())

    def buckets(self, *, namespace: str | None = None) -> list[BucketInfo]:
        resp = self._call("GET", "/v1/buckets", headers=self._ns_headers(namespace))
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
        namespace: str | None = None,
        producer_surface: str | None = None,
        visibility_scope: VisibilityScope = "public_commons",
        allowed_surfaces: Sequence[str] | None = None,
        target_cohorts: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
    ) -> ObjectMeta:
        headers = {"content-type": content_type}
        if metadata is not None:
            headers["x-tessera-metadata"] = json.dumps(metadata, separators=(",", ":"))
        gov: dict[str, Any] = {}
        if producer_surface is not None:
            gov["producer_surface"] = producer_surface
        if visibility_scope != "public_commons":
            gov["visibility_scope"] = visibility_scope
        if allowed_surfaces is not None:
            gov["allowed_surfaces"] = list(allowed_surfaces)
        if target_cohorts is not None:
            gov["target_cohorts"] = list(target_cohorts)
        if tags is not None:
            gov["tags"] = list(tags)
        if gov:
            headers["x-tessera-governance"] = json.dumps(gov, separators=(",", ":"))
        params: dict[str, Any] = {}
        if expected_version is not None:
            params["expected_version"] = expected_version
        resp = self._call(
            "POST",
            f"/v1/buckets/{bucket}/objects/{key}",
            content=data,
            headers=self._ns_headers(namespace, headers),
            params=params or None,
        )
        return ObjectMeta.model_validate(resp.json())

    def get_object_meta(
        self,
        bucket: str,
        key: str,
        version: int | None = None,
        *,
        namespace: str | None = None,
    ) -> ObjectMeta:
        params: dict[str, Any] = {"meta": "true"}
        if version is not None:
            params["version"] = version
        resp = self._call(
            "GET",
            f"/v1/buckets/{bucket}/objects/{key}",
            params=params,
            headers=self._ns_headers(namespace),
        )
        return ObjectMeta.model_validate(resp.json())

    def get_object(
        self,
        bucket: str,
        key: str,
        version: int | None = None,
        *,
        namespace: str | None = None,
    ) -> tuple[ObjectMeta, bytes]:
        params: dict[str, Any] | None = {"version": version} if version is not None else None
        resp = self._call(
            "GET",
            f"/v1/buckets/{bucket}/objects/{key}",
            params=params,
            headers=self._ns_headers(namespace),
        )
        envelope = resp.headers.get("x-tessera-object-meta")
        meta = (
            ObjectMeta.model_validate(json.loads(envelope))
            if envelope
            else self.get_object_meta(bucket, key, version, namespace=namespace)
        )
        if hashlib.sha256(resp.content).hexdigest() != meta.sha256:
            raise TesseraError(f"object {bucket}/{key} failed its hash check")
        return meta, resp.content

    def list_objects(
        self,
        bucket: str,
        *,
        prefix: str = "",
        limit: int = 100,
        q: str = "",
        tag: str | None = None,
        category: str | None = None,
        cohort: str | None = None,
        surface: str | None = None,
        include_restricted: bool = False,
        namespace: str | None = None,
    ) -> list[ObjectMeta]:
        params: dict[str, Any] = {
            "limit": limit,
            **_filter_params(
                q=q,
                tag=tag,
                category=category,
                cohort=cohort,
                surface=surface,
                include_restricted=include_restricted,
            ),
        }
        if prefix:
            params["prefix"] = prefix
        resp = self._call(
            "GET",
            f"/v1/buckets/{bucket}/objects",
            params=params,
            headers=self._ns_headers(namespace),
        )
        return [ObjectMeta.model_validate(o) for o in resp.json()["objects"]]

    def object_history(
        self, bucket: str, key: str, *, namespace: str | None = None
    ) -> list[ObjectMeta]:
        resp = self._call(
            "GET",
            f"/v1/buckets/{bucket}/objects/{key}",
            params={"history": "true"},
            headers=self._ns_headers(namespace),
        )
        return [ObjectMeta.model_validate(o) for o in resp.json()["versions"]]

    def get_object_slice(
        self,
        bucket: str,
        key: str,
        *,
        start: int,
        end: int | None = None,
        namespace: str | None = None,
    ) -> bytes:
        range_val = f"bytes={start}-" if end is None else f"bytes={start}-{end}"
        return self._call(
            "GET",
            f"/v1/buckets/{bucket}/objects/{key}",
            headers=self._ns_headers(namespace, {"range": range_val}),
        ).content

    def get_blob_slice(
        self,
        sha256: str,
        *,
        start: int,
        end: int | None = None,
        namespace: str | None = None,
    ) -> bytes:
        range_val = f"bytes={start}-" if end is None else f"bytes={start}-{end}"
        return self._call(
            "GET",
            f"/v1/blobs/{sha256}",
            headers=self._ns_headers(namespace, {"range": range_val}),
        ).content

    def search_nouns(
        self,
        query: str = "",
        *,
        store: str | None = None,
        bucket: str | None = None,
        limit: int = 20,
        tag: str | None = None,
        category: str | None = None,
        cohort: str | None = None,
        surface: str | None = None,
        include_restricted: bool = False,
        namespace: str | None = None,
    ) -> list[NounScent]:
        params: dict[str, Any] = {
            "q": query,
            "limit": limit,
            **_filter_params(
                tag=tag,
                category=category,
                cohort=cohort,
                surface=surface,
                include_restricted=include_restricted,
            ),
        }
        if store is not None:
            params["store"] = store
        if bucket is not None:
            params["bucket"] = bucket
        resp = self._call("GET", "/v1/search", params=params, headers=self._ns_headers(namespace))
        return [NounScent.model_validate(s) for s in resp.json()["scents"]]

    def feeds(self, *, namespace: str | None = None) -> list[FeedSummary]:
        resp = self._call("GET", "/v1/feeds", headers=self._ns_headers(namespace))
        return [FeedSummary.model_validate(f) for f in resp.json()["feeds"]]

    def publish_feed(
        self,
        feed: str,
        payload: dict[str, Any],
        *,
        kind: str = "delta",
        summary: str = "",
        namespace: str | None = None,
        producer_surface: str | None = None,
        visibility_scope: VisibilityScope = "public_commons",
        allowed_surfaces: Sequence[str] | None = None,
        target_cohorts: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
    ) -> FeedEntry:
        body: dict[str, Any] = {
            "payload": payload,
            "kind": kind,
            "summary": summary,
            "visibility_scope": visibility_scope,
        }
        if producer_surface is not None:
            body["producer_surface"] = producer_surface
        if allowed_surfaces is not None:
            body["allowed_surfaces"] = list(allowed_surfaces)
        if target_cohorts is not None:
            body["target_cohorts"] = list(target_cohorts)
        if tags is not None:
            body["tags"] = list(tags)
        resp = self._call(
            "POST",
            f"/v1/feeds/{feed}",
            body=body,
            headers=self._ns_headers(namespace),
        )
        return FeedEntry.model_validate(resp.json())

    def poll_feed(
        self,
        feed: str,
        *,
        after: int = 0,
        limit: int = 100,
        q: str = "",
        tag: str | None = None,
        category: str | None = None,
        cohort: str | None = None,
        surface: str | None = None,
        include_restricted: bool = False,
        namespace: str | None = None,
    ) -> list[FeedEntry]:
        params: dict[str, Any] = {
            "after": after,
            "limit": limit,
            **_filter_params(
                q=q,
                tag=tag,
                category=category,
                cohort=cohort,
                surface=surface,
                include_restricted=include_restricted,
            ),
        }
        resp = self._call(
            "GET",
            f"/v1/feeds/{feed}",
            params=params,
            headers=self._ns_headers(namespace),
        )
        return [FeedEntry.model_validate(e) for e in resp.json()["entries"]]

    def match_reflex(
        self,
        query: str,
        *,
        limit: int = 5,
        tag: str | None = None,
        category: str | None = None,
        cohort: str | None = None,
        surface: str | None = None,
        include_restricted: bool = False,
        namespace: str | None = None,
    ) -> ReflexMatch:
        params: dict[str, Any] = {
            "q": query,
            "limit": limit,
            **_filter_params(
                tag=tag,
                category=category,
                cohort=cohort,
                surface=surface,
                include_restricted=include_restricted,
            ),
        }
        resp = self._call(
            "GET",
            "/v1/reflexes/match",
            params=params,
            headers=self._ns_headers(namespace),
        )
        return ReflexMatch.model_validate(resp.json())

    def handshake(
        self, request: HandshakeRequest, *, namespace: str | None = None
    ) -> HandshakeResponse:
        headers: dict[str, str] = {}
        chosen_surface = request.surface_id or self.surface_id
        if chosen_surface:
            headers["x-surface-id"] = chosen_surface
        resp = self._call(
            "POST",
            "/v1/handshake",
            body=request.model_dump(mode="json"),
            headers=self._ns_headers(namespace, headers),
        )
        return HandshakeResponse.model_validate(resp.json())
