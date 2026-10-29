"""Model verbs: tune hyperparameters, train adapters and evaluate through a swappable backend.

The backend is whatever `ctx.backend()` returns (the optional Transformers/PEFT one on a
GPU worker, or a fixture in tests). Run directories are create-only, so evidence from a
round is never overwritten; adapters are also kept as private chunked blobs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from examples.finetuning.data import file_digest

from .catalog import weights_digest
from .dataset import digest_bytes, jsonl
from .metrics import aggregate, score
from .sanity import sanity_messages, score_sanity
from .task import prompt_messages, target_text
from .verbs import Beat, Context, Result, holdout_rows, pointer, rows_of, tags


def model_info(ctx: Context, adapters: list[dict[str, Any]]) -> dict[str, Any]:
    base = ctx.specs["base_model"]
    return {
        "provider": "local",
        "name": base["model_id"],
        "base_id": base["base_id"],
        "weights_sha256": weights_digest(base),
        "adapters_sha256": [a["adapter_sha256"] for a in adapters],
        "invocation_origin": "worker_direct",
        "payer": "owner, local GPU",
        "license": base["license"]["name"],
    }


def usage(prompt: int, output: int | None, reason: str | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "source": "local_tokenizer_count",
        "input_tokens": prompt,
        "output_tokens": output,
    }
    if output is not None:
        value["total_tokens"] = prompt + output
    if reason:
        value["reason"] = reason
    return value


def recipe_for(ctx: Context, state: dict[str, Any] | None, overrides: dict[str, Any]):
    recipe = dict(ctx.specs["recipe"])
    if state and state.get("tuned"):
        recipe.update(state["tuned"])
    recipe.update(overrides)
    # Hardware fit belongs to the base noun; the hyperparameters stay the recipe's.
    runtime = ctx.specs["base_model"].get("runtime", {})
    if "gradient_checkpointing" in runtime:
        recipe["gradient_checkpointing"] = runtime["gradient_checkpointing"]
    return recipe


def run_dir(ctx: Context, brief: dict[str, Any], name: str) -> Path:
    path = ctx.root / "rounds" / f"round-{brief['round']:03d}-{ctx.tick}" / brief["step"] / name
    if path.exists():
        raise ValueError(f"{path} exists; evidence directories are never reused")
    path.mkdir(parents=True)
    return path


def adapter_dir(ctx: Context, value: dict[str, Any]) -> Path:
    """The adapter's files on disk if their digests match the noun, else restored from blobs."""
    local = ctx.root / value["local_path"]
    if local.is_dir() and all(
        (local / name).is_file() and file_digest(local / name) == sha
        for name, sha in value["files"].items()
    ):
        return local
    restored = ctx.root / "restored" / value["adapter_sha256"]
    if not restored.exists():
        restored.mkdir(parents=True)
        for name, blob in value["blobs"].items():
            (restored / name).write_bytes(ctx.fabric.get_bytes(blob))
    return restored


def train_one(ctx, brief, rows, recipe, label, out, training_set, init, beat):
    backend = ctx.backend()
    state, _ = pointer(ctx, brief)
    dev = holdout_rows(ctx, state["holdouts"])["dev"][:4]
    probes = [prompt_messages(row, ctx.tools) for row in dev]
    examples = [(prompt_messages(row, ctx.tools), target_text(row, ctx.tools)) for row in rows]
    init_path = adapter_dir(ctx, ctx.fabric.read(init)) if init else None
    report = backend.train(
        examples, recipe, out / "adapter", init_adapter=init_path, probes=probes, checkpoint=beat
    )
    # After training: the first load has verified the weights by then.
    ctx.environment.update(backend.environment())
    blobs = {}
    for name in report["adapter_files"]:
        beat()
        blobs[name] = ctx.fabric.put_bytes((out / "adapter" / name).read_bytes())
    base = ctx.specs["base_model"]
    value = {
        "category_id": "noun.model_spec",
        "role": "adapter",
        "status": "unadmitted_candidate",
        "learner": brief["learner"],
        "round": brief["round"],
        "label": label,
        "base": ctx.spec_refs["base_model"],
        "recipe": ctx.spec_refs["recipe"],
        "effective_recipe": recipe,
        "training_set": training_set,
        "init_adapter": init,
        "adapter_sha256": report["adapter_sha256"],
        "files": report["adapter_files"],
        "blobs": blobs,
        "local_path": str((out / "adapter").relative_to(ctx.root)),
        "training": report,
        "license": {
            "base": base["license"]["name"],
            "use_policy": "USE_POLICY.md of the base applies",
            "distribution": "private derivative; never published",
        },
        "environment": ctx.environment,
    }
    ref = ctx.fabric.put("models", "adapter", value, tags(brief, "status:unadmitted"))
    return ref, value


def evaluate_sets(ctx, brief, label, adapter, sets, beat):
    """Evaluate one model (base when adapter is None) on several pinned sets."""
    backend = ctx.backend()
    value = ctx.fabric.read(adapter) if adapter else None
    model = backend.load(adapter_dir(ctx, value) if value else None)
    ctx.environment.update(backend.environment())
    settings = dict(ctx.specs["recipe"]["evaluation"])
    runtime = ctx.specs["base_model"].get("runtime", {})
    settings["batch_size"] = runtime.get("eval_batch_size", settings["batch_size"])
    refs, prompt_total, output_total = {}, 0, 0
    try:
        for name, (rows, source) in sets.items():
            beat()
            backend.reset_peak()
            sanity = name == "sanity"
            conversations = [
                sanity_messages(row) if sanity else prompt_messages(row, ctx.tools) for row in rows
            ]
            allowance = settings["decode_allowance_tokens"]
            replies = backend.generate(
                model, conversations, allowance, settings["batch_size"], beat
            )
            records = []
            for row, reply in zip(rows, replies, strict=True):
                scored = (
                    score_sanity(row, reply["text"])
                    if sanity
                    else score(row, reply["text"], ctx.tools)
                )
                records.append(
                    {
                        **scored,
                        "reply": reply["text"],
                        **{
                            k: reply[k]
                            for k in ("prompt_tokens", "output_tokens", "reached_allowance")
                        },
                    }
                )
            if sanity:
                summary = {
                    "n": len(records),
                    "accuracy": round(sum(r["correct"] for r in records) / len(records), 6),
                    "tool_json_rate": round(sum(r["tool_json"] for r in records) / len(records), 6),
                }
            else:
                summary = aggregate(records)
            summary["reached_allowance"] = sum(r["reached_allowance"] for r in records)
            prompt = sum(r["prompt_tokens"] for r in records)
            output = sum(r["output_tokens"] for r in records)
            prompt_total, output_total = prompt_total + prompt, output_total + output
            run = {
                "category_id": "noun.eval_run",
                "role": "evaluation_run",
                "learner": brief["learner"],
                "round": brief["round"],
                "evaluation_role": brief["params"].get("role", "search_trial"),
                "model": {"label": label, "base": ctx.spec_refs["base_model"], "adapter": adapter},
                "set": {
                    "name": name,
                    "rows": len(rows),
                    "jsonl_sha256": digest_bytes(jsonl(rows)),
                    "source": source,
                },
                "aggregate": summary,
                "scores": ctx.fabric.put_bytes(jsonl(records)),
                "decoding": {
                    "greedy": True,
                    "allowance_tokens": allowance,
                    "batch_size": settings["batch_size"],
                },
                "tokens": {"prompt": prompt, "output": output},
                "seconds": round(sum(r["seconds"] for r in replies), 3),
                "memory": backend.peak(),
                "environment": ctx.environment,
                "status": "worker_reported",
            }
            refs[name] = ctx.fabric.put("evals", f"eval.{name}", run, tags(brief, f"set:{name}"))
    finally:
        backend.release(model)
    return refs, prompt_total, output_total, value


def candidate_entry(ctx, label, strategy, adapter, dev_ref, rows, tuned):
    agg = ctx.fabric.read(dev_ref)["aggregate"]
    return {
        "label": label,
        "strategy": strategy,
        "adapter": adapter,
        "dev_eval": dev_ref,
        "dev_exact": agg["exact_match_strict"],
        "dev_json_valid": agg["json_valid_rate"],
        "rows": rows,
        "tuned": tuned,
    }


def select(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    def rank(c: dict[str, Any]) -> tuple[Any, ...]:
        lr = (c.get("tuned") or {}).get("learning_rate", 0.0)
        return (-c["dev_exact"], -(c["dev_json_valid"] or 0.0), lr, c["rows"], c["label"])

    return sorted(candidates, key=rank)[0]


def _selection(ctx, brief, candidates, objective) -> tuple[dict[str, Any], dict[str, Any]]:
    selected = select(candidates)
    value = {
        "category_id": "noun.eval_run",
        "role": "selection",
        "learner": brief["learner"],
        "round": brief["round"],
        "objective": objective,
        "candidates": candidates,
        "selected": selected["label"],
        "reads_only": ["training rows", "dev holdout"],
    }
    return selected, ctx.fabric.put("evals", "selection", value, tags(brief))


def tune_hyperparameters(ctx: Context, brief: dict[str, Any], deps: dict, beat: Beat) -> Result:
    strategy = brief["params"]["strategy"]
    training_set = deps["training_sets"]["nouns"]["training_sets"][strategy["id"]]
    rows = rows_of(ctx, training_set)
    state, _ = pointer(ctx, brief)
    dev = holdout_rows(ctx, state["holdouts"])["dev"]
    search = ctx.specs["search"]
    candidates, adapters, prompt_t, label_t = [], [], 0, 0
    for index, trial in enumerate(search["trials"][: search["max_trials"]]):
        recipe = recipe_for(ctx, None, trial)
        label = f"r{brief['round']}-{strategy['id']}-lr{trial['learning_rate']}"
        out = run_dir(ctx, brief, f"trial-{index}")
        ref, value = train_one(ctx, brief, rows, recipe, label, out, training_set, None, beat)
        adapters.append(value)
        evals, p, o, _ = evaluate_sets(
            ctx, brief, label, ref, {"dev": (dev, state["holdouts"])}, beat
        )
        prompt_t, label_t = prompt_t + value["training"]["tokens"]["processed"] + p, label_t + o
        candidates.append(
            candidate_entry(ctx, label, strategy["id"], ref, evals["dev"], len(rows), trial)
        )
    selected, selection = _selection(ctx, brief, candidates, search["objective"])
    lines = [f"{c['label']}: dev exact {c['dev_exact']}" for c in candidates]
    return Result(
        nouns={"selection": selection, "adapters": [c["adapter"] for c in candidates]},
        summary={"selected": selected, "candidates": candidates},
        problem="Choose training hyperparameters on dev only, within a fixed trial budget",
        given=f"{len(rows)} training rows; {len(candidates)} trials from search {search['search_id']}",
        solution=f"Selected {selected['label']} by dev exact match",
        went_right=lines,
        next_steps=["Score the dev winner once on the frozen test, the fresh holdout and sanity"],
        types=(
            "training set, search space, dev holdout",
            "adapter nouns, dev evaluation runs, selection",
        ),
        refs=[selection, *[c["adapter"] for c in candidates]],
        model=model_info(ctx, adapters),
        usage=usage(prompt_t, label_t, "input counts training forward tokens plus dev prompts"),
    )


def train_adapter(ctx: Context, brief: dict[str, Any], deps: dict, beat: Beat) -> Result:
    strategy = brief["params"]["strategy"]
    training_set = deps["training_sets"]["nouns"]["training_sets"][strategy["id"]]
    rows = rows_of(ctx, training_set)
    state, _ = pointer(ctx, brief)
    init = None
    if strategy.get("init") == "champion" and state and state["champion"]["kind"] == "adapter":
        init = state["champion"]["adapter"]
    recipe = recipe_for(ctx, state, {})
    label = f"r{brief['round']}-{strategy['id']}"
    ref, value = train_one(
        ctx, brief, rows, recipe, label, run_dir(ctx, brief, "train"), training_set, init, beat
    )
    report = value["training"]
    return Result(
        nouns={"adapter": ref},
        summary={
            "label": label,
            "strategy": strategy["id"],
            "adapter": ref,
            "rows": len(rows),
            "tuned": state.get("tuned") if state else None,
            "init": "champion" if init else "base",
        },
        problem=f"Train the {strategy['id']} strategy's adapter on its pinned training set",
        given=f"{len(rows)} rows; init from {'champion adapter' if init else 'base'}; lr {recipe['learning_rate']}",
        solution=f"{report['optimizer_steps']} steps in {report['training_seconds']} s; reload equivalent",
        went_right=[
            "Frozen base unchanged",
            "Adapter changed",
            "Fresh-base reload gave identical probe replies",
        ],
        next_steps=["Evaluate on dev with the other strategies; dev alone selects"],
        types=("training set and recipe", "unadmitted adapter noun"),
        refs=[ref],
        model=model_info(ctx, [value]),
        usage=usage(report["tokens"]["processed"], None, "training does not sample replies"),
    )


def evaluate(ctx: Context, brief: dict[str, Any], deps: dict, beat: Beat) -> Result:
    role = brief["params"]["role"]
    state, _ = pointer(ctx, brief)
    holdouts = deps["freeze"]["nouns"]["holdouts"] if role == "baseline" else state["holdouts"]
    frozen = holdout_rows(ctx, holdouts)
    used, prompt_t, output_t, lines = [], 0, 0, []
    summary: dict[str, Any] = {}

    def run(label, adapter, sets):
        nonlocal prompt_t, output_t
        refs, p, o, value = evaluate_sets(ctx, brief, label, adapter, sets, beat)
        prompt_t, output_t = prompt_t + p, output_t + o
        if value:
            used.append(value)
        for name, ref in refs.items():
            agg = ctx.fabric.read(ref)["aggregate"]
            lines.append(
                f"{label} {name}: " + str(agg.get("exact_match_strict", agg.get("accuracy")))
            )
        return refs

    if role == "baseline":
        sets = {name: (frozen[name], holdouts) for name in ("dev", "test", "sanity")}
        summary["evaluations"] = {"base": run("base", None, sets)}
    elif role == "selection":
        candidates = []
        for step in brief["params"]["candidates"]:
            item = deps[step]["summary"]
            refs = run(item["label"], item["adapter"], {"dev": (frozen["dev"], holdouts)})
            candidates.append(
                candidate_entry(
                    ctx,
                    item["label"],
                    item["strategy"],
                    item["adapter"],
                    refs["dev"],
                    item["rows"],
                    item.get("tuned"),
                )
            )
        selected, selection = _selection(ctx, brief, candidates, ctx.specs["search"]["objective"])
        summary.update(selected=selected, candidates=candidates, selection=selection)
    else:
        fresh_ref = deps["freeze_fresh"]["nouns"]["fresh"]
        fresh = (rows_of(ctx, fresh_ref), fresh_ref)
        champion = state["champion"]
        evaluations: dict[str, Any] = {}
        if role == "holdout":
            chosen = deps[brief["params"]["selection_step"]]["summary"]
            selected = chosen["selected"]
            evaluations["candidate"] = run(
                selected["label"],
                selected["adapter"],
                {
                    "test": (frozen["test"], holdouts),
                    "fresh": fresh,
                    "sanity": (frozen["sanity"], holdouts),
                },
            )
            summary["ablation"] = {
                c["label"]: run(c["label"], c["adapter"], {"test": (frozen["test"], holdouts)})[
                    "test"
                ]
                for c in chosen["candidates"]
                if c["label"] != selected["label"]
            }
        champ_adapter = champion.get("adapter")
        evaluations["champion"] = run(
            f"champion:{champion['label']}",
            champ_adapter,
            {
                "fresh": fresh,
                **({"sanity": (frozen["sanity"], holdouts)} if role == "monitor" else {}),
            },
        )
        evaluations["base"] = (
            evaluations["champion"]
            if champ_adapter is None
            else run("base", None, {"fresh": fresh})
        )
        summary.update(fresh=fresh_ref, evaluations=evaluations)
    nouns = {"evaluations": summary.get("evaluations", {})}
    if "selection" in summary:
        nouns["selection"] = summary["selection"]
    flat = [r for group in nouns["evaluations"].values() for r in group.values()] + [
        r for r in [summary.get("selection")] if r
    ]
    flat += list(summary.get("ablation", {}).values())
    return Result(
        nouns=nouns,
        summary=summary,
        problem=f"Evaluate ({role}) with strict typed tool-call metrics on pinned sets",
        given=f"Role {role}; frozen holdouts {holdouts['key'][:24]}",
        solution="; ".join(lines)[:2000],
        went_right=lines[:8],
        next_steps=["Decide promotion only from frozen test, fresh holdout and sanity results"]
        if role != "selection"
        else ["Score only the dev winner on held-out sets"],
        types=(
            "models and pinned evaluation sets",
            "evaluation run nouns with per-row score blobs",
        ),
        refs=flat,
        model=model_info(ctx, used),
        usage=usage(prompt_t, output_t),
    )
