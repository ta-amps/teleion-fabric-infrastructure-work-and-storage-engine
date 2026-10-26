"""The in-process Tessera store: the reference implementation of SPEC.md.

Thread-safe: each thread gets its own SQLite connection, and every write is one IMMEDIATE
transaction that also updates the counts record and appends to the event log. Several processes may
share one database file (the `tessera` CLI does), because SQLite serializes writers.
"""

from __future__ import annotations

from tessera._jobs import JobOps
from tessera._observe import ObserveOps
from tessera._storage import StorageOps


class Store(JobOps, ObserveOps, StorageOps):
    """Queues, leases, receipts, model jobs and atomic storage on one SQLite file."""
