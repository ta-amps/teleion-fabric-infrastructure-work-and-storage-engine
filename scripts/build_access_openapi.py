#!/usr/bin/env python3
"""Generate the shared access OpenAPI subset from actual Python route and model schemas."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from tessera.access import ServiceSettings
from tessera.service import create_app
from tessera.store import Store

PATHS = {
    "/v1/executions/recall",
    "/v1/tasks/{task_id}/executions",
    "/v1/executions/{run_id}",
    "/v1/session/anonymous",
    "/v1/me",
    "/v1/me/audit",
    "/v1/admin/audit",
    "/v1/public/activity",
    "/v1/keys",
    "/v1/keys/{key_id}/revoke",
    "/v1/verbs",
    "/v1/claims/{task_id}/events",
    "/v1/tasks/claim",
    "/v1/tasks/heartbeat",
    "/v1/tasks/submit",
    "/v1/tasks/cancel",
    "/v1/stores/{store_name}/records/{record_key}",
}


def generate() -> str:
    with tempfile.TemporaryDirectory(prefix="tessera-schema-") as directory:
        store = Store(Path(directory) / "schema.db")
        try:
            schema = create_app(store, ServiceSettings()).openapi()
            schema["info"]["title"] = "Tessera external agent access"
            schema["info"]["description"] = (
                "Shared tracked anonymous and keyed HTTP endpoints. Anonymous sessions auto-route to their private demo namespace. Discovery and MCP descriptors are available at /connect.json and /mcp."
            )
            schema["paths"] = {
                path: operations for path, operations in schema["paths"].items() if path in PATHS
            }
            # Remove unreferenced components so unrelated Python-only routes are not advertised.
            components = schema["components"]["schemas"]
            needed = set()
            pending = [schema["paths"]]
            while pending:
                text = json.dumps(pending.pop())
                for name, component in components.items():
                    if name not in needed and f'"#/components/schemas/{name}"' in text:
                        needed.add(name)
                        pending.append(component)
            schema["components"]["schemas"] = {name: components[name] for name in sorted(needed)}
            return json.dumps(schema, indent=2, sort_keys=True) + "\n"
        finally:
            store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = Path(__file__).resolve().parents[1] / "deploy/cloudflare/access_openapi.json"
    generated = generate()
    if args.check:
        if not path.exists() or path.read_text() != generated:
            print("access_openapi.json is stale; run uv run python scripts/build_access_openapi.py")
            return 1
        print("Access OpenAPI matches the route/model contract.")
        return 0
    path.write_text(generated)
    print("Generated deploy/cloudflare/access_openapi.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
