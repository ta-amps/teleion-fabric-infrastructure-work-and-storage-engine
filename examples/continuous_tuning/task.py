"""Deterministic typed tool-call examples: request, offered tools, label and features.

Every value comes from repository templates and a SHA-256 counter draw, so the same
batch name always yields the same bytes on any Python version. Labels are computed,
never model-written: the target is the template's arguments, canonicalized and checked
against the served schema, or a refusal whose reason the template fixes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any, TypeVar

from examples.finetuning.data import canonical

from .templates import FRAMES, SUBSTITUTES, TEMPLATES, UNSUPPORTED, VOCAB, Template
from .tools import (
    FAMILY_OF,
    FAMILY_SPLIT,
    canonical_arguments,
    json_type,
    render_tools,
    tools_in_split,
    validate,
)

GENERATOR_VERSION = 1
OFFERED = 5
REFUSALS = ("no_matching_tool", "missing_required_argument")
INSTRUCTION = (
    "You call tools for the Tessera work fabric. Reply with exactly one JSON object and "
    "nothing else: no prose and no code fences.\n"
    'To call a tool reply {"tool": "<name>", "arguments": {...}}.\n'
    "- Use only a tool listed below, with argument names from its inputSchema.\n"
    "- Give every value its schema type: objects and arrays as JSON objects and arrays "
    "(never a string that contains JSON), integers as numbers, booleans as true or false.\n"
    "- Include every required argument. Include an optional argument only when the "
    "request states its value.\n"
    'If no listed tool can do the request, reply {"refusal": "no_matching_tool"}.\n'
    "If the request leaves out a value that a required argument needs, reply "
    '{"refusal": "missing_required_argument"}.'
)
UNSUPPORTED_FAMILY = {
    "delete_record": "records",
    "rename_queue": "verb_discovery",
    "lease_time": "verb_discovery",
    "revoke_key": "access",
    "undo_write": "records",
    "delete_object": "buckets",
    "rename_bucket": "buckets",
    "text_digest": "feeds",
    "translate": "buckets",
    "merge_feeds": "feeds",
}
INT_RANGES = {
    "fencing_token": (1, 60, 1),
    "limit": (2, 60, 1),
    "after": (1, 500, 1),
    "version": (1, 12, 1),
    "priority": (10, 100, 10),
    "offset": (16, 4096, 16),
    "max_bytes": (64, 8192, 64),
    "max_chars": (500, 8000, 500),
    "park": (1, 5, 1),
    "seconds": (60, 3600, 60),
}
ARRAYS = {
    "kinds": ("kind", 2),
    "effects": ("effect", 1),
    "tags": ("tag", 2),
    "queues": ("queue", 2),
    "stores": ("store", 2),
}
X = TypeVar("X")


class Draw:
    """SHA-256 counter draws: identical on every Python version and platform."""

    def __init__(self, *parts: Any) -> None:
        self.seed = hashlib.sha256(canonical(list(parts))).digest()
        self.count = 0

    def _next(self) -> bytes:
        self.count += 1
        return hashlib.sha256(self.seed + self.count.to_bytes(8, "big")).digest()

    def below(self, n: int) -> int:
        return int.from_bytes(self._next()[:8], "big") % n

    def choice(self, items: Sequence[X]) -> X:
        return items[self.below(len(items))]

    def chance(self, probability: float) -> bool:
        return self.below(1_000_000) < int(probability * 1_000_000)

    def sample(self, items: Sequence[X], k: int) -> list[X]:
        pool, out = list(items), []
        for _ in range(min(k, len(pool))):
            out.append(pool.pop(self.below(len(pool))))
        return out

    def shuffle(self, items: Sequence[X]) -> list[X]:
        return self.sample(items, len(items))

    def hex(self, n: int) -> str:
        return self._next().hex()[:n]


def integer(name: str, d: Draw, prop: dict[str, Any]) -> int:
    lo, hi, step = INT_RANGES[name]
    lo, hi = max(lo, prop.get("minimum", lo)), min(hi, prop.get("maximum", hi))
    values = [v for v in range(lo, hi + 1, step) if v != prop.get("default")]
    return d.choice(values)


def objects(name: str, d: Draw) -> Any:
    if name == "brief":
        if d.chance(0.5):
            return {"goal": d.choice(VOCAB["goal"]), "max_items": d.below(20) + 1}
        return {"goal": d.choice(VOCAB["goal"]), "inputs": d.sample(VOCAB["key"], 2)}
    if name == "value":
        status = d.choice(("draft", "checked", "proposal"))
        return {"title": d.choice(VOCAB["title"]), "status": status, "score": d.below(101)}
    if name == "output":
        if d.chance(0.5):
            return {"summary": d.choice(VOCAB["goal"]), "count": d.below(50)}
        return {"status": "done", "items": d.below(30) + 1}
    if name == "report":
        return {"version": 1, "summaries": {"problem": d.choice(VOCAB["goal"])}}
    if name == "input":
        return {"approved": d.chance(0.5), "note": d.choice(VOCAB["note"])}
    if name == "children":
        goals = d.sample(VOCAB["goal"], 2)
        return [{"brief": {"goal": goal}} for goal in goals]
    if name == "writes":
        store, keys = d.choice(VOCAB["store"]), d.sample(VOCAB["key"], 2)
        status = ("draft", "checked")
        return [
            {"store": store, "key": k, "value": {"status": s}}
            for k, s in zip(keys, status, strict=True)
        ]
    if name == "payload":
        event = d.choice(VOCAB["event"])
        return {"event": event, "key": d.choice(VOCAB["key"]), "version": d.below(12) + 1}
    if name == "metadata":
        if d.chance(0.5):
            return {"source": "example", "rows": d.below(500) + 1}
        return {"lang": "en", "pages": d.below(40) + 1}
    raise KeyError(name)


OBJECT_SLOTS = {
    "brief",
    "value",
    "output",
    "report",
    "input",
    "children",
    "writes",
    "payload",
    "metadata",
}


def flat(value: Any) -> bool:
    items = value.values() if isinstance(value, dict) else value
    return all(not isinstance(item, (dict, list)) for item in items)


def described(value: Any) -> str:
    """A readable, non-JSON rendering; the model must still emit typed JSON."""
    if isinstance(value, dict):
        pairs = ", ".join(f"{key}: {json.dumps(item)}" for key, item in value.items())
        return f"({pairs})"
    words = [str(item) for item in value]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def slot(name: str, d: Draw, prop: dict[str, Any]) -> tuple[str, Any, bool]:
    """Returns (request text, argument value, rendered as a JSON literal)."""
    base = name.rstrip("0123456789")
    if base == "task_id":
        value: Any = "t_" + d.hex(8)
    elif base == "run_id":
        value = "run_" + d.hex(16)
    elif base == "idem":
        value = "submit-" + d.hex(6)
    elif base == "deps":
        value = ["t_" + d.hex(8), "t_" + d.hex(8)]
    elif base in INT_RANGES:
        value = integer(base, d, prop)
    elif base in ARRAYS:
        vocab, size = ARRAYS[base]
        value = d.sample(VOCAB[vocab], size)
    elif base in OBJECT_SLOTS:
        value = objects(base, d)
    else:
        value = d.choice(VOCAB[base])
    if isinstance(value, (dict, list)):
        literal = not flat(value) or d.chance(0.5)
        return (json.dumps(value) if literal else described(value)), value, literal
    return str(value), value, False


def offered(
    target: str | None, family: str | None, split: str, d: Draw, *, include: bool
) -> list[str]:
    pool = tools_in_split(split) + (tools_in_split("train") if split != "train" else [])
    excluded = {target, *SUBSTITUTES.get(target or "", ())}
    pool = [tool for tool in pool if tool not in excluded]
    same = [tool for tool in pool if family and FAMILY_OF[tool] == family]
    picks = d.sample(same, 2)
    picks += d.sample([t for t in pool if t not in picks], OFFERED - len(picks) - include)
    if include and target:
        picks.append(target)
    return d.shuffle(picks)


def frame(sentence: str, split: str, d: Draw) -> tuple[str, str]:
    index = d.below(len(FRAMES[split]))
    text = FRAMES[split][index].format(s=sentence, lc=sentence[0].lower() + sentence[1:])
    return text, f"{split}.{index}"


def fill(text: str, refs: dict[str, Any], d: Draw, tools: dict[str, Any], tool: str | None):
    props = tools[tool]["inputSchema"].get("properties", {}) if tool else {}
    slots: dict[str, tuple[str, Any, bool]] = {}
    for arg, ref in refs.items():
        if isinstance(ref, str) and ref.startswith("$") and ref[1:] not in slots:
            slots[ref[1:]] = slot(ref[1:], d, props.get(arg, {}))
    for name in _placeholders(text):
        if name not in slots:
            slots[name] = slot(name, d, {})
    args = {
        arg: slots[ref[1:]][1] if isinstance(ref, str) and ref.startswith("$") else ref
        for arg, ref in refs.items()
    }
    literal = any(item[2] for item in slots.values())
    return text.format(**{name: item[0] for name, item in slots.items()}), args, literal


def _placeholders(text: str) -> list[str]:
    import string

    return [name for _, name, _, _ in string.Formatter().parse(text) if name]


def build(template: Template, split: str, d: Draw, tools: dict[str, Any], *, mode: str) -> dict:
    """mode: call, missing (template omits a required value) or excluded (tool not offered)."""
    clauses = [c for c in template.optional if mode == "call" and d.chance(0.45)]
    refs = dict(template.args)
    for clause in clauses:
        refs.update(clause.args)
    text = template.text + "".join(clause.text for clause in clauses)
    sentence, args, literal = fill(text, refs, d, tools, template.tool)
    schema = tools[template.tool]["inputSchema"]
    if mode == "missing":
        if template.missing not in schema.get("required", []) or template.missing in args:
            raise ValueError(f"{template.tid}: a missing template must omit a required argument")
        target: dict[str, Any] = {"refusal": "missing_required_argument"}
    elif mode == "excluded":
        target = {"refusal": "no_matching_tool"}
    else:
        arguments = canonical_arguments(args, schema)
        errors = validate(arguments, schema)
        if errors:
            raise ValueError(
                f"{template.tid}: generated label violates the served schema: {errors}"
            )
        target = {"tool": template.tool, "arguments": arguments}
    family = FAMILY_OF[template.tool]
    tools_offered = offered(template.tool, family, split, d, include=mode != "excluded")
    request, frame_id = frame(sentence, split, d)
    return _row(
        split,
        template.tool,
        family,
        template.tid,
        frame_id,
        tools_offered,
        request,
        target,
        literal,
        tools,
    )


def build_unsupported(name: str, text: str, split: str, d: Draw, tools: dict[str, Any]) -> dict:
    sentence, _, literal = fill(text, {}, d, tools, None)
    # The hint only picks tempting distractors; an unsupported request belongs to no family.
    hint = UNSUPPORTED_FAMILY.get(name)
    tools_offered = offered(None, hint, split, d, include=False)
    request, frame_id = frame(sentence, split, d)
    target = {"refusal": "no_matching_tool"}
    return _row(
        split,
        None,
        None,
        f"unsupported.{name}",
        frame_id,
        tools_offered,
        request,
        target,
        literal,
        tools,
    )


def _row(split, tool, family, template, frame_id, tools_offered, request, target, literal, tools):
    row = {
        "split": split,
        "tool": tool,
        "family": family,
        "template": template,
        "frame": frame_id,
        "kind": "refusal" if "refusal" in target else "call",
        "offered": tools_offered,
        "request": request,
        "target": target,
        "generator_version": GENERATOR_VERSION,
    }
    row["features"] = features(row, tools, literal)
    return row


def features(row: dict[str, Any], tools: dict[str, Any], literal: bool) -> list[str]:
    found = {f"kind:{row['kind']}", f"family:{row['family']}", f"tool:{row['tool']}"}
    target = row["target"]
    if "refusal" in target:
        found.add(f"refusal:{target['refusal']}")
    else:
        schema = tools[target["tool"]]["inputSchema"]
        found.update(f"arg_type:{json_type(value)}" for value in target["arguments"].values())
        optional = set(target["arguments"]) - set(schema.get("required", []))
        found.add("optionals:some" if optional else "optionals:none")
    if literal:
        found.add("literal_json")
    if row["tool"] and any(
        FAMILY_OF[t] == row["family"] for t in row["offered"] if t != row["tool"]
    ):
        found.add("confusable")
    return sorted(found)


def split_templates(split: str) -> tuple[list[Template], list[Template]]:
    owned = [t for t in TEMPLATES if FAMILY_SPLIT[FAMILY_OF[t.tool]] == split]
    return [t for t in owned if t.missing is None], [t for t in owned if t.missing]


def generate(
    split: str,
    count: int,
    batch: str,
    origin: str,
    tools: dict[str, Any],
    *,
    refusal_every: int = 5,
) -> list[dict[str, Any]]:
    """Balanced batch: call templates cycle in a batch-specific order; every
    `refusal_every`-th row is a refusal (missing value, tool not offered, unsupported)."""
    calls, missing = split_templates(split)
    order = Draw(GENERATOR_VERSION, batch, "order").shuffle(calls)
    rows = []
    for index in range(count):
        d = Draw(GENERATOR_VERSION, batch, index)
        if index % refusal_every == refusal_every - 1:
            variant = (index // refusal_every) % 3
            if variant == 0 and missing:
                row = build(d.choice(missing), split, d, tools, mode="missing")
            elif variant == 1:
                row = build(d.choice(calls), split, d, tools, mode="excluded")
            else:
                name, text = d.choice(UNSUPPORTED[split])
                row = build_unsupported(name, text, split, d, tools)
        else:
            position = index - index // refusal_every
            row = build(order[position % len(order)], split, d, tools, mode="call")
        rows.append({"id": f"{batch}-{index:04d}", "batch": batch, "origin": origin, **row})
    return rows


def generate_targeted(
    requirements: list[list[str]], batch: str, origin: str, tools: dict[str, Any]
) -> list[dict[str, Any]]:
    """One new training row per requirement: train-family templates only, with every
    listed feature present (rejection sampling over seeded draws)."""
    calls, missing = split_templates("train")
    rows = []
    for index, required in enumerate(requirements):
        for attempt in range(400):
            d = Draw(GENERATOR_VERSION, batch, index, attempt)
            if "refusal:missing_required_argument" in required:
                row = build(d.choice(missing), "train", d, tools, mode="missing")
            elif "refusal:no_matching_tool" in required:
                if d.chance(0.5):
                    row = build(d.choice(calls), "train", d, tools, mode="excluded")
                else:
                    name, text = d.choice(UNSUPPORTED["train"])
                    row = build_unsupported(name, text, "train", d, tools)
            else:
                row = build(d.choice(calls), "train", d, tools, mode="call")
            if set(required) <= set(row["features"]):
                break
        else:
            raise ValueError(f"no training template satisfies {required}")
        rows.append({"id": f"{batch}-{index:04d}", "batch": batch, "origin": origin, **row})
    return rows


def prompt_messages(row: dict[str, Any], tools: dict[str, Any]) -> list[dict[str, str]]:
    # Augmented training rows carry renamed tool variants; evaluation rows never do.
    offered = {**tools, **row.get("tool_overrides", {})}
    system = INSTRUCTION + "\n\nAVAILABLE TOOLS:\n" + render_tools(row["offered"], offered)
    return [{"role": "system", "content": system}, {"role": "user", "content": row["request"]}]


def target_text(row: dict[str, Any], tools: dict[str, Any]) -> str:
    target = row["target"]
    if "refusal" in target:
        return json.dumps({"refusal": target["refusal"]})
    schema = {**tools, **row.get("tool_overrides", {})}[target["tool"]]["inputSchema"]
    order = list(schema.get("properties", {}))
    arguments = dict(sorted(target["arguments"].items(), key=lambda item: order.index(item[0])))
    return json.dumps({"tool": target["tool"], "arguments": arguments}, ensure_ascii=False)
