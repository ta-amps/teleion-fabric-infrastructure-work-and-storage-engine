"""Continuous tuning example: real tool definitions, held-out splits, contamination checks,
strict metrics, mining, the pre-registered rule, a full noun/verb loop and the seed pack.

Every test runs without torch; the GPU backend is exercised by recorded runs only.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tessera import RecordWrite, Store, TaskSpec
from tessera.ontology import validate_category_payload

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from examples.continuous_tuning import pack as pack_module  # noqa: E402
from examples.continuous_tuning.dataset import accept, contamination, fingerprints  # noqa: E402
from examples.continuous_tuning.fixture_backend import FixtureBackend  # noqa: E402
from examples.continuous_tuning.loop import tick  # noqa: E402
from examples.continuous_tuning.metrics import aggregate, score  # noqa: E402
from examples.continuous_tuning.mining import mine  # noqa: E402
from examples.continuous_tuning.promotion import RULE_PATH, decide, load_rule, paired_bootstrap  # noqa: E402
from examples.continuous_tuning.task import generate, prompt_messages, target_text  # noqa: E402
from examples.continuous_tuning.templates import SUBSTITUTES  # noqa: E402
from examples.continuous_tuning.tools import (  # noqa: E402
    BENCHMARK_TOOLS_SHA256,
    FAMILY_OF,
    FAMILY_SPLIT,
    SERVED,
    benchmark_drift,
    export_snapshot,
    load_snapshot,
    load_tools,
    tools_in_split,
    validate,
)

HERE = ROOT / "examples/continuous_tuning"
# A made-up base description: the fixture backend never loads weights.
BASE = ROOT / "tests/fixtures/continuous_tuning_base.json"
# The rule was committed before round 1; editing it must be a visible, deliberate act.
# Revised once for wording only (one limitation sentence, 2026-11-01); the rule itself is unchanged.
RULE_SHA256 = "4526698009cb5c7d69c380ddb8c67f56d0f7cc03d07b55e166da1a91857b39cc"
TOOLS = load_tools()


def test_served_snapshot_is_the_live_tool_list() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is required to read the hosted tool definitions")
    assert export_snapshot(ROOT)["tools"] == load_snapshot(SERVED)["tools"], (
        "Served tool definitions changed: run `python -m examples.continuous_tuning.cli export-tools`"
    )


def test_benchmark_snapshot_is_pinned_and_its_labels_stay_valid_calls() -> None:
    benchmark = load_snapshot()
    assert benchmark["tools_sha256"] == BENCHMARK_TOOLS_SHA256
    drift = benchmark_drift(benchmark, load_snapshot(SERVED))
    assert drift["removed"] == [] and drift["added"] == [] and drift["schema_changed"] == [], (
        f"Served schemas moved away from the frozen benchmark {drift}; register a new benchmark version"
    )
    # Recorded and allowed: PR #13 rewrote submit's description to mention the learning block.
    assert set(drift["description_changed"]) <= {"submit"}


def test_python_mcp_door_serves_the_hosted_tools_and_required_arguments(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    from tessera.access import ClaimLimiter, Gate, ServiceSettings
    from tessera.mcp_door import build_mcp

    store = Store(tmp_path / "door.db")
    settings = ServiceSettings()
    try:
        server, _ = build_mcp(
            store, Gate(store), settings, ClaimLimiter(settings.claims_per_minute, settings)
        )
        served = {tool.name: tool.input_schema for tool in asyncio.run(server.list_tools())}
    finally:
        store.close()
    hosted = load_tools(SERVED)
    # Known divergence: the Python door also serves execution observations; hosted does not yet.
    assert set(served) - set(hosted) == {"list_execution_observations"}
    assert set(hosted) <= set(served)
    for name, tool in hosted.items():
        schema = tool["inputSchema"]
        assert set(schema.get("required", [])) == set(served[name].get("required", [])), name
        # The hosted schemas may add governance fields; the door never adds unknown ones.
        assert set(served[name].get("properties", {})) <= set(schema.get("properties", {})), name


def test_generation_is_deterministic_and_every_label_validates() -> None:
    for split, count in (("train", 80), ("dev", 80), ("test", 150)):
        first = generate(split, count, f"{split}-check", "initial", TOOLS)
        assert first == generate(split, count, f"{split}-check", "initial", TOOLS)
        for row in first:
            target, schema = row["target"], None
            if row["tool"]:
                schema = TOOLS[row["tool"]]["inputSchema"]
            if "refusal" not in target:
                assert row["tool"] in row["offered"] and not validate(target["arguments"], schema)
            elif target["refusal"] == "missing_required_argument":
                assert row["tool"] in row["offered"]
                assert set(SUBSTITUTES.get(row["tool"], ())).isdisjoint(row["offered"])
            elif row["tool"]:
                assert row["tool"] not in row["offered"]
                assert set(SUBSTITUTES.get(row["tool"], ())).isdisjoint(row["offered"])
            assert json.loads(target_text(row, TOOLS)) is not None


def test_whole_families_templates_and_frames_are_held_out() -> None:
    rows = {
        split: generate(split, 200, f"{split}-holdout", "x", TOOLS)
        for split in ("train", "dev", "test")
    }
    for split, items in rows.items():
        assert {FAMILY_SPLIT[r["family"]] for r in items if r["family"]} == {split}
    for field in ("template", "frame", "tool"):
        sets = {split: {r[field] for r in items if r[field]} for split, items in rows.items()}
        assert not sets["train"] & sets["dev"] and not sets["train"] & sets["test"]
        assert not sets["dev"] & sets["test"]
    train_tools = set(tools_in_split("train"))
    # Training prompts never even show a held-out tool definition.
    assert all(set(r["offered"]) <= train_tools for r in rows["train"])
    assert not contamination(rows["train"], rows["dev"] + rows["test"])["shape"]


def test_curation_rejects_contaminated_duplicate_and_held_out_rows() -> None:
    train = generate("train", 20, "train-c", "initial", TOOLS)
    test = generate("test", 20, "test-c", "frozen_holdout", TOOLS)
    leaked = {**copy.deepcopy(test[0]), "id": "leak", "origin": "stream", "split": "train"}
    accepted, rejected = accept([*train, train[0] | {"id": "dup"}, leaked], [], test)
    reasons = {item["id"]: item["reason"] for item in rejected}
    assert reasons["leak"] == "held-out family" and reasons["dup"].startswith("duplicate")
    copied = {**copy.deepcopy(train[1]), "id": "copied"}
    _, rejected = accept([copied], [], [{**train[1], "origin": "frozen_holdout"}])
    assert rejected[0]["reason"].startswith("contamination")
    assert len(accepted) == len({fingerprints(r)["request"] for r in train})


def test_metrics_catch_the_field_failures_and_separate_formatting() -> None:
    row = next(
        r for r in generate("test", 30, "m", "x", TOOLS) if "arg_type:object" in r["features"]
    )
    gold = json.loads(target_text(row, TOOLS))
    name = next(k for k, v in gold["arguments"].items() if isinstance(v, dict))
    cases = {
        "exact": json.dumps(gold),
        "fenced": "```json\n" + json.dumps(gold) + "\n```",
        "stringified": json.dumps({**gold, "arguments": {**gold["arguments"], name: json.dumps(gold["arguments"][name])}}),
        "alias": json.dumps({"name": gold["tool"], "parameters": gold["arguments"]}),
        "refused": json.dumps({"refusal": "no_matching_tool"}),
    }  # fmt: skip
    scores = {k: score(row, v, TOOLS) for k, v in cases.items()}
    assert scores["exact"]["exact"] and scores["exact"]["schema_valid"]
    assert not scores["fenced"]["exact"] and scores["fenced"]["exact_lenient"]
    assert scores["stringified"]["stringified"] and not scores["stringified"]["exact_lenient"]
    assert scores["alias"]["failures"][0]["category"] == "wrong_shape"
    assert scores["refused"]["failures"][0]["category"] == "false_refusal"
    summary = aggregate(list(scores.values()))
    assert summary["exact_match_strict"] == 0.2 and summary["json_valid_rate"] == 0.8


def test_mining_turns_dev_failures_into_training_family_rows_with_a_control() -> None:
    dev = generate("dev", 40, "dev-m", "frozen_holdout", TOOLS)
    scores = []
    for row in dev:
        gold = target_text(row, TOOLS)
        reply = gold
        if row["kind"] == "call" and any(
            isinstance(v, int) and not isinstance(v, bool)
            for v in row["target"]["arguments"].values()
        ):
            value = json.loads(gold)
            value["arguments"] = {
                k: (str(v) if isinstance(v, int) and not isinstance(v, bool) else v)
                for k, v in value["arguments"].items()
            }
            reply = json.dumps(value)
        scores.append(score(row, reply, TOOLS))
    result = mine(scores, {r["id"]: r for r in dev}, 12, 1, TOOLS)
    assert result["mined"] and len(result["control"]) == len(result["mined"])
    for row in result["mined"]:
        assert row["split"] == "train" and FAMILY_SPLIT[FAMILY_OF[row["tool"]]] == "train"
        assert set(row["lineage"]["requires"]) <= set(row["features"])
        assert "arg_type:integer" in row["features"]
    assert not contamination(result["mined"], dev)["shape"]


def test_rule_is_pre_registered_and_keeping_the_champion_is_an_outcome() -> None:
    assert hashlib.sha256(RULE_PATH.read_bytes()).hexdigest() == RULE_SHA256
    rule = load_rule()
    ids = [f"r{i}" for i in range(100)]

    def evals(test_exact: list[bool], sanity: float = 0.8) -> dict:
        rate = sum(test_exact) / len(test_exact)
        agg = {
            "exact_match_strict": rate,
            "json_valid_rate": 1.0,
            "schema_valid_rate": 0.9,
            "refusal_accuracy": 0.9,
        }
        return {"test": {"aggregate": agg, "ids": ids, "exact": test_exact}, "sanity": {"aggregate": {"accuracy": sanity}}, "fresh": {"aggregate": {"exact_match_strict": rate}}}  # fmt: skip

    good = {name: True for name in rule["promotion"]["integrity"]}
    champion = evals([i < 40 for i in range(100)])
    assert (
        decide(rule, evals([i < 41 for i in range(100)]), champion, champion, good)["outcome"]
        == "keep_champion"
    )
    better = evals([i < 60 for i in range(100)])
    assert decide(rule, better, champion, champion, good)["outcome"] == "promote_candidate"
    assert (
        decide(rule, evals([i < 60 for i in range(100)], 0.6), champion, champion, good)["outcome"]
        == "keep_champion"
    )
    assert (
        decide(rule, better, champion, champion, {**good, "no_contamination": False})["outcome"]
        == "keep_champion"
    )
    first = paired_bootstrap(better["test"]["exact"], champion["test"]["exact"], 500, "s", 0.95)
    assert first == paired_bootstrap(
        better["test"]["exact"], champion["test"]["exact"], 500, "s", 0.95
    )
    assert first["delta"] == 0.2 and first["lower"] > 0


@pytest.fixture(scope="module")
def loop_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    root = tmp_path_factory.mktemp("tuning")
    spec = json.loads((HERE / "learner_spec.json").read_bytes())
    backend = FixtureBackend(TOOLS, spec["data_plan"])
    # Rounds 0 to 4 a few minutes apart, a no-op, then one tick a day later: evaluation only.
    times = [1_900_000_000 + 1000 * i for i in range(6)] + [1_900_000_000 + 5000 + 90_000]
    ticks = [tick(root, BASE, lambda: backend, now=lambda t=t: float(t)) for t in times]
    return {"root": root, "ticks": ticks}


def test_loop_runs_rounds_as_claimed_verbs_with_execution_reports(loop_run: dict) -> None:
    ticks = loop_run["ticks"]
    assert [t["round"] for t in ticks] == [0, 1, 2, 3, 4, 4, 4]
    assert all(v["ok"] for t in ticks[:5] for v in t["verbs"])
    assert ticks[5]["verbs"][0]["status"] == "no_op" and len(ticks[5]["verbs"]) == 1
    first = [v["step"] for v in ticks[4]["verbs"][1:4]]
    assert first == ["generate_for_round", "accept_for_round", "training_sets"]
    monitor = [v["stage"] for v in ticks[6]["verbs"]]
    stages = ["tick", "generate_examples", "curate_dataset", "evaluate", "decide_promotion"]
    assert monitor == stages and all(v["ok"] for v in ticks[6]["verbs"])
    assert ticks[6]["verbs"][0]["summary"]["kind"] == "monitor"
    assert ticks[6]["pointer_version"] == ticks[5]["pointer_version"] + 1
    stages = {v["stage"] for t in ticks for v in t["verbs"]}
    assert stages == {"tick", "generate_examples", "curate_dataset", "tune_hyperparameters", "train_adapter", "evaluate", "decide_promotion", "mine_hard_examples"}  # fmt: skip
    store = Store(loop_run["root"] / "fabric.db")
    try:
        ids = json.loads((loop_run["root"] / "principals.json").read_text())
        with store.scope_context(namespace="conformance-tuning", key_id=ids["worker_key_id"]):
            grant = store.get_key(ids["worker_key_id"])
            for verb in ticks[1]["verbs"]:
                run = store.get_execution(verb["run_ids"][0], grant=grant)
                report = run["report"]
                assert run["receipt_hash"] == verb["receipt_hash"]
                assert report["summaries"]["problem"] and report["contract"]["output_schema"]
                assert "usage" in report and "model" in report
                output = store.results(verb["task_id"])[-1].output
                learning = output["learning"]
                assert (
                    learning["outcome"]
                    and learning["types"]["input"]
                    and learning["duration_seconds"] >= 0
                )
                model_verb = verb["stage"] in ("tune_hyperparameters", "train_adapter", "evaluate")
                assert (output["model"] is not None) == model_verb
    finally:
        store.close()


def test_other_principal_reads_no_private_noun_blob_or_execution(loop_run: dict) -> None:
    for item in loop_run["ticks"][:5]:
        privacy = item["privacy"]
        assert privacy["leaked"] == [] and privacy["denied"] == privacy["probes"] > 0
        assert (
            privacy["records"] and privacy["blobs"] and privacy["executions"] == len(item["verbs"])
        )


def test_decisions_pin_the_rule_and_training_sets_stay_clean(loop_run: dict) -> None:
    from examples.continuous_tuning.report import learning_curve

    curve = learning_curve(loop_run["root"], BASE)
    assert [r["round"] for r in curve["rounds"]] == [0, 1, 2, 3, 4]
    for item in curve["rounds"][1:]:
        assert not any(item["contamination"].values())
        assert item["outcome"] in ("promote_candidate", "keep_champion")
        assert all(c["passed"] is not False for c in item["checks"]) == (
            item["outcome"] == "promote_candidate"
        )
        assert item["integrity"] and all(item["integrity"].values())
    from examples.continuous_tuning.fabric import Fabric

    fabric = Fabric(loop_run["root"])
    try:
        with fabric.scope():
            history = fabric.store.record_history("tuning.models", f"champion.{curve['learner']}")
            initial = [
                ref["key"]
                for ref, v in fabric.list("data", "accepted_batch.")
                if v["origin"] == "initial"
            ]
    finally:
        fabric.close()
    for version in history:
        state = version.value
        if state["champion"]["kind"] == "adapter":
            # Every strategy trains on the initial rows or continues from a champion that did.
            assert set(initial) <= set(state["champion_training_batches"]), version.version


def test_pack_is_deterministic_offline_and_valid_against_tessera(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("building a seed pack performs no network, process or enqueue effect")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(Store, "enqueue", forbidden)
    pack = pack_module.build_pack()
    assert pack == pack_module.build_pack()
    files = pack_module.pack_files(pack)
    checked_in = HERE / "pack"
    for path, text in files.items():
        assert (checked_in / path).read_text() == text, f"{path} is stale: run the pack command"
    manifest = json.loads((checked_in / "manifest.json").read_text())
    assert manifest == json.loads(pack_module.dumps(pack_module.manifest(pack, files)))
    spec = importlib.util.spec_from_file_location("research_seed", ROOT / "tools/research_seed.py")
    assert spec and spec.loader
    research = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = research
    spec.loader.exec_module(research)
    # A supplied base adds its description and one tick verb seed.
    pack = pack_module.build_pack(bases=[BASE])
    assert len(pack["tasks"]) == 1
    keys = {record["key"]: record for record in pack["records"]}
    for record in pack["records"]:
        RecordWrite.model_validate(record)
        value = record["value"]
        result = validate_category_payload(
            value["category_id"], value["dimensions"], value["relations"], record["tags"]
        )
        assert result.valid and not result.errors and not result.warnings, record["key"]
        assert all(rel["target_id"] in keys for rel in value["relations"])
        assert value["activation"] == "none" and record["visibility_scope"] == "private"
    from examples.finetuning.data import digest

    for task in pack["tasks"]:
        TaskSpec.model_validate(task)
        assert (
            not task["brief"]["execution"]["server_fallback"]
            and task["effect_class"] == "write-internal"
        )
        for ref in task["brief"]["spec_refs"].values():
            research.NounRef.model_validate(ref)
            assert digest(keys[ref["key"]]["value"]) == ref["value_sha256"]


def test_pack_seed_runs_a_baseline_round_through_the_worker(tmp_path: Path) -> None:
    from examples.continuous_tuning import loop, verbs
    from examples.continuous_tuning.fabric import QUEUE, WORKER, Fabric
    from examples.finetuning.data import digest

    pack = pack_module.build_pack(bases=[BASE])
    spec = json.loads((HERE / "learner_spec.json").read_bytes())
    backend = FixtureBackend(TOOLS, spec["data_plan"])
    fabric = Fabric(tmp_path)
    try:
        with fabric.scope():
            written = fabric.store.put_records(
                [RecordWrite.model_validate(r) for r in pack["records"]]
            )
            for record, source in zip(written, pack["records"], strict=True):
                assert record.version == 1 and digest(record.value) == digest(source["value"])
            fabric.store.enqueue(TaskSpec.model_validate(pack["tasks"][0]))
            ctx = verbs.Context(
                fabric=fabric, tools=TOOLS, spec_refs={}, specs={}, root=tmp_path,
                backend=lambda: backend, now=lambda: 1.9e9, environment={}, toolchain={}, tick="seed",
            )  # fmt: skip
            ran = []
            while (
                lease := fabric.store.claim(QUEUE, WORKER, key_id=fabric.worker_key_id)
            ) is not None:
                ran.append(loop.run_verb(ctx, lease))
            stages = ["tick", "generate_examples", "curate_dataset", "evaluate", "decide_promotion"]
            assert [v["stage"] for v in ran] == stages and all(v["ok"] for v in ran)
            state, version = fabric.pointer(pack["tasks"][0]["brief"]["learner"])
            assert version == 1 and state is not None and state["champion"]["kind"] == "base"
    finally:
        fabric.close()


def test_example_modules_import_without_ml_libraries() -> None:
    script = (
        "import sys; import examples.continuous_tuning.loop, examples.continuous_tuning.pack, "
        "examples.continuous_tuning.backend_hf, examples.continuous_tuning.cli; "
        "assert all(name not in sys.modules for name in ['torch', 'transformers', 'peft'])"
    )
    subprocess.run([sys.executable, "-c", script], cwd=ROOT, check=True)
    project = (ROOT / "pyproject.toml").read_text()
    assert '"torch' not in project and '"peft' not in project and '"transformers' not in project


def test_completion_encoder_cuts_labels_at_end_of_turn() -> None:
    from examples.continuous_tuning.backend_hf import HFBackend

    class Tokenizer:
        def apply_chat_template(self, messages, tokenize, add_generation_prompt):
            ids = [1]
            for message in messages:
                ids += [5, *[ord(c) for c in message["content"]], 9]
            return [*ids, 5]  # Always appends an assistant header, like the Kaggle template.

        def decode(self, ids):
            return "".join(chr(i) for i in ids)

    backend = HFBackend.__new__(HFBackend)
    backend.tokenizer, backend.eos_ids, backend.template = Tokenizer(), [9], {}
    row = generate("train", 1, "enc", "x", TOOLS)[0]
    target = target_text(row, TOOLS)
    encoded = backend.encode(prompt_messages(row, TOOLS), target, 10**6)
    labels = [t for t in encoded["labels"] if t != -100]
    assert labels[-1] == 9 and "".join(chr(t) for t in labels[:-1]) == target
    with pytest.raises(ValueError, match="no truncation"):
        backend.encode(prompt_messages(row, TOOLS), target, 10)


def test_report_carries_the_learning_block_only_where_the_store_accepts_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.continuous_tuning import catalog

    from tessera.execution_matching import normalize_report

    learning = catalog.learning_block(
        outcome="succeeded", went_right=["ok"], went_wrong=[], next_steps=["next"],
        types=("in", "out"), method="evaluate", duration=1.5, advice=[],
    )  # fmt: skip
    common = {
        "problem": "p", "given": "g", "solution": "s", "learning": learning, "refs": [],
        "model": None, "usage": catalog.no_model_usage(), "context": {}, "environment": {}, "toolchain": {},
    }  # fmt: skip
    current = catalog.execution_report("evaluate", **common)
    assert normalize_report(current) == current  # accepted unchanged by this store
    supported = catalog.store_supports()
    assert ("learning" in current) == supported["learning"]
    if not supported["null_model"]:
        assert current["model"] == {
            "used": False,
            "reason": "deterministic verb; no model was invoked",
        }
    monkeypatch.setattr(catalog, "store_supports", lambda: {"learning": True, "null_model": True})
    future = catalog.execution_report("evaluate", **common)
    assert future["learning"] == learning and future["model"] is None
    assert learning["unknowns"][0]["field"] == "cost" and learning["duration_seconds"] == 1.5


def test_augmented_rows_rename_schemas_and_never_teach_served_names() -> None:
    from examples.continuous_tuning.augment import check_aliases, generate_augmented

    check_aliases(TOOLS)
    rows = generate_augmented(60, "aug-check", TOOLS)
    assert rows == generate_augmented(60, "aug-check", TOOLS)
    served = set(TOOLS) | {
        p for t in TOOLS.values() for p in t["inputSchema"].get("properties", {})
    }
    for row in rows:
        assert (
            set(row["offered"]) == set(row["tool_overrides"]) and not set(row["offered"]) & served
        )
        if row["tool"]:
            assert FAMILY_SPLIT[FAMILY_OF[row["tool"]]] == "train"
        target = row["target"]
        if "refusal" not in target:
            schema = row["tool_overrides"][target["tool"]]["inputSchema"]
            assert not validate(target["arguments"], schema)
            assert not set(target["arguments"]) & served
        prompt = prompt_messages(row, TOOLS)[0]["content"]
        assert all(f'"name":"{name}"' in prompt for name in row["offered"])
        expected = {"refusal": target["refusal"]} if "refusal" in target else target
        assert json.loads(target_text(row, TOOLS)) == expected
    dev = generate("dev", 80, "dev-a", "frozen_holdout", TOOLS)
    test = generate("test", 150, "test-a", "frozen_holdout", TOOLS)
    assert not any(contamination(rows, dev + test).values())


def test_a_continued_champion_inherits_its_parents_training_lineage() -> None:
    from examples.continuous_tuning.decide import training_lineage

    state = {"champion_training_batches": ["accepted_batch.initial.a", "accepted_batch.mined.b"]}
    new = {"sources": ["accepted_batch.stream.c", "replay:32"]}
    continued = training_lineage(state, new, {"init_adapter": {"key": "adapter.x"}})
    assert continued == [
        "accepted_batch.initial.a",
        "accepted_batch.mined.b",
        "accepted_batch.stream.c",
    ]
    assert training_lineage(state, new, {"init_adapter": None}) == ["accepted_batch.stream.c"]
