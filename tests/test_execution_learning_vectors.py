"""Problem fingerprints, learning-block checks and observations match across Python and Worker."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tessera import fingerprints as F
from tessera import observations as O
from tessera.execution_matching import normalize_report

ROOT = Path(__file__).resolve().parents[1]
VECTORS = json.loads((ROOT / "tests/fixtures/execution_learning_vectors.json").read_text())

NODE = r"""
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
const m = await import(pathToFileURL(process.argv[1] + '/deploy/cloudflare/execution_observations.js'));
const matching = await import(pathToFileURL(process.argv[1] + '/deploy/cloudflare/execution_matching.js'));
const {canonicalJson} = await import(pathToFileURL(process.argv[1] + '/deploy/cloudflare/cloud_buckets.js'));
const v = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = {fingerprints: [], similarities: [], tokens: [], type_signatures: [], learning_valid: [], learning_invalid: [], reports: [], observations: []};
const byName = {};
for (const c of v.fingerprints) {
  const report = matching.normalize_report(c.report), view = await m.brief_view(c.brief);
  const fp = await m.problem_fingerprints(c.category, view, report, c.input_digest);
  byName[c.name] = fp;
  out.fingerprints.push({view, features: m.features(c.category, view, report), fingerprints: fp});
}
for (const p of v.similarities) out.similarities.push(m.similarity(byName[p.a].minhash, byName[p.b].minhash));
for (const t of v.tokens) out.tokens.push(m.tokens(t.text));
for (const t of v.type_signatures) out.type_signatures.push(m.type_signature(t.value));
for (const t of v.learning_valid) out.learning_valid.push(m.normalize_learning(t.value));
for (const t of v.learning_invalid) { try { m.normalize_learning(t.value); out.learning_invalid.push(null); } catch (e) { out.learning_invalid.push(e.message); } }
for (const r of v.reports) out.reports.push(matching.normalize_report(r.report));
for (const o of v.observations) out.observations.push(await m.observation(o.envelope, {category: o.category, ok: o.ok, state_after: o.state_after, error: o.error}));
process.stdout.write(canonicalJson(out));
"""


@pytest.fixture(scope="module")
def worker() -> dict[str, Any]:
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", NODE, str(ROOT)],
        input=json.dumps(VECTORS),
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    result: dict[str, Any] = json.loads(proc.stdout)
    return result


def test_python_reproduces_every_fingerprint_vector() -> None:
    for case in VECTORS["fingerprints"]:
        report = normalize_report(case["report"])
        view = F.brief_view(case["brief"])
        assert view == case["expected"]["view"], case["name"]
        assert F.features(case["category"], view, report) == case["expected"]["features"]
        found = F.problem_fingerprints(case["category"], view, report, case["input_digest"])
        assert found == case["expected"]["fingerprints"], case["name"]
    for item in VECTORS["tokens"]:
        assert F.tokens(item["text"]) == item["tokens"]
    for item in VECTORS["type_signatures"]:
        assert F.type_signature(item["value"]) == item["signature"]


def test_worker_reproduces_every_fingerprint_vector(worker: dict[str, Any]) -> None:
    for case, actual in zip(VECTORS["fingerprints"], worker["fingerprints"], strict=True):
        assert actual == case["expected"], case["name"]
    assert worker["similarities"] == [pair["similarity"] for pair in VECTORS["similarities"]]
    assert worker["tokens"] == [item["tokens"] for item in VECTORS["tokens"]]
    assert worker["type_signatures"] == [item["signature"] for item in VECTORS["type_signatures"]]


def test_learning_block_checks_agree_with_identical_messages(worker: dict[str, Any]) -> None:
    for item, actual in zip(VECTORS["learning_valid"], worker["learning_valid"], strict=True):
        assert O.normalize_learning(item["value"]) == item["normalized"] == actual
    for item, actual in zip(VECTORS["learning_invalid"], worker["learning_invalid"], strict=True):
        with pytest.raises(ValueError, match=r"^report\.learning") as caught:
            O.normalize_learning(item["value"])
        assert str(caught.value) == item["message"] == actual
    for item, actual in zip(VECTORS["reports"], worker["reports"], strict=True):
        assert normalize_report(item["report"]) == item["normalized"] == actual


def test_observations_agree(worker: dict[str, Any]) -> None:
    for item, actual in zip(VECTORS["observations"], worker["observations"], strict=True):
        found = O.observation(
            item["envelope"],
            category=item["category"],
            ok=item["ok"],
            state_after=item["state_after"],
            error=item["error"],
        )
        assert found == item["observation"] == actual


def test_vector_properties_that_matter() -> None:
    by_name = {case["name"]: case["expected"] for case in VECTORS["fingerprints"]}
    pairs = {(p["a"], p["b"]): p["similarity"] for p in VECTORS["similarities"]}
    # A paraphrase of the same verb clears the memory learner's 0.3 floor; an unrelated verb
    # in the same category does not.
    assert pairs[("plain", "paraphrase")] > 0.3 > pairs[("plain", "unrelated")]
    assert pairs[("plain", "plain")] == 1.0
    assert (
        by_name["plain"]["fingerprints"]["exact"] != by_name["paraphrase"]["fingerprints"]["exact"]
    )
    assert by_name["unrelated"]["fingerprints"]["exact"] is None
    # Strings with lone surrogates are skipped rather than hashed differently per runtime.
    assert by_name["lone_surrogate_skipped"]["view"]["text"] == {"title": "kept title"}
    assert by_name["lone_surrogate_skipped"]["view"]["template"] is None
    # Brief text shares one 4096 code point budget; words cap at 512 distinct features.
    assert sum(len(t) for t in by_name["long_text_budget"]["view"]["text"].values()) == 4096
    assert by_name["long_text_budget"]["fingerprints"]["feature_count"] == 1 + 512
    assert by_name["many_keys_and_types"]["view"]["type"].endswith(",...}")
    assert by_name["long_keys_hashed_type"]["view"]["type"].startswith("sha256:")
    assert len(by_name["many_keys_and_types"]["view"]["shape"]) == 32
    assert by_name["empty_brief"]["fingerprints"]["bands"] == []
    assert by_name["empty_brief"]["fingerprints"]["minhash"] is not None
    observed = VECTORS["observations"]
    assert observed[1]["observation"]["brief"] is None
    assert observed[1]["observation"]["report"]["total_tokens"] == 7
    assert observed[0]["observation"]["report"]["model"] == "none"
    assert len(observed[2]["observation"]["error"]) == 512
