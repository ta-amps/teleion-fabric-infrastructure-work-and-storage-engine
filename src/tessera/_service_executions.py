"""Execution nouns retain task permissions across HTTP and MCP."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from tessera.access import Gate
from tessera.grants import KeyGrant
from tessera.store import Store


class RecallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    fencing_token: StrictInt = Field(ge=1)
    execution_report: dict[str, Any] | None = None
    limit: StrictInt = Field(default=5, ge=1, le=20)


def mount_execution_routes(
    app: FastAPI, store: Store, gate: Gate, require_key: Callable[..., Any]
) -> None:
    key = Depends(require_key)

    @app.post("/v1/executions/recall")
    def recall(body: RecallBody, grant: KeyGrant = key) -> dict[str, Any]:
        return store.recall_execution(
            body.task_id,
            body.fencing_token,
            grant=grant,
            execution_report=body.execution_report,
            limit=body.limit,
        )

    @app.get("/v1/executions/{run_id}")
    def execution(run_id: str, grant: KeyGrant = key) -> dict[str, Any]:
        return store.get_execution(run_id, grant=grant)

    @app.get("/v1/tasks/{task_id}/executions")
    def executions(
        task_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=100),
        grant: KeyGrant = key,
    ) -> dict[str, Any]:
        return store.list_executions(task_id, grant=grant, after=after, limit=limit)
