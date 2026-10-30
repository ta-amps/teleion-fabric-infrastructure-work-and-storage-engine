"""One authenticated request boundary for HTTP and MCP; discovery remains public."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from tessera._principals import DemoPayloadTooLarge
from tessera.access import Gate, ServiceSettings, bearer_secret
from tessera.errors import InvalidRequest, RateLimited, TesseraError
from tessera.grants import KeyGrant
from tessera.store import Store


@dataclass
class RequestAccess:
    grant: KeyGrant | None
    meta: dict[str, Any]


REQUEST_ACCESS: ContextVar[RequestAccess | None] = ContextVar("tessera_access", default=None)
MCP_ACTIONS = {
    "put_record": "putRecords",
    "put_records": "putRecords",
    "get_record": "getRecord",
    "record_history": "recordHistory",
    "list_stores": "listStores",
    "list_records": "listRecords",
    "search_nouns": "searchNouns",
    "claim_events": "claimEvents",
    "whoami": "me",
    "open_anonymous_session": "mint_session",
    "explain_access_modes": "discovery",
    "get_blob": "getBlob",
    "put_blob": "putBlob",
    "get_object": "getObject",
    "put_object": "putObject",
    "list_buckets": "listBuckets",
    "list_objects": "listObjects",
    "list_verbs": "listVerbs",
    "buckets": "listBuckets",
    "define_bucket": "defineBucket",
    "object_history": "getObject",
    "list_feeds": "readFeed",
    "poll_feed": "readFeed",
    "publish_feed": "publishFeed",
    "read_feed": "readFeed",
    "match_reflex": "matchReflex",
    "recall_execution": "recallExecution",
    "get_execution": "getExecution",
    "list_executions": "listExecutions",
    "list_execution_observations": "listExecutions",
}
PUBLIC_PATHS = {"/v1/health", "/v1/openapi.json", "/v1/public/activity", "/v1/session/anonymous"}


def access_error(exc: TesseraError) -> JSONResponse:
    headers = (
        {"retry-after": str(max(1, int(exc.retry_after + 0.999)))}
        if isinstance(exc, RateLimited)
        else {}
    )
    return JSONResponse(
        {
            "type": "about:blank",
            "title": exc.code,
            "status": exc.status,
            "detail": exc.detail,
            "code": exc.code,
        },
        status_code=exc.status,
        headers=headers,
        media_type="application/problem+json",
    )


def http_action(path: str, method: str, args: dict[str, Any]) -> str:
    if path.startswith("/work/"):
        return path.rsplit("/", 1)[1]
    if path == "/v1/executions/recall":
        return "recallExecution"
    if path == "/v1/executions":
        return "listExecutions"
    if path.startswith("/v1/executions/"):
        args["run_id"] = path.rsplit("/", 1)[1]
        return "getExecution"
    if path == "/v1/session/anonymous":
        return "mint_session"
    if path == "/v1/me":
        return "me"
    if path in {"/v1/me/audit", "/v1/admin/audit"}:
        return "admin_audit" if "/admin/" in path else "my_audit"
    if path == "/v1/keys":
        return "mint_key" if method == "POST" else "my_keys"
    if path.endswith("/revoke"):
        args["key_id"] = path.split("/")[-2]
        return "revoke_key"
    if path == "/v1/tasks":
        return "enqueue"
    if path in {
        "/v1/tasks/claim",
        "/v1/tasks/heartbeat",
        "/v1/tasks/submit",
        "/v1/tasks/cancel",
        "/v1/tasks/park",
        "/v1/tasks/resume",
        "/v1/tasks/split",
    }:
        return path.rsplit("/", 1)[1]
    if path.startswith("/v1/tasks/") and path.endswith("/executions"):
        args["task_id"] = path.split("/")[-2]
        return "listExecutions"
    match = re.fullmatch(r"/v1/tasks/([^/]+)(/results)?", path)
    if match:
        args["task_id"] = match[1]
        return "results" if match[2] else "task"
    match = re.fullmatch(r"/v1/claims/([^/]+)/events", path)
    if match:
        args["task_id"] = match[1]
        return "claimEvents"
    if path == "/v1/stores/batch":
        return "putRecords"
    match = re.fullmatch(r"/v1/stores/([^/]+)/records(?:/([^/]+)(/history)?)?", path)
    if match:
        args["store"] = match[1]
        if match[2]:
            args["key"] = match[2]
        return (
            "putRecords"
            if method == "POST"
            else "recordHistory"
            if match[3]
            else "getRecord"
            if match[2]
            else "listRecords"
        )
    if path.startswith("/v1/buckets/"):
        return (
            "defineBucket"
            if path.endswith("/define")
            else "putObject"
            if method == "POST"
            else "getObject"
        )
    if path.startswith("/v1/blobs/"):
        return "getBlob"
    if path.startswith("/v1/feeds"):
        return "publishFeed" if method == "POST" else "readFeed"
    if path.startswith("/v1/jobs"):
        return "updateJob" if method == "POST" else "query"
    return {
        "/v1/queues": "queues",
        "/v1/verbs": "listVerbs",
        "/v1/handshake": "handshake",
        "/v1/reconcile": "reconcile",
        "/v1/reconcile/adjudicate": "reconcile",
        "/v1/counts": "counts",
        "/v1/stores": "listStores",
        "/v1/search": "searchNouns",
        "/v1/blobs": "putBlob",
    }.get(path, "mutation" if method == "POST" else "query")


async def access_dispatch(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
    store: Store,
    gate: Gate,
    settings: ServiceSettings,
) -> Response:
    path = request.scope.get("path", "/")
    work_form = path.startswith("/work/") and request.method == "POST"
    if not (path.startswith("/v1/") or path == "/mcp" or work_form):
        return await call_next(request)
    request_id = request.headers.get("x-request-id", "")
    if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", request_id):
        request_id = secrets.token_hex(16)
    # CF-Connecting-IP is accepted only behind the configured trusted edge lock.
    ip = (
        request.headers.get("cf-connecting-ip", "unknown")
        if settings.edge_secret
        else request.client.host
        if request.client
        else "unknown"
    )
    identity = await run_in_threadpool(
        store.request_identity, ip, request.headers.get("user-agent", "")
    )
    meta: dict[str, Any] = {**identity, "request_id": request_id, "route_or_tool": path}
    grant: KeyGrant | None = None
    action = path
    args: dict[str, Any] = {}
    try:
        form: dict[str, str] = {}
        if work_form:
            raw_form = await request.body()
            if len(raw_form) > 65536:
                raise InvalidRequest("form request exceeds byte limit")
            try:
                form = {k: v[-1] for k, v in parse_qs(raw_form.decode(), max_num_fields=20).items()}
            except (ValueError, UnicodeError) as exc:
                raise InvalidRequest("invalid work form") from exc
            meta["bytes_in"] = len(raw_form)
        secret = bearer_secret(request) or form.pop("key", None)
        if secret:
            grant = await run_in_threadpool(gate.grant, secret)
        body: Any = form if work_form else {}
        if work_form:
            for field in ("output", "input"):
                if field in body:
                    try:
                        body[field] = json.loads(body[field])
                    except ValueError as exc:
                        raise InvalidRequest("form output/input must be JSON") from exc
        if request.method in {"POST", "PUT", "PATCH"} and "json" in request.headers.get(
            "content-type", ""
        ):
            bounded = bool(
                (grant and grant.anonymous)
                or path == "/v1/session/anonymous"
                or (not grant and path == "/mcp")
            )
            if bounded:
                chunks = []
                total = 0
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > 65536:
                        raise DemoPayloadTooLarge("demo request exceeds byte limit")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                request._body = raw
            else:
                raw = await request.body()
            meta["bytes_in"] = len(raw)
            try:
                body = json.loads(raw) if raw else {}
            except ValueError as exc:
                raise InvalidRequest("request body must be valid JSON") from exc
            if not isinstance(body, dict):
                raise InvalidRequest("request body must be a JSON object")
        args = dict(body)
        public = path in PUBLIC_PATHS
        if path == "/mcp":
            method = body.get("method")
            if request.method == "POST" and not isinstance(method, str):
                raise InvalidRequest("MCP method must be a string")
            params = body.get("params") or {}
            if not isinstance(params, dict):
                raise InvalidRequest("MCP params must be an object")
            tool = params.get("name", "")
            if not isinstance(tool, str) or len(tool) > 128:
                raise InvalidRequest("MCP tool name must be a string of at most 128 characters")
            if method == "tools/call":
                raw_args = params.get("arguments") or {}
                if not isinstance(raw_args, dict):
                    raise InvalidRequest("tool arguments must be an object")
                args = dict(raw_args)
                action = MCP_ACTIONS.get(tool, tool)
                meta["route_or_tool"] = tool
                public = tool in {"open_anonymous_session", "explain_access_modes"}
            else:
                action = "discovery"
                public = (
                    method in {"initialize", "tools/list", "notifications/initialized", "ping"}
                    or request.method != "POST"
                )
        else:
            action = http_action(path, request.method, args)
        if (
            action in {"submit", "recallExecution"}
            and args.get("execution_report") is not None
            and not isinstance(args["execution_report"], dict)
        ):
            raise InvalidRequest("execution_report must be a JSON object, not serialized JSON text")
        if not public and grant is None:
            grant = await run_in_threadpool(gate.grant, secret)
        namespace = store.current_scope().namespace
        if grant and grant.anonymous and not getattr(request.state, "explicit_namespace", False):
            namespace = grant.namespace
        meta["namespace"] = namespace
        if grant:
            if not public:
                await run_in_threadpool(
                    store.enforce_principal,
                    grant,
                    action,
                    args,
                    namespace,
                    ip_hash=meta.get("ip_hash"),
                )
            elif not grant.allows_namespace(namespace):
                from tessera.errors import ScopeDenied

                raise ScopeDenied("this principal may not access this namespace")
        request.state.access_grant = grant
        request.state.access_meta = meta
        token = REQUEST_ACCESS.set(RequestAccess(grant, meta))
        try:
            with store.scope_context(
                namespace=namespace,
                key_id=grant.key_id if grant else None,
                surface=gate.effective_surface(grant, request.headers.get("x-surface-id")) or ""
                if grant
                else "",
                is_admin=grant.is_admin if grant else False,
                anonymous=grant.anonymous if grant else False,
                labels=grant.labels if grant else {},
            ):
                response = await call_next(request)
                chunks = [chunk async for chunk in response.body_iterator]  # type: ignore[attr-defined]
                raw_response = b"".join(
                    chunk.encode() if isinstance(chunk, str) else bytes(chunk) for chunk in chunks
                )
                response = Response(
                    content=raw_response,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                )
        finally:
            REQUEST_ACCESS.reset(token)
        meta["bytes_out"] = len(raw_response)
        output: dict[str, Any] = getattr(request.state, "access_result", {})
        try:
            decoded = json.loads(raw_response)
            if isinstance(decoded, dict):
                output = decoded
                if path == "/mcp":
                    result = decoded.get("result", {})
                    output = result.get("structuredContent") or result
                    if decoded.get("error"):
                        output = {"isError": True, "code": "mcp_error"}
        except ValueError:
            pass
        if action == "mint_session" and response.status_code < 400:
            minted = output.get("session_token")
            if isinstance(minted, str):
                grant = await run_in_threadpool(gate.grant, minted)
                meta["namespace"] = grant.namespace
        if grant or not public or action == "mint_session":
            status = 400 if output.get("isError") else response.status_code
            await run_in_threadpool(
                store.audit_access,
                grant,
                action,
                args,
                status=status,
                meta=meta,
                output=output,
                error_code=output.get("code") if status >= 400 else None,
            )
        response.headers["x-request-id"] = request_id
        return response
    except TesseraError as exc:
        await run_in_threadpool(
            store.audit_access,
            grant,
            action,
            args,
            status=exc.status,
            meta=meta,
            error_code=exc.code,
        )
        response = access_error(exc)
        response.headers["x-request-id"] = request_id
        return response
