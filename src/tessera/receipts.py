"""Canonical JSON and receipt hashes (SPEC §5.4). A receipt proves which output a fencing token wrote."""

from __future__ import annotations

import hashlib
import json

MAX_EXACT = 2**53  # beyond this, whole floats can't round-trip through a JavaScript number


def _whole(value: object) -> object:
    """Write whole floats as integers (1.0 -> 1), as JavaScript and most JSON libraries do, so a
    receipt computed in another language matches. Other numbers keep their shortest round-trip form."""
    if isinstance(value, float) and value.is_integer() and abs(value) < MAX_EXACT:
        return int(value)
    if isinstance(value, dict):
        return {k: _whole(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_whole(v) for v in value]
    return value


def canonical_json(value: object) -> str:
    """Sorted keys, no spaces, UTF-8, no NaN, whole numbers without a fraction: the same value always
    gives the same text, whichever language wrote it."""
    return json.dumps(
        _whole(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def receipt_hash(
    task_id: str, fencing_token: int, ok: bool, output: object, error: str | None
) -> str:
    body = canonical_json(
        {
            "task_id": task_id,
            "fencing_token": fencing_token,
            "ok": ok,
            "output": output,
            "error": error,
        }
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def blob_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
