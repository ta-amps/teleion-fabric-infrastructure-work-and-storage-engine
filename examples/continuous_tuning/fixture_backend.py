"""A deterministic, dependency-free stand-in for the model backend (tests and CPU demos).

It exercises every noun, verb, receipt and decision path without torch. Its replies
are a hash-chosen mix of correct labels and the field failures (stringified objects,
quoted integers, missed refusals); the correct share grows with training rows. It is
a plumbing fixture: its numbers say nothing about any real model.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from examples.finetuning.data import digest, file_digest

from .sanity import sanity_rows
from .task import generate, target_text


def corrupt(gold: str) -> str:
    value = json.loads(gold)
    if "refusal" in value:
        return json.dumps({"tool": "whoami", "arguments": {}})
    for name, item in value["arguments"].items():
        if isinstance(item, (dict, list, int)) and not isinstance(item, bool):
            value["arguments"][name] = json.dumps(item)
            return json.dumps(value)
    return "```json\n" + gold + "\n```"


class FixtureBackend:
    name = "fixture"

    def __init__(self, tools: dict[str, Any], plan: dict[str, int]) -> None:
        self.answers: dict[str, str] = {}
        batches = [("dev", plan["dev"], "dev-frozen-v1"), ("test", plan["test"], "test-frozen-v1")]
        batches += [("test", plan["fresh_holdout"] * 3, f"test-fresh-r{r}") for r in range(1, 6)]
        for split, count, batch in batches:
            for row in generate(split, count, batch, "fixture", tools):
                self.answers[row["request"]] = target_text(row, tools)
        for row in sanity_rows(plan["sanity"]):
            self.answers[row["prompt"]] = row["answer"]

    def environment(self) -> dict[str, Any]:
        return {"versions": {"fixture": "1"}, "hardware": {"device": "cpu"}}

    def reset_peak(self) -> None:
        return None

    def peak(self) -> dict[str, None]:
        return {"peak_allocated_bytes": None, "peak_reserved_bytes": None}

    def load(self, adapter: Path | None = None) -> dict[str, Any]:
        if adapter is None:
            return {"rows": 0, "id": "base"}
        return {
            "rows": int((adapter / "rows.txt").read_text()),
            "id": file_digest(adapter / "rows.txt"),
        }

    def release(self, *models: Any) -> None:
        return None

    def generate(self, model, conversations, allowance, batch_size, checkpoint=None):
        replies = []
        for messages in conversations:
            text = messages[-1]["content"]
            gold = self.answers.get(text, "")
            roll = int(hashlib.sha256((text + model["id"]).encode()).hexdigest()[:8], 16) % 100
            quality = 25 + min(70, model["rows"] // 2)
            if roll < quality or not gold:
                reply = gold
            else:
                reply = gold if gold[:1] not in "{[" else corrupt(gold)
            replies.append({
                "text": reply,
                "prompt_tokens": sum(len(m["content"]) for m in messages) // 4,
                "output_tokens": len(reply) // 4 + 1,
                "reached_allowance": False,
                "seconds": 0.0,
            })  # fmt: skip
        return replies

    def train(self, examples, recipe, output, *, init_adapter=None, probes, checkpoint=None):
        if output.exists():
            raise ValueError("adapter output exists")
        output.mkdir(parents=True)
        prior = int((init_adapter / "rows.txt").read_text()) if init_adapter else 0
        (output / "rows.txt").write_text(str(prior + len(examples)))
        (output / "adapter_model.bin").write_bytes(digest([e[1] for e in examples]).encode())
        if checkpoint:
            checkpoint()
        files = {p.name: file_digest(p) for p in sorted(output.iterdir())}
        steps = max(1, len(examples) * recipe["epochs"] // recipe["gradient_accumulation"])
        return {
            "adapter_files": files,
            "adapter_sha256": digest(files),
            "init": "adapter" if init_adapter else "base",
            "rows": len(examples),
            "optimizer_steps": steps,
            "warmup_steps": 1,
            "losses": [1.0 / (i + 1) for i in range(steps)],
            "tokens": {"processed": len(examples) * 800, "label_tokens": len(examples) * 40},
            "sequence_tokens": {"min": 400, "max": 900},
            "trainable_parameters": 1,
            "total_parameters": 2,
            "frozen_parameters_unchanged": True,
            "frozen_parameters_sha256": "0" * 64,
            "adapter_parameters_changed": True,
            "reload": {"fresh_base": True, "equivalent": True, "max_logit_difference": 0.0, "tolerance": recipe["reload_logit_tolerance"], "greedy_probe_replies_identical": True, "probes": len(probes)},
            "training_seconds": 0.0,
            "memory": self.peak(),
        }  # fmt: skip
