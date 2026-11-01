"""Deterministic verbs: generate examples, curate datasets and mine hard examples.

Each handler runs inside one claimed Tessera task, reads pinned nouns, writes new
create-only private nouns and returns a Result that becomes the submitted output and
execution report. No handler calls a model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .augment import generate_augmented
from .dataset import accept, contamination, fingerprint_sets, jsonl, manifest, read_jsonl
from .fabric import Fabric
from .mining import mine
from .sanity import sanity_rows
from .task import GENERATOR_VERSION, Draw, generate
from .tools import FAMILY_SPLIT

Beat = Callable[[], None]


@dataclass
class Context:
    fabric: Fabric
    tools: dict[str, Any]
    spec_refs: dict[str, dict[str, Any]]
    specs: dict[str, dict[str, Any]]
    root: Path
    backend: Callable[[], Any]
    now: Callable[[], float]
    environment: dict[str, Any]
    toolchain: dict[str, Any]
    tick: str = ""


@dataclass
class Result:
    nouns: dict[str, Any]
    summary: dict[str, Any]
    problem: str
    given: str
    solution: str
    went_right: list[str]
    went_wrong: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    types: tuple[str, str] = ("", "")
    refs: list[dict[str, Any]] = field(default_factory=list)
    model: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    status: str = "ok"
    outcome: str = "succeeded"


def tags(brief: dict[str, Any], *extra: str) -> list[str]:
    return [f"learner:{brief['learner']}", f"round:{brief['round']}", *extra]


def rows_of(ctx: Context, ref: dict[str, Any]) -> list[dict[str, Any]]:
    return read_jsonl(ctx.fabric.get_bytes(ctx.fabric.read(ref)["jsonl"]))


def pointer(ctx: Context, brief: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
    return ctx.fabric.pointer(brief["learner"])


def holdout_rows(ctx: Context, holdouts_ref: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    value = ctx.fabric.read(holdouts_ref)
    return {
        name: read_jsonl(ctx.fabric.get_bytes(part["jsonl"]))
        for name, part in value["parts"].items()
    }


def evaluation_rows(ctx: Context, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Every row ever used for evaluation: frozen dev/test/sanity plus each fresh holdout."""
    rows = [row for part in holdout_rows(ctx, state["holdouts"]).values() for row in part]
    for ref in state.get("fresh_holdouts", []):
        rows += read_jsonl(ctx.fabric.get_bytes(ctx.fabric.read(ref)["jsonl"]))
    return rows


def accepted(ctx: Context) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return ctx.fabric.list("data", "accepted_batch.")


def batch_noun(ctx: Context, brief: dict[str, Any], spec: dict[str, Any], rows: list[dict]):
    value = {
        "category_id": "noun.benchmark_suite",
        "role": "example_batch",
        "status": "candidate",
        "learner": brief["learner"],
        "round": brief["round"],
        "name": spec["name"],
        "batch": spec["batch"],
        "origin": spec["origin"],
        "manifest": manifest("example_batch", {spec["name"]: rows}),
        "jsonl": ctx.fabric.put_bytes(jsonl(rows)),
        "generator_version": GENERATOR_VERSION,
        "tool_snapshot": ctx.spec_refs["tools"],
    }
    return ctx.fabric.put("data", f"batch.{spec['name']}", value, tags(brief, "status:candidate"))


def generate_examples(ctx: Context, brief: dict[str, Any], deps: dict, beat: Beat) -> Result:
    made, counts, short = {}, {}, []
    state, _ = pointer(ctx, brief)
    for spec in brief["params"]["batches"]:
        beat()
        if spec.get("kind") == "sanity":
            rows = sanity_rows(spec["count"], spec["batch"])
        elif spec.get("kind") == "augmented":
            rows = generate_augmented(spec["count"], spec["batch"], ctx.tools)
        elif spec.get("kind") == "fresh":
            prior = fingerprint_sets(evaluation_rows(ctx, state)) if state else None
            pool = generate(
                spec["split"], spec["count"] * 3, spec["batch"], spec["origin"], ctx.tools
            )
            rows, seen = [], set()
            for row in pool:
                fp = fingerprint_sets([row])
                text = next(iter(fp["request"]))
                if text in seen or (prior and (fp["request"] & prior["request"])):
                    continue
                seen.add(text)
                rows.append(row)
                if len(rows) == spec["count"]:
                    break
            if len(rows) < spec["count"]:
                short.append(f"{spec['name']}: {len(rows)} of {spec['count']} fresh rows")
        else:
            rows = generate(spec["split"], spec["count"], spec["batch"], spec["origin"], ctx.tools)
        made[spec["name"]] = batch_noun(ctx, brief, spec, rows)
        counts[spec["name"]] = len(rows)
    return Result(
        nouns={"batches": made},
        summary={"rows": counts},
        problem="Produce owned example typed tool-call examples with computed labels",
        given=f"Batch plan {[s['batch'] for s in brief['params']['batches']]}",
        solution=f"Generated {counts} rows from repository templates; every call label is schema-valid",
        went_right=[f"{n} rows in batch {name}" for name, n in counts.items()],
        went_wrong=short,
        next_steps=["Curate: fingerprint, deduplicate and check contamination before any use"],
        types=("batch plan (names, splits, counts)", "candidate example batch nouns"),
        refs=list(made.values()),
        outcome="partial" if short else "succeeded",
    )


def _part(ctx: Context, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"rows": len(rows), "jsonl": ctx.fabric.put_bytes(jsonl(rows))}


def _accepted_noun(ctx, brief, origin, source, rows, rejected, checked):
    value = {
        "category_id": "noun.benchmark_suite",
        "role": "accepted_batch",
        "learner": brief["learner"],
        "round": brief["round"],
        "origin": origin,
        "source_batch": source,
        "rows": len(rows),
        "jsonl": ctx.fabric.put_bytes(jsonl(rows)),
        "rejected": rejected[:200],
        "checked_against": checked,
        "manifest": manifest("accepted_batch", {origin: rows}),
    }
    return ctx.fabric.put(
        "data", f"accepted_batch.{origin}", value, tags(brief, f"origin:{origin}")
    )


def curate_dataset(ctx: Context, brief: dict[str, Any], deps: dict, beat: Beat) -> Result:
    mode = brief["params"]["mode"]
    state, _ = pointer(ctx, brief)
    nouns: dict[str, Any] = {}
    right, wrong = [], []
    if mode == "freeze_holdouts":
        batches = deps["generate_holdouts"]["nouns"]["batches"]
        parts = {name: rows_of(ctx, batches[name]) for name in ("dev", "test", "sanity")}
        for name, rows in parts.items():
            if len({r["id"] for r in rows}) != len(rows):
                raise ValueError(f"{name}: duplicate row ids")
            wanted = "sanity" if name == "sanity" else name
            for row in rows:
                if row.get("family") and FAMILY_SPLIT[row["family"]] != wanted:
                    raise ValueError(f"{row['id']}: family belongs to another split")
        sets = {name: fingerprint_sets(rows) for name, rows in parts.items()}
        for left in sets:
            for right_name in sets:
                if left < right_name and sets[left]["request"] & sets[right_name]["request"]:
                    raise ValueError(f"{left} and {right_name} share request text")
        value = {
            "category_id": "noun.benchmark_suite",
            "role": "frozen_holdouts",
            "status": "frozen",
            "learner": brief["learner"],
            "frozen_at_round": brief["round"],
            "manifest": manifest("frozen_holdouts", parts),
            "parts": {name: _part(ctx, rows) for name, rows in parts.items()},
        }
        nouns["holdouts"] = ctx.fabric.put("data", "holdouts", value, tags(brief, "status:frozen"))
        evaluation = [row for rows in parts.values() for row in rows]
        initial = rows_of(ctx, batches["initial"])
        rows, rejected = accept(initial, [], evaluation)
        nouns["accepted"] = {
            "initial": _accepted_noun(
                ctx, brief, "initial", batches["initial"], rows, rejected, [nouns["holdouts"]]
            )
        }
        right.append(
            f"Froze dev {len(parts['dev'])}, test {len(parts['test'])}, sanity {len(parts['sanity'])}"
        )
        right.append(f"Accepted {len(rows)} of {len(initial)} initial training rows")
        wrong += [f"rejected {r['id']}: {r['reason']}" for r in rejected[:4]]
    elif mode == "accept":
        if state is None:
            raise ValueError("no frozen holdouts; run the baseline round first")
        evaluation = evaluation_rows(ctx, state)
        pool = [row for ref, _ in accepted(ctx) for row in rows_of(ctx, ref)]
        nouns["accepted"] = {}
        for step, names in brief["params"]["sources"].items():
            for name in names:
                beat()
                source = deps[step]["nouns"]["batches"].get(name)
                if source is None:
                    continue
                candidates = rows_of(ctx, source)
                rows, rejected = accept(candidates, pool, evaluation)
                pool += rows
                origin = ctx.fabric.read(source)["origin"]
                nouns["accepted"][name] = _accepted_noun(
                    ctx, brief, origin, source, rows, rejected, [state["holdouts"]]
                )
                right.append(f"Accepted {len(rows)} of {len(candidates)} {name} rows")
                wrong += [f"rejected {r['id']}: {r['reason']}" for r in rejected[:2]]
    elif mode == "training_sets":
        if state is None:
            raise ValueError("no frozen holdouts; run the baseline round first")
        evaluation = evaluation_rows(ctx, state)
        nouns["training_sets"] = {}
        for strategy in brief["params"]["strategies"]:
            rows, sources = training_rows(ctx, state, strategy)
            counts = contamination(rows, evaluation)
            if any(counts.values()):
                raise ValueError(f"{strategy['id']}: contamination {counts}")
            value = {
                "category_id": "noun.benchmark_suite",
                "role": "training_set",
                "learner": brief["learner"],
                "round": brief["round"],
                "strategy": strategy,
                "sources": sources,
                "rows": len(rows),
                "jsonl": ctx.fabric.put_bytes(jsonl(rows)),
                "contamination": counts,
                "checked_rows": len(evaluation),
                "manifest": manifest("training_set", {strategy["id"]: rows}),
            }
            nouns["training_sets"][strategy["id"]] = ctx.fabric.put(
                "data", f"training_set.{strategy['id']}", value, tags(brief)
            )
            right.append(
                f"{strategy['id']}: {len(rows)} rows, zero shared fingerprints with {len(evaluation)} evaluation rows"
            )
    elif mode == "freeze_fresh":
        rows = rows_of(ctx, deps["fresh"]["nouns"]["batches"]["fresh"])
        pool = [row for ref, _ in accepted(ctx) for row in rows_of(ctx, ref)]
        counts = contamination(pool, rows)
        if any(v for k, v in counts.items() if k != "held_out_family_rows"):
            raise ValueError(f"fresh holdout overlaps training rows: {counts}")
        value = {
            "category_id": "noun.benchmark_suite",
            "role": "fresh_holdout",
            "status": "frozen",
            "learner": brief["learner"],
            "round": brief["round"],
            "jsonl": ctx.fabric.put_bytes(jsonl(rows)),
            "rows": len(rows),
            "manifest": manifest("fresh_holdout", {"fresh": rows}),
            "checked_training_rows": len(pool),
        }
        nouns["fresh"] = ctx.fabric.put(
            "data", "fresh_holdout", value, tags(brief, "status:frozen")
        )
        right.append(
            f"Froze a fresh holdout of {len(rows)} rows, disjoint from {len(pool)} training rows"
        )
    else:
        raise ValueError(f"unknown curation mode {mode}")
    refs = [v for v in _flatten(nouns)]
    return Result(
        nouns=nouns,
        summary={"mode": mode},
        problem="Keep evaluation data out of training while accepting new owned rows",
        given=f"Curation mode {mode}",
        solution="; ".join(right) or "No rows needed curation",
        went_right=right,
        went_wrong=wrong,
        next_steps=["Train or evaluate only from the pinned nouns written here"],
        types=(
            "candidate batches and frozen sets",
            "frozen holdout, accepted batch or training set nouns",
        ),
        refs=refs,
    )


def training_rows(ctx: Context, state: dict[str, Any], strategy: dict[str, Any]):
    batches = accepted(ctx)
    if strategy["train_on"] == "new_since_champion":
        used = set(state.get("champion_training_batches", []))
        chosen = [(r, v) for r, v in batches if v["origin"] in ("initial", "stream", "mined")]
        fresh = [(r, v) for r, v in chosen if r["key"] not in used]
        old = [row for r, v in chosen if r["key"] in used for row in rows_of(ctx, r)]
        rows = [row for r, _ in fresh for row in rows_of(ctx, r)]
        replay = Draw("replay", strategy["id"], len(old)).sample(old, strategy.get("replay", 0))
        return rows + replay, [r["key"] for r, _ in fresh] + [f"replay:{len(replay)}"]
    chosen = [(r, v) for r, v in batches if v["origin"] in strategy["train_on"]]
    rows = [
        row for r, _ in sorted(chosen, key=lambda item: item[0]["key"]) for row in rows_of(ctx, r)
    ]
    return rows, sorted(r["key"] for r, _ in chosen)


def _flatten(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict) and {"store", "key", "version", "value_sha256"} <= set(value):
        return [value]
    if isinstance(value, dict):
        return [ref for item in value.values() for ref in _flatten(item)]
    if isinstance(value, list):
        return [ref for item in value for ref in _flatten(item)]
    return []


def mine_hard_examples(ctx: Context, brief: dict[str, Any], deps: dict, beat: Beat) -> Result:
    state, _ = pointer(ctx, brief)
    if state is None:
        raise ValueError("no frozen holdouts")
    selection = deps[brief["params"]["selection_step"]]["summary"]["selected"]
    run = ctx.fabric.read(selection["dev_eval"])
    scores = read_jsonl(ctx.fabric.get_bytes(run["scores"]))
    dev = {row["id"]: row for row in holdout_rows(ctx, state["holdouts"])["dev"]}
    result = mine(scores, dev, brief["params"]["budget"], brief["round"], ctx.tools)
    made = {}
    for name in ("mined", "control"):
        rows = result[name]
        if rows:
            spec = {"name": name, "batch": f"{name}-r{brief['round']}", "origin": name}
            made[name] = batch_noun(ctx, brief, spec, rows)
    failures = sum(1 for s in scores if not s["exact"])
    return Result(
        nouns={"batches": made},
        summary={
            "dev_failures": failures,
            "allocation": result["allocation"],
            "rows": {k: len(result[k]) for k in ("mined", "control")},
        },
        problem="Turn the selected candidate's dev failures into new labeled training rows",
        given=f"{failures} failed dev rows of {len(scores)}; budget {brief['params']['budget']}",
        solution=f"Mined {len(result['mined'])} targeted rows and a matched control of {len(result['control'])}",
        went_right=[
            f"{a['rows']} rows for {'+'.join(a['requires'])} ({a['failures']} failures)"
            for a in result["allocation"][:6]
        ],
        went_wrong=[]
        if failures
        else ["No dev failures to mine; the next round relies on streamed rows"],
        next_steps=["Accept the batches against every holdout, then train the next round"],
        types=("dev evaluation run with per-row failures", "mined and control candidate batches"),
        refs=list(made.values()),
    )
