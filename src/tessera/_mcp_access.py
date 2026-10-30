"""MCP access tools use the same principal context as HTTP."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from tessera._access_boundary import REQUEST_ACCESS
from tessera._access_contract import access_description
from tessera._access_routes import claim_events, list_verbs
from tessera._principals import public_principal
from tessera.access import Gate, ServiceSettings
from tessera.grants import KeyGrant
from tessera.store import Store


def register_access_tools(
    server: MCPServer[Any],
    store: Store,
    gate: Gate,
    settings: ServiceSettings,
    run: Callable[..., Awaitable[Any]],
    current_grant: Callable[[], Awaitable[KeyGrant]],
) -> None:
    @server.tool(
        description="Open a tracked one-hour anonymous demo session. Personal assistants and coding agents welcome; only synthetic demo nouns and verbs, fixed quotas, private 24-hour retention. No pre-issued key needed."
    )
    async def open_anonymous_session(agent_label: str = "") -> dict[str, Any]:
        if not settings.anonymous_access_enabled:
            raise ToolError("anonymous_disabled: anonymous sessions are not enabled")
        context = REQUEST_ACCESS.get()
        if context is None:
            raise ToolError("invalid_request: a request context is required")
        return cast(
            dict[str, Any],
            await run(
                lambda: store.open_anonymous_session(
                    agent_label,
                    ip_hash=context.meta["ip_hash"],
                    ip_window=context.meta.get("ip_window"),
                )
            ),
        )

    @server.tool(
        description="Explain open discovery, tracked anonymous sessions, scoped operator-issued keys, quotas and audit. No key needed."
    )
    async def explain_access_modes() -> dict[str, Any]:
        return access_description(
            settings.public_url,
            settings.anonymous_access_enabled,
            settings.requester_signup_enabled,
        )

    @server.tool(
        description="Show your principal identity, account, scopes, labels, quotas and expiry. Requires a session or API key."
    )
    async def whoami() -> dict[str, Any]:
        return public_principal(await current_grant())

    @server.tool(
        description="Read only your own content-free access audit. Requires a session or API key."
    )
    async def my_audit(after: int = 0, limit: int = 50) -> dict[str, Any]:
        grant = await current_grant()
        return cast(
            dict[str, Any],
            await run(lambda: store.own_audit(grant, after=after, limit=limit), grant),
        )

    @server.tool(description="Read the operator audit. Requires the admin scope.")
    async def admin_audit(after: int = 0, limit: int = 50) -> dict[str, Any]:
        grant = await current_grant()
        return cast(
            dict[str, Any],
            await run(lambda: store.own_audit(grant, after=after, limit=limit, admin=True), grant),
        )

    @server.tool(
        description="Cancel a verb you are authorized to mutate. Session or key required; no production verbs for anonymous sessions."
    )
    async def cancel(task_id: str, reason: str = "") -> dict[str, Any]:
        grant = await current_grant()

        def cancel_task() -> dict[str, Any]:
            gate.cancel_task(grant, task_id)
            return store.cancel(task_id, reason).model_dump(mode="json")

        return cast(dict[str, Any], await run(cancel_task, grant))

    @server.tool(
        name="claim_events",
        description="Read monotonic events for an authorized verb. HTTP /v1/claims/{task_id}/events?after=N&wait=15 supports bounded long-poll.",
    )
    async def claim_events_tool(task_id: str, after: int = 0) -> dict[str, Any]:
        grant = await current_grant()
        return cast(
            dict[str, Any],
            await run(lambda: claim_events(store, gate, grant, task_id, after), grant),
        )

    @server.tool(
        name="list_verbs",
        description="List authorized atomic verbs by queue and state. Requires a session or key. Anonymous callers see their private demo verbs, and in the main namespace only verbs tagged open:anonymous.",
    )
    async def verbs(
        queue: str | None = None, status: str = "ready", limit: int = 100
    ) -> dict[str, Any]:
        grant = await current_grant()
        return cast(
            dict[str, Any], await run(lambda: list_verbs(store, grant, queue, status, limit), grant)
        )
