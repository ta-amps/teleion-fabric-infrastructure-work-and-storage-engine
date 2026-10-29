"""Repository-authored request templates for typed Tessera tool calls (MIT, no model text).

`{slot}` in a text names a value drawn by `task.py`; an argument written "$slot" takes
that value, anything else is a constant. Optional clauses add optional arguments. A
template with `missing` omits that required argument, so its label is a refusal.
Templates belong to their tool's family, so whole families and templates stay in one split.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Clause:
    text: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Template:
    tool: str
    tid: str
    text: str
    args: dict[str, Any]
    optional: tuple[Clause, ...] = field(default=())
    missing: str | None = None


def T(tool: str, tid: str, text: str, args: dict[str, Any], *optional: Clause) -> Template:
    return Template(tool, f"{tool}.{tid}", text, args, optional)


def M(tool: str, tid: str, text: str, args: dict[str, Any], missing: str) -> Template:
    return Template(tool, f"{tool}.{tid}", text, args, (), missing)


C = Clause
TASK = {"task_id": "$task_id"}
LEASE = {"task_id": "$task_id", "fencing_token": "$fencing_token"}
PRIVATE = C(" Keep it private.", {"visibility_scope": "private"})
RESTRICTED = C(" Include restricted items.", {"include_restricted": True})

# fmt: off
TEMPLATES: tuple[Template, ...] = (
    # ---- lease (train) ----
    T("claim", "a", "Claim the next ready task in queue {queue} as worker {worker}.",
      {"queue": "$queue", "worker": "$worker"},
      C(" Only take kinds {kinds}.", {"kinds": "$kinds"}),
      C(" Only effect classes {effects}.", {"effect_classes": "$effects"})),
    T("claim", "b", "Lease a task from {queue}; my worker name is {worker}.",
      {"queue": "$queue", "worker": "$worker"},
      C(" Only take kinds {kinds}.", {"kinds": "$kinds"})),
    M("claim", "m", "Claim the next ready task in queue {queue}.", {"queue": "$queue"}, "worker"),
    T("heartbeat", "a", "Extend my lease on task {task_id}; the fencing token is {fencing_token}.", LEASE),
    T("heartbeat", "b", "Send a heartbeat for task {task_id} with fencing token {fencing_token}.", LEASE),
    T("heartbeat", "c", "Keep task {task_id} alive, fencing token {fencing_token}.", LEASE),
    M("heartbeat", "m", "Send a heartbeat for task {task_id}.", TASK, "fencing_token"),
    T("submit", "a", "Submit the result of task {task_id} (fencing token {fencing_token}) with output {output} and idempotency key {idem}.",
      {**LEASE, "output": "$output", "idempotency_key": "$idem"},
      C(' Mark it as failed with error "{error}".', {"ok": False, "error": "$error"}),
      C(" Attach the execution report {report}.", {"execution_report": "$report"})),
    T("submit", "b", "Finish task {task_id} using fencing token {fencing_token}: the output is {output}; use idempotency key {idem}.",
      {**LEASE, "output": "$output", "idempotency_key": "$idem"},
      C(" Attach the execution report {report}.", {"execution_report": "$report"})),
    M("submit", "m", "Submit the result of task {task_id} with output {output} and idempotency key {idem}.",
      {"task_id": "$task_id", "output": "$output", "idempotency_key": "$idem"}, "fencing_token"),
    T("park", "a", 'Park task {task_id}, fencing token {fencing_token}, with reason "{reason}".',
      {**LEASE, "reason": "$reason"},
      C(" Bring it back within {seconds} seconds.", {"resume_within": "$seconds"})),
    T("park", "b", 'Pause task {task_id} with fencing token {fencing_token} until outside input arrives; reason: "{reason}".',
      {**LEASE, "reason": "$reason"}),
    T("resume", "a", "Resume parked task {task_id} with input {input}.",
      {"task_id": "$task_id", "input": "$input"},
      C(" This answers park number {park}.", {"park": "$park"})),
    T("resume", "b", "Give the parked task {task_id} the input it is waiting for: {input}.",
      {"task_id": "$task_id", "input": "$input"}),
    M("resume", "m", "Resume the parked task with input {input}.", {"input": "$input"}, "task_id"),
    T("split", "a", "Split task {task_id} (fencing token {fencing_token}) into children {children}.",
      {**LEASE, "children": "$children"}),
    T("split", "b", "Break the task I hold, {task_id} with fencing token {fencing_token}, into these child tasks: {children}.",
      {**LEASE, "children": "$children"}),
    T("cancel", "a", "Cancel task {task_id}.", TASK, C(' Reason: "{reason}".', {"reason": "$reason"})),
    T("cancel", "b", "Stop task {task_id} from running.", TASK),
    # ---- verb discovery (train) ----
    T("queues", "a", "Which queues can this key use?", {}),
    T("queues", "b", "List my queues with their lease settings.", {}),
    T("enqueue", "a", "Enqueue a task in queue {queue} with brief {brief}.",
      {"queue": "$queue", "brief": "$brief"},
      C(" Set priority {priority}.", {"priority": "$priority"}),
      C(" It depends on tasks {deps}.", {"depends_on": "$deps"}),
      C(" Use kind {kind}.", {"kind": "$kind"}),
      PRIVATE),
    T("enqueue", "b", "Add a new verb to {queue}; its brief is {brief}.",
      {"queue": "$queue", "brief": "$brief"},
      C(" Its effect class is read.", {"effect_class": "read"}),
      C(" Tag it {tags}.", {"tags": "$tags"}),
      C(" Use task id {task_id}.", {"task_id": "$task_id"})),
    M("enqueue", "m", "Enqueue a task with brief {brief}.", {"brief": "$brief"}, "queue"),
    T("list_verbs", "a", "List the verbs visible to me.", {},
      C(" Only in queue {queue}.", {"queue": "$queue"}),
      C(" Only those that are {status}.", {"status": "$status"}),
      C(" At most {limit}.", {"limit": "$limit"})),
    T("claim_events", "a", "Show the claim events for task {task_id}.", TASK,
      C(" Only events after {after}.", {"after": "$after"})),
    T("claim_events", "b", "What claim history does task {task_id} have?", TASK),
    # ---- records (train) ----
    T("list_stores", "a", "List every noun store with its record count.", {}),
    T("list_stores", "b", "Which record stores exist?", {}),
    T("list_records", "a", "List the records in store {store}.", {"store_name": "$store"},
      C(" Only keys starting with {prefix}.", {"prefix": "$prefix"}),
      C(" Return at most {limit}.", {"limit": "$limit"}),
      RESTRICTED),
    T("list_records", "b", "Show the current records of noun store {store}.", {"store_name": "$store"},
      C(" Only those tagged {tag}.", {"tag": "$tag"}),
      C(" Only category {category}.", {"category": "$category"})),
    M("list_records", "m", "List the records whose keys start with {prefix}.", {"prefix": "$prefix"}, "store_name"),
    T("put_record", "a", "Write a record to store {store} under key {key} with value {value}.",
      {"store_name": "$store", "key": "$key", "value": "$value"},
      C(" Only if the current version is {version}.", {"expected_version": "$version"}),
      PRIVATE),
    T("put_record", "b", "Save {value} as the record {key} in noun store {store}.",
      {"store_name": "$store", "key": "$key", "value": "$value"},
      C(" Tag it {tags}.", {"tags": "$tags"})),
    M("put_record", "m", "Write the value {value} to store {store}.", {"store_name": "$store", "value": "$value"}, "key"),
    T("put_records", "a", "Write these records in one batch: {writes}.", {"writes": "$writes"}),
    T("put_records", "b", "Atomically save this batch of noun writes: {writes}.", {"writes": "$writes"}),
    T("get_record", "a", "Read record {key} from store {store}.", {"store_name": "$store", "key": "$key"},
      C(" I need version {version}.", {"version": "$version"})),
    T("get_record", "b", "Fetch the latest version of {key} in noun store {store}.", {"store_name": "$store", "key": "$key"}),
    M("get_record", "m", "Read the record I wrote earlier to store {store}.", {"store_name": "$store"}, "key"),
    T("record_history", "a", "Show every saved version of record {key} in store {store}.", {"store_name": "$store", "key": "$key"}),
    T("record_history", "b", "List the version history of {key} in noun store {store}.", {"store_name": "$store", "key": "$key"}),
    # ---- access (train) ----
    T("open_anonymous_session", "a", "Open an anonymous demo session.", {},
      C(" Label it {label}.", {"agent_label": "$label"})),
    T("open_anonymous_session", "b", "Start a temporary synthetic session labeled {label}.", {"agent_label": "$label"}),
    T("explain_access_modes", "a", "How can I connect, and what are the access limits?", {}),
    T("explain_access_modes", "b", "Explain the access modes.", {}),
    T("whoami", "a", "Who am I connected as?", {}),
    T("whoami", "b", "Show my key's identity and grants.", {}),
    T("my_audit", "a", "Show my own audit log.", {},
      C(" Entries after {after}.", {"after": "$after"}),
      C(" At most {limit} entries.", {"limit": "$limit"})),
    T("admin_audit", "a", "Show the operator audit log.", {},
      C(" Entries after {after}.", {"after": "$after"})),
    T("admin_audit", "b", "Read the admin audit trail.", {},
      C(" At most {limit} entries.", {"limit": "$limit"})),
    T("handshake", "a", "Negotiate a surface handshake for surface {surface_id}.", {"surface_id": "$surface_id"},
      C(" Request queues {queues}.", {"requested_queues": "$queues"}),
      C(" Ask for at most {max_chars} characters.", {"requested_max_chars": "$max_chars"})),
    T("handshake", "b", "Run the surface handshake as {surface_id}.", {"surface_id": "$surface_id"},
      C(" Request stores {stores}.", {"requested_stores": "$stores"})),
    M("handshake", "m", "Negotiate a surface handshake requesting queues {queues}.", {"requested_queues": "$queues"}, "surface_id"),
    # ---- executions (dev) ----
    T("recall_execution", "a", "Recall prior executions for task {task_id}, which I hold with fencing token {fencing_token}.", LEASE,
      C(" Return at most {limit} matches.", {"limit": "$limit"}),
      C(" Use the execution report {report} as context.", {"execution_report": "$report"})),
    T("recall_execution", "b", "Look up similar past runs for my task {task_id} (fencing token {fencing_token}).", LEASE),
    M("recall_execution", "m", "Recall similar past runs for my task {task_id}.", TASK, "fencing_token"),
    T("get_execution", "a", "Read the retained execution {run_id}.", {"run_id": "$run_id"}),
    T("get_execution", "b", "Fetch execution run {run_id}.", {"run_id": "$run_id"}),
    M("get_execution", "m", "Read a retained execution run.", {}, "run_id"),
    T("list_executions", "a", "List the execution references for task {task_id}.", TASK,
      C(" Start after fencing token {after}.", {"after": "$after"}),
      C(" At most {limit} references.", {"limit": "$limit"})),
    T("list_executions", "b", "Which runs were retained for task {task_id}?", TASK),
    # ---- discovery (dev) ----
    T("search_nouns", "a", 'Search the nouns for "{query}".', {"query": "$query"},
      C(" Only in store {store}.", {"store_name": "$store"}),
      C(" At most {limit} results.", {"limit": "$limit"}),
      RESTRICTED),
    T("search_nouns", "b", 'Find reusable nouns about "{query}".', {"query": "$query"},
      C(" Only in bucket {bucket}.", {"bucket": "$bucket"}),
      C(" Only category {category}.", {"category": "$category"})),
    T("match_reflex", "a", 'Check for a reflex recipe or known dead end matching "{query}".', {"query": "$query"},
      C(" Return at most {limit}.", {"limit": "$limit"})),
    T("match_reflex", "b", 'Before solving, match reflexes for "{query}".', {"query": "$query"},
      C(" Only tag {tag}.", {"tag": "$tag"})),
    M("match_reflex", "m", "Check for a reflex recipe or known dead end.", {}, "query"),
    # ---- buckets (test) ----
    T("define_bucket", "a", "Create a file bucket named {bucket}.", {"name": "$bucket"},
      C(' Describe it as "{description}".', {"description": "$description"}),
      PRIVATE),
    T("define_bucket", "b", "Set up a persistent bucket called {bucket}.", {"name": "$bucket"},
      C(" Tag it {tags}.", {"tags": "$tags"})),
    T("buckets", "a", "List the file buckets with their object counts.", {}),
    T("buckets", "b", "Which buckets exist, and how big are they?", {}),
    T("put_object", "a", 'Store the text "{content}" in bucket {bucket} at key {object_key}.',
      {"bucket": "$bucket", "key": "$object_key", "content": "$content"},
      C(" Its content type is {ctype}.", {"content_type": "$ctype"}),
      C(" Add metadata {metadata}.", {"metadata": "$metadata"})),
    T("put_object", "b", 'Upload a file to {bucket} under {object_key} containing "{content}".',
      {"bucket": "$bucket", "key": "$object_key", "content": "$content"},
      C(" Only if the current version is {version}.", {"expected_version": "$version"}),
      C(" Add metadata {metadata}.", {"metadata": "$metadata"})),
    M("put_object", "m", 'Store the text "{content}" in bucket {bucket}.', {"bucket": "$bucket", "content": "$content"}, "key"),
    T("get_object", "a", "Read {object_key} from bucket {bucket}.", {"bucket": "$bucket", "key": "$object_key"},
      C(" I need version {version}.", {"version": "$version"}),
      C(" Return it as base64.", {"encoding": "base64"})),
    T("get_object", "b", "Fetch the file {object_key} in bucket {bucket}.", {"bucket": "$bucket", "key": "$object_key"},
      C(" Start at byte {offset}.", {"offset": "$offset"}),
      C(" Read at most {max_bytes} bytes.", {"max_bytes": "$max_bytes"})),
    M("get_object", "m", "Read the file I uploaded earlier to bucket {bucket}.", {"bucket": "$bucket"}, "key"),
    T("list_objects", "a", "List the files in bucket {bucket}.", {"bucket": "$bucket"},
      C(" Only keys starting with {prefix}.", {"prefix": "$prefix"}),
      C(" At most {limit}.", {"limit": "$limit"})),
    T("list_objects", "b", "Show the current objects stored in {bucket}.", {"bucket": "$bucket"},
      RESTRICTED),
    T("object_history", "a", "Show every saved version of {object_key} in bucket {bucket}.", {"bucket": "$bucket", "key": "$object_key"}),
    T("object_history", "b", "List the version history of the file {object_key} in {bucket}.", {"bucket": "$bucket", "key": "$object_key"}),
    # ---- feeds (test) ----
    T("list_feeds", "a", "Which live delta feeds exist?", {}),
    T("list_feeds", "b", "List the swarm feeds.", {}),
    T("poll_feed", "a", "Read new entries from feed {feed}.", {"feed": "$feed"},
      C(" Only entries after sequence {after}.", {"after": "$after"}),
      C(" At most {limit} entries.", {"limit": "$limit"})),
    T("poll_feed", "b", "Poll the live feed {feed}.", {"feed": "$feed"}, RESTRICTED),
    M("poll_feed", "m", "Poll for entries after sequence {after}.", {"after": "$after"}, "feed"),
    T("publish_feed", "a", "Publish to feed {feed} the payload {payload}.", {"feed": "$feed", "payload": "$payload"},
      C(' Summary: "{summary}".', {"summary": "$summary"}),
      C(" Use kind {fkind}.", {"kind": "$fkind"})),
    T("publish_feed", "b", "Add a delta entry to the swarm feed {feed} with payload {payload}.", {"feed": "$feed", "payload": "$payload"},
      C(" Tag it {tags}.", {"tags": "$tags"})),
    M("publish_feed", "m", "Publish the payload {payload}.", {"payload": "$payload"}, "feed"),
)
# fmt: on

# Operations no Tessera tool performs. Each split has its own pool.
UNSUPPORTED: dict[str, tuple[tuple[str, str], ...]] = {
    "train": (
        ("delete_record", "Delete record {key} from store {store}."),
        ("rename_queue", "Rename queue {queue} to {queue2}."),
        ("email_summary", "Email the latest summary to the review team."),
        ("lease_time", "Change the lease time of queue {queue} to 900 seconds."),
        ("revoke_key", "Revoke the key used by worker {worker}."),
        ("deploy", "Deploy the edge worker to production."),
    ),
    "dev": (
        ("undo_write", "Undo the last write to store {store}."),
        ("reset_password", "Reset my password."),
        ("restart", "Restart the Tessera service."),
    ),
    "test": (
        ("delete_object", "Delete the file {object_key} from bucket {bucket}."),
        ("rename_bucket", "Rename bucket {bucket} to {bucket2}."),
        ("text_digest", "Send the feed digest to my phone."),
        ("translate", "Translate the bucket description into French."),
        ("merge_feeds", "Merge feeds {feed} and {feed2}."),
    ),
}

# Phrasing frames wrap a request sentence; each split has its own frames.
FRAMES: dict[str, tuple[str, ...]] = {
    "train": (
        "{s}",
        "Please {lc}",
        "Next step: {s}",
        "Working through the queue now. {s}",
        "Agent request: {s}",
    ),
    "dev": ("Could you {lc}", "Quick one: {s}"),
    "test": ("Hey, {lc}", "For the next step, {lc}"),
}

# Tools that can stand in for one another; a no-matching-tool case excludes the whole group.
SUBSTITUTES: dict[str, frozenset[str]] = {
    "put_record": frozenset({"put_records"}),
    "put_records": frozenset({"put_record"}),
    "list_records": frozenset({"search_nouns"}),
    "list_objects": frozenset({"search_nouns"}),
    "search_nouns": frozenset({"list_records", "list_objects", "match_reflex"}),
    "match_reflex": frozenset({"search_nouns"}),
}

# fmt: off
VOCAB: dict[str, tuple[str, ...]] = {
    "queue": ("ingest", "review", "triage", "render", "nightly", "eval-arms", "summaries", "repairs", "pack-checks"),
    "worker": ("worker-ada", "gpu-worker-2", "pi-harness", "runner-7", "local-cpu", "reviewer-3", "atlas-worker"),
    "store": ("pack.candidates", "demo.artifacts", "eval.scores", "notes.daily", "registry.feeds", "tool.cards", "sources.index"),
    "key": ("SYNTHETIC-addition-v1", "profile.default", "run-summary-17", "card.mcp-door", "seed.weather-alerts", "recipe.digest-v2", "note.alpha"),
    "bucket": ("reports", "media-drafts", "datasets", "exports", "captions", "fixtures", "transcripts"),
    "object_key": ("summaries/day-12.md", "fixtures/case-04.json", "captions/scene-3.vtt", "exports/scores.csv", "notes/readme.txt", "drafts/plan-v2.md"),
    "feed": ("deltas.reviews", "alerts.sources", "swarm.progress", "queue.events", "notes.changes"),
    "surface_id": ("status-board", "docs-portal", "cli-demo", "review-console", "pack-browser"),
    "query": ("typed integer addition", "caption timing drift", "feed etag mismatch", "duplicate submit replay", "stale lease recovery", "csv header repair"),
    "reason": ("waiting for a reviewer", "needs the owner's approval", "blocked on a missing source file", "waiting for a rate limit reset"),
    "goal": ("Summarize the review notes", "Check the fixture hashes", "Draft a caption for scene 3", "Compare two scoring runs", "List stale sources", "Repair the CSV header"),
    "title": ("Daily digest", "Fixture audit", "Caption draft", "Score table", "Source list"),
    "label": ("demo-agent", "pilot-run", "eval-probe", "notebook-test"),
    "prefix": ("SYNTHETIC-", "run-", "card.", "seed.", "drafts/", "fixtures/"),
    "tag": ("synthetic", "priority-high", "domain-arithmetic", "status-proposal", "lang-en"),
    "category": ("noun.code_node", "noun.eval_run", "noun.benchmark_suite", "noun.model_spec"),
    "kind": ("verb.solve_draft", "verb.verify_gate", "verb.benchmark_harness", "verb.ingest"),
    "effect": ("read", "write-internal", "write-repo"),
    "status": ("ready", "leased", "waiting", "done", "dead", "cancelled"),
    "error": ("schema check failed", "source file missing", "timeout after retries"),
    "summary": ("Two records changed", "New source verified", "Review finished", "Scores updated"),
    "description": ("Weekly report drafts", "Synthetic fixtures for tests", "Caption files for the demo"),
    "content": ("hello from the fixture", "row,count\nalpha,3", "Draft notes for review", "scene 3 caption"),
    "ctype": ("text/markdown", "text/csv", "application/json"),
    "fkind": ("status", "alert", "summary"),
    "event": ("noun_updated", "verb_done", "source_changed"),
    "note": ("looks good", "use the second source", "approved with edits"),
}
# fmt: on
