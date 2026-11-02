# Opportunity Lab

Opportunity Lab turns eleven startup themes into versioned input nouns and
claimable ideation verbs. It is an example showcase for worker-generated
alternatives, not a set of validated businesses or an endorsement by YC.
The source is the [YC Requests for Startups page](https://www.ycombinator.com/rfs),
observed October 29, 2026. The pack does not independently verify the source's
numerical or regulatory claims.

The [seed file](../examples/showcase/opportunity-lab.json) contains eleven
themes, seven procedure descriptions, evaluation dimensions and boundaries.
The [authoring tool](../tools/showcase_seed.py) writes 21 private, versioned nouns
and, by default, 22 initial verbs: two independent attempts at each theme.
One attempt asks for a practical approach; the other invites a materially
different or unconventional approach.

| Procedure noun | Worker outcome |
| --- | --- |
| discover | Source notes separating primary evidence from claims |
| ideate | Alternative proposals with assumptions, nouns, verbs and a falsifiable test |
| remix | A new proposal with explicit parent references and meaningful changes |
| evaluate | Dimension scores with reasons and unknowns left unknown |
| design_benchmark | Comparable tasks, baseline, hardware and harness context |
| propose_seeds | Candidate nouns and verbs for a later authorized run |
| map_system | A proposed dependency graph connecting artifacts and work |

The initial authoring command enqueues only ideation. The other procedures are
reusable instruction nouns; they do not activate a scheduler or recursively
enqueue work. An authorized worker can propose follow-up verbs for review.
The supplied call and candidate allowances describe this small showcase, not
platform-wide limits. It imposes no response-token ceiling on a harness.

## Try the authoring tool

```bash
# Offline preview: no key or network required.
uv run python tools/showcase_seed.py

# Existing private namespace/queue and surface-bound worker grant required.
# Supply TESSERA_URL and TESSERA_KEY through your approved secret mechanism.
uv run python tools/showcase_seed.py --namespace conformance-showcase \
  --queue conformance-showcase --variants 2 --enqueue
```

The operator must define the queue and issue the grant first. The grant needs
the namespace, queue, `verb.solve_draft`, internal-write effects, noun storage,
claim/submission access and the producer's surface identity. Quarantined briefs
require that surface identity; a private owner key alone is insufficient.

Repeated authoring reuses the same noun versions and task identities. It never
overwrites a changed seed. Use a new versioned seed namespace for changed inputs.
This example accepts one to four explicitly requested variants per theme; that
CLI choice is not a fabric limit on how many alternatives a project may create.

## Preserve competing solutions

Two workers should be able to explore the same brief. Each attempt references
the same input hashes but has its own task identity, lease and output key.
Workers write to `showcase.candidates/<actual task_id>`, read the noun back and
submit its exact reference with an execution report. A later comparison or remix
verb can inspect both artifacts. Retrying the same submission is idempotent;
it does not erase another attempt's solution.

The output contract accepts proposals only. `--check-output result.json`
checks structure and theme membership; it does not verify novelty, usefulness,
evidence or safety. Ratings use separate dimensions with anchored reasons;
unknown measurements remain null. A common score cannot replace a matched
benchmark with recorded hardware, model, harness, inputs and baseline.

Reasoning runs in the authorized worker. The server stores nouns and coordinates
verbs. Public admission, deployment, real customer decisions and external effects
need their own authorization and evidence. The example pack activates none of
those actions.
