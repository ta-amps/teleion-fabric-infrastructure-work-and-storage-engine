"""Schema-perturbation augmentation: teach reading the offered schema, not memorizing names.

Dev failures after round 1 were mostly argument naming on unseen tools (the query text
placed in `category` or `tag`, look-alike tools swapped). These training rows take
training-family requests and offer renamed variants of the training tools: new tool
names and argument names, same descriptions and types. The label uses the renamed
names, so the only way to be right is to read the schema in the prompt. Aliases are
disjoint from every served tool and argument name, so no dev or test name is taught,
and evaluation still uses the real definitions only.
"""

from __future__ import annotations

import re
from typing import Any

from .task import Draw, generate
from .tools import FAMILY_OF, FAMILY_SPLIT, canonical_arguments, validate

AUGMENT_VERSION = 1
TOOL_ALIASES = {
    "claim": ("take_next_job", "lease_next_job"),
    "heartbeat": ("renew_lease", "keep_job_alive"),
    "submit": ("deliver_result", "finish_job"),
    "park": ("pause_job", "hold_job"),
    "resume": ("continue_job", "unpause_job"),
    "split": ("fan_out_job", "divide_job"),
    "cancel": ("abort_job", "stop_job"),
    "queues": ("show_lanes", "my_lanes"),
    "enqueue": ("add_job", "schedule_job"),
    "list_verbs": ("show_jobs", "browse_jobs"),
    "claim_events": ("job_history", "lease_log"),
    "list_stores": ("show_collections", "list_collections"),
    "list_records": ("browse_entries", "list_entries"),
    "put_record": ("save_entry", "write_entry"),
    "put_records": ("save_entries", "write_entry_batch"),
    "get_record": ("read_entry", "fetch_entry"),
    "record_history": ("entry_versions", "entry_log"),
    "open_anonymous_session": ("start_guest_session", "open_trial_session"),
    "explain_access_modes": ("describe_access", "access_help"),
    "whoami": ("current_identity", "who_is_calling"),
    "my_audit": ("personal_log", "my_activity"),
    "admin_audit": ("operator_log", "staff_activity"),
    "handshake": ("negotiate_client", "connect_client"),
}
ARG_ALIASES = {
    "queue": ("lane", "channel"),
    "worker": ("agent_name", "runner"),
    "kinds": ("job_types", "allowed_types"),
    "effect_classes": ("effect_levels", "permitted_effects"),
    "task_id": ("job_id", "work_id"),
    "fencing_token": ("lease_number", "fence"),
    "output": ("result", "deliverable"),
    "idempotency_key": ("dedupe_key", "retry_key"),
    "ok": ("succeeded", "success"),
    "error": ("failure_reason", "problem"),
    "execution_report": ("run_report", "telemetry"),
    "reason": ("why", "note_text"),
    "resume_within": ("wake_after_seconds", "timeout_seconds"),
    "input": ("answer", "provided_input"),
    "park": ("pause_number", "hold_index"),
    "children": ("subjobs", "parts"),
    "brief": ("spec", "job_spec"),
    "priority": ("urgency", "rank"),
    "depends_on": ("after_jobs", "prerequisites"),
    "kind": ("job_type", "verb_kind"),
    "effect_class": ("effect_level", "impact"),
    "visibility_scope": ("visibility", "audience"),
    "tags": ("labels", "markers"),
    "status": ("state", "phase"),
    "limit": ("max_results", "page_size"),
    "after": ("since", "cursor"),
    "store_name": ("collection", "store_label"),
    "prefix": ("starts_with", "key_prefix"),
    "include_restricted": ("show_hidden", "with_restricted"),
    "tag": ("label", "marker"),
    "category": ("kind_filter", "class_filter"),
    "key": ("entry_key", "record_id"),
    "value": ("data", "body"),
    "expected_version": ("if_version", "base_version"),
    "writes": ("entries", "batch_items"),
    "version": ("revision", "at_revision"),
    "agent_label": ("display_name", "session_label"),
    "surface_id": ("client_surface", "surface_name"),
    "requested_queues": ("wanted_lanes", "lane_list"),
    "requested_stores": ("wanted_collections", "collection_list"),
    "requested_max_chars": ("char_budget", "max_characters"),
    "protocol_version": ("wire_version",),
    "surface_version": ("client_version",),
    "q": ("text_query",),
    "cohort": ("audience_group",),
    "surface": ("client",),
    "producer_surface": ("origin_client",),
    "allowed_surfaces": ("permitted_clients",),
    "target_cohorts": ("audiences",),
    "owner_key_id": ("owner_id",),
}


def check_aliases(tools: dict[str, Any]) -> None:
    """Aliases never reuse a served tool or argument name, so no held-out name is taught."""
    served = set(tools) | {
        p for t in tools.values() for p in t["inputSchema"].get("properties", {})
    }
    aliases = {a for names in (*TOOL_ALIASES.values(), *ARG_ALIASES.values()) for a in names}
    if aliases & served:
        raise ValueError(f"aliases reuse served names: {sorted(aliases & served)}")
    missing = {
        p for t in TOOL_ALIASES for p in tools[t]["inputSchema"].get("properties", {})
    } - set(ARG_ALIASES)
    if missing or set(TOOL_ALIASES) != {t for t in tools if FAMILY_SPLIT[FAMILY_OF[t]] == "train"}:
        raise ValueError(f"every training tool and argument needs an alias: {sorted(missing)}")


def renamed(tool: dict[str, Any], name: str, argmap: dict[str, str]) -> dict[str, Any]:
    schema = tool["inputSchema"]
    new_schema = {
        **schema,
        "properties": {argmap[p]: v for p, v in schema.get("properties", {}).items()},
    }
    if "required" in schema:
        new_schema["required"] = [argmap[p] for p in schema["required"]]
    description = tool["description"]
    for old, new in argmap.items():
        description = re.sub(rf"\b{re.escape(old)}\b", new, description)
    return {"name": name, "description": description, "inputSchema": new_schema}


def perturb(row: dict[str, Any], d: Draw, tools: dict[str, Any]) -> dict[str, Any]:
    names = {t: d.choice(TOOL_ALIASES[t]) for t in row["offered"]}
    overrides, gold_map = {}, {}
    for tool in row["offered"]:
        props = tools[tool]["inputSchema"].get("properties", {})
        argmap = {p: d.choice(ARG_ALIASES[p]) for p in props}
        overrides[names[tool]] = renamed(tools[tool], names[tool], argmap)
        if tool == row["tool"]:
            gold_map = argmap
    target = row["target"]
    if "refusal" not in target:
        schema = overrides[names[row["tool"]]]["inputSchema"]
        arguments = canonical_arguments(
            {gold_map[k]: v for k, v in target["arguments"].items()}, schema
        )
        if validate(arguments, schema):
            raise ValueError(f"{row['id']}: renamed label violates the renamed schema")
        target = {"tool": names[row["tool"]], "arguments": arguments}
    return {
        **row,
        "offered": [names[t] for t in row["offered"]],
        "tool_overrides": overrides,
        "target": target,
        "features": sorted({*row["features"], "augmented:schema"}),
    }


def generate_augmented(count: int, batch: str, tools: dict[str, Any]) -> list[dict[str, Any]]:
    check_aliases(tools)
    rows = generate("train", count, batch, "augmented", tools)
    return [
        perturb(row, Draw("perturb", AUGMENT_VERSION, batch, i), tools)
        for i, row in enumerate(rows)
    ]
