"""Real async cancellation, fairness, resource bounds and HTTP default admission."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tessera import QueueConfig, Store
from tessera.access import ClaimLimiter, ServiceSettings
from tessera.claim_admission import AdaptiveClaims
from tessera.errors import RateLimited
from tessera.service import create_app


def test_same_key_can_make_more_than_sixty_healthy_claims(tmp_path: Path) -> None:
    store = Store(tmp_path / "admission.db")
    store.define_queue(QueueConfig(name="conformance-admission"))
    key, _ = store.issue_key("synthetic", queues=["conformance-admission"])
    with TestClient(create_app(store, ServiceSettings())) as client:
        for _ in range(100):
            response = client.post(
                "/v1/tasks/claim",
                json={"queue": "conformance-admission", "worker": "synthetic"},
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
        with pytest.raises(ValueError, match="synthetic store failure"):
            async with limiter.admit("a"):
                raise ValueError("synthetic store failure")
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
