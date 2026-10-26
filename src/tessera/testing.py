"""Factories the conformance suite loads by name (TESSERA_BACKEND_FACTORY="module:function").

A factory takes (db_path, clock) and returns a Backend. A backend whose `clock` attribute is the
clock it was given lets the suite move time instead of sleeping.
"""

from __future__ import annotations

import importlib
import json
import os
import random
from collections.abc import Callable
from typing import Any

from tessera.backend import Backend
from tessera.client import Client
from tessera.clock import Clock
from tessera.store import Store

Factory = Callable[[str, Clock], Any]


def create_store_backend(db_path: str, clock: Clock) -> Store:
    """The in-process reference store, with seeded jitter so runs repeat."""
    return Store(db_path, clock=clock, rng=random.Random(7))


class EmptyBackend:
    """Implements nothing. The suite must fail against it: proof the tests test something (L2)."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    def close(self) -> None:
        return None

    def __getattr__(self, name: str) -> Callable[..., Any]:
        def missing(*args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError(f"EmptyBackend.{name}")

        return missing


def create_empty_backend(db_path: str, clock: Clock) -> EmptyBackend:
    return EmptyBackend(clock)


def create_http_backend(db_path: str, clock: Clock) -> Client:
    """A Tessera service at $TESSERA_URL, using the admin key in $TESSERA_TEST_KEYS. Time-based
    tests then sleep for real, on the short queue timings the suite sets for that case."""
    keys = json.loads(os.environ.get("TESSERA_TEST_KEYS", "{}"))
    return Client(os.environ["TESSERA_URL"], key=keys.get("admin"))


def load_factory(spec: str) -> Factory:
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise ValueError(f"factory must look like 'package.module:function', got {spec!r}")
    factory: Factory = getattr(importlib.import_module(module_name), attr)
    return factory


def _typecheck(store: Store) -> Backend:
    """Static proof (for mypy) that Store satisfies the Backend protocol."""
    return store
