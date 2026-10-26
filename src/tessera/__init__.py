"""Tessera: atomic work queue and atomic storage for agent swarms.

Every task is a tile: claimed once, finished once, receipted. See SPEC.md for the contract.
"""

from tessera._version import __version__
from tessera.backend import Backend
from tessera.clock import Clock, FakeClock, SystemClock
from tessera.errors import (
    Forbidden,
    IdempotencyConflict,
    InvalidRequest,
    NotFound,
    RateLimited,
    StaleLease,
    TesseraError,
    Unauthorized,
    Unavailable,
    VersionConflict,
)
from tessera.models import (
    ChildResult,
    ChildSpec,
    Event,
    Health,
    Job,
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
from tessera.store import Store

__all__ = [
    "Backend",
    "ChildResult",
    "ChildSpec",
    "Clock",
    "Event",
    "FakeClock",
    "Forbidden",
    "Health",
    "IdempotencyConflict",
    "InvalidRequest",
    "Job",
    "Lease",
    "NotFound",
    "QueueConfig",
    "QueueCounts",
    "RateLimited",
    "Receipt",
    "RecordVersion",
    "RecordWrite",
    "ResultView",
    "StaleLease",
    "Store",
    "SystemClock",
    "TaskSpec",
    "TaskView",
    "TesseraError",
    "Unauthorized",
    "Unavailable",
    "VersionConflict",
    "__version__",
]
