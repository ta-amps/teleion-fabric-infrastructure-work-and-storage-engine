"""Completion-only tokenization with explicit fit checks and no truncation."""

from __future__ import annotations

from typing import Any

from .data import messages


class ByteTokenizer:
    """Local smoke tokenizer; it is not a pretrained Meta tokenizer."""

    vocab_size = 259

    def apply_chat_template(
        self, conversation: list[dict[str, str]], *, tokenize: bool, add_generation_prompt: bool
    ) -> list[int]:
        if not tokenize:
            raise ValueError("smoke tokenizer returns token ids only")
        text = "".join(
            message["role"] + ":\n" + message["content"] + "\n" for message in conversation
        )
        if add_generation_prompt:
            text += "assistant:\n"
        result = [1, *[byte + 3 for byte in text.encode()]]
        if not add_generation_prompt:
            result.append(2)
        return result


def encode(row: dict[str, Any], tokenizer: Any, sequence_budget: int) -> dict[str, Any]:
    if type(sequence_budget) is not int or sequence_budget < 2:
        raise ValueError("sequence budget must be a positive explicit memory recipe")
    chat = messages(row)
    prompt = tokenizer.apply_chat_template(chat[:1], tokenize=True, add_generation_prompt=True)
    full = tokenizer.apply_chat_template(chat, tokenize=True, add_generation_prompt=False)
    if full[: len(prompt)] != prompt or len(full) <= len(prompt):
        raise ValueError(
            "chat template is not a completion-prefix format; choose an explicit adapter"
        )
    if len(full) > sequence_budget:
        raise ValueError(
            f"{row['id']}: {len(full)} tokens exceed sequence budget {sequence_budget}; no truncation performed"
        )
    return {
        "id": row["id"],
        "input_ids": full,
        "labels": [-100] * len(prompt) + full[len(prompt) :],
        "target_tokens": len(full) - len(prompt),
    }
