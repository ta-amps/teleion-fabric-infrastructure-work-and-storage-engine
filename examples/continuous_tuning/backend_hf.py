"""Optional torch/Transformers/PEFT worker backend; never imported by Tessera's service.

One instance holds one pinned base-model noun. Training keeps the bf16 base frozen
(checked by parameter digest) and saves a LoRA adapter, then proves the saved adapter
reloads onto a fresh base with the same greedy replies and near-identical logits.
Generation is greedy with a recorded decode allowance; replies that reach it are
flagged, never silently cut.
"""

from __future__ import annotations

import gc
import importlib.metadata
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from examples.finetuning.data import digest, file_digest
from examples.finetuning.peft_backend import parameter_digest

from .task import Draw

Checkpoint = Callable[[], None] | None
SMALL_FILES = ("config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json")


class HFBackend:
    name = "transformers-peft-lora"

    def __init__(self, base: dict[str, Any], path: Path, *, device: str = "cuda") -> None:
        import torch
        from transformers import AutoTokenizer

        self.torch, self.base, self.path, self.device = torch, base, path, device
        if device == "cuda" and not torch.cuda.is_available():
            raise ValueError("CUDA is unavailable; prepare a compatible worker environment")
        for name in SMALL_FILES:
            if file_digest(path / name) != base["files_sha256"][name]:
                raise ValueError(f"{name} differs from the pinned base-model noun")
        self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
        self.tokenizer.padding_side = "left"
        self.pad_id = self.tokenizer.convert_tokens_to_ids(base["pad_token"])
        self.eos_ids = list(base["eos_token_ids"])
        self.template = dict(base.get("chat_template_kwargs", {}))
        self.dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        self.weights_verified = False

    def verify_weights(self) -> bool:
        names = self.base.get("weights", ["model.safetensors"])
        return all(file_digest(self.path / n) == self.base["files_sha256"][n] for n in names)

    def environment(self) -> dict[str, Any]:
        torch = self.torch
        names = ("torch", "transformers", "peft", "accelerate", "safetensors", "tokenizers")
        hardware: dict[str, Any] = {"device": self.device, "compute_dtype": str(self.dtype)}
        if self.device == "cuda":
            props = torch.cuda.get_device_properties(0)
            hardware.update(gpu=props.name, total_vram_bytes=props.total_memory)
        return {
            "versions": {name: importlib.metadata.version(name) for name in names},
            "cuda": torch.version.cuda,
            "hardware": hardware,
            "weights_verified": self.weights_verified,
        }

    def reset_peak(self) -> None:
        if self.device == "cuda":
            self.torch.cuda.reset_peak_memory_stats()

    def peak(self) -> dict[str, int | None]:
        if self.device != "cuda":
            return {"peak_allocated_bytes": None, "peak_reserved_bytes": None}
        return {
            "peak_allocated_bytes": self.torch.cuda.max_memory_allocated(),
            "peak_reserved_bytes": self.torch.cuda.max_memory_reserved(),
        }

    def _ids(self, messages: list[dict[str, str]], generation: bool) -> list[int]:
        rendered = self.tokenizer.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=generation, **self.template
        )
        return list(rendered["input_ids"] if hasattr(rendered, "keys") else rendered)

    def prompt_ids(self, messages: list[dict[str, str]]) -> list[int]:
        return self._ids(messages, True)

    def encode(self, messages: list[dict[str, str]], target: str, budget: int) -> dict[str, Any]:
        """Completion-only labels; the label ends at the first end-of-turn token."""
        prompt = self.prompt_ids(messages)
        full = self._ids([*messages, {"role": "assistant", "content": target}], False)
        if full[: len(prompt)] != prompt:
            raise ValueError("chat template is not a completion-prefix format")
        rest = full[len(prompt) :]
        stop = next((i for i, token in enumerate(rest) if token in self.eos_ids), None)
        if stop is None or self.tokenizer.decode(rest[:stop]) != target:
            raise ValueError("label tokens do not round-trip to the label text")
        labels = rest[: stop + 1]
        ids = prompt + labels
        if len(ids) > budget:
            raise ValueError(f"{len(ids)} tokens exceed sequence budget {budget}; no truncation")
        return {"input_ids": ids, "labels": [-100] * len(prompt) + labels, "prompt": len(prompt)}

    def load(self, adapter: Path | None = None, *, trainable: bool = False) -> Any:
        from peft import PeftModel
        from transformers import AutoModelForCausalLM

        if not self.weights_verified:
            # Once per worker process: the bytes must be the ones the base-model noun pins.
            if not self.verify_weights():
                raise ValueError("base weights differ from the pinned base-model noun")
            self.weights_verified = True
        model = AutoModelForCausalLM.from_pretrained(
            self.path, local_files_only=True, dtype=self.dtype
        ).to(self.device)
        if adapter is not None:
            model = PeftModel.from_pretrained(model, adapter, is_trainable=trainable)
        model.eval()
        return model

    def release(self, *models: Any) -> None:
        del models
        gc.collect()
        if self.device == "cuda":
            self.torch.cuda.empty_cache()

    def generate(
        self,
        model: Any,
        conversations: list[list[dict[str, str]]],
        allowance: int,
        batch_size: int,
        checkpoint: Checkpoint = None,
    ) -> list[dict[str, Any]]:
        """Greedy replies in a fixed length-sorted batching, so every arm sees the same batches."""
        torch = self.torch
        prompts = [self.prompt_ids(messages) for messages in conversations]
        order = sorted(range(len(prompts)), key=lambda i: (len(prompts[i]), i))
        replies: list[dict[str, Any] | None] = [None] * len(prompts)
        for start in range(0, len(order), batch_size):
            if checkpoint:
                checkpoint()
            chunk = order[start : start + batch_size]
            width = max(len(prompts[i]) for i in chunk)
            ids = [[self.pad_id] * (width - len(prompts[i])) + prompts[i] for i in chunk]
            mask = [[0] * (width - len(prompts[i])) + [1] * len(prompts[i]) for i in chunk]
            began = time.perf_counter()
            with torch.no_grad():
                out = model.generate(
                    input_ids=torch.tensor(ids, device=self.device),
                    attention_mask=torch.tensor(mask, device=self.device),
                    max_new_tokens=allowance,
                    do_sample=False,
                    temperature=None,
                    top_p=None,
                    top_k=None,
                    eos_token_id=self.eos_ids,
                    pad_token_id=self.pad_id,
                )
            seconds = (time.perf_counter() - began) / len(chunk)
            for row, index in zip(out[:, width:].tolist(), chunk, strict=True):
                stop = next((j for j, token in enumerate(row) if token in self.eos_ids), None)
                tokens = row if stop is None else row[:stop]
                replies[index] = {
                    "text": self.tokenizer.decode(tokens),
                    "prompt_tokens": len(prompts[index]),
                    "output_tokens": len(tokens) + (0 if stop is None else 1),
                    "reached_allowance": stop is None,
                    "seconds": round(seconds, 4),
                }
        return [reply for reply in replies if reply is not None]

    def last_logits(self, model: Any, conversations: list[list[dict[str, str]]]) -> Any:
        torch = self.torch
        rows = []
        with torch.no_grad():
            for messages in conversations:
                ids = torch.tensor([self.prompt_ids(messages)], device=self.device)
                rows.append(model(input_ids=ids).logits[0, -1].float().cpu())
        return torch.stack(rows)

    def train(
        self,
        examples: list[tuple[list[dict[str, str]], str]],
        recipe: dict[str, Any],
        output: Path,
        *,
        init_adapter: Path | None = None,
        probes: list[list[dict[str, str]]],
        checkpoint: Checkpoint = None,
    ) -> dict[str, Any]:
        torch = self.torch
        from peft import LoraConfig, get_peft_model

        if output.exists():
            raise ValueError("adapter output exists; every run writes a new directory")
        torch.manual_seed(recipe["seed"])
        if self.device == "cuda":
            torch.cuda.manual_seed_all(recipe["seed"])
        self.reset_peak()
        began = time.perf_counter()
        encoded = [self.encode(m, t, recipe["sequence_budget"]) for m, t in examples]
        if init_adapter is not None:
            model = self.load(init_adapter, trainable=True)
        else:
            model = get_peft_model(
                self.load(),
                LoraConfig(
                    task_type="CAUSAL_LM",
                    r=recipe["rank"],
                    lora_alpha=recipe["alpha"],
                    lora_dropout=recipe["dropout"],
                    target_modules=list(self.base["lora_target_modules"]),
                    bias="none",
                ),
            )
        if recipe.get("gradient_checkpointing"):
            model.gradient_checkpointing_enable()
            model.enable_input_require_grads()
        frozen_before = parameter_digest(model, trainable=False)
        adapter_before = parameter_digest(model, trainable=True)
        params = [p for p in model.parameters() if p.requires_grad]
        optimizer = torch.optim.AdamW(
            params, lr=recipe["learning_rate"], weight_decay=recipe["weight_decay"]
        )
        accumulation = recipe["gradient_accumulation"]
        micro_total = len(encoded) * recipe["epochs"]
        steps = math.ceil(micro_total / accumulation)
        warmup = max(1, math.ceil(steps * recipe["warmup_fraction"]))

        def factor(step: int) -> float:
            if step < warmup:
                return (step + 1) / warmup
            return max(0.0, (steps - step) / max(1, steps - warmup))

        scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, factor)
        sequence = [
            index
            for epoch in range(recipe["epochs"])
            for index in Draw("train-order", recipe["seed"], epoch).shuffle(range(len(encoded)))
        ]
        losses, tokens_seen, targets_seen = [], 0, 0
        model.train()
        for step in range(steps):
            optimizer.zero_grad(set_to_none=True)
            micro = sequence[step * accumulation : (step + 1) * accumulation]
            step_loss = 0.0
            for index in micro:
                row = encoded[index]
                ids = torch.tensor([row["input_ids"]], device=self.device)
                labels = torch.tensor([row["labels"]], device=self.device)
                loss = model(input_ids=ids, attention_mask=torch.ones_like(ids), labels=labels).loss
                if not torch.isfinite(loss):
                    raise ValueError("non-finite training loss")
                (loss / len(micro)).backward()
                step_loss += float(loss.detach()) / len(micro)
                tokens_seen += len(row["input_ids"])
                targets_seen += len(row["input_ids"]) - row["prompt"]
            torch.nn.utils.clip_grad_norm_(params, recipe["max_grad_norm"], error_if_nonfinite=True)
            optimizer.step()
            scheduler.step()
            losses.append(round(step_loss, 6))
            if checkpoint and step % 4 == 0:
                checkpoint()
        training_seconds = time.perf_counter() - began
        model.eval()
        frozen_unchanged = frozen_before == parameter_digest(model, trainable=False)
        adapter_changed = adapter_before != parameter_digest(model, trainable=True)
        if not frozen_unchanged or not adapter_changed:
            raise ValueError("frozen-base or adapter-update invariant failed")
        expected_logits = self.last_logits(model, probes)
        expected_text = [r["text"] for r in self.generate(model, probes, 96, len(probes))]
        model.save_pretrained(output, safe_serialization=True)
        trainable = sum(p.numel() for p in params)
        total = sum(p.numel() for p in model.parameters())
        memory = self.peak()
        # Drop every reference before loading a fresh base, or a 3B run holds two copies.
        del model, optimizer, scheduler, params, loss
        self.release()
        if checkpoint:
            checkpoint()
        restored = self.load(output)
        actual_logits = self.last_logits(restored, probes)
        actual_text = [r["text"] for r in self.generate(restored, probes, 96, len(probes))]
        self.release(restored)
        difference = float((expected_logits - actual_logits).abs().max())
        equivalent = difference <= recipe["reload_logit_tolerance"] and expected_text == actual_text
        if not equivalent:
            raise ValueError("saved adapter failed fresh-base reload equivalence")
        files = {p.name: file_digest(p) for p in sorted(output.iterdir()) if p.is_file()}
        return {
            "adapter_files": files,
            "adapter_sha256": digest(files),
            "init": "adapter" if init_adapter else "base",
            "rows": len(encoded),
            "optimizer_steps": steps,
            "warmup_steps": warmup,
            "losses": losses,
            "tokens": {"processed": tokens_seen, "label_tokens": targets_seen},
            "sequence_tokens": {
                "min": min(len(r["input_ids"]) for r in encoded),
                "max": max(len(r["input_ids"]) for r in encoded),
            },
            "trainable_parameters": trainable,
            "total_parameters": total,
            "frozen_parameters_unchanged": frozen_unchanged,
            "frozen_parameters_sha256": frozen_before,
            "adapter_parameters_changed": adapter_changed,
            "reload": {
                "fresh_base": True,
                "equivalent": equivalent,
                "max_logit_difference": difference,
                "tolerance": recipe["reload_logit_tolerance"],
                "greedy_probe_replies_identical": expected_text == actual_text,
                "probes": len(probes),
            },
            "training_seconds": round(training_seconds, 3),
            "memory": memory,
        }
