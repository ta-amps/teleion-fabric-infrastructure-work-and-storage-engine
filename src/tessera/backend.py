"""The contract every Tessera backend meets: the in-process Store, the HTTP Client, and any adapter an
app puts in front of them. The conformance suite runs against this protocol."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from tessera.models import (
    ChildSpec,
    Event,
    Health,
    Job,
    JobStatus,
    Lease,
    QueueConfig,
    QueueCounts,
    Receipt,
    RecordVersion,
    RecordWrite,
    ResultView,
    TaskSpec,
    TaskView,
)


class Backend(Protocol):
    # queues and tasks
    def define_queue(self, config: QueueConfig) -> QueueConfig: ...
    def queues(self) -> list[QueueConfig]: ...
    def enqueue(self, spec: TaskSpec) -> str: ...

    # the five verbs
    def claim(
        self,
        queue: str,
        worker: str,
        *,
        kinds: Sequence[str] | None = None,
        effect_classes: Sequence[str] | None = None,
    ) -> Lease | None: ...
    def heartbeat(self, task_id: str, fencing_token: int) -> Lease: ...
    def split(
        self, task_id: str, fencing_token: int, children: Sequence[ChildSpec]
    ) -> list[str]: ...
    def submit(
        self,
        task_id: str,
        fencing_token: int,
        output: dict[str, Any],
        *,
        idempotency_key: str,
        ok: bool = True,
        error: str | None = None,
    ) -> Receipt: ...
    def cancel(self, task_id: str, reason: str = "") -> TaskView: ...

    # operations and observation
    def reap(self) -> int: ...
    def task(self, task_id: str) -> TaskView: ...
    def results(self, task_id: str) -> list[ResultView]: ...
    def counts(self) -> dict[str, QueueCounts]: ...
    def recount(self) -> dict[str, QueueCounts]: ...
    def health(self) -> Health: ...
    def events(self, after: int = 0, limit: int = 1000) -> list[Event]: ...

    # model jobs
    def register_job(self, task_id: str, fencing_token: int, job_id: str, model: str) -> Job: ...
    def update_job(
        self,
        job_id: str,
        *,
        status: JobStatus,
        input_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
    ) -> Job: ...
    def jobs(self, task_id: str | None = None) -> list[Job]: ...

    # atomic storage
    def put_record(
        self, store: str, key: str, value: Any, *, expected_version: int | None
    ) -> RecordVersion: ...
    def put_records(self, writes: Sequence[RecordWrite]) -> list[RecordVersion]: ...
    def get_record(self, store: str, key: str, version: int | None = None) -> RecordVersion: ...
    def record_history(self, store: str, key: str) -> list[RecordVersion]: ...
    def put_blob(self, data: bytes) -> str: ...
    def get_blob(self, sha256: str) -> bytes: ...

    def close(self) -> None: ...
