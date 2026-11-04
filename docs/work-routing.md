# Route agent work through nouns and verbs

Represent a bounded action as a Teleion Fabric verb and its inputs, contract, artifacts
and evidence as versioned nouns. An external worker can discover an eligible task,
claim it, read only permitted context, work under a lease and submit references to
its results. Repository work is one use of this protocol; research, retrieval,
compilation, testing, clarification and human decisions can use the same fabric.

Teleion Fabric is infrastructure: it remains the queue and noun store. Eranos and one
overview site are the only public surfaces, and everything else is nouns and
verbs in seed packs. A Frontier seed/contract pack can supply
declarative gap briefs, templates and governed artifact references through the
existing APIs. An importer creates noun and task records; authorized workers
perform the verbs. The release catalog references governed Teleion Fabric artifacts, so
the seed repository needs no separate runtime, library database or scheduler.
Calling the compiler from the seed pack for composing checked code, or
Eranos verification, from a worker is an integration pattern unless the particular
adapter is implemented and tested. Their existence alone does not wire every task
through them.

## A small executable slice

```sh
uv run python tools/team_private_work.py
uv run pytest -q tests/test_work_routing.py tests/test_external_access_edge.py
```

The [example](../tools/team_private_work.py) creates temporary local state and calls
the actual HTTP handlers. It provisions separate requester and worker credentials
in `conformance-routing`, with a common authenticated surface and explicit queue,
kind, namespace and action grants. Tokens stay in memory. No provider or remote
service is called, and no public artifact is created.

The requester stores a typed `noun.skill_pack` contract and an input noun, then
enqueues `verb.solve_draft`. The worker reads pinned versions and checks hashes.
Missing input causes a park; a different key supplies a new input noun reference
and resumes the task. The worker splits two deterministic calculations, stores
child artifacts, submits their records and reclaims the parent to combine them.
The final artifact and self-check evidence are `noun.eval_run` records. The script
prints the measured result, checked boundaries and record hashes with a run
fingerprint. Its temporary database is removed when it exits.

This example uses a typed worker contract. It does not install a global schema
requirement on CAS, a new scheduler, a model execution service, or a general public
promotion gate. Generic CAS continues to accept arbitrary JSON. A worker that
understands this example's contract validates it explicitly before acting.

## Keep the task envelope separate from its contract

The existing `TaskSpec` supplies the routing and lifecycle fields:

```json
{
  "namespace": "team_example",
  "queue": "work.external",
  "kind": "verb.solve_draft",
  "effect_class": "write-internal",
  "producer_surface": "surface:team_example",
  "visibility_scope": "surface_only",
  "allowed_surfaces": [],
  "target_cohorts": ["all"],
  "tags": ["category:verb.solve_draft"],
  "idempotency_key": "work-request-unique-id",
  "brief": {
    "contract": "tessera.work.example.v1",
    "category_id": "verb.solve_draft",
    "contract_ref": {
      "store": "work.contracts",
      "key": "sum-squares",
      "version": 1,
      "sha256": "<SHA-256 of canonical noun value>"
    },
    "input_ref": {
      "store": "work.inputs",
      "key": "numbers",
      "version": 1,
      "sha256": "<SHA-256 of canonical noun value>"
    }
  }
}
```

`work.external` is an example queue name that an operator must provision. It is
not a new built-in endpoint or a model selection instruction. `kind` can use an
existing canonical verb category. The category and tags describe the work;
authenticated grants determine whether it can be claimed.

The contract noun describes input/output types, one bounded operation, acceptance
checks, optional shim references and the execution preference. In the example,
`execution.worker` is `external`, `server_fallback` is `false`, and shim mode is
`none`. These values are validated by the example worker, not treated as permission
to spend provider credits or as instructions to execute arbitrary code.

Pin `{store, key, version, sha256}` when work must be reproducible. Check the actual
bytes as well as the ACL on each read. A digest identifies content; it does not
make untrusted content safe. Submit compact artifact/evidence references rather
than copying large documents into every task result.

## Permissions for personal and team-private work

| Intended audience | Existing envelope | How a worker receives access |
| --- | --- | --- |
| One credential owner | `private` | The same owning key, with the required namespace/action/queue grants |
| Several workers in one private workspace | Dedicated namespace plus `surface_only` | Separate keys explicitly bound to that workspace's surface and namespace |
| Selected service surfaces | Dedicated namespace plus `allowlist_surfaces` | Explicit surface scopes matching the producer or allowlist, plus other required grants |
| Public reusable artifact | A workflow-authorized public catalog promotion | The applicable deterministic evidence and independent review gate |

`private` means owner-key-only when an owner is set. Sharing an account ID, label,
namespace, queue or `worker` string does not let another key read or claim it.
Use `surface_only` for team-private work rather than pretending an owner-private
task is delegated. Give each team worker a separate key; do not share a single
god key. Authorized operators retain administrative access.

For example, a worker can have only:

```json
{
  "queues": ["work.external"],
  "kinds": ["verb.solve_draft", "verb.verify_gate"],
  "effect_classes": ["read", "write-internal"],
  "scopes": [
    "namespace:team_example",
    "surface:team_example",
    "nouns:read",
    "cas:write",
    "verbs:claim",
    "verbs:submit"
  ]
}
```

Namespaces and surface scopes must come from credential issuance. Query filters,
cohorts, category tags, a caller-supplied `X-Surface-Id`, and display labels do not
grant those permissions. Python now matches the Worker: a label such as
`surface:team_example` cannot create a surface binding.
Operators migrating older Python keys must explicitly reissue the intended scope;
the service does not silently upgrade their grants.

Team members with `cas:write` may mutate non-private records produced by their
authorized surface, subject to CAS. This is a shared workspace boundary, not an
arbitrary per-person ACL system. Separate sensitive material into owner-private
nouns or another namespace/surface. A shared task should reference only nouns its
intended workers may read. Quarantined nouns retain additional restrictions.

Current issuance labels any surface-bound key as `principal_kind=service_binding`,
even when an external harness presents that key over HTTP. That audit classification
does not prove the request travelled over an internal Worker binding. Use the
recorded key/account IDs and actual grants; do not treat this field as network
attestation.

## Execution and review choices

| Work | Existing categories and primitives | Execution rule |
| --- | --- | --- |
| Retrieve, investigate or situate | `verb.probe_source`, `verb.fetch_feed`, `verb.situate` | An eligible external worker reads scoped input nouns |
| Draft or repair | `verb.solve_draft`, `verb.recover_fault` | Worker self-checks and stores candidate/evidence nouns |
| Break down large work | `verb.decompose_dag`, `split`, dependencies | Bound depth/attempts; retain parent permissions |
| Compile or deterministic QA | `verb.compile_dag`, `verb.verify_gate` | Run an implemented deterministic adapter with explicit records |
| Ask for missing input | `park`, input noun, `resume` | Another authorized principal supplies a precise answer |
| Additional independent judgment | A review queue with `verb.verify_gate` or an appropriate judgment category | A capable independent agent or human claims the review verb |
| Explicit private server work or exceptional fallback | Separate operator-provisioned server queue and scoped server worker | Require explicit policy, budget reservation and eligible credentials before any model call |

Default to external workers and deterministic server contract checks. Server-funded
reasoning is a bounded exception or explicitly requested private/server-only work.
An external worker cannot switch to a server queue merely by changing a tag,
contract field or model name. Give the server worker only its own queue grants;
the operator's dispatcher and provider budget gate must authorize the fallback.
The example denies the external key access to its server queue and makes no model
call. It does not add an automatic fallback dispatcher to Teleion Fabric.

An optional shim can provide a compact, caller-scoped view of allowed contract and
input nouns. Preserve their references and treat their text as data. A shim must
not broaden a grant, publish a candidate, or turn missing evidence into a pass.

Generic dependencies become eligible when upstream tasks terminate, including
failure or cancellation. A downstream worker must inspect `state`, `ok` and the
record before using an upstream result. Public Frontier promotion separately
requires successful stage records and distinct actors at storage. That gate is
not a model judge on every task or conversation turn.

The current HTTP boundary requires admin for `write-repo` and `public` effects;
do not grant admin to an outside worker simply to bypass that restriction. A
worker can prepare an internal artifact, then route any consequential application
or publication through the existing approved operator workflow.

See [design and evidence](work-design-and-evidence.md) for fencing, retries,
exactly-once limitations and common recovery paths, and
[publication contracts](frontier-publication.md) for the stronger release gate.
