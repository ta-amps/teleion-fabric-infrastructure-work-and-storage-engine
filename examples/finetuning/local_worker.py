"""Run a real tiny training verb through a private, task-owned local Tessera store."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tessera import QueueConfig, RecordWrite, Store, TaskSpec
from tessera.errors import Forbidden, NotFound

from .author import payloads
from .data import canonical, digest, load
from .peft_backend import recipe_from, run


def execute(dataset: Path, output: Path, recipe_path: Path) -> dict[str, Any]:
    recipe = recipe_from(recipe_path)
    if recipe["mode"] != "smoke":
        raise ValueError(
            "local fabric demo accepts the bounded random smoke only; real jobs use the owning harness"
        )
    plan = payloads(dataset, recipe_path)
    manifest, _ = load(dataset)
    output.mkdir(parents=True, exist_ok=False)
    namespace = queue = "conformance-finetune"
    store = Store(output / "fabric.db")
    _, grant = store.issue_key(
        "training-worker", queues=[queue], scopes=["store", "namespace:" + namespace]
    )
    _, outsider = store.issue_key(
        "other-worker", queues=[queue], scopes=["store", "namespace:" + namespace]
    )
    try:
        with store.scope_context(namespace=namespace, key_id=grant.key_id):
            store.define_queue(QueueConfig(name=queue))
            records = store.put_records(
                [RecordWrite.model_validate(value) for value in plan["records"]]
            )
            for record in records:
                original = next(value for value in plan["records"] if value["key"] == record.key)
                if record.version != 1 or digest(record.value) != digest(original["value"]):
                    raise ValueError("input readback differs")
            # Only the two actually implemented stages are enqueued; no pretend quality review.
            for value in plan["tasks"][:2]:
                store.enqueue(TaskSpec.model_validate(value))
            curated = store.claim(queue, "training-worker")
            if curated is None or curated.brief["stage"] != "curate_split":
                raise ValueError("curation claim missing")
            load(dataset)
            curate_receipt = store.submit(
                curated.task_id,
                curated.fencing_token,
                {
                    "dataset_sha256": manifest["dataset_sha256"],
                    "checks": "hashes and disjoint source/project families",
                },
                idempotency_key="curate:" + manifest["dataset_sha256"],
            )
            lease = store.claim(queue, "training-worker")
            if lease is None or lease.brief["stage"] != "train":
                raise ValueError("training claim missing")

            def checkpoint() -> None:
                store.heartbeat(lease.task_id, lease.fencing_token)

            report = run(dataset, output / "run", recipe, checkpoint=checkpoint)
            checkpoint()
            blobs = {}
            for directory in ("base", "adapter"):
                for path in sorted((output / "run" / directory).iterdir()):
                    if path.is_file():
                        raw = path.read_bytes()
                        sha = store.put_blob(raw, visibility_scope="private")
                        if store.get_blob(sha) != raw:
                            raise ValueError("saved artifact readback differs")
                        blobs[directory + "/" + path.name] = sha
            record = store.put_records(
                [
                    RecordWrite(
                        store="finetuning.candidates",
                        key="run." + digest(report),
                        value={
                            "category_id": "noun.eval_run",
                            "report": report,
                            "artifact_blobs": blobs,
                            "status": "worker_reported_plumbing_only",
                        },
                        expected_version=0,
                        visibility_scope="private",
                    )
                ]
            )[0]
            ref = {"store": record.store, "key": record.key, "version": record.version}
            receipt = store.submit(
                lease.task_id,
                lease.fencing_token,
                {
                    "artifact": ref,
                    "adapter_sha256": report["adapter_sha256"],
                    "reload_equivalent": report["reload"]["equivalent"],
                    "promotion": "none",
                },
                idempotency_key="train:" + report["adapter_sha256"],
                execution_report={
                    "version": 1,
                    "harness": {"name": "tessera-local-training-example"},
                    "model": report["base_model"],
                    "summaries": {
                        "problem": "Verify optional worker-side adapter plumbing",
                        "solution": "Real random tiny-decoder LoRA training, saved adapter and fresh-base reload; quality unmeasured",
                    },
                    "contract": {
                        "context_hash": manifest["dataset_sha256"],
                        "environment_hash": digest(report["versions"]),
                    },
                    "artifact_refs": [ref],
                },
            )
            executions = store.list_executions(lease.task_id, grant=grant)["executions"]
            if len(executions) != 1 or executions[0]["receipt_hash"] != receipt.receipt_hash:
                raise ValueError("execution capture differs from training receipt")
            captured = store.get_execution(executions[0]["run_id"], grant=grant)
            if captured["output"]["artifact"] != ref or not captured["ok"]:
                raise ValueError("execution artifact capture failed")
        denied = []
        with store.scope_context(namespace=namespace, key_id=outsider.key_id):
            for name, read in [
                ("record", lambda: store.get_record(record.store, record.key, record.version)),
                ("blob", lambda: store.get_blob(next(iter(blobs.values())))),
                ("execution", lambda: store.get_execution(executions[0]["run_id"], grant=outsider)),
            ]:
                try:
                    read()
                except (Forbidden, NotFound):
                    denied.append(name)
                else:
                    raise ValueError("other principal could access private training artifacts")
        result = {
            "status": "private_local_training_verb_pass",
            "store_backend": "in_process_sqlite",
            "device": report["hardware"]["device"],
            "pretrained_weights_used": False,
            "curation_receipt": curate_receipt.receipt_hash,
            "training_receipt": receipt.receipt_hash,
            "execution_run_id": executions[0]["run_id"],
            "artifact": ref,
            "saved_blob_count": len(blobs),
            "private_reads_denied": denied,
            "adapter_sha256": report["adapter_sha256"],
            "dataset_sha256": manifest["dataset_sha256"],
            "promotion": "none",
            "limitations": "Local owned store, one tiny random-model worker; not a GPU fleet, hosted API test, pretrained model result or quality benchmark.",
        }
        (output / "fabric-report.json").write_bytes(canonical(result) + b"\n")
        return result
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recipe", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(execute(args.dataset, args.output, args.recipe), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
