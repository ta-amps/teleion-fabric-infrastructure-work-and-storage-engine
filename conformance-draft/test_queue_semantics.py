"""Queue semantics every backend must meet (SPEC §5). Test names carry the requirement ids T03 to T15."""

from __future__ import annotations

import hashlib
import json
import random
import threading
import time
import uuid
from collections import Counter
from typing import Any

import pytest

from tessera import ChildSpec, IdempotencyConflict, Receipt, StaleLease, TaskSpec

pytestmark = pytest.mark.fabric_all


def ikey() -> str:
    return uuid.uuid4().hex


def enqueue_n(backend: Any, queue: str, n: int, prefix: str | None = None) -> list[str]:
    return [
        backend.enqueue(
            TaskSpec(queue=queue, brief={"n": i}, task_id=f"{prefix}-{i}" if prefix else None)
        )
        for i in range(n)
    ]


def spec_whole(value: Any) -> Any:
    """SPEC §5.4: a whole number is written without a fraction (1.0 as 1)."""
    if isinstance(value, float) and value.is_integer() and abs(value) < 2**53:
        return int(value)
    if isinstance(value, dict):
        return {k: spec_whole(v) for k, v in value.items()}
    if isinstance(value, list):
        return [spec_whole(v) for v in value]
    return value


def spec_receipt_hash(task_id: str, token: int, ok: bool, output: Any, error: str | None) -> str:
    """SPEC §5.4, written out independently of the implementation under test."""
    body = json.dumps(
        spec_whole(
            {"task_id": task_id, "fencing_token": token, "ok": ok, "output": output, "error": error}
        ),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def run_threads(target: Any, names: list[str]) -> list[BaseException]:
    errors: list[BaseException] = []

    def wrapped(name: str) -> None:
        try:
            target(name)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=150)
    return errors


def test_T03_no_task_is_held_twice(backend: Any, make_queue: Any) -> None:
    """T03: 8 workers race for 1,000 claims; no task is ever leased to two of them."""
    make_queue("q-race")
    ids = enqueue_n(backend, "q-race", 1000)
    claimed: list[str] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        while (lease := backend.claim("q-race", name)) is not None:
            with lock:
                claimed.append(lease.task_id)
            backend.submit(lease.task_id, lease.fencing_token, {"by": name}, idempotency_key=ikey())

    assert run_threads(worker, [f"w{i}" for i in range(8)]) == []
    per_task = Counter(claimed)
    assert set(per_task) == set(ids)
    assert max(per_task.values()) == 1
    assert backend.counts()["q-race"].done == 1000


def test_T04_lapsed_leases_come_back(backend: Any, make_queue: Any, tick: Any) -> None:
    """T04: after the missed-heartbeat limit the task is ready again, with a higher token."""
    q = make_queue("q-lapse")
    (task,) = enqueue_n(backend, "q-lapse", 1)
    first = backend.claim("q-lapse", "w1")
    assert first is not None
    tick.advance(q.heartbeat_seconds * q.miss_limit + 0.5)
    backend.reap()
    assert backend.task(task).state == "ready"
    tick.advance(q.backoff_cap_seconds + 1)
    second = backend.claim("q-lapse", "w2")
    assert second is not None and second.task_id == task
    assert second.fencing_token > first.fencing_token


def test_T04_heartbeats_keep_a_lease(backend: Any, make_queue: Any, tick: Any) -> None:
    q = make_queue("q-beat")
    enqueue_n(backend, "q-beat", 1)
    lease = backend.claim("q-beat", "w1")
    assert lease is not None
    for _ in range(5):
        tick.advance(q.heartbeat_seconds * 0.9)
        lease = backend.heartbeat(lease.task_id, lease.fencing_token)
    backend.reap()
    assert backend.task(lease.task_id).state == "leased"


def test_T04_lease_has_a_hard_cap(
    backend: Any, make_queue: Any, tick: Any, controls_time: bool
) -> None:
    """Heartbeats can't hold a lease past lease_seconds."""
    if not controls_time:
        pytest.skip("needs a controllable clock")
    make_queue("q-hard", lease_seconds=300, heartbeat_seconds=60, miss_limit=3)
    enqueue_n(backend, "q-hard", 1)
    lease = backend.claim("q-hard", "w1")
    assert lease is not None
    with pytest.raises(StaleLease):  # noqa: PT012 - the loop is what runs into the cap
        for _ in range(10):
            tick.advance(50)
            lease = backend.heartbeat(lease.task_id, lease.fencing_token)


def test_T05_fencing_refuses_the_old_holder(backend: Any, make_queue: Any, tick: Any) -> None:
    """T05: the old holder's submit after a re-lease is refused; the new holder's succeeds."""
    q = make_queue("q-fence")
    enqueue_n(backend, "q-fence", 1)
    old = backend.claim("q-fence", "w-old")
    assert old is not None
    tick.advance(q.heartbeat_seconds * q.miss_limit + 0.5)
    backend.reap()
    tick.advance(q.backoff_cap_seconds + 1)
    new = backend.claim("q-fence", "w-new")
    assert new is not None and new.task_id == old.task_id
    with pytest.raises(StaleLease):
        backend.submit(old.task_id, old.fencing_token, {"by": "old"}, idempotency_key=ikey())
    receipt = backend.submit(new.task_id, new.fencing_token, {"by": "new"}, idempotency_key=ikey())
    assert receipt.ok and receipt.state_after == "done"
    with pytest.raises(StaleLease):
        backend.heartbeat(old.task_id, old.fencing_token)


def test_T06_dead_letter_after_three_failures(backend: Any, make_queue: Any, tick: Any) -> None:
    """T06: a task that fails 3 times lands in dead letter, with every attempt logged."""
    q = make_queue("q-dead", max_attempts=3)
    (task,) = enqueue_n(backend, "q-dead", 1)
    receipt: Receipt | None = None
    for attempt in range(1, 4):
        tick.advance(q.backoff_cap_seconds + 1)
        lease = backend.claim("q-dead", "w1")
        assert lease is not None and lease.attempt == attempt
        receipt = backend.submit(
            lease.task_id,
            lease.fencing_token,
            {"attempt": attempt},
            idempotency_key=ikey(),
            ok=False,
            error=f"failure {attempt}",
        )
    assert receipt is not None and receipt.state_after == "dead"
    assert backend.task(task).state == "dead"
    assert [r.ok for r in backend.results(task)] == [False, False, False]
    claims = [e for e in backend.events(limit=10_000) if e.task_id == task and e.type == "claimed"]
    assert len(claims) == 3
    assert backend.counts()["q-dead"].dead == 1
    tick.advance(q.backoff_cap_seconds + 1)
    assert backend.claim("q-dead", "w1") is None


def test_T06_backoff_grows_within_bounds(
    backend: Any, make_queue: Any, clock: Any, controls_time: bool
) -> None:
    """Retries wait between half and all of min(cap, base * 2^(failures-1))."""
    if not controls_time:
        pytest.skip("needs a controllable clock")
    make_queue("q-backoff", max_attempts=10, backoff_base_seconds=30, backoff_cap_seconds=900)
    (task,) = enqueue_n(backend, "q-backoff", 1)
    for failures in range(1, 9):
        clock.advance(10_000)
        lease = backend.claim("q-backoff", "w1")
        assert lease is not None
        now = clock.now()
        backend.submit(task, lease.fencing_token, {}, idempotency_key=ikey(), ok=False)
        delay = backend.task(task).not_before - now
        ceiling = min(900.0, 30.0 * 2 ** (failures - 1))
        assert ceiling * 0.5 - 1e-6 <= delay <= ceiling + 1e-6


def test_T07_submit_is_idempotent(backend: Any, make_queue: Any) -> None:
    """T07: the same submit sent twice stores one result and returns the first receipt."""
    make_queue("q-idem")
    task, other = enqueue_n(backend, "q-idem", 2, prefix="idem")  # same instant: id order
    lease = backend.claim("q-idem", "w1")
    assert lease is not None and lease.task_id == task
    key = ikey()
    first = backend.submit(task, lease.fencing_token, {"v": 1}, idempotency_key=key)
    again = backend.submit(task, lease.fencing_token, {"v": 1}, idempotency_key=key)
    assert again.receipt_hash == first.receipt_hash
    assert again.replayed and not first.replayed
    assert len(backend.results(task)) == 1
    assert backend.counts()["q-idem"].done == 1
    second = backend.claim("q-idem", "w1")
    assert second is not None and second.task_id == other
    with pytest.raises(IdempotencyConflict):
        backend.submit(other, second.fencing_token, {"v": 2}, idempotency_key=key)


def test_T08_split_keeps_the_parent(backend: Any, make_queue: Any) -> None:
    """T08: the parent waits until every child is done, then returns for the merge."""
    make_queue("q-split")
    (parent,) = enqueue_n(backend, "q-split", 1)
    lease = backend.claim("q-split", "w-parent")
    assert lease is not None
    kids = backend.split(
        parent, lease.fencing_token, [ChildSpec(brief={"part": i}) for i in range(3)]
    )
    assert len(kids) == 3
    assert backend.task(parent).state == "waiting"
    with pytest.raises(StaleLease):
        backend.submit(parent, lease.fencing_token, {}, idempotency_key=ikey())
    for done in range(1, 4):
        child = backend.claim("q-split", "w-child")
        assert child is not None and child.task_id in kids
        backend.submit(
            child.task_id,
            child.fencing_token,
            {"part": child.brief["part"]},
            idempotency_key=ikey(),
        )
        expected = "waiting" if done < 3 else "ready"
        assert backend.task(parent).state == expected
    merge = backend.claim("q-split", "w-merge")
    assert merge is not None and merge.task_id == parent
    assert {c.task_id for c in merge.children} == set(kids)
    assert all(c.ok for c in merge.children)
    backend.submit(parent, merge.fencing_token, {"merged": 3}, idempotency_key=ikey())
    assert backend.task(parent).state == "done"


def test_T09_one_counts_record(backend: Any, make_queue: Any) -> None:
    """T09: after 500 tasks with mixed outcomes, the counts record matches a recount."""
    make_queue("q-a")
    make_queue("q-b")
    enqueue_n(backend, "q-a", 250)
    enqueue_n(backend, "q-b", 250)
    rng = random.Random(9)
    for i in range(500):
        lease = backend.claim(rng.choice(["q-a", "q-b"]), f"w{i % 4}")
        if lease is None:
            continue
        roll = rng.random()
        if roll < 0.6:
            backend.submit(lease.task_id, lease.fencing_token, {}, idempotency_key=ikey())
        elif roll < 0.8:
            backend.submit(lease.task_id, lease.fencing_token, {}, idempotency_key=ikey(), ok=False)
        elif roll < 0.9:
            backend.cancel(lease.task_id, "conformance test")
    counts = backend.counts()
    assert counts == backend.recount()
    # Only this test's queues: a shared service also holds other tests' tasks.
    assert sum(sum(counts[q].model_dump().values()) for q in ("q-a", "q-b")) == 500


def test_T10_health_is_honest(backend: Any, make_queue: Any, tick: Any) -> None:
    """T10: health reports the database, the reaper's last run and queue depths."""
    q = make_queue("q-health")
    enqueue_n(backend, "q-health", 2)
    assert backend.claim("q-health", "w1") is not None
    tick.advance(q.heartbeat_seconds * q.miss_limit + 0.5)
    queue = {h.name: h for h in backend.health().queues}["q-health"]
    assert queue.expired_leases == 1
    backend.reap()
    health = backend.health()
    assert health.db_ok and health.reaper_last_run is not None
    assert {h.name: h for h in health.queues}["q-health"].counts == backend.counts()["q-health"]


def test_T13_it_holds_up(backend: Any, make_queue: Any) -> None:
    """T13: 4 workers move 50 tasks through 2 queues: none lost, none doubled, all receipted."""
    make_queue("q-stage1")
    make_queue("q-stage2")
    enqueue_n(backend, "q-stage1", 50, prefix="item")
    receipts: list[Receipt] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        idle = 0
        while idle < 20:
            progressed = False
            for queue in ("q-stage1", "q-stage2"):
                lease = backend.claim(queue, name)
                if lease is None:
                    continue
                progressed = True
                if queue == "q-stage1":
                    backend.enqueue(
                        TaskSpec(queue="q-stage2", brief=lease.brief, task_id=f"{lease.task_id}-s2")
                    )
                receipt = backend.submit(
                    lease.task_id,
                    lease.fencing_token,
                    {"stage": queue},
                    idempotency_key=f"{lease.task_id}:{lease.fencing_token}",
                )
                with lock:
                    receipts.append(receipt)
            idle = 0 if progressed else idle + 1
            if not progressed:
                time.sleep(0.01)

    assert run_threads(worker, [f"w{i}" for i in range(4)]) == []
    counts = backend.counts()
    assert counts["q-stage1"].done == 50 and counts["q-stage2"].done == 50
    assert len(receipts) == 100 and len({r.task_id for r in receipts}) == 100


def test_T14_it_survives_a_restart(backend: Any, make_queue: Any, restart: Any, tick: Any) -> None:
    """T14: a crash mid-run loses nothing and doubles nothing."""
    q = make_queue("q-restart")
    ids = enqueue_n(backend, "q-restart", 40, prefix="r")
    for _ in range(10):
        lease = backend.claim("q-restart", "w-before")
        backend.submit(
            lease.task_id,
            lease.fencing_token,
            {"ok": 1},
            idempotency_key=f"{lease.task_id}:{lease.fencing_token}",
        )
    for _ in range(5):  # leased when the crash happens
        assert backend.claim("q-restart", "w-crashed") is not None
    backend = restart(backend)
    tick.advance(q.heartbeat_seconds * q.miss_limit + 0.5)
    backend.reap()
    tick.advance(q.backoff_cap_seconds + 1)  # the lapsed tasks wait out their retry backoff
    while (lease := backend.claim("q-restart", "w-after")) is not None:
        backend.submit(
            lease.task_id,
            lease.fencing_token,
            {"ok": 1},
            idempotency_key=f"{lease.task_id}:{lease.fencing_token}",
        )
    assert backend.counts()["q-restart"].done == 40
    for task in ids:
        assert sum(r.ok for r in backend.results(task)) == 1
    backend.close()


def test_T15_receipts_match_results(backend: Any, make_queue: Any) -> None:
    """T15: every done task's receipt hash matches its stored result (SPEC §5.4)."""
    make_queue("q-receipt")
    ids = enqueue_n(backend, "q-receipt", 20)
    while (lease := backend.claim("q-receipt", "w1")) is not None:
        output = {"task": lease.task_id, "values": [lease.brief["n"], "SYNTHETIC", None, 1.5]}
        receipt = backend.submit(lease.task_id, lease.fencing_token, output, idempotency_key=ikey())
        assert receipt.receipt_hash == spec_receipt_hash(
            lease.task_id, lease.fencing_token, True, output, None
        )
    for task in ids:
        (result,) = [r for r in backend.results(task) if r.ok]
        assert result.receipt_hash == spec_receipt_hash(
            task, result.fencing_token, True, result.output, result.error
        )
        assert backend.task(task).state == "done"
