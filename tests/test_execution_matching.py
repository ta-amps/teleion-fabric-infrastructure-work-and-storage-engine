"""Execution fingerprints are portable retrieval evidence, not completion proofs."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tessera.execution_matching import compare, fingerprints, normalize_report

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "tests/fixtures/execution_matching_vectors.json"


def _contract() -> dict[str, Any]:
    return {
        "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}}},
        "output_schema": {"type": "integer"},
        "context_hash": "a" * 64,
        "environment_hash": "b" * 64,
        "toolchain_hash": "c" * 64,
    }


def _report() -> dict[str, Any]:
    return {"version": 1, "contract": _contract(), "summaries": {"problem": "Add two integers."}}


def _node(payload: Any) -> Any:
    source = r"""
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
const m = await import(pathToFileURL(process.argv[1] + '/deploy/cloudflare/execution_matching.js'));
const {canonicalJson} = await import(pathToFileURL(process.argv[1] + '/deploy/cloudflare/cloud_buckets.js'));
const p = JSON.parse(fs.readFileSync(0, 'utf8'));
const result = {valid:[], invalid:[], comparisons:[]};
for (const v of p.valid || []) result.valid.push({normalized:m.normalize_report(v.report), fingerprints:await m.fingerprints(v.input, v.report)});
for (const v of p.invalid || []) {
  try { m.normalize_report(v); result.invalid.push(null); }
  catch(e) { result.invalid.push(e.message); }
}
for (const c of p.comparisons || []) result.comparisons.push(m.compare(await m.fingerprints(c.a.input,c.a.report),await m.fingerprints(c.b.input,c.b.report),c.a.report,c.b.report));
if (p.special) {
  result.special = [];
  for (const v of [NaN,Infinity,-Infinity]) {
    try { m.normalize_report({embedding:{model:'m',revision:'r',preprocessing:'p',dimensions:1,vector:[v]}}); result.special.push(false); }
    catch { result.special.push(true); }
  }
  try { m.normalize_report(JSON.parse('{"__proto__":{"version":1}}')); result.special.push(false); }
  catch { result.special.push(true); }
}
process.stdout.write(canonicalJson(result));
"""
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", source, str(ROOT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return json.loads(proc.stdout)


def test_shared_matching_vectors_python_and_worker() -> None:
    fixtures = json.loads(VECTORS.read_text())
    actual = _node(fixtures)
    for i, vector in enumerate(fixtures["valid"]):
        assert normalize_report(vector["report"]) == vector["normalized"], vector["name"]
        assert fingerprints(vector["input"], vector["report"]) == vector["fingerprints"], vector[
            "name"
        ]
        assert actual["valid"][i] == {
            "normalized": vector["normalized"],
            "fingerprints": vector["fingerprints"],
        }, vector["name"]
    for bad, message in zip(fixtures["invalid"], actual["invalid"], strict=True):
        with pytest.raises(ValueError, match="report"):
            normalize_report(bad)
        assert isinstance(message, str) and message


@pytest.mark.parametrize(
    "component",
    ["input_schema", "output_schema", "context_hash", "environment_hash", "toolchain_hash"],
)
def test_same_summary_does_not_hide_changed_execution_dependency(component: str) -> None:
    a = _report()
    b = copy.deepcopy(a)
    b["contract"][component] = {"type": "string"} if component.endswith("schema") else "d" * 64
    af, bf = fingerprints({"x": 1}, a), fingerprints({"x": 1}, b)
    assert af["exact_hash"] != bf["exact_hash"]
    assert compare(af, bf, a, b)["match_kind"] != "exact_input"
    assert _node(
        {
            "comparisons": [
                {"a": {"input": {"x": 1}, "report": a}, "b": {"input": {"x": 1}, "report": b}}
            ]
        }
    )["comparisons"][0] == compare(af, bf, a, b)


def test_exact_inputs_preserve_case_order_and_schema_semantics() -> None:
    report = _report()
    a = fingerprints({"word": "CAPITAL", "values": [1, 2]}, report)
    assert fingerprints({"values": [1, 2], "word": "CAPITAL"}, report) == a
    for value in [{"word": "capital", "values": [1, 2]}, {"word": "CAPITAL", "values": [2, 1]}]:
        assert fingerprints(value, report)["exact_hash"] != a["exact_hash"]
    assert compare(a, a, report, report) == {
        "match_kind": "exact_input",
        "score": 1.0,
        "reasons": ["same recorded inputs and full contract"],
    }


def test_missing_contract_is_an_example_and_shape_alone_is_not_a_match() -> None:
    empty = fingerprints({"x": 1}, {})
    assert empty["exact_hash"] is None and empty["shape_hash"] is None
    assert compare(empty, empty, {}, {}) == {
        "match_kind": "similar_example",
        "score": 1.0,
        "reasons": ["same recorded inputs; contract incomplete"],
    }
    report = {"contract": {"input_schema": {"type": "string"}, "output_schema": {"type": "string"}}}
    assert (
        compare(fingerprints("unrelated", report), fingerprints("other", report), report, report)
        is None
    )
    # A missing snapshot is explicitly nulled by storage, not hashed as an equal sentinel.
    unavailable = {**empty, "input_hash": None, "exact_hash": None}
    assert compare(unavailable, unavailable, {}, {}) is None


def test_shape_needs_actual_similarity_and_lexical_threshold_is_explicit() -> None:
    report = _report()
    a, b = fingerprints({"x": 1}, report), fingerprints({"x": 9}, report)
    result = compare(a, b, report, report)
    assert result["match_kind"] == "compatible_method"
    assert "text SimHash Hamming distance 0/128 <= 24" in result["reasons"]
    js = _node(
        {
            "comparisons": [
                {
                    "a": {"input": {"x": 1}, "report": report},
                    "b": {"input": {"x": 9}, "report": report},
                }
            ]
        }
    )
    assert js["comparisons"][0] == result
    no_contract = {"summaries": report["summaries"]}
    assert (
        compare(
            fingerprints(1, no_contract), fingerprints(2, no_contract), no_contract, no_contract
        )["match_kind"]
        == "similar_example"
    )
    # Fingerprints are internal derived records. Pin the inclusive distance boundary.
    zero = {"simhash": "0" * 32}
    at_limit = {"simhash": f"{(1 << 24) - 1:032x}"}
    over_limit = {"simhash": f"{(1 << 25) - 1:032x}"}
    assert compare(zero, at_limit, {}, {})["match_kind"] == "similar_example"
    assert compare(zero, over_limit, {}, {}) is None


def test_text_is_ascii_normalized_but_results_are_not_request_similarity() -> None:
    a = {"summaries": {"problem": "A"}}
    expected = hashlib.sha256(b"tessera-execution-shingle-v1\0problem\0a").hexdigest()[:32]
    assert fingerprints(1, a)["simhash"] == expected
    assert fingerprints(1, a)["bands"] == [
        f"{i // 4}:{expected[i : i + 4]}" for i in range(0, 32, 4)
    ]
    whitespace = {"summaries": {"problem": "  A\r\n\t "}}
    assert fingerprints(2, whitespace)["simhash"] == expected
    assert fingerprints(1, a)["exact_hash"] is None
    result_only = {"summaries": {"solution": "A"}, "blocks": [{"kind": "error", "text": "A"}]}
    assert fingerprints(1, result_only)["simhash"] is None
    assert (
        fingerprints(1, {"summaries": {"problem": "É"}})["simhash"]
        != fingerprints(1, {"summaries": {"problem": "é"}})["simhash"]
    )


def test_actual_input_numeric_hash_uses_existing_receipt_canonicalization() -> None:
    value = {
        "whole": 1.0,
        "negative_zero": -0.0,
        "normal": 1.5,
        "tiny": 1e-7,
        "large": 1.5e16,
        "large_integer": 2**53 + 2,
    }
    encoded = b'{"large":1.5e+16,"large_integer":9007199254740994,"negative_zero":0,"normal":1.5,"tiny":1e-07,"whole":1}'
    expected = hashlib.sha256(encoded).hexdigest()
    assert fingerprints(value, {})["input_hash"] == expected
    assert (
        _node({"valid": [{"input": value, "report": {}}]})["valid"][0]["fingerprints"]["input_hash"]
        == expected
    )


def test_embedding_identity_validation_and_candidate_only_comparison() -> None:
    emb = {
        "model": "example",
        "revision": "v1",
        "preprocessing": "text-v1",
        "dimensions": 2,
        "vector": [1.0, -0.0],
    }
    a, b = {"embedding": emb}, {"embedding": {**emb, "vector": [0.9, 0.1]}}
    af, bf = fingerprints(1, a), fingerprints(2, b)
    result = compare(af, bf, a, b)
    assert result["match_kind"] == "similar_example" and result["score"] >= 0.85
    assert result["reasons"] == ["compatible caller-supplied embedding cosine >= 0.85"]
    js = _node({"comparisons": [{"a": {"input": 1, "report": a}, "b": {"input": 2, "report": b}}]})[
        "comparisons"
    ][0]
    assert js["match_kind"] == result["match_kind"] and js["score"] == pytest.approx(
        result["score"], abs=1e-12
    )
    for field in ("model", "revision", "preprocessing"):
        incompatible = {"embedding": {**emb, field: "different"}}
        assert compare(af, fingerprints(2, incompatible), a, incompatible) is None
    for vector in [[0, 1], [-1, 0]]:
        incompatible = {"embedding": {**emb, "vector": vector}}
        assert compare(af, fingerprints(2, incompatible), a, incompatible) is None
    for value in [float("nan"), float("inf"), -float("inf"), True, "1", None, 1e40, 1e-50]:
        with pytest.raises(ValueError, match="report"):
            normalize_report({"embedding": {**emb, "dimensions": 1, "vector": [value]}})
    assert all(_node({"special": True})["special"])


def test_report_bounds_invalid_unicode_and_no_input_mutation() -> None:
    report = _report()
    before = copy.deepcopy(report)
    normalized = normalize_report(report)
    assert report == before and normalized == before
    for invalid in [
        {"summaries": {"problem": "x" * 4097}},
        {"model": {"large": "x" * 32768}},
        {"summaries": {"problem": "\ud800"}},
        {"blocks": [{"kind": [], "text": "bad"}]},
        {"harness": {"depth": [[[[[[[[[[[[[[[[[1]]]]]]]]]]]]]]]]]}},
    ]:
        with pytest.raises(ValueError, match="report"):
            normalize_report(invalid)
    errors = _node(
        {"invalid": [{"summaries": {"problem": "\ud800"}}, {"model": {"large": "x" * 32768}}]}
    )["invalid"]
    assert all(errors)
