"""Anonymous session, self identity, audit, key lifecycle and long-poll HTTP routes."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from tessera._access_contract import CONNECT_SCRIPT, access_description
from tessera._access_models import (
    AnonymousSession,
    AuditEvents,
    ClaimEvents,
    IssuedKey,
    KeyList,
    PrincipalView,
    PublicActivity,
    Verbs,
)
from tessera._principals import public_principal
from tessera._service_buckets import IssueKeyBody
from tessera.access import Gate, ServiceSettings
from tessera.errors import Forbidden, InvalidRequest, Unavailable
from tessera.grants import KeyGrant
from tessera.store import Store


class AnonymousBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_label: str = Field(default="", max_length=64)


class AccountKeyBody(IssueKeyBody):
    account_id: str | None = Field(default=None, min_length=1, max_length=128)


def claim_events(
    store: Store, gate: Gate, grant: KeyGrant, task_id: str, after: int = 0
) -> dict[str, Any]:
    gate.queue(grant, store.task(task_id).queue)
    events = [e.model_dump(mode="json") for e in store.events(after=after) if e.task_id == task_id]
    return {
        "events": events,
        "cursor": events[-1]["seq"] if events else after,
        "streaming": "long-poll",
    }


def list_verbs(
    store: Store, grant: KeyGrant, queue: str | None = None, status: str = "ready", limit: int = 100
) -> dict[str, Any]:
    if status not in {"ready", "leased", "waiting", "done", "dead", "cancelled", "all"}:
        raise InvalidRequest("invalid verb status")
    ns = store.current_scope().namespace
    sql = "SELECT * FROM tasks WHERE namespace=?"
    params: list[Any] = [ns]
    if queue:
        sql += " AND queue=?"
        params.append(queue)
    if status != "all":
        sql += " AND state=?"
        params.append(status)
    sql += " ORDER BY created_at,id"
    verbs = []
    with store._read() as c:
        for row in c.execute(sql, params):
            if (grant.is_admin or grant.allows_queue(row["queue"])) and store._task_visible(row):
                verbs.append(store._task_view(c, ns, row["id"]).model_dump(mode="json"))
                if len(verbs) >= max(1, min(limit, 200)):
                    break
    return {"verbs": verbs}


def mount_access_routes(
    app: FastAPI,
    store: Store,
    gate: Gate,
    settings: ServiceSettings,
    require_key: Callable[..., Any],
) -> None:
    key: Any = Depends(require_key)

    @app.get("/connect.json")
    def connect_json() -> dict[str, Any]:
        return access_description(settings.public_url, settings.anonymous_access_enabled)

    @app.get("/connect.js", include_in_schema=False)
    def connect_script() -> Response:
        return Response(CONNECT_SCRIPT, media_type="application/javascript")

    @app.post("/v1/session/anonymous", status_code=201, response_model=AnonymousSession)
    def open_anonymous(body: AnonymousBody, request: Request) -> dict[str, Any]:
        if not settings.anonymous_access_enabled:
            raise Unavailable("anonymous sessions are not enabled")
        meta = request.state.access_meta
        return store.open_anonymous_session(body.agent_label, ip_hash=meta["ip_hash"])

    @app.get("/v1/verbs", response_model=Verbs)
    def verbs(
        queue: str | None = None, status: str = "ready", limit: int = 100, grant: KeyGrant = key
    ) -> dict[str, Any]:
        return list_verbs(store, grant, queue, status, limit)

    @app.get("/v1/me", response_model=PrincipalView)
    def me(grant: KeyGrant = key) -> dict[str, Any]:
        return public_principal(grant)

    @app.get("/v1/me/audit", response_model=AuditEvents)
    def my_audit(after: int = 0, limit: int = 50, grant: KeyGrant = key) -> dict[str, Any]:
        return store.own_audit(grant, after=after, limit=limit)

    @app.get("/v1/admin/audit", response_model=AuditEvents)
    def admin_audit(after: int = 0, limit: int = 50, grant: KeyGrant = key) -> dict[str, Any]:
        return store.own_audit(grant, after=after, limit=limit, admin=True)

    @app.get("/v1/public/activity", response_model=PublicActivity)
    def public_activity() -> dict[str, Any]:
        return store.public_activity()

    @app.post("/v1/keys", status_code=201, response_model=IssuedKey)
    def issue_key(body: AccountKeyBody, grant: KeyGrant = key) -> dict[str, Any]:
        if not grant.is_admin:
            if "admin:keys" not in grant.scopes:
                raise Forbidden("this needs an admin:keys scope")
            if (
                not set(body.scopes) <= set(grant.scopes)
                or not set(body.queues) <= set(grant.queues)
                or not set(body.kinds) <= set(grant.kinds)
                or not set(body.effect_classes) <= set(grant.effect_classes)
                or store.clock.now() + body.days * 86400 > grant.expires_at
                or (body.account_id and body.account_id != grant.account_id)
            ):
                raise Forbidden("keys cannot expand the issuer grants, lifetime or account")
        if not grant.is_admin:
            parent_fine = any(
                ":" in scope and not scope.startswith(("surface:", "namespace:"))
                for scope in grant.scopes
            )
            child_fine = any(
                ":" in scope and not scope.startswith(("surface:", "namespace:"))
                for scope in body.scopes
            )
            if parent_fine and not child_fine:
                raise Forbidden("delegated keys must retain fine-grained scope enforcement")
            parent_namespaces = {
                scope for scope in grant.scopes if scope.startswith("namespace:")
            } or {"namespace:main"}
            child_namespaces = {
                scope for scope in body.scopes if scope.startswith("namespace:")
            } or {"namespace:main"}
            if "namespace:*" not in parent_namespaces and not child_namespaces <= parent_namespaces:
                raise Forbidden("delegated keys cannot expand their effective namespaces")
            proposed = grant.model_copy(update={"label": body.label, "scopes": body.scopes})
            if proposed.bound_surface() != grant.bound_surface():
                raise Forbidden("keys cannot expand surface authority through their labels")
        secret, issued = store.issue_key(
            body.label,
            queues=body.queues,
            kinds=body.kinds,
            effect_classes=body.effect_classes,
            scopes=body.scopes,
            days=body.days,
            account_id=body.account_id or grant.account_id,
        )
        return {"key": secret, **issued.model_dump(mode="json")}

    @app.get("/v1/keys", response_model=KeyList)
    def my_keys(grant: KeyGrant = key) -> dict[str, Any]:
        if grant.anonymous:
            raise Forbidden("keyed principal required")
        return {
            "keys": [
                g.model_dump(mode="json")
                for g in store.list_keys()
                if not g.anonymous
                and (
                    g.key_id == grant.key_id or (g.account_id and g.account_id == grant.account_id)
                )
            ]
        }

    @app.post("/v1/keys/{key_id}/revoke", response_model=KeyGrant)
    def revoke(key_id: str, grant: KeyGrant = key) -> dict[str, Any]:
        target = store.get_key(key_id)
        if grant.anonymous or not (
            grant.is_admin
            or grant.key_id == key_id
            or (grant.account_id and grant.account_id == target.account_id)
        ):
            raise Forbidden("key owner or admin required")
        return store.revoke_key(key_id).model_dump(mode="json")

    @app.get("/v1/claims/{task_id}/events", response_model=ClaimEvents)
    async def events(
        task_id: str, response: Response, after: int = 0, wait: float = 0, grant: KeyGrant = key
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0, min(wait, 15))
        while True:
            result = claim_events(store, gate, grant, task_id, after)
            if result["events"] or time.monotonic() >= deadline:
                response.headers["retry-after"] = "1"
                return result
            await asyncio.sleep(min(0.75, max(0, deadline - time.monotonic())))
