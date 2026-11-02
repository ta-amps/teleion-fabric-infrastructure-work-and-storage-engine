"""One on-demand tick: write spec nouns, enqueue a tick verb, then claim and run ready verbs.

The tick verb reads the trigger noun and the champion pointer and, when the trigger
fires, enqueues one round as Tessera tasks with `depends_on`. Tessera orders the work;
this worker only claims what is ready, heartbeats while working and submits once with
an execution report. A later scheduler would enqueue the same tick verb; it would not
run anything itself.
"""

from __future__ import annotations

import json
import platform
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from examples.finetuning.data import digest, file_digest

import tessera
from tessera import TaskSpec

from . import decide, model_verbs, serve, verbs
from .catalog import VERBS, execution_report, learning_block, no_model_usage, store_supports
from .fabric import NAMESPACE, QUEUE, WORKER, Fabric
from .tools import SNAPSHOT, load_snapshot

HERE = Path(__file__).parent
SPEC_FILES = {
    "learner_spec": ("learner_spec.json", "noun.code_node", "learner_spec"),
    "trigger": ("trigger.json", "noun.code_node", "trigger"),
    "rule": ("promotion_rule.json", "noun.code_node", "promotion_rule"),
    "recipe": ("recipes/lora-sft-r16.json", "noun.code_node", "training_recipe"),
    "search": ("recipes/search-lr3.json", "noun.code_node", "search_space"),
}
HANDLERS: dict[str, Callable[..., verbs.Result]] = {
    "generate_examples": verbs.generate_examples,
    "curate_dataset": verbs.curate_dataset,
    "mine_hard_examples": verbs.mine_hard_examples,
    "decide_promotion": decide.decide_promotion,
    "tune_hyperparameters": model_verbs.tune_hyperparameters,
    "train_adapter": model_verbs.train_adapter,
    "evaluate": model_verbs.evaluate,
    "serve_check": serve.serve_check,
}


def toolchain() -> dict[str, str]:
    files = [*sorted(HERE.glob("*.py")), SNAPSHOT]
    files += [HERE.parent / "finetuning" / name for name in ("data.py", "peft_backend.py")]
    return {f"{p.parent.name}/{p.name}": file_digest(p) for p in files}


def bootstrap(fabric: Fabric, base_path: Path) -> dict[str, dict[str, Any]]:
    """Create-only spec nouns; identical files always yield identical references."""
    refs = {}
    snapshot = load_snapshot()
    value = {"category_id": "noun.mcp_server", "role": "tool_snapshot", **snapshot}
    refs["tools"] = fabric.put("specs", "tool_snapshot", value, ["category:noun.mcp_server"])
    base = json.loads(base_path.read_bytes())
    value = {"category_id": "noun.model_spec", "role": "base_model", **base}
    refs["base_model"] = fabric.put("specs", "base_model", value, ["category:noun.model_spec"])
    for name, (path, category, role) in SPEC_FILES.items():
        value = {"category_id": category, "role": role, **json.loads((HERE / path).read_bytes())}
        refs[name] = fabric.put("specs", role, value, [f"category:{category}"])
    return refs


def learner_key(specs: dict[str, dict[str, Any]]) -> str:
    return f"{specs['learner_spec']['learner']}:{specs['base_model']['base_id']}"


def trigger(ctx: verbs.Context, learner: str) -> tuple[str, int, str]:
    rule = ctx.specs["trigger"]
    state, _ = ctx.fabric.pointer(learner)
    if state is None:
        return "baseline", 0, "no baseline evaluation exists"
    seen = set(state.get("last_candidate_training_batches", [])) | set(
        state.get("champion_training_batches", [])
    )
    new = sum(
        value["rows"]
        for ref, value in verbs.accepted(ctx)
        if value["origin"] in rule["counted_origins"] and ref["key"] not in seen
    )
    if new >= rule["min_new_accepted_rows"] and state["round"] < rule["max_training_rounds"]:
        return "training", state["round"] + 1, f"{new} accepted rows not yet trained on"
    if ctx.now() - state["last_evaluated_at"] >= rule["interval_hours"] * 3600:
        return "monitor", state["round"], "evaluation interval elapsed"
    return "no_op", state["round"], f"{new} new rows; interval not elapsed"


def plan(kind: str, round_id: int, spec: dict[str, Any], now: float) -> list[dict[str, Any]]:
    data = spec["data_plan"]
    if kind == "baseline":
        batches = [
            {"name": name, "split": split, "count": data[name], "batch": batch, "origin": origin}
            for name, split, batch, origin in (
                ("dev", "dev", "dev-frozen-v1", "frozen_holdout"),
                ("test", "test", "test-frozen-v1", "frozen_holdout"),
                ("initial", "train", "train-initial-v1", "initial"),
            )
        ]
        batches.append(
            {
                "name": "sanity",
                "kind": "sanity",
                "count": data["sanity"],
                "batch": "sanity-v1",
                "origin": "sanity",
            }
        )
        return [
            step("generate_holdouts", "generate_examples", {"batches": batches}, []),
            step("freeze", "curate_dataset", {"mode": "freeze_holdouts"}, ["generate_holdouts"]),
            step("baseline_eval", "evaluate", {"role": "baseline"}, ["freeze"]),
            step(
                "establish", "decide_promotion", {"mode": "baseline"}, ["freeze", "baseline_eval"]
            ),
        ]
    fresh = {
        "name": "fresh",
        "split": "test",
        "count": data["fresh_holdout"],
        "kind": "fresh",
        "origin": "fresh_holdout",
    }
    if kind == "monitor":
        fresh["batch"] = f"test-monitor-r{round_id}-{int(now)}"
        return [
            step("fresh", "generate_examples", {"batches": [fresh]}, []),
            step("freeze_fresh", "curate_dataset", {"mode": "freeze_fresh"}, ["fresh"]),
            step("holdout_eval", "evaluate", {"role": "monitor"}, ["freeze_fresh"]),
            step("record", "decide_promotion", {"mode": "monitor"}, ["holdout_eval"]),
        ]
    strategies = spec["strategies"].get(str(round_id), spec["strategies"]["default"])
    # A strategy may declare rows it needs drawn and accepted before its training set.
    needed = [
        {**g, "batch": f"{g['name']}-r{round_id}"}
        for s in strategies
        for g in s.get("generate", [])
    ]
    steps, before = [], []
    if needed:
        sources = {"generate_for_round": [g["name"] for g in needed]}
        steps += [
            step("generate_for_round", "generate_examples", {"batches": needed}, []),
            step(
                "accept_for_round",
                "curate_dataset",
                {"mode": "accept", "sources": sources},
                ["generate_for_round"],
            ),
        ]
        before = ["accept_for_round"]
    steps.append(
        step(
            "training_sets",
            "curate_dataset",
            {"mode": "training_sets", "strategies": strategies},
            before,
        )
    )
    if strategies[0].get("search"):
        selection = "search"
        steps.append(
            step("search", "tune_hyperparameters", {"strategy": strategies[0]}, ["training_sets"])
        )
    else:
        selection = "select"
        names = [f"train_{s['id']}" for s in strategies]
        steps += [
            step(n, "train_adapter", {"strategy": s}, ["training_sets"])
            for n, s in zip(names, strategies, strict=True)
        ]
        steps.append(step("select", "evaluate", {"role": "selection", "candidates": names}, names))
    fresh["batch"] = f"test-fresh-r{round_id}"
    steps += [
        step("fresh", "generate_examples", {"batches": [fresh]}, [selection]),
        step("freeze_fresh", "curate_dataset", {"mode": "freeze_fresh"}, ["fresh"]),
        step("holdout_eval", "evaluate", {"role": "holdout", "selection_step": selection}, [selection, "freeze_fresh"]),
        step("decide", "decide_promotion", {"mode": "promote", "selection_step": selection}, ["holdout_eval", selection]),
        step("mine", "mine_hard_examples", {"budget": data["mined_cap"], "selection_step": selection}, [selection, "decide"]),
        step("stream", "generate_examples", {"batches": [{"name": "stream", "split": "train", "count": data["stream"], "batch": f"train-stream-r{round_id}", "origin": "stream"}]}, ["decide"]),
        step("accept", "curate_dataset", {"mode": "accept", "sources": {"mine": ["mined", "control"], "stream": ["stream"]}}, ["mine", "stream"]),
    ]  # fmt: skip
    return steps


def step(name: str, stage: str, params: dict[str, Any], deps: list[str]) -> dict[str, Any]:
    return {"step": name, "stage": stage, "params": params, "deps": deps}


def task_spec(ctx, learner, round_id, item, depends_on, tick_id) -> TaskSpec:
    category = VERBS[item["stage"]]["category"]
    identity = digest(
        {"learner": learner, "round": round_id, "step": item["step"], "tick": tick_id}
    )
    return TaskSpec(
        namespace=NAMESPACE,
        queue=QUEUE,
        task_id="ct_" + identity[:48],
        idempotency_key="continuous-tuning:" + identity,
        kind=category,
        effect_class="write-internal",
        visibility_scope="private",
        depends_on=depends_on,
        tags=[
            f"category:{category}",
            f"stage:{item['stage']}",
            f"round:{round_id}",
            "data:example",
        ],
        brief={
            "stage": item["stage"],
            "step": item["step"],
            "round": round_id,
            "learner": learner,
            "spec_refs": ctx.spec_refs,
            "params": item["params"],
            "execution": {
                "reasoning": "authorized_worker",
                "server_fallback": False,
                "automatic_promotion": False,
                "automatic_children": 0,
            },
            "required": VERBS[item["stage"]]["checks"],
        },
    )


def tick_verb(ctx: verbs.Context, brief: dict[str, Any], deps: dict, beat) -> verbs.Result:
    kind, round_id, reason = trigger(ctx, brief["learner"])
    if kind == "no_op":
        return verbs.Result(
            nouns={}, summary={"kind": kind, "reason": reason}, status="no_op", outcome="no_change",
            problem="Decide whether the learner should run a round now", given=reason,
            solution="Trigger not met; nothing enqueued", went_right=[reason],
            types=("trigger noun and champion pointer", "nothing"),
        )  # fmt: skip
    ids: dict[str, str] = {}
    for item in plan(kind, round_id, ctx.specs["learner_spec"], ctx.now()):
        spec = task_spec(
            ctx, brief["learner"], round_id, item, [ids[d] for d in item["deps"]], ctx.tick
        )
        ids[item["step"]] = ctx.fabric.store.enqueue(spec)
    return verbs.Result(
        nouns={}, summary={"kind": kind, "round": round_id, "reason": reason, "enqueued": ids},
        problem="Decide whether the learner should run a round now", given=reason,
        solution=f"Enqueued a {kind} round {round_id} of {len(ids)} verbs",
        went_right=[reason, f"{len(ids)} verbs enqueued with dependencies"],
        next_steps=["Authorized workers claim the ready verbs in dependency order"],
        types=("trigger noun and champion pointer", "Tessera tasks"),
    )  # fmt: skip


def run_verb(ctx: verbs.Context, lease: Any) -> dict[str, Any]:
    began = time.perf_counter()
    brief = lease.brief
    ctx.specs = {name: ctx.fabric.read(ref) for name, ref in brief["spec_refs"].items()}
    ctx.spec_refs = brief["spec_refs"]

    def beat() -> None:
        ctx.fabric.store.heartbeat(lease.task_id, lease.fencing_token)

    deps, problems = {}, []
    for dep in lease.dependencies:
        out = dep.output if isinstance(dep.output, dict) else {}
        if dep.state != "done" or dep.ok is not True or out.get("status") != "ok":
            problems.append(dep.task_id)
        if "step" in out:
            deps[out["step"]] = out
    handler = tick_verb if brief["stage"] == "tick" else HANDLERS[brief["stage"]]
    try:
        if problems:
            raise ValueError(f"dependencies did not succeed: {problems}")
        result, ok, error = handler(ctx, brief, deps, beat), True, None
    except Exception as exc:  # recorded as a failed verb; never retried silently
        error = f"{type(exc).__name__}: {exc}"[:2000]
        result, ok = verbs.Result(
            nouns={}, summary={}, status="failed", outcome="failed",
            problem=f"{brief['stage']} for round {brief['round']}", given=brief["step"],
            solution="Failed before completion", went_right=[], went_wrong=[error],
            next_steps=["Inspect the error and the dependency receipts; a new tick plans a fresh attempt"],
            types=("brief and dependency outputs", "none"),
        ), False  # fmt: skip
    duration = time.perf_counter() - began
    learning = learning_block(
        outcome=result.outcome, went_right=result.went_right, went_wrong=result.went_wrong,
        next_steps=result.next_steps, types=result.types, method=brief["stage"], duration=duration,
        advice=[{"learner": brief["learner"][:64], "strategy": ctx.specs["learner_spec"]["strategy"][:64], "version": ctx.specs["learner_spec"]["version"], "store": ctx.spec_refs["learner_spec"]["store"], "key": ctx.spec_refs["learner_spec"]["key"], "record_version": ctx.spec_refs["learner_spec"]["version"]}],
    )  # fmt: skip
    tokens = result.usage or no_model_usage()
    output = {
        "step": brief["step"],
        "stage": brief["stage"],
        "round": brief["round"],
        "status": result.status,
        "nouns": result.nouns,
        "summary": result.summary,
        "learning": learning,
        "duration_seconds": round(duration, 3),
        "model": result.model,
        "tokens": tokens,
    }
    report = execution_report(
        brief["stage"], problem=result.problem, given=result.given, solution=result.solution,
        learning=learning, refs=result.refs, model=result.model, usage=tokens,
        context={"brief": brief, "dependencies": sorted(deps)}, environment=ctx.environment,
        toolchain=ctx.toolchain,
    )  # fmt: skip
    receipt = ctx.fabric.store.submit(
        lease.task_id, lease.fencing_token, output, idempotency_key=f"{lease.task_id}:{lease.fencing_token}",
        ok=ok, error=error, execution_report=report,
    )  # fmt: skip
    return {
        "task_id": lease.task_id, "stage": brief["stage"], "step": brief["step"], "round": brief["round"],
        "ok": ok, "status": result.status, "error": error, "receipt_hash": receipt.receipt_hash,
        "seconds": round(duration, 3), "summary": result.summary,
    }  # fmt: skip


def blob_chunks(value: Any) -> list[str]:
    if isinstance(value, dict):
        own = value.get("chunks") if isinstance(value.get("chunks"), list) else []
        return own + [c for item in value.values() for c in blob_chunks(item)]
    if isinstance(value, list):
        return [c for item in value for c in blob_chunks(item)]
    return []


def privacy_probe(fabric: Fabric, ran: list[dict[str, Any]]) -> dict[str, Any]:
    """Collect this tick's nouns, blobs and execution runs; another principal must see none."""
    grant = fabric.grant()
    refs, runs = [], []
    for item in ran:
        task = fabric.store.task(item["task_id"])
        results = fabric.store.results(item["task_id"])
        output = results[-1].output if results else {}
        refs += verbs._flatten(output.get("nouns", {}) if isinstance(output, dict) else {})
        listed = fabric.store.list_executions(task.task_id, grant=grant)["executions"]
        item["run_ids"] = [run["run_id"] for run in listed]
        runs += item["run_ids"]
    chunks = sorted({c for ref in refs for c in blob_chunks(fabric.read(ref))})
    return {
        "records": len(refs),
        "blobs": len(chunks),
        "executions": len(runs),
        **fabric.denied_to_other(refs, chunks, runs),
    }


def tick(
    root: Path,
    base_path: Path,
    backend: Callable[[], Any],
    *,
    now: Callable[[], float] = time.time,
    max_verbs: int = 64,
    log: Callable[[str], None] = lambda _line: None,
    stage: str = "tick",
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Enqueue one seed verb (the tick, or a single auxiliary verb such as serve_check) and
    claim ready verbs until the queue is empty."""
    fabric = Fabric(root)
    try:
        with fabric.scope():
            refs = bootstrap(fabric, base_path)
            specs = {name: fabric.read(ref) for name, ref in refs.items()}
            tick_id = f"{int(now() * 1000):x}"
            ctx = verbs.Context(
                fabric=fabric, tools={t["name"]: t for t in specs["tools"]["tools"]},
                spec_refs=refs, specs=specs, root=root, backend=backend, now=now,
                environment={"python": sys.version.split()[0], "platform": platform.platform(), "tessera": tessera.__version__},
                toolchain=toolchain(), tick=tick_id,
            )  # fmt: skip
            learner = learner_key(specs)
            item = step(stage, stage, params or {}, [])
            current, _ = fabric.pointer(learner)
            round_id = 0 if stage == "tick" or current is None else current["round"]
            fabric.store.enqueue(task_spec(ctx, learner, round_id, item, [], tick_id))
            ran = []
            for _ in range(max_verbs):
                lease = fabric.store.claim(QUEUE, WORKER, key_id=fabric.worker_key_id)
                if lease is None:
                    break
                log(
                    f"claimed {lease.brief['stage']}:{lease.brief['step']} round {lease.brief['round']}"
                )
                ran.append(run_verb(ctx, lease))
                log(
                    f"  -> ok={ran[-1]['ok']} status={ran[-1]['status']} {ran[-1]['seconds']} s {ran[-1]['error'] or ''}"
                )
            state, version = fabric.pointer(learner)
            result = {
                "learner": learner,
                "tick": tick_id,
                "verbs": ran,
                "pointer_version": version,
                "round": state["round"] if state else None,
                "privacy": privacy_probe(fabric, ran),
                "store_supports": store_supports(),
            }
            logs = root / "ticks"
            logs.mkdir(exist_ok=True)
            path = logs / f"tick-{tick_id}.json"
            if path.exists():
                raise ValueError(f"{path} exists; tick evidence is never overwritten")
            path.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
            return result
    finally:
        fabric.close()
