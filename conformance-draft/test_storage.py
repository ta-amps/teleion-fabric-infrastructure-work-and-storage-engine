"""ST01 to ST04: atomic storage. Records change only by compare-and-set and keep every version;
blobs are addressed by their SHA-256; a batch applies completely or not at all (SPEC §6)."""

from __future__ import annotations

import hashlib
import threading
from typing import Any

import pytest

from tessera import NotFound, RecordVersion, RecordWrite, VersionConflict

pytestmark = pytest.mark.fabric_all


def test_ST01_compare_and_set_has_one_winner(backend: Any) -> None:
    first = backend.put_record("cases", "C-FS-PH-03", {"route": "T2"}, expected_version=None)
    assert first.version == 1
    outcomes: list[RecordVersion | VersionConflict] = []
    lock = threading.Lock()

    def writer(n: int) -> None:
        try:
            result: RecordVersion | VersionConflict = backend.put_record(
                "cases", "C-FS-PH-03", {"route": f"T{n % 4 + 1}"}, expected_version=1
            )
        except VersionConflict as exc:
            result = exc
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    wins = [o for o in outcomes if isinstance(o, RecordVersion)]
    conflicts = [o for o in outcomes if isinstance(o, VersionConflict)]
    assert len(wins) == 1 and wins[0].version == 2
    assert len(conflicts) == 7 and all(c.current_version == 2 for c in conflicts)
    with pytest.raises(VersionConflict):
        backend.put_record("cases", "C-FS-PH-03", {}, expected_version=None)
    with pytest.raises(VersionConflict):
        backend.put_record("cases", "C-NEW", {}, expected_version=3)


def test_ST02_every_version_is_kept(backend: Any) -> None:
    for i in range(5):
        backend.put_record("notes", "n-1", {"i": i}, expected_version=None if i == 0 else i)
    history = backend.record_history("notes", "n-1")
    assert [h.version for h in history] == [1, 2, 3, 4, 5]
    assert [h.value["i"] for h in history] == [0, 1, 2, 3, 4]
    assert backend.get_record("notes", "n-1").version == 5
    assert backend.get_record("notes", "n-1", version=2).value == {"i": 1}
    with pytest.raises(NotFound):
        backend.get_record("notes", "missing")


def test_ST03_blobs_are_content_addressed(backend: Any) -> None:
    data = b"EXAMPLE evidence file, not a real document\n" * 200
    digest = backend.put_blob(data)
    assert digest == hashlib.sha256(data).hexdigest()
    assert backend.put_blob(data) == digest
    assert backend.get_blob(digest) == data
    with pytest.raises(NotFound):
        backend.get_blob("0" * 64)


def test_ST04_a_batch_applies_completely_or_not_at_all(backend: Any) -> None:
    backend.put_record("ledger", "a", 1, expected_version=None)
    with pytest.raises(VersionConflict):
        backend.put_records(
            [
                RecordWrite(store="ledger", key="b", value=2, expected_version=None),
                RecordWrite(store="ledger", key="a", value=3, expected_version=7),
            ]
        )
    with pytest.raises(NotFound):
        backend.get_record("ledger", "b")
    assert backend.get_record("ledger", "a").value == 1
    written = backend.put_records(
        [
            RecordWrite(store="ledger", key="b", value=2, expected_version=None),
            RecordWrite(store="ledger", key="a", value=3, expected_version=1),
        ]
    )
    assert [(w.key, w.version) for w in written] == [("b", 1), ("a", 2)]
