"""Task graphs every backend must meet (SPEC §5.2 and §5.6): dependencies (fan-in), children that
run in waves, and parking a task while it waits for outside input. Test names carry T17 to T19."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tessera import ChildSpec, InvalidRequest, InvalidState, NotFound, StaleLease, TaskSpec

pytestmark = pytest.mark.fabric_all


def ikey() -> str:
    return uuid.uuid4().hex


def finish(backend: Any, queue: str, worker: str, ok: bool = True) -> str:
    lease = backend.claim(queue, worker)
    assert lease is not None
    backend.submit(
        lease.task_id,
        lease.fencing_token,
        {"by": worker, "brief": lease.brief},
        idempotency_key=ikey(),
        ok=ok,
        error=None if ok else "failed on purpose",
    )
    return str(lease.task_id)


def test_T17_a_dependent_waits_for_every_dependency(backend: Any, make_queue: Any) -> None:
    """T17: a task with dependencies waits until each is resolved, then carries their results."""
    make_queue("q-dep-src")
    make_queue("q-dep-join")
    first = backend.enqueue(TaskSpec(queue="q-dep-src", brief={"part": 1}))
    second = backend.enqueue(TaskSpec(queue="q-dep-src", brief={"part": 2}))
    join = backend.enqueue(
        TaskSpec(queue="q-dep-join", brief={"join": True}, depends_on=[first, second])
    )
    view = backend.task(join)
    assert view.state == "waiting" and view.wait_reason == "dependencies"
    assert view.pending_deps == 2 and view.depends_on == [first, second]
    assert backend.claim("q-dep-join", "w-join") is None
    finish(backend, "q-dep-src", "w1")
    assert backend.task(join).state == "waiting"
    finish(backend, "q-dep-src", "w2")
    assert backend.task(join).state == "ready"
    lease = backend.claim("q-dep-join", "w-join")
    assert lease is not None and lease.task_id == join
    assert [d.task_id for d in lease.dependencies] == [first, second]
    assert all(d.state == "done" and d.ok for d in lease.dependencies)
    assert {d.output["brief"]["part"] for d in lease.dependencies} == {1, 2}
    counts = backend.counts()
    assert counts == backend.recount()


def test_T17_failed_or_cancelled_dependencies_still_release(backend: Any, make_queue: Any) -> None:
    """T17: a dead or cancelled dependency releases its dependent, which sees what happened."""
    make_queue("q-fail", max_attempts=1)
    doomed = backend.enqueue(TaskSpec(queue="q-fail", brief={"n": 1}))
    dropped = backend.enqueue(TaskSpec(queue="q-fail", brief={"n": 2}, priority=0))
    after = backend.enqueue(
        TaskSpec(queue="q-fail", brief={"after": True}, depends_on=[doomed, dropped], priority=0)
    )
    finish(backend, "q-fail", "w1", ok=False)
    assert backend.task(doomed).state == "dead"
    assert backend.task(after).state == "waiting"
    backend.cancel(dropped, "not needed")
    assert backend.task(after).state == "ready"
    lease = backend.claim("q-fail", "w2")
    assert lease is not None and lease.task_id == after
    assert {d.task_id: d.state for d in lease.dependencies} == {
        doomed: "dead",
        dropped: "cancelled",
    }
    assert all(d.ok is None for d in lease.dependencies)


def test_T17_dependencies_must_exist_and_resolved_ones_cost_nothing(
    backend: Any, make_queue: Any
) -> None:
    """T17: an unknown dependency is refused; one already resolved doesn't hold the task."""
    make_queue("q-deps")
    with pytest.raises(NotFound):
        backend.enqueue(TaskSpec(queue="q-deps", brief={}, depends_on=["no-such-task"]))
    done = backend.enqueue(TaskSpec(queue="q-deps", brief={"n": 0}))
    finish(backend, "q-deps", "w1")
    later = backend.enqueue(TaskSpec(queue="q-deps", brief={"n": 1}, depends_on=[done]))
    assert backend.task(later).state == "ready"
    assert backend.task(later).pending_deps == 0


def test_T18_children_can_run_in_waves(backend: Any, make_queue: Any) -> None:
    """T18: a child may wait for earlier siblings, so a split can run in ordered waves."""
    make_queue("q-wave")
    root = backend.enqueue(TaskSpec(queue="q-wave", brief={"root": True}))
    lease = backend.claim("q-wave", "w-root")
    assert lease is not None and lease.task_id == root
    kids = backend.split(
        root,
        lease.fencing_token,
        [
            ChildSpec(brief={"wave": 1, "i": 0}),
            ChildSpec(brief={"wave": 1, "i": 1}),
            ChildSpec(brief={"wave": 2}, after=[0, 1]),
        ],
    )
    assert backend.task(kids[2]).state == "waiting"
    seen = [finish(backend, "q-wave", "w"), finish(backend, "q-wave", "w")]
    assert set(seen) == set(kids[:2])
    assert backend.task(kids[2]).state == "ready"
    assert finish(backend, "q-wave", "w") == kids[2]
    merge = backend.claim("q-wave", "w-root")
    assert merge is not None and merge.task_id == root
    with pytest.raises(InvalidRequest):
        backend.split(root, merge.fencing_token, [ChildSpec(brief={}, after=[0])])


def test_T19_park_waits_for_input_without_a_failure(backend: Any, make_queue: Any) -> None:
    """T19: park ends the lease with no failure; resume brings the task back with the input."""
    make_queue("q-park")
    task = backend.enqueue(TaskSpec(queue="q-park", brief={"case": "C-1"}))
    lease = backend.claim("q-park", "w1")
    assert lease is not None
    view = backend.park(task, lease.fencing_token, reason="waiting for the payslips")
    assert view.state == "waiting" and view.wait_reason == "input" and view.failures == 0
    assert backend.claim("q-park", "w2") is None
    with pytest.raises(StaleLease):
        backend.submit(task, lease.fencing_token, {}, idempotency_key=ikey())
    backend.resume(task, {"documents": ["payslip-1"]})
    with pytest.raises(InvalidState):
        backend.resume(task, {"documents": ["again"]})
    again = backend.claim("q-park", "w2")
    assert again is not None and again.task_id == task
    assert again.inputs == [{"documents": ["payslip-1"]}]
    assert again.fencing_token > lease.fencing_token
    receipt = backend.submit(task, again.fencing_token, {"route": "T1"}, idempotency_key=ikey())
    assert receipt.state_after == "done"
    assert backend.task(task).failures == 0
    assert backend.counts() == backend.recount()


def test_T19_a_stale_holder_cannot_park(backend: Any, make_queue: Any, tick: Any) -> None:
    """T19: only the live lease may park a task."""
    make_queue("q-park-stale")
    task = backend.enqueue(TaskSpec(queue="q-park-stale", brief={}))
    lease = backend.claim("q-park-stale", "w1")
    assert lease is not None
    with pytest.raises(StaleLease):
        backend.park(task, lease.fencing_token + 1, reason="wrong token")
    assert backend.task(task).state == "leased"


def test_T19_a_parked_task_wakes_when_its_time_is_up(
    backend: Any, make_queue: Any, tick: Any, controls_time: bool
) -> None:
    """T19: with resume_within set, a parked task comes back by itself, marked timed out."""
    if not controls_time:
        pytest.skip("needs a controllable clock")
    make_queue("q-park-timeout")
    task = backend.enqueue(TaskSpec(queue="q-park-timeout", brief={}))
    lease = backend.claim("q-park-timeout", "w1")
    assert lease is not None
    backend.park(task, lease.fencing_token, reason="waiting", resume_within=3600)
    tick.advance(3599)
    assert backend.claim("q-park-timeout", "w2") is None
    tick.advance(2)
    again = backend.claim("q-park-timeout", "w2")
    assert again is not None and again.task_id == task
    assert again.inputs == [{"timed_out": True}]


def test_T20_a_child_is_never_broader_than_its_parent(backend: Any, make_queue: Any) -> None:
    """T20: split may not widen what a task may change (read < write-internal < write-repo)."""
    make_queue("q-effect")
    task = backend.enqueue(TaskSpec(queue="q-effect", brief={}, effect_class="read"))
    lease = backend.claim("q-effect", "w1")
    assert lease is not None and lease.task_id == task
    with pytest.raises(InvalidRequest):
        backend.split(task, lease.fencing_token, [ChildSpec(brief={}, effect_class="write-repo")])
    kids = backend.split(task, lease.fencing_token, [ChildSpec(brief={}, effect_class="read")])
    assert backend.task(kids[0]).effect_class == "read"
