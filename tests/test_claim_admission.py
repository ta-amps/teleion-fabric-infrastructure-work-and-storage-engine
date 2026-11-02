"""Real async cancellation, fairness, resource bounds and HTTP default admission."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tessera import QueueConfig, Store, TaskSpec
from tessera.access import ClaimLimiter, Gate, ServiceSettings
from tessera.claim_admission import AdaptiveClaims, IdleClaims, empty_claim_retry_after
from tessera.errors import RateLimited, Unauthorized
from tessera.service import create_app


def test_same_key_can_make_more_than_sixty_healthy_claims(tmp_path: Path) -> None:
    store = Store(tmp_path / "admission.db")
    store.define_queue(QueueConfig(name="conformance-admission"))
    key, _ = store.issue_key("example", queues=["conformance-admission"])
    with TestClient(create_app(store, ServiceSettings())) as client:
        for _ in range(100):
            response = client.post(
                "/v1/tasks/claim",
                json={"queue": "conformance-admission", "worker": "example"},
                headers={"authorization": f"Bearer {key}"},
            )
            assert response.status_code == 204
            assert "x-claim-wait-ms" in response.headers
    store.close()


def test_fair_waiting_cancellation_and_slot_recovery() -> None:
    async def scenario() -> None:
        gate = AdaptiveClaims(initial=1, maximum=1)
        first = await gate.acquire("first")
        a1 = asyncio.create_task(gate.acquire("a"))
        a2 = asyncio.create_task(gate.acquire("a"))
        b = asyncio.create_task(gate.acquire("b"))
        await asyncio.sleep(0)
        gate.release(first)
        gate.release(await a1)
        assert not a2.done()
        b.cancel()  # Cancellation after the slot was granted.
        with pytest.raises(asyncio.CancelledError):
            await b
        gate.release(await a2)
        assert (gate.active, gate.pending, len(gate.queues)) == (0, 0, 0)
        first = await gate.acquire("first")
        waiting = asyncio.create_task(gate.acquire("cancel"))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        gate.release(first)
        assert (gate.active, gate.pending) == (0, 0)

    asyncio.run(scenario())


def test_pending_overload_and_expiry_never_create_a_late_claim() -> None:
    async def scenario() -> None:
        gate = AdaptiveClaims(initial=1, maximum=1, max_pending=1, max_wait=0.01)
        first = await gate.acquire("a")
        waiter = asyncio.create_task(gate.acquire("b"))
        await asyncio.sleep(0)
        with pytest.raises(RateLimited, match="pending queue full") as error:
            await gate.acquire("c")
        assert error.value.retry_after >= 1
        with pytest.raises(RateLimited, match="pacing deadline"):
            await waiter
        gate.release(first)
        assert (gate.active, gate.pending, len(gate.queues)) == (0, 0, 0)

    asyncio.run(scenario())


def test_slow_service_reduces_concurrency_and_healthy_service_recovers() -> None:
    async def scenario() -> None:
        now = [0.0]
        gate = AdaptiveClaims(initial=8, maximum=10, target=0.1, clock=lambda: now[0])
        permit = await gate.acquire("a")
        now[0] += 1
        gate.release(permit)
        assert gate.limit == 5
        for _ in range(40):
            permits = [await gate.acquire("a") for _ in range(gate.limit)]
            now[0] += 0.01
            for permit in permits:
                gate.release(permit)
            now[0] += 0.1
        assert gate.limit == 10

    asyncio.run(scenario())


def test_failed_claim_always_releases_admission() -> None:
    async def scenario() -> None:
        limiter = ClaimLimiter()
        with pytest.raises(ValueError, match="example store failure"):
            async with limiter.admit("a"):
                raise ValueError("example store failure")
        assert limiter.admission.active == 0

    asyncio.run(scenario())


def test_invalid_config_and_explicit_legacy_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TESSERA_CLAIMS_PER_MINUTE", raising=False)
    assert ServiceSettings.from_env().claims_per_minute == 0
    monkeypatch.setenv("TESSERA_CLAIMS_PER_MINUTE", "5")
    assert ServiceSettings.from_env().claims_per_minute == 5
    with pytest.raises(ValueError, match="invalid claim admission"):
        AdaptiveClaims(initial=4, maximum=3)
    with pytest.raises(ValueError, match="must be finite"):
        AdaptiveClaims(target=float("nan"))


def test_revoked_waiting_worker_cannot_claim(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "revocation.db")
        store.define_queue(QueueConfig(name="conformance-admission"))
        task_id = store.enqueue(
            TaskSpec(queue="conformance-admission", brief={"example_only": True})
        )
        _, grant = store.issue_key("example", queues=["conformance-admission"])
        gate = Gate(store)
        limiter = ClaimLimiter(
            settings=ServiceSettings(claim_initial_concurrency=1, claim_max_concurrency=1)
        )
        held = await limiter.admission.acquire("first")

        async def claim() -> None:
            async with limiter.admit(grant.key_id):
                gate.recheck(grant)
                store.claim("conformance-admission", "example", key_id=grant.key_id)

        pending = asyncio.create_task(claim())
        await asyncio.sleep(0)
        store.revoke_key(grant.key_id)
        limiter.admission.release(held)
        with pytest.raises(Unauthorized, match="expired or revoked"):
            await pending
        assert store.task(task_id).state == "ready"
        assert limiter.admission.active == 0
        store.close()

    asyncio.run(scenario())


def test_empty_claim_hints_match_the_worker_and_reset_on_work(tmp_path: Path) -> None:
    assert [empty_claim_retry_after(n) for n in (1, 2, 3, 4, 5, 6, 50)] == [
        5,
        10,
        20,
        40,
        60,
        60,
        60,
    ]
    idle = IdleClaims()
    assert [idle.empty("main", "q", "k1") for _ in range(3)] == [5, 10, 20]
    assert idle.empty("main", "q", "k2") == 5
    store = Store(tmp_path / "idle.db")
    store.define_queue(QueueConfig(name="conformance-idle"))
    key, _ = store.issue_key("example", queues=["conformance-idle"])
    headers = {"authorization": f"Bearer {key}"}
    claim = {"queue": "conformance-idle", "worker": "example"}
    with TestClient(create_app(store, ServiceSettings())) as client:
        hints = [client.post("/v1/tasks/claim", json=claim, headers=headers) for _ in range(4)]
        assert [(r.status_code, r.headers["retry-after"]) for r in hints] == [
            (204, "5"),
            (204, "10"),
            (204, "20"),
            (204, "40"),
        ]
        task = {"queue": "conformance-idle", "brief": {"example_only": True}}
        assert client.post("/v1/tasks", json=task, headers=headers).status_code == 201
        assert client.post("/v1/tasks/claim", json=claim, headers=headers).status_code == 200
        empty = client.post("/v1/tasks/claim", json=claim, headers=headers)
        assert (empty.status_code, empty.headers["retry-after"]) == (204, "5")
    store.close()


def test_one_principal_cannot_hold_every_waiting_slot() -> None:
    async def scenario() -> None:
        gate = AdaptiveClaims(initial=1, maximum=1, max_pending=4, max_wait=5.0, target=0.5)

        async def outcome(principal: str) -> str:
            try:
                gate.release(await gate.acquire(principal))
            except RateLimited as exc:
                return exc.detail
            return "granted"

        held = await gate.acquire("holder")
        noisy = [asyncio.create_task(outcome("noisy")) for _ in range(4)]
        await asyncio.sleep(0)
        assert gate.pending == 4
        # Alone, a principal may still use every slot; the total never grows.
        assert "pending queue full" in await outcome("noisy")
        quiet = asyncio.create_task(outcome("quiet"))
        await asyncio.sleep(0)
        assert gate.pending == 4
        gate.release(held)
        assert await quiet == "granted"
        results = await asyncio.gather(*noisy)
        assert sum(r == "granted" for r in results) == 3
        assert sum("fair share" in r for r in results) == 1
        assert gate.pending == 0 and gate.active == 0

    asyncio.run(scenario())
