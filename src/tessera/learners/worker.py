"""Run one claimed learner verb with its registered deterministic runner, then submit.

The registry is the swap point: a new strategy registers its own runner under its
(learner, strategy, version) and is enabled only through a config noun. Nothing here
claims work by itself, retries forever or calls a model.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol

from tessera._version import __version__
from tessera.errors import VersionConflict
from tessera.learners import memory, path_statistics, routing
from tessera.learners.runtime import Api, LearnerRun, load_run
from tessera.models import Lease, Receipt

Runner = Callable[[Api, LearnerRun], dict[str, Any]]
RUNNERS: dict[tuple[str, str, int], Runner] = {
    (memory.LEARNER, memory.STRATEGY, memory.VERSION): memory.run,
    (
        path_statistics.LEARNER,
        path_statistics.STRATEGY,
        path_statistics.VERSION,
    ): path_statistics.run,
    (routing.LEARNER, routing.STRATEGY, routing.VERSION): routing.run,
}
OUTCOMES = {"updated": "succeeded", "no_change": "no_change", "superseded": "no_change"}


class WorkerApi(Api, Protocol):
    def submit(
        self,
        task_id: str,
        fencing_token: int,
        output: Any,
        *,
        idempotency_key: str,
        ok: bool = True,
        error: str | None = None,
        execution_report: dict[str, Any] | None = None,
        namespace: str | None = None,
    ) -> Receipt: ...


def report_for(run: LearnerRun, output: dict[str, Any], seconds: float) -> dict[str, Any]:
    """The learner's own execution report: no model, zero tokens, explicit unknowns.

    No free-text summaries: they would add the core SimHash band rows to every run.
    """
    return {
        "version": 1,
        "harness": {"name": "tessera-learner-runtime", "version": __version__},
        "model": None,
        "usage": {
            "source": "deterministic_no_model",
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
        "learning": {
            "outcome": OUTCOMES[output["outcome"]],
            "types": {"input": "execution-observation-v1[]", "output": f"{run.learner} nouns"},
            "path": {"method": f"{run.strategy}-v{run.spec_version}"},
            "duration_seconds": max(0.0, seconds),
            "cost": None,
            "unknowns": [{"field": "cost", "reason": "hosting and storage cost not metered here"}],
        },
    }


def run_claimed(
    api: WorkerApi, lease: Lease, *, clock: Callable[[], float] = time.monotonic
) -> dict[str, Any]:
    """Verify pins, run the registered strategy, submit once with a stable idempotency key."""
    started = clock()
    run = load_run(api, lease, clock=clock)
    runner = RUNNERS.get((run.learner, run.strategy, run.spec_version))
    if runner is None or run.spec_value.verb.runner is None:
        raise ValueError("no registered runner for this learner strategy and version")
    try:
        output = runner(api, run)
    except VersionConflict:
        output = {
            "outcome": "superseded",
            "observations_read": 0,
            "reason": "another run advanced the learner state; a later run starts from it",
        }
    output = {
        "learner": run.learner,
        "strategy": run.strategy,
        "version": run.spec_version,
        "spec": run.spec.model_dump(),
        **output,
    }
    receipt = api.submit(
        lease.task_id,
        lease.fencing_token,
        output,
        idempotency_key=f"{lease.task_id}:{lease.fencing_token}",
        execution_report=report_for(run, output, clock() - started),
        namespace=lease.namespace,
    )
    return {"receipt": receipt.model_dump(mode="json"), "output": output}
