"""Tessera's real MCP tool definitions, their families, and a strict schema checker.

The snapshot `mcp_tools.json` is exactly what the hosted `tools/list` returns
(`[...TOOLS, ...ACCESS_TOOLS]` in deploy/cloudflare). `export_snapshot` regenerates it
with Node; tests compare it with the live JavaScript and the Python MCP door. When the
tool surface changes, the benchmark built from it is a new version, never a silent edit.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from examples.finetuning.data import canonical, digest, file_digest

SNAPSHOT = Path(__file__).with_name("mcp_tools.json")
SNAPSHOT_FORMAT = "tessera.continuous-tuning.mcp-tools.v1"
SOURCES = ("deploy/cloudflare/cloud_mcp.js", "deploy/cloudflare/cloud_access_routes.js")
EXPORT_SCRIPT = """
const mcp = await import(process.argv[1]);
const access = await import(process.argv[2]);
process.stdout.write(JSON.stringify([...mcp.TOOLS, ...access.ACCESS_TOOLS]));
"""

# Families group tools by the noun or verb they act on. Whole families are held out, so
# dev and test measure generalization to tools the adapter never saw as targets.
FAMILIES: dict[str, tuple[str, ...]] = {
    "lease": ("claim", "heartbeat", "submit", "park", "resume", "split", "cancel"),
    "verb_discovery": ("queues", "enqueue", "list_verbs", "claim_events"),
    "records": (
        "list_stores",
        "list_records",
        "put_record",
        "put_records",
        "get_record",
        "record_history",
    ),
    "access": (
        "open_anonymous_session",
        "explain_access_modes",
        "whoami",
        "my_audit",
        "admin_audit",
        "handshake",
    ),
    "executions": ("recall_execution", "get_execution", "list_executions"),
    "discovery": ("search_nouns", "match_reflex"),
    "buckets": (
        "define_bucket",
        "buckets",
        "put_object",
        "get_object",
        "list_objects",
        "object_history",
    ),
    "feeds": ("list_feeds", "poll_feed", "publish_feed"),
}
FAMILY_SPLIT = {
    "lease": "train",
    "verb_discovery": "train",
    "records": "train",
    "access": "train",
    "executions": "dev",
    "discovery": "dev",
    "buckets": "test",
    "feeds": "test",
}
FAMILY_OF = {tool: family for family, tools in FAMILIES.items() for tool in tools}
JSON_TYPES = ("object", "array", "string", "integer", "number", "boolean", "null")


def export_snapshot(repo: Path) -> dict[str, Any]:
    """Read the served tool list through Node; a development-time step, never at runtime."""
    paths = [str((repo / source).resolve()) for source in SOURCES]
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", EXPORT_SCRIPT, *paths],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    tools = json.loads(completed.stdout)
    return {
        "format": SNAPSHOT_FORMAT,
        "served_by": "tools/list = [...TOOLS, ...ACCESS_TOOLS]",
        "source_files_sha256": {source: file_digest(repo / source) for source in SOURCES},
        "tools_sha256": digest(tools),
        "tools": tools,
    }


def write_snapshot(repo: Path, path: Path = SNAPSHOT) -> dict[str, Any]:
    snapshot = export_snapshot(repo)
    path.write_text(json.dumps(snapshot, indent=1, ensure_ascii=False) + "\n")
    return snapshot


def load_snapshot(path: Path = SNAPSHOT) -> dict[str, Any]:
    snapshot = json.loads(path.read_bytes())
    if snapshot.get("format") != SNAPSHOT_FORMAT:
        raise ValueError("unsupported tool snapshot")
    if digest(snapshot["tools"]) != snapshot["tools_sha256"]:
        raise ValueError("tool snapshot content differs from its recorded digest")
    names = [tool["name"] for tool in snapshot["tools"]]
    if len(set(names)) != len(names):
        raise ValueError("duplicate tool names in snapshot")
    if set(names) != set(FAMILY_OF):
        raise ValueError(
            "every served tool needs exactly one family; update FAMILIES and version the benchmark"
        )
    return snapshot


def load_tools(path: Path = SNAPSHOT) -> dict[str, dict[str, Any]]:
    return {tool["name"]: tool for tool in load_snapshot(path)["tools"]}


def tools_in_split(split: str) -> list[str]:
    return [tool for tool, family in FAMILY_OF.items() if FAMILY_SPLIT[family] == split]


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "integer" if value.is_integer() else "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise TypeError(f"not a JSON value: {type(value).__name__}")


def allowed_types(schema: dict[str, Any]) -> tuple[str, ...]:
    declared = schema.get("type")
    if declared is None:
        return JSON_TYPES  # `{}` accepts any JSON value, as for submit.output.
    return (declared,) if isinstance(declared, str) else tuple(declared)


def type_matches(value: Any, schema: dict[str, Any]) -> bool:
    actual = json_type(value)
    types = allowed_types(schema)
    return actual in types or (actual == "integer" and "number" in types)


def validate(value: Any, schema: dict[str, Any], path: str = "arguments") -> list[str]:
    """The JSON Schema subset these tools use: type, enum, required, properties,
    additionalProperties, items, minimum, maximum, maxLength. No coercion of any kind."""
    if not type_matches(value, schema):
        return [f"{path}: expected {'|'.join(allowed_types(schema))}, got {json_type(value)}"]
    errors = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if json_type(value) in ("integer", "number") and value is not None:
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        errors.append(f"{path}: longer than {schema['maxLength']}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(validate(item, schema["items"], f"{path}[{index}]"))
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        errors.extend(
            f"{path}: missing required {name!r}"
            for name in schema.get("required", [])
            if name not in value
        )
        for name, item in value.items():
            if name in properties:
                errors.extend(validate(item, properties[name], f"{path}.{name}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property {name!r}")
    return errors


def whole(value: Any) -> Any:
    """Numbers compare by value: 300 and 300.0 are the same argument."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [whole(item) for item in value]
    if isinstance(value, dict):
        return {key: whole(item) for key, item in value.items()}
    return value


def canonical_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Drop optional arguments that restate the declared default or pass null; keep the rest."""
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    kept = {}
    for name, value in arguments.items():
        declared = properties.get(name, {})
        if name not in required and (
            value is None or ("default" in declared and whole(value) == whole(declared["default"]))
        ):
            continue
        kept[name] = whole(value)
    return dict(sorted(kept.items()))


def render_tools(names: list[str], tools: dict[str, dict[str, Any]]) -> str:
    """Tool definitions exactly as served, one JSON object per line, in the offered order."""
    return "\n".join(canonical(tools[name]).decode() for name in names)
