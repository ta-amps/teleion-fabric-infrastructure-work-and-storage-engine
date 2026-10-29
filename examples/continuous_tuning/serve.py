"""Several specialists over one base: one loaded base, adapters attached by name.

The `serve_check` verb loads the base once, attaches several stored adapter nouns,
routes each dev prompt set through one named adapter at a time (and through the base
with adapters disabled), and compares the greedy replies with the replies recorded
when each model was evaluated alone. Same prompts, batching and decoding as before.
"""

from __future__ import annotations

from typing import Any

from .dataset import read_jsonl
from .model_verbs import adapter_dir, model_info, usage
from .task import prompt_messages
from .verbs import Beat, Context, Result, holdout_rows, pointer, tags


def serve_check(ctx: Context, brief: dict[str, Any], deps: dict, beat: Beat) -> Result:
    from peft import PeftModel

    state, _ = pointer(ctx, brief)
    if state is None:
        raise ValueError("no champion pointer; run the baseline round first")
    dev = holdout_rows(ctx, state["holdouts"])["dev"]
    runs, seen = [], set()
    for ref, run in sorted(
        ctx.fabric.list("evals", "eval.dev."), key=lambda item: item[1]["round"]
    ):
        adapter = run["model"]["adapter"]
        if adapter and run["learner"] == brief["learner"] and adapter["key"] not in seen:
            seen.add(adapter["key"])
            runs.append((ref, run))
    runs = runs[: brief["params"].get("max_adapters", 3)]
    if len(runs) < 2:
        raise ValueError("need at least two evaluated adapters to check shared-base serving")
    backend = ctx.backend()
    backend.reset_peak()
    model, attached = None, []
    base = backend.load()
    for index, (ref, run) in enumerate(runs):
        value = ctx.fabric.read(run["model"]["adapter"])
        name = f"specialist_{index}"
        if model is None:
            model = PeftModel.from_pretrained(base, adapter_dir(ctx, value), adapter_name=name)
        else:
            model.load_adapter(str(adapter_dir(ctx, value)), adapter_name=name)
        attached.append((name, ref, run, value))
    assert model is not None
    model.eval()
    conversations = [prompt_messages(row, ctx.tools) for row in dev]
    results, prompt_t, output_t = [], 0, 0

    def compare(run: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        nonlocal prompt_t, output_t
        decoding = run["decoding"]
        replies = backend.generate(
            model, conversations, decoding["allowance_tokens"], decoding["batch_size"], beat
        )
        stored = [record["reply"] for record in read_jsonl(ctx.fabric.get_bytes(run["scores"]))]
        prompt_t += sum(r["prompt_tokens"] for r in replies)
        output_t += sum(r["output_tokens"] for r in replies)
        return sum(r["text"] == s for r, s in zip(replies, stored, strict=True)), replies

    for name, ref, run, value in attached:
        model.set_adapter(name)
        same, _ = compare(run)
        results.append({"adapter": name, "label": run["model"]["label"], "adapter_sha256": value["adapter_sha256"], "evaluation": ref, "identical_replies": same, "rows": len(dev)})  # fmt: skip
    base_run = ctx.fabric.read(state["base_evals"]["dev"])
    with model.disable_adapter():
        base_same, _ = compare(base_run)
    memory = backend.peak()
    backend.release(model, base)
    value = {
        "category_id": "noun.eval_run",
        "role": "serving_check",
        "learner": brief["learner"],
        "round": brief["round"],
        "adapters": results,
        "base_with_adapters_disabled": {"identical_replies": base_same, "rows": len(dev), "evaluation": state["base_evals"]["dev"]},
        "memory": memory,
        "loaded_bases": 1,
        "note": "One base in memory serves every attached adapter; the champion pointer chooses which one a request uses.",
    }  # fmt: skip
    noun = ctx.fabric.put("evals", "serving_check", value, tags(brief))
    all_same = all(r["identical_replies"] == r["rows"] for r in results) and base_same == len(dev)
    lines = [f"{r['label']}: {r['identical_replies']}/{r['rows']} identical" for r in results]
    return Result(
        nouns={"serving_check": noun},
        summary={"adapters": len(results), "all_identical": all_same, "peak_allocated_bytes": memory["peak_allocated_bytes"]},
        problem="Serve several specialist adapters from one loaded base without changing their replies",
        given=f"{len(results)} adapter nouns and the base; {len(dev)} dev prompts each",
        solution=f"One base, {len(results)} named adapters; base with adapters disabled {base_same}/{len(dev)} identical",
        went_right=[*lines, f"base: {base_same}/{len(dev)} identical"],
        went_wrong=[] if all_same else ["Some replies differed from the solo evaluation; inspect numerics before sharing a base"],
        next_steps=["Route each request by its specialist's champion pointer; fall back to the base"],
        types=("adapter nouns and recorded dev evaluations", "serving check noun"),
        refs=[noun],
        model=model_info(ctx, [item[3] for item in attached]),
        usage=usage(prompt_t, output_t),
    )  # fmt: skip
