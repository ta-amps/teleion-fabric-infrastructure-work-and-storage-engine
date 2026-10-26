"""Fixtures for the Tessera conformance suite (SPEC.md §10).

Choose the backend with TESSERA_BACKEND_FACTORY="package.module:function"; the default is the
in-process store. A factory takes (db_path, clock) and returns a Backend. If the backend keeps the
clock it was given, the suite moves time instead of sleeping; otherwise it sets short queue timings
and sleeps for real. Service-only tests also need TESSERA_URL and TESSERA_TEST_KEYS (README.md).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tessera import FakeClock, QueueConfig
from tessera.clock import Clock
from tessera.testing import load_factory

DEFAULT_FACTORY = "tessera.testing:create_store_backend"
REAL_TIME_TIMINGS = {
    "lease_seconds": 30,
    "heartbeat_seconds": 1,
    "miss_limit": 2,
    "backoff_base_seconds": 0.2,
    "backoff_cap_seconds": 1.0,
}


class Tick:
    """Moves the fake clock, or sleeps when the backend runs on real time."""

    def __init__(self, clock: FakeClock, controllable: bool) -> None:
        self.clock = clock
        self.controllable = controllable

    def advance(self, seconds: float) -> None:
        if self.controllable:
            self.clock.advance(seconds)
        else:
            time.sleep(seconds)


@pytest.fixture(scope="session")
def factory() -> Callable[[str, Clock], Any]:
    return load_factory(os.environ.get("TESSERA_BACKEND_FACTORY", DEFAULT_FACTORY))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "tessera.db")


@pytest.fixture
def backend(factory: Callable[[str, Clock], Any], db_path: str, clock: FakeClock) -> Iterator[Any]:
    instance = factory(db_path, clock)
    yield instance
    instance.close()


@pytest.fixture
def controls_time(backend: Any, clock: FakeClock) -> bool:
    return getattr(backend, "clock", None) is clock


@pytest.fixture
def tick(clock: FakeClock, controls_time: bool) -> Tick:
    return Tick(clock, controls_time)


@pytest.fixture
def make_queue(backend: Any, controls_time: bool) -> Callable[..., QueueConfig]:
    """Define a queue with the plan's settings, or short ones when the suite must sleep."""

    def make(name: str, **overrides: Any) -> QueueConfig:
        settings: dict[str, Any] = {} if controls_time else dict(REAL_TIME_TIMINGS)
        settings.update(overrides)
        config: QueueConfig = backend.define_queue(QueueConfig(name=name, **settings))
        return config

    return make


@pytest.fixture
def restart(
    factory: Callable[[str, Clock], Any], db_path: str, clock: FakeClock, controls_time: bool
) -> Callable[[Any], Any]:
    """Simulate a crash and restart: a new backend on the same database (in-process), or the
    command in TESSERA_RESTART_CMD for a service."""

    def do_restart(current: Any) -> Any:
        if controls_time:
            current.close()
            return factory(db_path, clock)
        command = os.environ.get("TESSERA_RESTART_CMD")
        if not command:
            pytest.skip("set TESSERA_RESTART_CMD to restart the service under test")
        subprocess.run(shlex.split(command), check=True, timeout=300)
        return current

    return do_restart
