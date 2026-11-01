"""Optional torch/PEFT backend; never imported by Tessera's service or data authoring."""

from __future__ import annotations

import gc
import hashlib
import importlib.metadata
import json
import math
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .data import SPLITS, canonical, digest, file_digest, load
from .encoding import ByteTokenizer, encode


def parameter_digest(model: Any, *, trainable: bool) -> str:
    import torch

    fingerprint = hashlib.sha256()
    for name, parameter in model.named_parameters():
        if parameter.requires_grad is trainable:
            fingerprint.update(canonical([name, str(parameter.dtype), list(parameter.shape)]))
            fingerprint.update(
                parameter.detach().cpu().contiguous().view(torch.uint8).numpy().tobytes()
            )
    return fingerprint.hexdigest()


def recipe_from(path: Path) -> dict[str, Any]:
    return validate_recipe(json.loads(path.read_bytes()))


def validate_recipe(recipe: dict[str, Any]) -> dict[str, Any]:
    if recipe.get("version") != 1 or recipe.get("mode") not in {"smoke", "pretrained"}:
        raise ValueError("unsupported training recipe")
    for name in ("steps", "gradient_accumulation", "sequence_budget", "rank"):
        if type(recipe[name]) is not int or recipe[name] < 1:
            raise ValueError(f"{name} must be a positive explicit job allowance")
    if not math.isfinite(recipe["learning_rate"]) or recipe["learning_rate"] <= 0:
        raise ValueError("learning_rate must be finite and positive")
    if recipe["device"] not in {"cpu", "cuda"}:
        raise ValueError("select cpu or cuda explicitly")
    if recipe["mode"] == "pretrained":
        revision = recipe["revision"]
        if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
            raise ValueError("pretrained recipes require an immutable model revision")
        if recipe["device"] != "cuda" or recipe["quantization"] != "nf4":
            raise ValueError("this pretrained backend implements single-device CUDA QLoRA only")
    elif recipe["quantization"] != "none":
        raise ValueError("smoke uses a full-precision random base with LoRA")
    if type(recipe["seed"]) is not int or recipe["seed"] < 0:
        raise ValueError("seed must be a nonnegative integer")
    if not recipe["target_modules"] or not all(
        isinstance(name, str) and name for name in recipe["target_modules"]
    ):
        raise ValueError("explicit LoRA target modules are required")
    return recipe


def run(
    dataset: Path,
    output: Path,
    recipe: dict[str, Any],
    *,
    allow_download: bool = False,
    checkpoint: Callable[[], None] | None = None,
) -> dict[str, Any]:
    validate_recipe(recipe)
    # Importing this optional backend is cheap; torch is loaded only for an explicit worker run.
    import torch
    from peft import LoraConfig, PeftModel, get_peft_model, prepare_model_for_kbit_training
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        BitsAndBytesConfig,
        LlamaConfig,
        LlamaForCausalLM,
    )

    manifest, splits = load(dataset)
    if checkpoint:
        checkpoint()
    if output.exists():
        raise ValueError("output already exists; choose a new run directory")
    device = recipe["device"]
    if device == "cuda" and not torch.cuda.is_available():
        raise ValueError(
            "CUDA torch/device unavailable; choose CPU smoke or prepare a compatible worker"
        )
    output.mkdir(parents=True)
    started = time.perf_counter()
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    torch.manual_seed(recipe["seed"])
    torch.set_num_threads(2)
    if device == "cuda":
        torch.cuda.manual_seed_all(recipe["seed"])
        torch.cuda.reset_peak_memory_stats()
    smoke = recipe["mode"] == "smoke"
    dtype = (
        torch.float32
        if smoke
        else torch.bfloat16
        if torch.cuda.is_bf16_supported()
        else torch.float16
    )
    if smoke:
        tokenizer = ByteTokenizer()
        config = LlamaConfig(
            vocab_size=tokenizer.vocab_size,
            hidden_size=64,
            intermediate_size=128,
            num_hidden_layers=2,
            num_attention_heads=4,
            num_key_value_heads=2,
            max_position_embeddings=recipe["sequence_budget"],
            bos_token_id=1,
            eos_token_id=2,
            pad_token_id=0,
            attention_dropout=0.0,
            use_cache=False,
        )
        base = LlamaForCausalLM(config).to(device)
        base.save_pretrained(output / "base", safe_serialization=True)

        def fresh_base() -> Any:
            return AutoModelForCausalLM.from_pretrained(
                output / "base", local_files_only=True, dtype=torch.float32
            ).to(device)
    else:
        source = {
            "revision": recipe["revision"],
            "local_files_only": not allow_download,
            "trust_remote_code": False,
        }
        tokenizer = AutoTokenizer.from_pretrained(recipe["model_id"], **source)
        quantization = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=dtype,
        )

        def fresh_base() -> Any:
            loaded = AutoModelForCausalLM.from_pretrained(
                recipe["model_id"],
                **source,
                quantization_config=quantization,
                dtype=dtype,
                device_map={"": 0},
            )
            return prepare_model_for_kbit_training(loaded, use_gradient_checkpointing=True)

        base = fresh_base()
        if recipe["sequence_budget"] > base.config.max_position_embeddings:
            raise ValueError("sequence budget exceeds the pinned model context")
        tokenizer.save_pretrained(output / "tokenizer")
    encoded = {
        name: [encode(row, tokenizer, recipe["sequence_budget"]) for row in splits[name]]
        for name in SPLITS
    }
    if checkpoint:
        checkpoint()

    def batch(row: dict[str, Any]) -> dict[str, Any]:
        ids = torch.tensor([row["input_ids"]], device=device, dtype=torch.long)
        return {
            "input_ids": ids,
            "attention_mask": torch.ones_like(ids),
            "labels": torch.tensor([row["labels"]], device=device, dtype=torch.long),
        }

    def evaluate(model: Any, rows: list[dict[str, Any]]) -> tuple[float, Any]:
        model.eval()
        total, tokens, probe = 0.0, 0, None
        with torch.no_grad():
            for row in rows:
                result = model(**batch(row))
                total += float(result.loss) * row["target_tokens"]
                tokens += row["target_tokens"]
                if probe is None:
                    probe = result.logits[0, -1].float().cpu()
        value = total / tokens
        if not math.isfinite(value):
            raise ValueError("non-finite held-out loss")
        return value, probe

    before = {split: evaluate(base, encoded[split])[0] for split in ("dev", "test")}
    lora = LoraConfig(
        task_type="CAUSAL_LM",
        r=recipe["rank"],
        lora_alpha=recipe["rank"] * 2,
        lora_dropout=0.0,
        target_modules=recipe["target_modules"],
        bias="none",
    )
    model = get_peft_model(base, lora)
    frozen_before = parameter_digest(model, trainable=False)
    trainable_before = parameter_digest(model, trainable=True)
    trainable_count = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total_count = sum(p.numel() for p in model.parameters())
    optimizer = torch.optim.AdamW(
        [p for p in model.parameters() if p.requires_grad], lr=recipe["learning_rate"]
    )
    training_losses = []
    training_started = time.perf_counter()
    model.train()
    accumulation = recipe["gradient_accumulation"]
    for step in range(recipe["steps"]):
        optimizer.zero_grad(set_to_none=True)
        step_loss = 0.0
        for micro in range(accumulation):
            if checkpoint:
                checkpoint()
            row = encoded["train"][(step * accumulation + micro) % len(encoded["train"])]
            loss = model(**batch(row)).loss
            if not torch.isfinite(loss):
                raise ValueError("non-finite training loss")
            step_loss += float(loss.detach()) / accumulation
            (loss / accumulation).backward()
        norm = torch.nn.utils.clip_grad_norm_(
            [p for p in model.parameters() if p.requires_grad], 1.0, error_if_nonfinite=True
        )
        if not torch.isfinite(norm):
            raise ValueError("non-finite adapter gradient")
        optimizer.step()
        training_losses.append(step_loss)
    training_seconds = time.perf_counter() - training_started
    frozen_unchanged = frozen_before == parameter_digest(model, trainable=False)
    adapter_changed = trainable_before != parameter_digest(model, trainable=True)
    if not frozen_unchanged or not adapter_changed:
        raise ValueError("adapter update / frozen-base invariant failed")
    after = {split: evaluate(model, encoded[split])[0] for split in ("dev", "test")}
    _, expected_probe = evaluate(model, encoded["test"][:1])
    adapter_path = output / "adapter"
    if checkpoint:
        checkpoint()
    model.save_pretrained(adapter_path, safe_serialization=True)
    del model, base, optimizer, loss, norm
    gc.collect()
    if device == "cuda":
        torch.cuda.empty_cache()
    # Reload from a fresh frozen base, not the already adapted in-memory object.
    restored = PeftModel.from_pretrained(fresh_base(), adapter_path, is_trainable=False)
    reloaded = {split: evaluate(restored, encoded[split])[0] for split in ("dev", "test")}
    _, actual_probe = evaluate(restored, encoded["test"][:1])
    max_difference = float(torch.max(torch.abs(expected_probe - actual_probe)))
    tolerance = 1e-5 if smoke else 0.002
    equivalent = torch.allclose(
        expected_probe, actual_probe, rtol=tolerance, atol=tolerance
    ) and all(abs(after[s] - reloaded[s]) <= tolerance for s in after)
    if not equivalent:
        raise ValueError("saved adapter failed fresh-base reload equivalence")
    if checkpoint:
        checkpoint()
    adapter_files = {
        path.name: file_digest(path) for path in sorted(adapter_path.iterdir()) if path.is_file()
    }
    hardware: dict[str, Any] = {
        "device": device,
        "compute_dtype": str(dtype),
        "gpu": None,
        "total_vram_bytes": None,
        "peak_allocated_bytes": None,
        "peak_reserved_bytes": None,
    }
    if device == "cuda":
        properties = torch.cuda.get_device_properties(0)
        hardware.update(
            gpu=properties.name,
            total_vram_bytes=properties.total_memory,
            peak_allocated_bytes=torch.cuda.max_memory_allocated(),
            peak_reserved_bytes=torch.cuda.max_memory_reserved(),
        )
    report = {
        "format": "tessera.finetuning-run.v1",
        "status": "worker_reported_plumbing_pass",
        "mode": recipe["mode"],
        "pretrained_weights_used": not smoke,
        "base_model": {
            "id": recipe["model_id"],
            "revision": recipe["revision"],
            "random_initialization": smoke,
        },
        "dataset_sha256": manifest["dataset_sha256"],
        "manifest_sha256": file_digest(dataset / "manifest.json"),
        "recipe": recipe,
        "recipe_sha256": digest(recipe),
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("torch", "transformers", "peft", "accelerate")
        },
        "implementation_files_sha256": {
            name: file_digest(Path(__file__).with_name(name))
            for name in ("data.py", "encoding.py", "peft_backend.py", "run.py")
        },
        "hardware": hardware,
        "trainable_parameters": trainable_count,
        "total_parameters": total_count,
        "frozen_parameters_unchanged": frozen_unchanged,
        "frozen_parameters_sha256": frozen_before,
        "adapter_parameters_changed": adapter_changed,
        "base_files_sha256": {
            path.name: file_digest(path)
            for path in sorted((output / "base").iterdir())
            if path.is_file()
        }
        if smoke
        else None,
        "sequence_lengths": {
            split: {
                "min": min(len(row["input_ids"]) for row in rows),
                "max": max(len(row["input_ids"]) for row in rows),
                "target_tokens": sum(row["target_tokens"] for row in rows),
            }
            for split, rows in encoded.items()
        },
        "padding": "none; one sequence per microbatch; prompt labels masked with -100",
        "truncated_examples": 0,
        "loss": {
            "baseline": before,
            "adapter": after,
            "reloaded_adapter": reloaded,
            "training_steps": training_losses,
            "interpretation": "Teacher-forced held-out loss is a plumbing diagnostic, not factual grounding or useful task quality.",
        },
        "reload": {
            "fresh_base": True,
            "equivalent": bool(equivalent),
            "maximum_probe_logit_difference": max_difference,
            "tolerance": tolerance,
            "probe": "last-token logits of first held-out example plus all held-out losses",
        },
        "adapter_files": adapter_files,
        "adapter_sha256": digest(adapter_files),
        "training_seconds": training_seconds,
        "elapsed_seconds": time.perf_counter() - started,
        "quality_evaluation": {
            "factual_grounding": None,
            "useful_task_decomposition": None,
            "reason": "Not measured by this example plumbing example; a reviewed held-out generation benchmark is required.",
        },
        "promotion": "none; retain unchanged baseline until separate review and authorized admission",
    }
    (output / "report.json").write_bytes(canonical(report) + b"\n")
    return report
