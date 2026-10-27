"""Tessera's HTTP service (SPEC §7). Every route ends in one Store call; the service adds bearer keys,
scopes, POST-only writes, a claim rate limit, problem+json errors, the reaper loop, the MCP door, and
the agent door (/connect, /llms.txt, the agent card, the read-only board and keyed web forms)."""

from __future__ import annotations

import asyncio
import hmac
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from http import HTTPStatus
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from tessera import pages
from tessera._service_buckets import mount_storage_and_admin_routes
from tessera._version import __version__
from tessera.access import ClaimLimiter, Gate, ServiceSettings, bearer_secret
from tessera.errors import (
    Forbidden,
    InvalidRequest,
    MethodNotAllowed,
    RateLimited,
    TesseraError,
    VersionConflict,
)
from tessera.grants import KeyGrant
from tessera.models import ChildSpec, JobStatus, QueueConfig, RecordWrite, TaskSpec
from tessera.store import Store

WRITE_PATHS = (
    "/v1/queues/define",
    "/v1/tasks",
    "/v1/tasks/claim",
    "/v1/tasks/heartbeat",
    "/v1/tasks/submit",
    "/v1/tasks/split",
    "/v1/tasks/park",
    "/v1/tasks/resume",
    "/v1/tasks/cancel",
    "/v1/admin/reap",
    "/v1/admin/keys/{key_id}/revoke",
    "/v1/jobs/{job_id}",
    "/v1/stores/batch",
    "/v1/blobs",
    "/v1/buckets/define",
    "/work/claim",
    "/work/submit",
    "/work/heartbeat",
)
EDGE_EXEMPT = frozenset({"/v1/health", "/healthz"})
PAGE_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


def problem(
    status: int, code: str, detail: str, headers: dict[str, str] | None = None, **extra: Any
) -> JSONResponse:
    body = {
        "type": "about:blank",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        "code": code,
        **extra,
    }
    return JSONResponse(
        body, status_code=status, media_type="application/problem+json", headers=headers
    )


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParkBody(_Body):
    task_id: str
    fencing_token: int
    reason: str
    resume_within: float | None = None


class ResumeBody(_Body):
    task_id: str
    input: dict[str, Any]
    park: int | None = None


class ClaimBody(_Body):
    queue: str
    worker: str
    kinds: list[str] | None = None
    effect_classes: list[str] | None = None


class LeaseRef(_Body):
    task_id: str
    fencing_token: int


class SubmitBody(LeaseRef):
    output: dict[str, Any]
    ok: bool = True
    error: str | None = None


class SplitBody(LeaseRef):
    children: list[ChildSpec]


class CancelBody(_Body):
    task_id: str
    reason: str = ""


class JobBody(LeaseRef):
    job_id: str
    model: str


class JobUpdate(_Body):
    status: JobStatus
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class RecordBody(_Body):
    value: Any
    expected_version: int | None = None


class BatchBody(_Body):
    writes: list[RecordWrite]


def create_app(store: Store | None = None, settings: ServiceSettings | None = None) -> FastAPI:
    settings = settings or ServiceSettings.from_env()
    store = store or Store(settings.db_path)
    gate = Gate(store)
    limiter = ClaimLimiter(settings.claims_per_minute)

    from tessera.mcp_door import build_mcp  # imported here: the MCP SDK is an optional extra

    mcp_server, mcp_app = build_mcp(store, gate, settings, limiter)

    async def reaper() -> None:
        while True:
            await asyncio.sleep(settings.reaper_seconds)
            with suppress(Exception):
                await run_in_threadpool(store.reap)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(reaper())
        async with mcp_server.session_manager.run():
            yield
        task.cancel()

    app = FastAPI(
        title="Tessera",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/v1/openapi.json",
    )

    def require_key(request: Request) -> KeyGrant:
        return gate.grant(bearer_secret(request))

    key: Any = Depends(require_key)

    @app.middleware("http")
    async def edge_lock(request: Request, call_next: Callable[[Request], Any]) -> Response:
        if settings.edge_secret and request.url.path not in EDGE_EXEMPT:
            sent = request.headers.get("x-edge-auth", "")
            if not hmac.compare_digest(sent.encode(), settings.edge_secret.encode()):
                return problem(403, "forbidden", "requests must come through the edge")
        response: Response = await call_next(request)
        if request.url.path.startswith(("/v1/", "/work")):
            response.headers.setdefault("cache-control", "no-store")
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers.setdefault("content-security-policy", PAGE_CSP)
        response.headers.setdefault("x-content-type-options", "nosniff")
        return response

    @app.exception_handler(TesseraError)
    async def tessera_error(_: Request, exc: TesseraError) -> JSONResponse:
        headers: dict[str, str] = {}
        extra: dict[str, Any] = {}
        if isinstance(exc, RateLimited):
            headers["retry-after"] = str(int(exc.retry_after + 0.999))
        if isinstance(exc, VersionConflict):
            extra["current_version"] = exc.current_version
        return problem(exc.status, exc.code, exc.detail, headers, **extra)

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()))
        return problem(400, "invalid_request", f"{where}: {first.get('msg', 'invalid request')}")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "invalid_request")
        return problem(exc.status_code, code, str(exc.detail))

    # Writes are POST only. These routes answer any other method with 405 before the parameterized
    # read routes (and the MCP mount) can match the same path.
    def not_allowed() -> Response:
        raise MethodNotAllowed("writes are POST only; this path changes nothing on GET")

    for path in WRITE_PATHS:
        app.add_api_route(
            path,
            not_allowed,
            methods=["GET", "HEAD", "PUT", "PATCH", "DELETE"],
            include_in_schema=False,
        )

    # -- health, counts, events ---------------------------------------------------------------

    @app.get("/v1/health")
    @app.get("/healthz", include_in_schema=False)
    def health() -> dict[str, Any]:
        return store.health().model_dump(mode="json")

    @app.get("/v1/counts")
    def counts(recount: bool = False, grant: KeyGrant = key) -> dict[str, Any]:
        values = store.recount() if recount else store.counts()
        visible = {
            q: c.model_dump() for q, c in values.items() if grant.is_admin or grant.allows_queue(q)
        }
        return {"as_of": store.clock.now(), "queues": visible}

    @app.get("/v1/events")
    def events(after: int = 0, limit: int = 1000, grant: KeyGrant = key) -> dict[str, Any]:
        gate.admin(grant)
        return {
            "events": [e.model_dump(mode="json") for e in store.events(after=after, limit=limit)]
        }

    @app.post("/v1/admin/reap")
    def reap(grant: KeyGrant = key) -> dict[str, int]:
        gate.admin(grant)
        return {"reaped": store.reap()}

    # -- queues and tasks ---------------------------------------------------------------------

    @app.post("/v1/queues/define")
    def define(config: QueueConfig, grant: KeyGrant = key) -> dict[str, Any]:
        gate.admin(grant)
        return store.define_queue(config).model_dump()

    @app.get("/v1/queues")
    def queues(grant: KeyGrant = key) -> dict[str, Any]:
        return {
            "queues": [
                q.model_dump()
                for q in store.queues()
                if grant.is_admin or grant.allows_queue(q.name)
            ]
        }

    @app.post("/v1/tasks", status_code=201)
    def enqueue(spec: TaskSpec, grant: KeyGrant = key) -> dict[str, str]:
        gate.queue(grant, spec.queue)
        if spec.effect_class == "public" and not grant.is_admin:
            raise Forbidden("only an admin key may queue a task with the public effect class")
        # A dependent's lease carries its dependencies' outputs, so the key must cover their queues.
        for dep in spec.depends_on:
            gate.queue(grant, store.task(dep).queue)
        return {"task_id": store.enqueue(spec)}

    @app.get("/v1/tasks/{task_id}")
    def task(task_id: str, grant: KeyGrant = key) -> dict[str, Any]:
        view = store.task(task_id)
        gate.queue(grant, view.queue)
        return view.model_dump(mode="json")

    @app.get("/v1/tasks/{task_id}/results")
    def results(task_id: str, grant: KeyGrant = key) -> dict[str, Any]:
        gate.queue(grant, store.task(task_id).queue)
        return {"results": [r.model_dump(mode="json") for r in store.results(task_id)]}

    @app.post("/v1/tasks/claim", response_model=None)
    def claim(body: ClaimBody, grant: KeyGrant = key) -> Response:
        gate.queue(grant, body.queue)
        if not grant.is_admin:
            wait = limiter.retry_after(grant.key_id, time.monotonic())
            if wait is not None:
                raise RateLimited(
                    f"over {settings.claims_per_minute} claims a minute", retry_after=wait
                )
        config = next((q for q in store.queues() if q.name == body.queue), None)
        beat = str(config.heartbeat_seconds) if config else "60"
        effects = grant.effect_filter(body.effect_classes)
        kinds = grant.kind_filter(body.kinds)
        lease = None
        if effects and kinds != []:
            lease = store.claim(
                body.queue, body.worker, kinds=kinds, effect_classes=effects, key_id=grant.key_id
            )
        if lease is None:
            return Response(
                status_code=204, headers={"retry-after": "5", "x-heartbeat-seconds": beat}
            )
        return JSONResponse(lease.model_dump(mode="json"), headers={"x-heartbeat-seconds": beat})

    @app.post("/v1/tasks/heartbeat")
    def heartbeat(body: LeaseRef, grant: KeyGrant = key) -> dict[str, Any]:
        gate.holder(grant, body.task_id)
        return store.heartbeat(body.task_id, body.fencing_token).model_dump(mode="json")

    @app.post("/v1/tasks/submit")
    def submit(body: SubmitBody, request: Request, grant: KeyGrant = key) -> dict[str, Any]:
        idem = request.headers.get("idempotency-key")
        if not idem:
            raise InvalidRequest("the Idempotency-Key header is required")
        gate.holder(grant, body.task_id)
        receipt = store.submit(
            body.task_id,
            body.fencing_token,
            body.output,
            idempotency_key=idem,
            ok=body.ok,
            error=body.error,
        )
        return receipt.model_dump(mode="json")

    @app.post("/v1/tasks/split")
    def split(body: SplitBody, grant: KeyGrant = key) -> dict[str, list[str]]:
        gate.holder(grant, body.task_id)
        parent_queue = store.task(body.task_id).queue
        for child in body.children:
            gate.queue(grant, child.queue or parent_queue)
            if (
                child.effect_class
                and not grant.is_admin
                and child.effect_class not in grant.effect_classes
            ):
                raise Forbidden("this key may not create tasks of that effect class")
        return {"children": store.split(body.task_id, body.fencing_token, body.children)}

    @app.post("/v1/tasks/park")
    def park(body: ParkBody, grant: KeyGrant = key) -> dict[str, Any]:
        gate.holder(grant, body.task_id)
        view = store.park(
            body.task_id, body.fencing_token, reason=body.reason, resume_within=body.resume_within
        )
        return view.model_dump(mode="json")

    @app.post("/v1/tasks/resume")
    def resume(body: ResumeBody, grant: KeyGrant = key) -> dict[str, Any]:
        gate.queue(grant, store.task(body.task_id).queue)
        view = store.resume(
            body.task_id, body.input, park=body.park, by=grant.key_id, allow_self=grant.is_admin
        )
        return view.model_dump(mode="json")

    @app.post("/v1/tasks/cancel")
    def cancel(body: CancelBody, grant: KeyGrant = key) -> dict[str, Any]:
        view = store.task(body.task_id)
        gate.queue(grant, view.queue)
        if not grant.is_admin and view.effect_class not in grant.effect_classes:
            raise Forbidden("this key may not cancel tasks of that effect class")
        return store.cancel(body.task_id, body.reason).model_dump(mode="json")

    # -- model jobs ---------------------------------------------------------------------------

    @app.post("/v1/jobs", status_code=201)
    def register_job(body: JobBody, grant: KeyGrant = key) -> dict[str, Any]:
        gate.holder(grant, body.task_id)
        job = store.register_job(body.task_id, body.fencing_token, body.job_id, body.model)
        return job.model_dump(mode="json")

    @app.post("/v1/jobs/{job_id}")
    def update_job(job_id: str, body: JobUpdate, grant: KeyGrant = key) -> dict[str, Any]:
        job = next((j for j in store.jobs() if j.job_id == job_id), None)
        if job is not None and not grant.is_admin:
            gate.holder(grant, job.task_id)
        updated = store.update_job(job_id, **body.model_dump(exclude_none=True))
        return updated.model_dump(mode="json")

    @app.get("/v1/jobs")
    def jobs(task_id: str | None = None, grant: KeyGrant = key) -> dict[str, Any]:
        if task_id is None:
            gate.admin(grant)
        else:
            gate.queue(grant, store.task(task_id).queue)
        return {"jobs": [j.model_dump(mode="json") for j in store.jobs(task_id)]}

    # -- atomic storage -----------------------------------------------------------------------

    @app.post("/v1/stores/batch")
    def put_records(body: BatchBody, grant: KeyGrant = key) -> dict[str, Any]:
        gate.store_scope(grant)
        return {"records": [r.model_dump(mode="json") for r in store.put_records(body.writes)]}

    @app.post("/v1/stores/{store_name}/records/{record_key}")
    def put_record(
        store_name: str, record_key: str, body: RecordBody, grant: KeyGrant = key
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        write = RecordWrite(
            store=store_name,
            key=record_key,
            value=body.value,
            expected_version=body.expected_version,
        )
        return store.put_records([write])[0].model_dump(mode="json")

    @app.get("/v1/stores/{store_name}/records/{record_key}")
    def get_record(
        store_name: str, record_key: str, version: int | None = None, grant: KeyGrant = key
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        return store.get_record(store_name, record_key, version).model_dump(mode="json")

    @app.get("/v1/stores/{store_name}/records/{record_key}/history")
    def history(store_name: str, record_key: str, grant: KeyGrant = key) -> dict[str, Any]:
        gate.store_scope(grant)
        return {
            "versions": [
                v.model_dump(mode="json") for v in store.record_history(store_name, record_key)
            ]
        }

    @app.post("/v1/blobs", status_code=201)
    async def put_blob(request: Request, grant: KeyGrant = key) -> dict[str, Any]:
        gate.store_scope(grant)
        data = await request.body()
        digest = await run_in_threadpool(store.put_blob, data)
        return {"sha256": digest, "size": len(data)}

    @app.get("/v1/blobs/{sha256}")
    def get_blob(sha256: str, grant: KeyGrant = key) -> Response:
        gate.store_scope(grant)
        return Response(store.get_blob(sha256), media_type="application/octet-stream")

    mount_storage_and_admin_routes(app, store, gate)

    # -- the agent door and pages -------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/connect", response_class=HTMLResponse, include_in_schema=False)
    def connect() -> str:
        return pages.connect_html(settings)

    @app.get("/llms.txt", response_class=PlainTextResponse, include_in_schema=False)
    def llms() -> str:
        return pages.llms_txt(settings)

    @app.get("/.well-known/agent-card.json", include_in_schema=False)
    def agent_card() -> dict[str, Any]:
        return pages.agent_card(settings)

    @app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
    def robots() -> str:
        return pages.ROBOTS

    @app.get("/board", response_class=HTMLResponse, include_in_schema=False)
    def board() -> str:
        return pages.board_html(store.health(), settings)

    pages.mount_work_forms(app, store, gate, limiter, settings)

    # The MCP door answers on /mcp; mounted last so every route above wins.
    app.mount("/", mcp_app)
    return app
