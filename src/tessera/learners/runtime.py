"""Shared runtime for learner run verbs: pinned spec checks, bounded paging, output writes.

Learners use only the public client contract (HTTP Client or an equivalent object):
no store internals, no extra queue, no server-side model call.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from tessera.learners.contract import RUN_CONTRACT, LearnerSpec, NounRef, value_digest
from tessera.models import Lease, RecordVersion, RecordWrite
from tessera.receipts import canonical_json


class Api(Protocol):
    def get_record(
        self, store: str, key: str, version: int | None = None, *, namespace: str | None = None
    ) -> RecordVersion: ...

    def put_records(
        self, writes: list[RecordWrite], *, namespace: str | None = None
    ) -> list[RecordVersion]: ...

    def execution_observations(
        self,
        queue: str,
        *,
        kind: str | None = None,
        after: str | None = None,
        limit: int = 50,
        namespace: str | None = None,
    ) -> dict[str, Any]: ...

    def get_execution(self, run_id: str, *, namespace: str | None = None) -> dict[str, Any]: ...

    def heartbeat(
        self, task_id: str, fencing_token: int, *, namespace: str | None = None
    ) -> Any: ...


@dataclass
class LearnerRun:
    """A claimed learner verb after its pinned spec was read back and verified."""

    namespace: str
    task_id: str
    fencing_token: int
    heartbeat_seconds: int
    learner: str
    strategy: str
    spec_version: int
    spec: NounRef
    spec_value: LearnerSpec
    source_queue: str
    source_kind: str | None
    max_observations: int
    output_store: str
    state_version: int
    options: dict[str, Any]
    consumers: dict[str, Any]
    clock: Callable[[], float] = time.monotonic
    last_heartbeat: float = field(default=0.0)

    def output(self, key: str, value: Any, *, expected_version: int | None) -> RecordWrite:
        return RecordWrite(
            namespace=self.namespace,
            store=self.output_store,
            key=key,
            value=value,
            expected_version=expected_version,
            visibility_scope=self.consumers.get("visibility_scope", "private"),
            allowed_surfaces=list(self.consumers.get("allowed_surfaces", [])),
            # Two tags: each tag is an indexed row per record version (write cost).
            tags=[f"category:{self.spec_value.writes[0].category_id}", f"learner:{self.learner}"],
        )


def load_run(api: Api, lease: Lease, *, clock: Callable[[], float] = time.monotonic) -> LearnerRun:
    brief = lease.brief
    if brief.get("contract") != RUN_CONTRACT:
        raise ValueError("task is not a learner run verb")
    pin = NounRef.model_validate(brief["spec"])
    record = api.get_record(pin.store, pin.key, pin.version, namespace=lease.namespace)
    if (record.store, record.key, record.version) != (pin.store, pin.key, pin.version) or (
        value_digest(record.value) != pin.value_sha256
    ):
        raise ValueError("learner spec readback differs from its pin")
    spec = LearnerSpec.model_validate(record.value)
    if (spec.learner, spec.strategy, spec.version) != (
        brief.get("learner"),
        brief.get("strategy"),
        brief.get("spec_version"),
    ):
        raise ValueError("run brief and pinned spec disagree")
    # Output visibility comes only from the verified spec; a brief may repeat it, never widen it.
    consumers = spec.consumers.model_dump()
    if brief.get("consumers") is not None and brief["consumers"] != consumers:
        raise ValueError("run brief consumers differ from the pinned spec")
    source = brief["source"]
    limit = min(int(source["max_observations"]), spec.budget.max_observations)
    return LearnerRun(
        namespace=lease.namespace,
        task_id=lease.task_id,
        fencing_token=lease.fencing_token,
        heartbeat_seconds=lease.heartbeat_seconds,
        learner=spec.learner,
        strategy=spec.strategy,
        spec_version=spec.version,
        spec=pin,
        spec_value=spec,
        source_queue=source["queue"],
        source_kind=source.get("kind"),
        max_observations=limit,
        output_store=brief["output_store"],
        state_version=int(brief.get("state_version", 0)),
        options=dict(brief.get("options") or {}),
        consumers=consumers,
        clock=clock,
        last_heartbeat=clock(),
    )


def page_observations(
    api: Api, run: LearnerRun, *, after: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """Read up to the run's budget after a cursor; heartbeat only when the lease needs it."""
    found: list[dict[str, Any]] = []
    cursor = after
    while len(found) < run.max_observations:
        page = api.execution_observations(
            run.source_queue,
            kind=run.source_kind,
            after=cursor,
            limit=min(100, run.max_observations - len(found)),
            namespace=run.namespace,
        )
        items = page["observations"]
        found.extend(items)
        if items:
            cursor = items[-1]["seq"]
        if page["next_after"] is None:
            break
        cursor = page["next_after"]
        if run.clock() - run.last_heartbeat >= run.heartbeat_seconds / 2:
            api.heartbeat(run.task_id, run.fencing_token, namespace=run.namespace)
            run.last_heartbeat = run.clock()
    return found, cursor


def write_outputs(api: Api, run: LearnerRun, writes: list[RecordWrite]) -> list[dict[str, Any]]:
    """One atomic CAS batch; a version conflict means another run already advanced state."""
    budget = run.spec_value.budget.max_output_bytes
    if not writes:
        raise ValueError("learner produced no outputs")
    if any(len(canonical_json(w.value).encode("utf-8")) > budget for w in writes):
        raise ValueError("a learner output exceeds the spec's max_output_bytes")
    stored = api.put_records(writes, namespace=run.namespace)
    return [
        NounRef(
            store=item.store,
            key=item.key,
            version=item.version,
            value_sha256=value_digest(item.value),
        ).model_dump()
        for item in stored
    ]
