"""Prepare, but do not launch, a pinned official Glimmer QLoRA recipe on an H100 worker."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from .data import SPLITS, canonical, file_digest, load
from .encoding import encode

COOKBOOK_REVISION = "b30ca2ce0c4abeb2a0aa52a4f2610a550d5d7447"
CONFIG = "muse-glimmer/post-training/sft/axolotl/configs/muse-glimmer-30b-qlora.yaml"
CONFIG_SHA256 = "53dc0d99396c36979b7dd14ffdee1fce28b339ada81058747de78e892631e09c"
MODEL = "meta-models/Muse-Glimmer-30B"
MODEL_REVISION = "a4e59da52a7bc87ae7251dd5545c0dd437c44b68"


def configure(
    upstream: dict[str, Any], dataset: Path, output: Path, sequence_budget: int, steps: int
) -> dict[str, Any]:
    if sequence_budget < 1 or steps < 1:
        raise ValueError("explicit positive sequence and step allowances required")
    if upstream.get("base_model") != MODEL or upstream.get("model_revision") != MODEL_REVISION:
        raise ValueError("official model pin differs")
    config = dict(upstream)
    for field, split in [("datasets", "train"), ("test_datasets", "dev")]:
        config[field] = [
            {
                "path": str((dataset / (split + "-messages.jsonl")).resolve()),
                "type": "chat_template",
                "field_messages": "messages",
                "roles_to_train": ["assistant"],
                "train_on_eos": "turn",
                **({"split": "train"} if field == "test_datasets" else {}),
            }
        ]
    config.update(
        output_dir=str((output / "checkpoints").resolve()),
        dataset_prepared_path=str((output / "cache").resolve()),
        sequence_len=sequence_budget,
        max_steps=steps,
        micro_batch_size=1,
        gradient_accumulation_steps=1,
        sample_packing=False,
        eval_sample_packing=False,
        train_on_inputs=False,
        pad_to_sequence_len=False,
        wandb_project=None,
        wandb_mode="disabled",
        report_to=[],
        eval_strategy="steps",
        eval_steps=steps,
        save_strategy="steps",
        save_steps=steps,
    )
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cookbook",
        type=Path,
        required=True,
        help="Existing official cookbook checkout at the pinned revision",
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sequence-budget", type=int, required=True)
    parser.add_argument("--steps", type=int, required=True)
    args = parser.parse_args(argv)
    # These dependencies belong only in the official dedicated Axolotl environment.
    import yaml
    from transformers import AutoTokenizer

    revision = subprocess.run(
        ["git", "-C", str(args.cookbook), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source = args.cookbook / CONFIG
    if revision != COOKBOOK_REVISION or file_digest(source) != CONFIG_SHA256:
        raise ValueError("cookbook revision or QLoRA config differs from the reviewed pin")
    manifest, splits = load(args.dataset)
    tokenizer = AutoTokenizer.from_pretrained(
        MODEL, revision=MODEL_REVISION, local_files_only=True, trust_remote_code=False
    )
    # The actual cached tokenizer must fit every complete example before writing a runnable config.
    lengths = {
        split: [
            len(encode(row, tokenizer, args.sequence_budget)["input_ids"]) for row in splits[split]
        ]
        for split in SPLITS
    }
    config = configure(
        yaml.safe_load(source.read_text()),
        args.dataset,
        args.output,
        args.sequence_budget,
        args.steps,
    )
    args.output.mkdir(parents=True, exist_ok=False)
    path = args.output / "qlora.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    receipt = {
        "status": "configuration_prepared_not_trained",
        "cookbook_revision": revision,
        "source_config_sha256": CONFIG_SHA256,
        "generated_config_sha256": file_digest(path),
        "dataset_sha256": manifest["dataset_sha256"],
        "sequence_lengths": lengths,
        "truncated_examples": 0,
        "command": ["axolotl", "train", str(path.resolve())],
        "remaining_checks": [
            "Authorized H100 worker and pinned official Axolotl dependencies",
            "Actual GPU peak memory, full baseline/adapter held-out evaluation and fresh-base reload",
            "Independent review before model admission; no automatic promotion",
        ],
    }
    (args.output / "manifest.json").write_bytes(canonical(receipt) + b"\n")
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
