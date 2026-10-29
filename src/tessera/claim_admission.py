"""Async, process-local claim pacing; leases and access rules remain in the Store."""

from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

from tessera.errors import RateLimited


@dataclass
class Permit:
    started: float
    wait_seconds: float
    saturated: bool
    released: bool = False


@dataclass
class Pending:
    arrived: float
    future: asyncio.Future[Permit]
    timer: asyncio.TimerHandle | None = None


class AdaptiveClaims:
    """Round-robin waiting by principal with latency-driven concurrency, not a time quota."""

    def __init__(
        self,
        *,
        initial: int = 32,
        maximum: int = 1024,
        max_pending: int = 2048,
        max_wait: float = 10.0,
        target: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= initial <= maximum or max_pending < 1 or max_wait <= 0 or target <= 0:
            raise ValueError("invalid claim admission settings")
        if not math.isfinite(max_wait) or not math.isfinite(target):
            raise ValueError("claim timing settings must be finite")
        self.limit, self.maximum, self.max_pending = initial, maximum, max_pending
        self.max_wait, self.target, self.clock = max_wait, target, clock
        self.active = 0
        self.pending = 0
        self.queues: OrderedDict[str, deque[Pending]] = OrderedDict()
        self.ewma: float | None = None
        self.last_adjustment = -math.inf

    def pressure(self, reason: str) -> RateLimited:
        retry = max(
            1, math.ceil((self.ewma or self.target) * math.ceil((self.pending + 1) / self.limit))
        )
        return RateLimited(f"claim pressure: {reason}; retry with jitter", retry_after=retry)

    def start(self, arrived: float) -> Permit:
        self.active += 1
        now = self.clock()
        return Permit(now, max(0, now - arrived), self.active >= self.limit)

    async def acquire(self, principal: str) -> Permit:
        arrived = self.clock()
        if self.active < self.limit and self.pending == 0:
            return self.start(arrived)
        if self.pending >= self.max_pending and not self.yield_slot_to(principal):
            raise self.pressure("pending queue full")
        loop = asyncio.get_running_loop()
        entry = Pending(arrived, loop.create_future())
        self.queues.setdefault(principal, deque()).append(entry)
        self.pending += 1

        def expire() -> None:
            if self.discard(principal, entry):
                if not entry.future.done():
                    entry.future.set_exception(self.pressure("pacing deadline reached"))
                self.drain()

        entry.timer = loop.call_later(self.max_wait, expire)
        self.drain()
        try:
            return await entry.future
        except asyncio.CancelledError:
            self.discard(principal, entry)
            # Cancellation after a permit was granted must not leak the slot.
            if (
                entry.future.done()
                and not entry.future.cancelled()
                and entry.future.exception() is None
            ):
                self.release(entry.future.result())
            self.drain()
            raise

    def yield_slot_to(self, principal: str) -> bool:
        """Share a full waiting queue fairly (same rule as the Worker).

        When a principal holding fewer than its share of the pending slots arrives, the
        principal holding the most (and more than its share) gives up its newest waiter. One
        principal may still use every slot while nobody else waits; the total never grows.
        """
        waiting = len(self.queues) + (0 if principal in self.queues else 1)
        share = math.ceil(self.max_pending / waiting)
        if len(self.queues.get(principal, ())) >= share:
            return False
        heaviest, most = None, 0
        for other, queue in self.queues.items():
            if other != principal and len(queue) > most:
                heaviest, most = other, len(queue)
        if heaviest is None or most <= share:
            return False
        entry = self.queues[heaviest][-1]
        self.discard(heaviest, entry)
        if not entry.future.done():
            entry.future.set_exception(self.pressure("fair share of waiting slots"))
        return True

    def discard(self, principal: str, entry: Pending) -> bool:
        queue = self.queues.get(principal)
        if queue is None or entry not in queue:
            return False
        queue.remove(entry)
        if not queue:
            del self.queues[principal]
        self.pending -= 1
        if entry.timer:
            entry.timer.cancel()
        return True

    def drain(self) -> None:
        while self.active < self.limit and self.pending:
            principal, queue = self.queues.popitem(last=False)
            entry = queue.popleft()
            if queue:
                self.queues[principal] = queue
            self.pending -= 1
            if entry.timer:
                entry.timer.cancel()
            if entry.future.done():
                continue
            if self.clock() - entry.arrived >= self.max_wait:
                entry.future.set_exception(self.pressure("pacing deadline reached"))
            else:
                entry.future.set_result(self.start(entry.arrived))

    def release(self, permit: Permit) -> None:
        if permit.released:
            return
        permit.released = True
        self.active -= 1
        now = self.clock()
        elapsed = max(0, now - permit.started)
        self.ewma = elapsed if self.ewma is None else self.ewma * 0.8 + elapsed * 0.2
        if now - self.last_adjustment >= self.target:
            if self.ewma > self.target:
                self.limit = max(1, math.floor(self.limit * 0.7))
                self.last_adjustment = now
            elif self.ewma < self.target * 0.8 and (permit.saturated or self.pending):
                self.limit = min(self.maximum, self.limit + 1)
                self.last_adjustment = now
        self.drain()


# Idle pacing for empty claims, identical to the Worker. The Retry-After hint doubles while
# one key keeps finding a queue empty and resets when that key is granted a lease or the
# queue receives new work. It reports observed emptiness; it is not a limit.
EMPTY_CLAIM_BASE_SECONDS = 5
EMPTY_CLAIM_MAX_SECONDS = 60


def empty_claim_retry_after(consecutive_empty: int) -> int:
    n = max(1, int(consecutive_empty))
    return min(EMPTY_CLAIM_MAX_SECONDS, EMPTY_CLAIM_BASE_SECONDS << min(n - 1, 16))


class IdleClaims:
    """Per namespace, queue and key count of consecutive empty claims (process memory)."""

    def __init__(self, max_queues: int = 4096) -> None:
        self.max_queues = max_queues
        self.queues: dict[tuple[str, str], dict[str, int]] = {}

    def empty(self, namespace: str, queue: str, key_id: str) -> int:
        keys = self.queues.get((namespace, queue))
        if keys is None:
            if len(self.queues) >= self.max_queues:
                self.queues.clear()  # memory bound; hints restart at the base
            keys = self.queues[(namespace, queue)] = {}
        keys[key_id] = keys.get(key_id, 0) + 1
        return empty_claim_retry_after(keys[key_id])

    def granted(self, namespace: str, queue: str, key_id: str) -> None:
        self.queues.get((namespace, queue), {}).pop(key_id, None)

    def work(self, namespace: str, queue: str) -> None:
        self.queues.pop((namespace, queue), None)
