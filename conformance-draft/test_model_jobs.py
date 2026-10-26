"""T12: model jobs (Meta Model API background responses) are tied to the lease that started them."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tessera import StaleLease, TaskSpec

pytestmark = pytest.mark.fabric_all


def test_T12_a_job_is_polled_to_done_with_its_cost(backend: Any, make_queue: Any) -> None:
    make_queue("q-jobs")
    task = backend.enqueue(TaskSpec(queue="q-jobs", brief={"case": "C-FS-PH-03"}))
    lease = backend.claim("q-jobs", "w1")
    assert lease is not None
    job = backend.register_job(task, lease.fencing_token, "resp_conformance_1", "muse-spark-1.3")
    assert job.status == "queued"
    backend.update_job("resp_conformance_1", status="in_progress")
    done = backend.update_job(
        "resp_conformance_1",
        status="completed",
        input_tokens=1200,
        cached_input_tokens=800,
        output_tokens=300,
        cost_usd=0.0024,
    )
    assert done.status == "completed" and done.output_tokens == 300
    assert done.cost_usd == pytest.approx(0.0024)
    assert [j.job_id for j in backend.jobs(task)] == ["resp_conformance_1"]


def test_T12_a_cancelled_job_returns_its_task(backend: Any, make_queue: Any) -> None:
    make_queue("q-jobs-cancel")
    task = backend.enqueue(TaskSpec(queue="q-jobs-cancel", brief={}))
    lease = backend.claim("q-jobs-cancel", "w1")
    assert lease is not None
    backend.register_job(task, lease.fencing_token, "resp_conformance_2", "muse-wonder-1.0")
    backend.update_job("resp_conformance_2", status="cancelled")
    assert backend.task(task).state == "ready"
    with pytest.raises(StaleLease):
        backend.submit(task, lease.fencing_token, {}, idempotency_key=uuid.uuid4().hex)


def test_T12_a_lapsed_lease_asks_its_jobs_to_stop(backend: Any, make_queue: Any, tick: Any) -> None:
    q = make_queue("q-jobs-lapse")
    task = backend.enqueue(TaskSpec(queue="q-jobs-lapse", brief={}))
    lease = backend.claim("q-jobs-lapse", "w1")
    assert lease is not None
    backend.register_job(task, lease.fencing_token, "resp_conformance_3", "muse-spark-1.3")
    tick.advance(q.heartbeat_seconds * q.miss_limit + 0.5)
    backend.reap()
    assert [j.status for j in backend.jobs(task)] == ["cancel_requested"]
    with pytest.raises(StaleLease):
        backend.register_job(task, lease.fencing_token, "resp_conformance_4", "muse-spark-1.3")
