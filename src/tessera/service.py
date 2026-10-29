"""Tessera's HTTP service (SPEC §7). Every route ends in one Store call; the service adds bearer keys,
scopes, namespace isolation (/ns/{namespace} and X-Namespace), Layer 1/2 governance, POST-only writes,
a claim rate limit, problem+json errors, the reaper loop, the MCP door, and the agent door (/connect,
/llms.txt, /llms-full.txt, /favicon.svg, the agent card, Console Board v2 and keyed web forms)."""

from __future__ import annotations

import asyncio
import hmac
import json
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from http import HTTPStatus
from typing import Any, cast
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from tessera import pages
from tessera._access_boundary import access_dispatch
from tessera._access_contract import connect_html
from tessera._access_routes import mount_access_routes
from tessera._service_buckets import (
    check_claimed_content,
    mount_storage_and_admin_routes,
    parse_governance_header,
)
from tessera._service_executions import mount_execution_routes
from tessera._service_intelligence import mount_intelligence_routes, slice_bytes_response
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
from tessera.models import (
    NAMESPACE_PATTERN,
    ChildSpec,
    JobStatus,
    Lease,
    QueueConfig,
    Receipt,
    RecordVersion,
    RecordWrite,
    TaskSpec,
    VisibilityScope,
)
from tessera.store import Store

_NS_RE = re.compile(NAMESPACE_PATTERN)

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
    "/v1/handshake",
    "/v1/reconcile",
    "/v1/reconcile/adjudicate",
    "/work/claim",
    "/work/submit",
    "/work/heartbeat",
    "/work/resume",
)
EDGE_EXEMPT = frozenset({"/v1/health", "/healthz", "/api/health"})
PAGE_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
    "script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
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
    output: Any = None
    ok: StrictBool = True
    error: str | None = None
    execution_report: dict[str, Any] | None = None


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
    namespace: str | None = None
    producer_surface: str | None = None
    visibility_scope: VisibilityScope = "public_commons"
    allowed_surfaces: list[str] = Field(default_factory=list)
    target_cohorts: list[str] = Field(default_factory=lambda: ["all"])
    tags: list[str] = Field(default_factory=list)


class BatchBody(_Body):
    writes: list[RecordWrite]


def create_app(store: Store | None = None, settings: ServiceSettings | None = None) -> FastAPI:
    settings = settings or ServiceSettings.from_env()
    store = store or Store(settings.db_path)
    gate = Gate(store)
    limiter = ClaimLimiter(settings.claims_per_minute, settings)

    from tessera.mcp_door import build_mcp  # imported here: the MCP SDK is an optional extra

    mcp_server, mcp_app = build_mcp(store, gate, settings, limiter)

    async def reaper() -> None:
        while True:
            await asyncio.sleep(settings.reaper_seconds)
            with suppress(Exception):
                await run_in_threadpool(store.reap)
                await run_in_threadpool(store.purge_expired_principals)

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

    async def require_key(request: Request) -> AsyncIterator[KeyGrant]:
        grant = cast(
            KeyGrant,
            getattr(request.state, "access_grant", None)
            or await run_in_threadpool(gate.grant, bearer_secret(request)),
        )
        eff_surface = gate.effective_surface(grant, request.headers.get("x-surface-id"))
        with store.scope_context(
            surface=eff_surface or "",
            key_id=grant.key_id,
            is_admin=grant.is_admin,
            anonymous=grant.anonymous,
            labels=grant.labels,
        ):
            yield grant

    key: Any = Depends(require_key)

    @app.middleware("http")
    async def edge_and_namespace_lock(
        request: Request, call_next: Callable[[Request], Any]
    ) -> Response:
        raw_path = request.scope.get("path", "/")
        path_ns: str | None = None
        if raw_path.startswith("/ns/"):
            ns_seg, sep, tail = raw_path[4:].partition("/")
            if not ns_seg or not _NS_RE.fullmatch(ns_seg):
                return problem(400, "invalid_request", f"invalid namespace '{ns_seg}'")
            path_ns = ns_seg
            rewritten = "/" + tail if sep else "/"
            request.scope["path"] = rewritten
            request.scope["raw_path"] = rewritten.encode("utf-8")

        hdr_ns = (
            request.headers.get("x-namespace") or request.headers.get("x-tessera-namespace") or ""
        ).strip()
        if hdr_ns and not _NS_RE.fullmatch(hdr_ns):
            return problem(400, "invalid_request", f"invalid namespace '{hdr_ns}'")
        if path_ns and hdr_ns and path_ns != hdr_ns:
            return problem(
                400,
                "invalid_request",
                f"path namespace '{path_ns}' conflicts with X-Namespace '{hdr_ns}'",
            )
        effective_ns = path_ns or hdr_ns or "main"
        request.state.explicit_namespace = bool(path_ns or hdr_ns)

        effective_path = request.scope.get("path", "/")
        if settings.edge_secret and effective_path not in EDGE_EXEMPT:
            sent = request.headers.get("x-edge-auth", "")
            if not hmac.compare_digest(sent.encode(), settings.edge_secret.encode()):
                return problem(403, "forbidden", "requests must come through the edge")

        with store.scope_context(namespace=effective_ns, surface=""):
            # Surface identity is established only by the authenticated dependency.
            response: Response = await access_dispatch(request, call_next, store, gate, settings)

        if effective_path.startswith(("/v1/", "/work", "/api/")):
            response.headers.setdefault("cache-control", "no-store")
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers.setdefault("content-security-policy", PAGE_CSP)
        response.headers.setdefault("x-content-type-options", "nosniff")
        return response

    @app.middleware("http")
    async def agent_cors(request: Request, call_next: Callable[[Request], Any]) -> Response:
        is_agent = (
            request.url.path.startswith(("/v1/", "/mcp", "/ns/"))
            or request.url.path == "/connect.json"
        )
        origin = request.headers.get("origin")
        if origin and is_agent:
            try:
                parsed = urlsplit(origin)
                valid = (
                    parsed.scheme in {"http", "https"}
                    and bool(parsed.netloc)
                    and not parsed.username
                    and not parsed.password
                    and parsed.path in {"", "/"}
                    and not parsed.query
                    and not parsed.fragment
                )
            except ValueError:
                valid = False
            if not valid:
                return problem(403, "forbidden", "invalid agent Origin")
        if request.url.path.endswith("/mcp") and settings.allowed_hosts:
            host = request.headers.get("host", "")
            if not any(
                host == allowed or (allowed.endswith(":*") and host.startswith(allowed[:-1]))
                for allowed in settings.allowed_hosts
            ):
                return problem(421, "invalid_request", "invalid agent Host")
        if is_agent and request.method == "OPTIONS":
            response = Response(status_code=204)
        else:
            response = await call_next(request)
        if is_agent:
            response.headers["access-control-allow-origin"] = "*"
            response.headers["access-control-allow-methods"] = "GET, POST, OPTIONS"
            response.headers["access-control-allow-headers"] = (
                "Authorization, Content-Type, If-Match, X-Request-Id, X-Agent-Label, Idempotency-Key, MCP-Protocol-Version, X-Namespace"
            )
            response.headers["access-control-expose-headers"] = "ETag, Retry-After, X-Request-Id"
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
    @app.get("/api/health", include_in_schema=False)
    @app.get("/healthz", include_in_schema=False)
    def health() -> dict[str, Any]:
        return store.health(public_only=True).model_dump(mode="json")

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
    def enqueue(spec: TaskSpec, request: Request, grant: KeyGrant = key) -> dict[str, str]:
        gate.queue(grant, spec.queue)
        gate.effect(grant, spec.effect_class)
        hdr_idem = (request.headers.get("idempotency-key") or "").strip() or None
        if spec.idempotency_key is None and hdr_idem is not None:
            spec = spec.model_copy(update={"idempotency_key": hdr_idem})
        # A dependent's lease carries its dependencies' outputs, so the key must cover their queues.
        for dep in spec.depends_on:
            gate.queue(grant, store.task(dep).queue)
        task_id = store.enqueue(spec)
        limiter.idle.work(store.current_scope().namespace, spec.queue)
        return {"task_id": task_id}

    @app.get("/v1/tasks/{task_id}")
    def task(task_id: str, grant: KeyGrant = key) -> dict[str, Any]:
        view = store.task(task_id)
        gate.queue(grant, view.queue)
        return view.model_dump(mode="json")

    @app.get("/v1/tasks/{task_id}/results")
    def results(task_id: str, grant: KeyGrant = key) -> dict[str, Any]:
        gate.queue(grant, store.task(task_id).queue)
        return {"results": [r.model_dump(mode="json") for r in store.results(task_id)]}

    @app.post(
        "/v1/tasks/claim",
        response_model=Lease,
        responses={
            204: {"description": "No eligible task is ready; Retry-After gives the retry interval."}
        },
    )
    async def claim(body: ClaimBody, grant: KeyGrant = key) -> Response:
        gate.queue(grant, body.queue)
        configs = await run_in_threadpool(store.queues)
        config = next((q for q in configs if q.name == body.queue), None)
        beat = str(config.heartbeat_seconds) if config else "60"
        effects = grant.effect_filter(body.effect_classes)
        kinds = grant.kind_filter(body.kinds)
        lease = None
        async with limiter.admit(grant.key_id, grant.is_admin) as permit:
            await run_in_threadpool(gate.recheck, grant)
            if effects and kinds != []:
                lease = await run_in_threadpool(
                    store.claim,
                    body.queue,
                    body.worker,
                    kinds=kinds,
                    effect_classes=effects,
                    key_id=grant.key_id,
                )
        admission_headers = {
            "x-claim-wait-ms": str(round(permit.wait_seconds * 1000)),
            "x-claim-concurrency": str(limiter.admission.limit),
        }
        namespace = store.current_scope().namespace
        if lease is None:
            retry = limiter.idle.empty(namespace, body.queue, grant.key_id)
            return Response(
                status_code=204,
                headers={
                    "retry-after": str(retry),
                    "x-heartbeat-seconds": beat,
                    **admission_headers,
                },
            )
        limiter.idle.granted(namespace, body.queue, grant.key_id)
        return JSONResponse(
            lease.model_dump(mode="json"),
            headers={"x-heartbeat-seconds": beat, **admission_headers},
        )

    @app.post("/v1/tasks/heartbeat", response_model=Lease)
    def heartbeat(body: LeaseRef, grant: KeyGrant = key) -> dict[str, Any]:
        gate.holder(grant, body.task_id)
        return store.heartbeat(body.task_id, body.fencing_token).model_dump(mode="json")

    @app.post("/v1/tasks/submit", response_model=Receipt)
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
            completed_by_key=grant.key_id,
            execution_report=body.execution_report,
        )
        return receipt.model_dump(mode="json")

    @app.post("/v1/tasks/split")
    def split(body: SplitBody, grant: KeyGrant = key) -> dict[str, list[str]]:
        gate.holder(grant, body.task_id)
        parent = store.task(body.task_id)
        for child in body.children:
            gate.queue(grant, child.queue or parent.queue)
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
        target = store.task(body.task_id)
        gate.queue(grant, target.queue)
        gate.effect(grant, target.effect_class)
        view = store.resume(
            body.task_id, body.input, park=body.park, by=grant.key_id, allow_self=grant.is_admin
        )
        return view.model_dump(mode="json")

    @app.post("/v1/tasks/cancel")
    def cancel(body: CancelBody, grant: KeyGrant = key) -> dict[str, Any]:
        gate.cancel_task(grant, body.task_id)
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
    def put_records(body: BatchBody, request: Request, grant: KeyGrant = key) -> dict[str, Any]:
        gate.store_scope(grant)
        surf_hdr = request.headers.get("x-surface-id")
        for w in body.writes:
            gate.surface_write(grant, w.store, surf_hdr)
        return {"records": [r.model_dump(mode="json") for r in store.put_records(body.writes)]}

    @app.post("/v1/stores/{store_name}/records/{record_key}", response_model=RecordVersion)
    def put_record(
        store_name: str,
        record_key: str,
        body: RecordBody,
        request: Request,
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        gate.store_scope(grant)
        gate.surface_write(grant, store_name, request.headers.get("x-surface-id"))
        write_kwargs: dict[str, Any] = {
            "store": store_name,
            "key": record_key,
            "value": body.value,
            "expected_version": body.expected_version,
            "visibility_scope": body.visibility_scope,
            "allowed_surfaces": body.allowed_surfaces,
            "target_cohorts": body.target_cohorts,
            "tags": body.tags,
        }
        if body.namespace is not None:
            write_kwargs["namespace"] = body.namespace
        if body.producer_surface is not None:
            write_kwargs["producer_surface"] = body.producer_surface
        if body.expected_version is None and (match := request.headers.get("if-match")):
            try:
                write_kwargs["expected_version"] = int(match.strip('"'))
            except ValueError as exc:
                raise InvalidRequest("If-Match must contain a record version integer") from exc
        write = RecordWrite(**write_kwargs)
        return store.put_records([write])[0].model_dump(mode="json")

    @app.get("/v1/stores/{store_name}/records/{record_key}", response_model=RecordVersion)
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
        gov = parse_governance_header(request)
        data = await request.body()
        check_claimed_content(request, data)
        digest = await run_in_threadpool(lambda: store.put_blob(data, **gov))
        return {"sha256": digest, "size": len(data)}

    @app.get("/v1/blobs/{sha256}")
    def get_blob(sha256: str, request: Request, grant: KeyGrant = key) -> Response:
        gate.store_scope(grant)
        return slice_bytes_response(
            store.get_blob(sha256),
            request.headers.get("range"),
            media_type="application/octet-stream",
            headers={"etag": f'"{sha256}"'},
        )

    mount_access_routes(app, store, gate, settings, require_key)
    mount_execution_routes(app, store, gate, require_key)
    mount_storage_and_admin_routes(app, store, gate, require_key)
    mount_intelligence_routes(app, store, gate, require_key)

    # -- the agent door and pages -------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/connect", response_class=HTMLResponse, include_in_schema=False)
    async def connect() -> str:
        return connect_html(
            settings.public_url,
            settings.anonymous_access_enabled,
            len(await mcp_server.list_tools()),
        )

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> Response:
        return Response(pages.favicon_svg(), media_type="image/svg+xml")

    @app.get("/llms.txt", response_class=PlainTextResponse, include_in_schema=False)
    async def llms() -> str:
        tools = await mcp_server.list_tools()
        intro = pages.llms_txt(settings).replace(
            "You need a key from the operator. One key per agent; keys are scoped to queues and expire.",
            "Discovery is public. Personal assistants and coding agents can mint a tracked demo session at /v1/session/anonymous when enabled. Private or privileged work needs an operator-issued scoped key.",
        )
        intro = intro.replace("26 tools", f"{len(tools)} tools")
        return (
            intro
            + "\n## Access modes\nMachine-readable contract: /connect.json. Tools: "
            + ", ".join(t.name for t in tools)
            + "\nAnonymous sessions last one hour; private demo artifacts and access audit expire after 24 hours. Bearer tokens never belong in URLs.\n"
        )

    @app.get("/llms-full.txt", response_class=PlainTextResponse, include_in_schema=False)
    def llms_full() -> str:
        return pages.llms_full_txt(settings)

    @app.get("/.well-known/agent-card.json", include_in_schema=False)
    async def agent_card() -> dict[str, Any]:
        card = pages.agent_card(settings)
        tools = await mcp_server.list_tools()
        card["tools"] = [t.model_dump(mode="json", exclude_none=True) for t in tools]
        card["skills"] = [
            {"id": t.name, "name": t.name, "description": t.description or ""} for t in tools
        ]
        card["capabilities"] = {"streaming": "long-poll", "pushNotifications": False}
        card["security"] = [{}, {"bearer": []}]
        card["accessModesUrl"] = f"{settings.public_url}/connect.json"
        return card

    @app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
    def robots() -> str:
        return pages.ROBOTS

    @app.get("/board", response_class=HTMLResponse, include_in_schema=False)
    def board() -> str:
        snap = store.board_snapshot()
        return pages.board_html(snap["health"], settings, snapshot=snap)

    pages.mount_work_forms(app, store, gate, limiter, settings)

    # The MCP door answers on /mcp; mounted last so every route above wins.
    # Keep JSON helper usage cleanly imported for strict linters.
    _ = json
    app.mount("/", mcp_app)
    original_openapi = app.openapi

    def openapi_with_access() -> dict[str, Any]:
        schema = original_openapi()
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["bearer"] = {
            "type": "http",
            "scheme": "bearer",
            "description": "A tracked tan_ session token or scoped tsk_ API key.",
        }
        public = {"/v1/health", "/v1/public/activity", "/v1/session/anonymous", "/connect.json"}
        for path, operations in schema["paths"].items():
            for operation in operations.values():
                if isinstance(operation, dict):
                    operation["security"] = [] if path in public else [{"bearer": []}]
        return schema

    app.openapi = openapi_with_access  # type: ignore[method-assign]
    return app
