"""Behavioral checks for optional training data, fit rejection, and existing noun/verb contracts."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tessera import QueueConfig, RecordWrite, Store, TaskSpec

ROOT = Path(__file__).resolve().parents[1]
# Import the example without installing ML packages or adding a service dependency.
sys.path.insert(0, str(ROOT))
from examples.finetuning.author import payloads  # noqa: E402
from examples.finetuning.data import digest, example_rows, load, prepare, validate_rows  # noqa: E402
from examples.finetuning.encoding import ByteTokenizer, encode  # noqa: E402
from examples.finetuning.glimmer import MODEL, MODEL_REVISION, configure  # noqa: E402
from examples.finetuning.peft_backend import recipe_from  # noqa: E402

RECIPE = ROOT / "examples/finetuning/recipes/tiny-smoke.json"


def test_dataset_is_reproducible_and_families_do_not_overlap(tmp_path: Path) -> None:
    first, second = prepare(tmp_path / "a"), prepare(tmp_path / "b")
    assert first == second
    manifest, splits = load(tmp_path / "a")
    assert manifest["dataset_sha256"] == digest(example_rows())
    assert [len(splits[split]) for split in ("train", "dev", "test")] == [12, 4, 4]
    for field in ("source_family", "project_family"):
        sets = [{row[field] for row in rows} for rows in splits.values()]
        assert all(
            not left & right for index, left in enumerate(sets) for right in sets[index + 1 :]
        )
    assert not manifest["provenance"]["model_generated_training_data"]


@pytest.mark.parametrize("field", ["source_family", "project_family"])
def test_family_leakage_is_rejected(field: str) -> None:
    rows = example_rows()
    rows[-1][field] = rows[0][field]
    with pytest.raises(ValueError, match="family leaks"):
        validate_rows(rows)


def test_duplicate_identity_and_activated_target_are_rejected() -> None:
    rows = example_rows()
    with pytest.raises(ValueError, match="unique"):
        validate_rows([*rows, rows[0]])
    rows[0]["output"]["follow_up"]["activation"] = "active"
    with pytest.raises(ValueError, match="cannot activate"):
        validate_rows(rows)


def test_changed_data_or_split_manifest_is_rejected(tmp_path: Path) -> None:
    prepare(tmp_path / "dataset")
    dataset = tmp_path / "dataset"
    path = dataset / "train.jsonl"
    original = path.read_bytes()
    path.write_bytes(original.replace(b"source extraction", b"unreviewed text"))
    with pytest.raises(ValueError, match="fingerprint"):
        load(dataset)
    path.write_bytes(original)
    manifest_path = dataset / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["splits"]["test"]["source_families"] = ["publisher-00"]
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="family manifest"):
        load(dataset)


def test_completion_mask_preserves_full_targets_and_refuses_truncation() -> None:
    row = example_rows()[0]
    encoded = encode(row, ByteTokenizer(), 1536)
    ids, labels = encoded["input_ids"], encoded["labels"]
    assert len(ids) == len(labels)
    prompt = len(labels) - encoded["target_tokens"]
    assert prompt > 0 and labels[:prompt] == [-100] * prompt
    assert labels[prompt:] == ids[prompt:]
    assert 0 not in ids  # No pad tokens are trained or added.
    assert ids[-1] == 2  # Full answer and EOS retained.
    with pytest.raises(ValueError, match="no truncation performed"):
        encode(row, ByteTokenizer(), len(ids) - 1)
    assert encode(row, ByteTokenizer(), len(ids)) == encoded


def test_nonprefix_chat_template_is_rejected() -> None:
    class Incompatible:
        def apply_chat_template(self, _messages: object, **kwargs: object) -> list[int]:
            return [1] if kwargs["add_generation_prompt"] else [2, 3]

    with pytest.raises(ValueError, match="completion-prefix"):
        encode(example_rows()[0], Incompatible(), 1536)


def test_authoring_runs_without_importing_training_libraries(tmp_path: Path) -> None:
    prepare(tmp_path / "data")
    script = (
        "from examples.finetuning.author import payloads; import sys; "
        + f"payloads(__import__('pathlib').Path({str(tmp_path / 'data')!r}), __import__('pathlib').Path({str(RECIPE)!r})); "
        + "assert all(name not in sys.modules for name in ['torch', 'transformers', 'peft'])"
    )
    subprocess.run([sys.executable, "-c", script], cwd=ROOT, check=True)


def test_payloads_round_trip_existing_store_and_dependency_contracts(tmp_path: Path) -> None:
    prepare(tmp_path / "data")
    plan = payloads(tmp_path / "data", RECIPE)
    assert plan["dry_run"] and len(plan["records"]) == 3 and len(plan["tasks"]) == 4
    store = Store(tmp_path / "fabric.db")
    namespace = queue = "conformance-finetune"
    _, grant = store.issue_key(
        "example-worker", queues=[queue], scopes=["store", "namespace:" + namespace]
    )
    try:
        with store.scope_context(namespace=namespace, key_id=grant.key_id):
            store.define_queue(QueueConfig(name=queue))
            records = store.put_records(
                [RecordWrite.model_validate(value) for value in plan["records"]]
            )
            by_key = {record.key: record for record in records}
            for value in plan["tasks"]:
                task = TaskSpec.model_validate(value)
                for ref in task.brief["input_refs"]:
                    record = by_key[ref["key"]]
                    assert (
                        record.version == ref["version"]
                        and digest(record.value) == ref["value_sha256"]
                    )
                assert not task.brief["execution"]["server_fallback"]
                assert not task.brief["execution"]["automatic_promotion"]
                assert task.visibility_scope == "private"
                assert store.enqueue(task) == task.task_id
            claimed = store.claim(queue, "example-worker")
            assert claimed is not None and claimed.brief["stage"] == "curate_split"
            assert store.claim(queue, "other-example-worker") is None
    finally:
        store.close()


def test_wrong_report_cannot_attach_to_different_dataset(tmp_path: Path) -> None:
    prepare(tmp_path / "data")
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps({"format": "tessera.finetuning-run.v1", "dataset_sha256": "wrong"})
    )
    with pytest.raises(ValueError, match="does not match"):
        payloads(tmp_path / "data", RECIPE, report_path=report)


def test_pretrained_recipes_pin_weights_and_explicit_resource_allowance(tmp_path: Path) -> None:
    # A pretrained recipe names the model it trains. This one is made up, so nothing is downloaded.
    path = tmp_path / "pretrained.json"
    path.write_text(json.dumps({
        "version": 1, "mode": "pretrained", "model_id": "example-org/example-1b-instruct",
        "revision": "0" * 40, "license": "Example license; existing authorized access required",
        "quantization": "nf4", "device": "cuda", "sequence_budget": 1536, "steps": 8,
        "gradient_accumulation": 4, "learning_rate": 0.0002, "rank": 8, "seed": 17,
        "target_modules": ["q_proj", "v_proj"],
        "hardware_target": "One 16 GB GPU; verify actual peak memory locally, not a fit guarantee",
        "purpose": "Bounded example plumbing run; not a quality benchmark",
    }))  # fmt: skip
    recipe = recipe_from(path)
    assert recipe["quantization"] == "nf4" and recipe["device"] == "cuda"
    assert len(recipe["revision"]) == 40 and recipe["steps"] > 0
    assert recipe["sequence_budget"] > 0


def test_glimmer_bridge_keeps_test_separate_and_tracking_disabled(tmp_path: Path) -> None:
    upstream = {
        "base_model": MODEL,
        "model_revision": MODEL_REVISION,
        "adapter": "qlora",
        "load_in_4bit": True,
    }
    original = copy.deepcopy(upstream)
    config = configure(upstream, tmp_path / "data", tmp_path / "run", 4096, 2)
    assert config["test_datasets"][0]["path"].endswith("dev-messages.jsonl")
    assert "test-messages.jsonl" not in json.dumps(config)
    assert config["max_steps"] == 2 and not config["train_on_inputs"]
    assert not config["sample_packing"] and not config["pad_to_sequence_len"]
    assert config["wandb_mode"] == "disabled" and config["report_to"] == []
    assert upstream == original
    with pytest.raises(ValueError, match="model pin"):
        configure({**upstream, "model_revision": "main"}, tmp_path, tmp_path, 4096, 2)


def test_optional_smoke_dependency_boundary() -> None:
    # Merely discovering the backend never requires importing torch on a server/CI host.
    assert importlib.util.find_spec("examples.finetuning.peft_backend") is not None
    project = (ROOT / "pyproject.toml").read_text()
    assert '"torch' not in project and '"peft' not in project


def test_local_worker_captures_artifacts_and_denies_other_principal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from examples.finetuning import local_worker

    prepare(tmp_path / "data")

    def fixture_backend(dataset: Path, output: Path, recipe: dict, *, checkpoint: object) -> dict:
        # Exercise real fabric ownership/capture with fixture bytes; the separate ML smoke
        # is responsible for proving training and reload, not this dependency-free test.
        assert callable(checkpoint)
        checkpoint()
        assert dataset == tmp_path / "data" and recipe["mode"] == "smoke"
        for directory in ("base", "adapter"):
            (output / directory).mkdir(parents=True)
            (output / directory / "fixture.bin").write_bytes(directory.encode())
        return {
            "hardware": {"device": "cpu"},
            "base_model": {"id": "fixture"},
            "versions": {"backend": "fixture"},
            "adapter_sha256": "a" * 64,
            "reload": {"equivalent": True},
        }

    monkeypatch.setattr(local_worker, "run", fixture_backend)
    result = local_worker.execute(tmp_path / "data", tmp_path / "run", RECIPE)
    assert result["saved_blob_count"] == 2
    assert result["private_reads_denied"] == ["record", "blob", "execution"]
    assert result["promotion"] == "none" and result["training_receipt"]


def test_authoring_has_no_network_or_process_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    prepare(tmp_path / "data")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("offline authoring cannot perform external work")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(Store, "enqueue", forbidden)
    assert payloads(tmp_path / "data", RECIPE)["dry_run"]
