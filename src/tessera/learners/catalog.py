"""Learner specs for every learning facet: three implemented, the rest proposed designs.

Each spec is the value of an ordinary private noun. Proposed specs document a design
and its evidence gate; they have no runner and cannot be run or activated.
"""

from __future__ import annotations

from tessera.learners.contract import (
    Budget,
    Consumers,
    Evaluation,
    LearnerSpec,
    Output,
    Trigger,
    VerbTemplate,
)

KEEP = "Keep the baseline unless the candidate beats it on the stated metric with min_evidence."

SPECS: tuple[LearnerSpec, ...] = (
    LearnerSpec(
        learner="memory",
        strategy="minhash-lsh",
        version=1,
        status="implemented",
        summary="Exact and MinHash fingerprints of execution observations in immutable index "
        "segments, plus consumer-side recall of similar accepted runs with outcome statistics.",
        reads=["execution_observation"],
        writes=[
            Output(kind="memory_index_segment", category_id="noun.eval_run", store="learn.memory"),
            Output(kind="memory_index_head", category_id="noun.eval_run", store="learn.memory"),
        ],
        trigger=Trigger(on_demand=True, new_observations=32),
        budget=Budget(max_observations=2000, max_runtime_seconds=300, max_output_bytes=524288),
        evaluation=Evaluation(
            baseline="Built-in server recall: exact input hash and SimHash text bands.",
            metric="hit@5 for same-template accepted runs; acceptance of runs citing the advice.",
            method="Leave-one-out replay over accepted observations, same queries for both "
            "strategies. " + KEEP,
            min_evidence=50,
        ),
        verb=VerbTemplate(
            kind="verb.ingest", runner="tessera.learners.memory:run", deterministic=True
        ),
        limitations=[
            "Lexical overlap of category, brief shape, declared types and problem words; not "
            "semantic equivalence.",
            "Eventually consistent: runs after the head cursor are not indexed until the next run.",
            "Recall scans all segments of one queue index; plan bucketed segments at large scale.",
        ],
    ),
    LearnerSpec(
        learner="path_statistics",
        strategy="wilson-grouping",
        version=1,
        status="implemented",
        summary="Deterministic per-path scorecards (category, template, phrasing, shim, method, "
        "harness, model, queue or tagset): attempts, acceptance with Wilson intervals, retries, "
        "tokens, durations, cost and common failure reasons.",
        reads=["execution_observation"],
        writes=[Output(kind="path_scorecard", category_id="noun.eval_run", store="learn.paths")],
        trigger=Trigger(on_demand=True, schedule="A schedule noun naming this spec, if enabled."),
        budget=Budget(max_observations=5000, max_runtime_seconds=300, max_output_bytes=524288),
        evaluation=Evaluation(
            baseline="No path advice: every path treated alike.",
            metric="Calibration of later acceptance against earlier Wilson intervals per path.",
            method="Hold out the newest window and check interval coverage. " + KEEP,
            min_evidence=30,
        ),
        verb=VerbTemplate(
            kind="verb.benchmark_harness",
            runner="tessera.learners.path_statistics:run",
            deterministic=True,
        ),
        limitations=[
            "Observational associations: paths saw different inputs, workers and times.",
            "Worker-reported fields are claims; acceptance is ok=true, not independent admission.",
        ],
    ),
    LearnerSpec(
        learner="routing_weights",
        strategy="beta-bandit",
        version=1,
        status="implemented",
        summary="Per category or task family, selection weights over alternatives (verb "
        "phrasing, shim, decomposition pattern, harness, provider/model, seed pack) from Beta "
        "posteriors of accepted versus failed runs, with an exploration floor.",
        reads=["execution_observation", "path_scorecard"],
        writes=[
            Output(
                kind="routing_weights", category_id="noun.reflex_procedure", store="learn.routing"
            )
        ],
        trigger=Trigger(on_demand=True, new_observations=100),
        budget=Budget(max_observations=5000, max_runtime_seconds=300, max_output_bytes=262144),
        evaluation=Evaluation(
            baseline="Uniform choice, or the caller's explicit choice when one is given.",
            metric="Acceptance and cost per accepted run among runs citing each weights version.",
            method="Reserve an exploration share for the baseline arm; require min_evidence per "
            "alternative before moving weight. " + KEEP,
            min_evidence=20,
        ),
        verb=VerbTemplate(
            kind="verb.distill_reflex",
            runner="tessera.learners.routing:run",
            deterministic=True,
        ),
        limitations=[
            "Weights advise; they never grant permission or override explicit caller or "
            "provider choices. Every choice records the weights version it used.",
        ],
    ),
    LearnerSpec(
        learner="phrasing",
        strategy="acceptance-by-phrasing",
        version=1,
        status="proposed",
        summary="Which verb descriptions get accepted work: compares phrasing hashes within a "
        "template, then a worker drafts candidate rewordings as proposals.",
        reads=["execution_observation", "path_scorecard"],
        writes=[
            Output(
                kind="phrasing_recommendation",
                category_id="noun.prompt_shim",
                store="learn.phrasing",
            )
        ],
        trigger=Trigger(on_demand=True, new_observations=200),
        budget=Budget(
            max_observations=5000, max_runtime_seconds=900, max_output_bytes=131072, model_calls=4
        ),
        evaluation=Evaluation(
            baseline="The template's current phrasing.",
            metric="Acceptance, retries and tokens per accepted run for each phrasing variant.",
            method="Shadow-assign new phrasings to a minority of new verbs. " + KEEP,
            min_evidence=30,
        ),
        verb=VerbTemplate(kind="verb.solve_draft", deterministic=False),
        limitations=["Drafting rewordings is worker reasoning within its own allowance."],
    ),
    LearnerSpec(
        learner="searchability",
        strategy="findability",
        version=1,
        status="proposed",
        summary="Which tags and descriptions make nouns findable: relates search queries to the "
        "nouns later read or cited by accepted runs, then proposes tag or description changes.",
        reads=["search_observation", "execution_observation"],
        writes=[
            Output(
                kind="tagging_proposal", category_id="noun.seed_hypothesis", store="learn.search"
            )
        ],
        trigger=Trigger(on_demand=True),
        budget=Budget(max_observations=5000, max_runtime_seconds=600, max_output_bytes=131072),
        evaluation=Evaluation(
            baseline="Existing tags and descriptions.",
            metric="Rank of the eventually used noun for held-out queries.",
            method="Replay held-out queries against current and proposed metadata. " + KEEP,
            min_evidence=50,
        ),
        verb=VerbTemplate(kind="verb.situate", deterministic=True),
        limitations=[
            "Needs a search_observation hook that core does not record today.",
            "Applying a tag change is a separate reviewed write to the owning noun.",
        ],
    ),
    LearnerSpec(
        learner="shim",
        strategy="injection-uplift",
        version=1,
        status="proposed",
        summary="Which context injections help: compares runs reporting each shim or prompt-shim "
        "noun against runs without one in the same category and template.",
        reads=["execution_observation"],
        writes=[Output(kind="shim_scorecard", category_id="noun.prompt_shim", store="learn.shims")],
        trigger=Trigger(on_demand=True, new_observations=100),
        budget=Budget(max_observations=5000, max_runtime_seconds=300, max_output_bytes=131072),
        evaluation=Evaluation(
            baseline="No injected shim.",
            metric="Acceptance and tokens per accepted run, with and without the shim.",
            method="Matched comparison within template and phrasing. " + KEEP,
            min_evidence=30,
        ),
        verb=VerbTemplate(kind="verb.benchmark_harness", deterministic=True),
        limitations=[
            "A shim is a worker claim in learning.path.shim until shims are pinned nouns."
        ],
    ),
    LearnerSpec(
        learner="decomposition",
        strategy="split-pattern",
        version=1,
        status="proposed",
        summary="Which split patterns (child count, waves, child categories) lead to accepted "
        "parents with fewer retries; emits reusable decomposition patterns.",
        reads=["execution_observation", "task_graph_observation"],
        writes=[
            Output(
                kind="decomposition_pattern",
                category_id="noun.reflex_procedure",
                store="learn.decomposition",
            )
        ],
        trigger=Trigger(on_demand=True, new_observations=100),
        budget=Budget(max_observations=5000, max_runtime_seconds=600, max_output_bytes=262144),
        evaluation=Evaluation(
            baseline="The planner's current decomposition.",
            metric="Parent acceptance, total child attempts and wall time.",
            method="Compare parents by pattern within a category. " + KEEP,
            min_evidence=20,
        ),
        verb=VerbTemplate(kind="verb.decompose_dag", deterministic=True),
        limitations=["Needs a task_graph_observation read (parent, children, waves)."],
    ),
    LearnerSpec(
        learner="preference",
        strategy="pairwise-choice",
        version=1,
        status="proposed",
        summary="Taste from explicit choices, such as palette picks: a pairwise model of what a "
        "person or team chose over what, kept private to them.",
        reads=["choice_observation"],
        writes=[
            Output(kind="preference_profile", category_id="noun.eval_run", store="learn.preference")
        ],
        trigger=Trigger(on_demand=True, new_observations=20),
        budget=Budget(max_observations=2000, max_runtime_seconds=120, max_output_bytes=65536),
        evaluation=Evaluation(
            baseline="No preference model.",
            metric="Agreement with held-out later choices.",
            method="Predict held-out pairs. " + KEEP,
            min_evidence=40,
        ),
        consumers=Consumers(visibility_scope="private"),
        verb=VerbTemplate(kind="verb.situate", deterministic=True),
        limitations=["Choices must be recorded deliberately by a surface; none are inferred."],
    ),
    LearnerSpec(
        learner="training_export",
        strategy="rights-cleared-export",
        version=1,
        status="proposed",
        summary="Turns opted-in, accepted, verified, rights-cleared runs into dataset manifest "
        "nouns compatible with the fine-tuning example contracts: family splits, holdout "
        "separation and dedup by fingerprint.",
        reads=["execution_observation", "execution_run"],
        writes=[
            Output(
                kind="dataset_manifest", category_id="noun.benchmark_suite", store="learn.datasets"
            )
        ],
        trigger=Trigger(on_demand=True),
        budget=Budget(max_observations=20000, max_runtime_seconds=900, max_output_bytes=1048576),
        evaluation=Evaluation(
            baseline="The unchanged base model.",
            metric="Held-out task metrics of each adapter against the unchanged base.",
            method="Downstream curate, train, evaluate and promote verbs on frozen holdouts. "
            + KEEP,
            min_evidence=100,
        ),
        verb=VerbTemplate(kind="verb.solve_draft", deterministic=True),
        limitations=[
            "Excludes raw Muse API outputs by default; provider terms restrict training "
            "competing models on them.",
            "Opt-in per run with provenance and rights evidence; reports are not a corpus.",
        ],
    ),
)


def spec(learner: str, strategy: str, version: int) -> LearnerSpec:
    for item in SPECS:
        if (item.learner, item.strategy, item.version) == (learner, strategy, version):
            return item
    raise KeyError(f"no learner spec {learner}:{strategy}:v{version}")
