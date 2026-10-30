"""Every agent-facing tool listing on the Python service matches what MCP tools/list serves.

The Cloudflare Worker has the same check against its own registry in
tests/workerd_agent_tool_listings.mjs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tessera import pages
from tessera.access import ServiceSettings
from tessera.service import create_app
from tessera.store import Store

COUNT = re.compile(r"\b(\d+) tools\b")
LABEL = re.compile(r"\btools(?:\s+total)?\)?:\s*(.+)$", re.IGNORECASE)
NAME = re.compile(r"[a-z][a-z0-9_-]*")
TOOL_LINE = "- MCP tools: "


def stated_counts(text: str) -> set[int]:
    return {int(n) for n in COUNT.findall(text)}


def labelled_tool_names(text: str) -> list[str]:
    """Names an agent reads as a tool list: comma-separated identifiers after a "... tools:" label."""
    names: list[str] = []
    for line in text.splitlines():
        label = LABEL.search(line)
        if label is None:
            continue
        items = [i.strip().strip("`") for i in re.sub(r"[.;]\s*$", "", label[1]).split(",")]
        if all(NAME.fullmatch(item) for item in items):
            names.extend(items)
    return names


def assert_lists_served(text: str, names: list[str]) -> None:
    assert stated_counts(text) == {len(names)}
    labelled = labelled_tool_names(text)
    assert [name for name in names if name not in labelled] == [], "omits a served tool"
    assert [name for name in labelled if name not in names] == [], "names an unserved tool"
    line = next(line for line in text.splitlines() if line.startswith(TOOL_LINE))
    assert line.removeprefix(TOOL_LINE).removesuffix(".").split(", ") == names


def test_python_pages_list_exactly_the_served_tools(tmp_path: Path) -> None:
    db_path = tmp_path / "listings.db"
    settings = ServiceSettings(db_path=str(db_path), reaper_seconds=3600)
    with TestClient(create_app(Store(db_path), settings)) as client:
        served: list[dict[str, Any]] = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        ).json()["result"]["tools"]
        names = [tool["name"] for tool in served]
        assert names
        assert len(set(names)) == len(names)

        assert_lists_served(client.get("/llms.txt").text, names)

        # llms-full.txt: the agent door and the generated reference, then (in a source checkout)
        # the repository's documents, whose dated prose is not a tool listing.
        door, found, rest = client.get("/llms-full.txt").text.partition("## MCP tool reference\n")
        assert found
        assert_lists_served(door, names)
        reference = rest.splitlines()[1 : 2 + len(served)]
        assert reference == [
            f"- `{tool['name']}`: {' '.join(tool['description'].split())}" for tool in served
        ] + [""]

        card = client.get("/.well-known/agent-card.json").json()
        assert card["tools"] == served
        assert [skill["id"] for skill in card["skills"]] == names
        assert [skill["description"] for skill in card["skills"]] == [
            tool["description"] for tool in served
        ]
        assert card["tool_count"] == len(names)

        assert stated_counts(client.get("/connect").text) == {len(names)}

    # The older connect page renderer (no route serves it) also renders only what it is given.
    older = pages.connect_html(settings, served)
    assert stated_counts(older) == {len(names)}
    assert all(f"<code>{name}</code>" in older for name in names)
