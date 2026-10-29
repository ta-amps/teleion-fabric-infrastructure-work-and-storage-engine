"""A small general-capability sanity set with computed answers (no tools, no system prompt).

It detects an adapter that answers everything with tool-call JSON or loses simple
instruction following. Prompts and answers come from templates and arithmetic.
"""

from __future__ import annotations

import json
from typing import Any

from .task import Draw

SANITY_VERSION = 1
KINDS = ("add", "subtract", "multiply", "upper", "larger", "repeat", "next_letter", "count_items")
WORDS = ("river", "lantern", "maple", "copper", "harbor", "meadow", "pebble", "orchard")
PHRASES = (
    "blue kites over the bay",
    "seven quiet lanterns",
    "the bridge opens at noon",
    "fresh bread on the shelf",
)
ITEMS = ("apple", "pear", "plum", "fig", "lime", "kiwi", "date", "grape")


def sanity_rows(count: int = 40, batch: str = "sanity-v1") -> list[dict[str, Any]]:
    rows = []
    for index in range(count):
        d = Draw("sanity", SANITY_VERSION, batch, index)
        kind = KINDS[index % len(KINDS)]
        if kind == "add":
            a, b = d.below(90) + 10, d.below(90) + 10
            prompt, answer = f"What is {a} + {b}? Reply with only the number.", str(a + b)
        elif kind == "subtract":
            a, b = d.below(50) + 50, d.below(40) + 10
            prompt, answer = f"What is {a} - {b}? Reply with only the number.", str(a - b)
        elif kind == "multiply":
            a, b = d.below(11) + 2, d.below(11) + 2
            prompt, answer = f"What is {a} times {b}? Reply with only the number.", str(a * b)
        elif kind == "upper":
            word = d.choice(WORDS)
            prompt = f'Write the word "{word}" in capital letters. Reply with only that word.'
            answer = word.upper()
        elif kind == "larger":
            a, b = d.sample(range(10, 1000), 2)
            prompt = f"Which is larger, {a} or {b}? Reply with only the number."
            answer = str(max(a, b))
        elif kind == "repeat":
            phrase = d.choice(PHRASES)
            prompt = f"Repeat this phrase exactly, with nothing else: {phrase}"
            answer = phrase
        elif kind == "next_letter":
            letter = chr(ord("b") + d.below(22))
            prompt = (
                f"Which letter comes right after {letter} in the English alphabet? "
                "Reply with only the letter."
            )
            answer = chr(ord(letter) + 1)
        else:
            items = d.sample(ITEMS, d.below(5) + 3)
            listed = ", ".join(items)
            prompt = f"How many items are in this list: {listed}? Reply with only the number."
            answer = str(len(items))
        rows.append(
            {
                "id": f"{batch}-{index:04d}",
                "batch": batch,
                "split": "sanity",
                "origin": "sanity",
                "kind": kind,
                "prompt": prompt,
                "answer": answer,
            }
        )
    return rows


def sanity_messages(row: dict[str, Any]) -> list[dict[str, str]]:
    return [{"role": "user", "content": row["prompt"]}]


def score_sanity(row: dict[str, Any], output: str) -> dict[str, Any]:
    text = output.strip().strip('"').strip("'").strip()
    text = text[:-1] if text.endswith(".") else text
    exact_case = row["kind"] in ("upper", "repeat")
    correct = text == row["answer"] if exact_case else text.lower() == row["answer"].lower()
    try:
        parsed = json.loads(output.strip())
    except ValueError:
        parsed = None
    intrusion = isinstance(parsed, dict) and bool({"tool", "refusal"} & set(parsed))
    return {"id": row["id"], "kind": row["kind"], "correct": correct, "tool_json": intrusion}
