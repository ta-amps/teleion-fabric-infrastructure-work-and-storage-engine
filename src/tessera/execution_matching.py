"""Bounded execution-report validation and candidate matching, never reuse authorization."""

from __future__ import annotations

import hashlib
import math
import re
import struct
from typing import Any, NoReturn

from tessera.observations import normalize_learning
from tessera.receipts import canonical_json

MAX_REPORT_BYTES = 32768
MAX_SAFE_INT = 2**53 - 1
TEXT_DISTANCE_LIMIT = 24
VECTOR_COSINE_MIN = 0.85
TEXT_PROFILE = "execution-text-simhash128-v1"
ROOT_FIELDS = {
    "version",
    "harness",
    "model",
    "summaries",
    "contract",
    "usage",
    "blocks",
    "artifact_refs",
    "embedding",
    "learning",
}
CONTRACT_FIELDS = {
    "input_schema",
    "output_schema",
    "context_hash",
    "environment_hash",
    "toolchain_hash",
}
TOKEN_FIELDS = {"input_tokens", "output_tokens", "total_tokens", "cached_tokens"}
BLOCK_KINDS = {"problem", "input", "context", "environment", "solution", "error"}
REQUEST_KINDS = {"problem", "input", "context", "environment"}
_ASCII_SPACE = re.compile(r"[ \t\n\v\f\r]+")


def _fail(path: str, detail: str) -> NoReturn:
    raise ValueError(f"{path}: {detail}")


def _json(value: Any, path: str = "report", depth: int = 0) -> None:
    if depth > 16:
        _fail(path, "maximum nesting is 16")
    if value is None or type(value) is bool:
        return
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            _fail(path, "invalid Unicode")
        return
    if type(value) in (int, float):
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            _fail(path, "number must be finite")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _json(item, f"{path}[{i}]", depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(path, "object keys must be strings")
            _json(key, path, depth + 1)
            _json(item, f"{path}.{key}", depth + 1)
        return
    _fail(path, "must be JSON data")


def _object(value: Any, path: str, fields: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    if fields is not None and set(value) - fields:
        _fail(path, "unknown fields")
    return dict(value)


def _text(value: Any, path: str, cap: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip(" \t\n\v\f\r")):
        _fail(path, "must be a string" if empty else "must be a nonempty string")
    if len(value.encode("utf-8")) > cap:
        _fail(path, f"exceeds {cap} UTF-8 bytes")
    return str(value)


def _integer(value: Any, path: str, minimum: int, maximum: int) -> int:
    if (
        type(value) not in (int, float)
        or not minimum <= value <= maximum
        or not float(value).is_integer()
    ):
        _fail(path, f"must be an integer in {minimum}..{maximum}")
    return int(value)


def normalize_report(value: Any) -> dict[str, Any]:
    """Validate untrusted report data; admission/provenance belong to the store."""
    _json(value)
    report = _object(value, "report", ROOT_FIELDS)
    if not report:
        return {}
    report["version"] = _integer(report.get("version", 1), "report.version", 1, 1)
    for name in ("harness", "model"):
        # A null model is an explicit declaration that the work used no model.
        if name in report and not (name == "model" and report[name] is None):
            report[name] = _object(report[name], f"report.{name}")
    if "summaries" in report:
        summaries = _object(
            report["summaries"], "report.summaries", {"problem", "input", "solution"}
        )
        report["summaries"] = {
            key: _text(item, f"report.summaries.{key}", 4096, empty=True)
            for key, item in summaries.items()
        }
    if "contract" in report:
        contract = _object(report["contract"], "report.contract", CONTRACT_FIELDS)
        for name, item in contract.items():
            if name.endswith("_schema"):
                _object(item, f"report.contract.{name}")
            elif not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item):
                _fail(f"report.contract.{name}", "must be a lowercase SHA-256 digest")
        report["contract"] = contract
    if "usage" in report:
        usage = _object(report["usage"], "report.usage", TOKEN_FIELDS | {"source", "reason"})
        _text(usage.get("source"), "report.usage.source", 128)
        if "reason" in usage:
            _text(usage["reason"], "report.usage.reason", 1024)
        for name in TOKEN_FIELDS & usage.keys():
            if usage[name] is None:
                _text(usage.get("reason"), "report.usage.reason", 1024)
            else:
                _integer(usage[name], f"report.usage.{name}", 0, MAX_SAFE_INT)
        report["usage"] = usage
    if "blocks" in report:
        blocks = report["blocks"]
        if not isinstance(blocks, list) or len(blocks) > 8:
            _fail("report.blocks", "must be an array of at most 8 blocks")
        for i, item in enumerate(blocks):
            block = _object(item, f"report.blocks[{i}]", {"kind", "text"})
            if not isinstance(block.get("kind"), str) or block["kind"] not in BLOCK_KINDS:
                _fail(f"report.blocks[{i}].kind", "unsupported block kind")
            _text(block.get("text"), f"report.blocks[{i}].text", 4096, empty=True)
    if "artifact_refs" in report:
        refs = report["artifact_refs"]
        if not isinstance(refs, list) or len(refs) > 32:
            _fail("report.artifact_refs", "must be an array of at most 32 references")
        for i, item in enumerate(refs):
            ref = _object(item, f"report.artifact_refs[{i}]", {"store", "key", "version"})
            _text(ref.get("store"), f"report.artifact_refs[{i}].store", 128)
            _text(ref.get("key"), f"report.artifact_refs[{i}].key", 512)
            _integer(ref.get("version"), f"report.artifact_refs[{i}].version", 1, MAX_SAFE_INT)
    if "embedding" in report:
        emb = _object(
            report["embedding"],
            "report.embedding",
            {
                "model",
                "revision",
                "preprocessing",
                "dimensions",
                "vector",
            },
        )
        for name in ("model", "revision", "preprocessing"):
            _text(emb.get(name), f"report.embedding.{name}", 256)
        dimensions = _integer(emb.get("dimensions"), "report.embedding.dimensions", 1, 2048)
        vector = emb.get("vector")
        if not isinstance(vector, list) or len(vector) != dimensions:
            _fail("report.embedding.vector", "length must equal dimensions")
        normalized: list[float] = []
        for item in vector:
            if type(item) not in (int, float) or not math.isfinite(item):
                _fail("report.embedding.vector", "values must be finite numbers")
            try:
                number = struct.unpack("<f", struct.pack("<f", item))[0]
            except (OverflowError, struct.error):
                _fail("report.embedding.vector", "values must fit finite float32")
            if not math.isfinite(number):
                _fail("report.embedding.vector", "values must fit finite float32")
            normalized.append(0.0 if number == 0 else number)
        if not any(normalized):
            _fail("report.embedding.vector", "zero vector is not comparable")
        report["embedding"] = {**emb, "vector": normalized}
    if "learning" in report:
        report["learning"] = normalize_learning(report["learning"])
    if len(canonical_json(report).encode("utf-8")) > MAX_REPORT_BYTES:
        _fail("report", "exceeds 32768 canonical UTF-8 bytes")
    return report


def _digest(value: Any, domain: str = "") -> str:
    return hashlib.sha256((domain + canonical_json(value)).encode("utf-8")).hexdigest()


def _normalized(text: str) -> str:
    folded = "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in text)
    return _ASCII_SPACE.sub(" ", folded).strip(" ")


def _text_blocks(report: dict[str, Any]) -> list[list[str]]:
    summaries = report.get("summaries", {})
    blocks = [[kind, summaries[kind]] for kind in ("problem", "input") if kind in summaries]
    blocks.extend(
        [block["kind"], block["text"]]
        for block in report.get("blocks", [])
        if block["kind"] in REQUEST_KINDS
    )
    return [[kind, _normalized(text)] for kind, text in blocks if _normalized(text)]


def _simhash(blocks: list[list[str]]) -> str | None:
    features: set[bytes] = set()
    for kind, text in blocks:
        raw = text.encode("utf-8")
        for i in range(max(1, len(raw) - 4)):
            features.add(kind.encode() + b"\0" + raw[i : i + 5])
    if not features:
        return None
    accum = [0] * 128
    for feature in features:
        digest = hashlib.sha256(b"tessera-execution-shingle-v1\0" + feature).digest()
        for bit in range(128):
            accum[bit] += 1 if digest[bit // 8] & (1 << (7 - bit % 8)) else -1
    value = 0
    for total in accum:
        value = (value << 1) | int(total > 0)
    return f"{value:032x}"


def fingerprints(input_snapshot: Any, report: Any) -> dict[str, Any]:
    """Fingerprint declared dependencies; exact equality does not authorize reuse."""
    _json(input_snapshot, "input_snapshot")
    normalized = normalize_report(report)
    input_hash = _digest(input_snapshot)
    contract = normalized.get("contract", {})
    contract_hash = _digest(contract, "execution-contract-v1\0") if contract else None
    complete = contract.keys() >= CONTRACT_FIELDS
    shape = {name: contract[name] for name in ("input_schema", "output_schema") if name in contract}
    blocks = _text_blocks(normalized)
    simhash = _simhash(blocks)
    return {
        "input_hash": input_hash,
        "contract_hash": contract_hash,
        "exact_hash": _digest(
            {"input_hash": input_hash, "contract": contract}, "execution-exact-v1\0"
        )
        if complete
        else None,
        "shape_hash": _digest(shape, "execution-shape-v1\0") if len(shape) == 2 else None,
        "simhash": simhash,
        "bands": [f"{i // 4}:{simhash[i : i + 4]}" for i in range(0, 32, 4)] if simhash else [],
        "text_hash": _digest({"profile": TEXT_PROFILE, "blocks": blocks}) if blocks else None,
    }


def _cosine(a: dict[str, Any], b: dict[str, Any]) -> float | None:
    x, y = a.get("embedding"), b.get("embedding")
    if (
        not x
        or not y
        or any(x[k] != y[k] for k in ("model", "revision", "preprocessing", "dimensions"))
    ):
        return None
    dot = nx = ny = 0.0
    for vx, vy in zip(x["vector"], y["vector"], strict=True):
        dot += vx * vy
        nx += vx * vx
        ny += vy * vy
    return max(-1.0, min(1.0, dot / (math.sqrt(nx) * math.sqrt(ny))))


def compare(
    candidate_fingerprints: dict[str, Any],
    query_fingerprints: dict[str, Any],
    candidate_report: Any,
    query_report: Any,
) -> dict[str, Any] | None:
    """Rank already-authorized history candidates; scores are not probabilities."""
    candidate, query = normalize_report(candidate_report), normalize_report(query_report)
    a, b = candidate_fingerprints, query_fingerprints
    if a.get("exact_hash") and a["exact_hash"] == b.get("exact_hash"):
        return {
            "match_kind": "exact_input",
            "score": 1.0,
            "reasons": ["same recorded inputs and full contract"],
        }
    reasons: list[str] = []
    score = 0.0
    same_input = bool(a.get("input_hash") and a["input_hash"] == b.get("input_hash"))
    if same_input:
        reasons.append("same recorded inputs")
        score = 1.0
    if a.get("simhash") and b.get("simhash"):
        distance = (int(a["simhash"], 16) ^ int(b["simhash"], 16)).bit_count()
        if distance <= TEXT_DISTANCE_LIMIT:
            reasons.append(f"text SimHash Hamming distance {distance}/128 <= 24")
            score = max(score, 1 - distance / 128)
    cosine = _cosine(candidate, query)
    if cosine is not None and cosine >= VECTOR_COSINE_MIN:
        reasons.append("compatible caller-supplied embedding cosine >= 0.85")
        score = max(score, cosine)
    if not reasons:
        return None
    if a.get("shape_hash") and a["shape_hash"] == b.get("shape_hash"):
        return {
            "match_kind": "compatible_method",
            "score": score,
            "reasons": ["same input/output schema shape", *reasons],
        }
    if same_input and (not a.get("exact_hash") or not b.get("exact_hash")):
        reasons[0] = "same recorded inputs; contract incomplete"
    return {"match_kind": "similar_example", "score": score, "reasons": reasons}
