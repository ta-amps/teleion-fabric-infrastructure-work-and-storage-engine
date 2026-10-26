"""SPEC §5.5 lists every event type, and the code emits only listed types: a new event can't ship
without a line in the spec, and a listed one can't quietly disappear from the code."""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def emitted() -> set[str]:
    names: set[str] = set()
    for path in (REPO / "src" / "tessera").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_event"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                names.add(node.args[1].value)
            # _wake(..., event="resumed") passes its event name through to _event.
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                for kw in node.keywords:
                    if kw.arg == "event" and isinstance(kw.value, ast.Constant):
                        names.add(str(kw.value.value))
    return names


def listed() -> set[str]:
    spec = (REPO / "conformance-draft" / "SPEC.md").read_text(encoding="utf-8")
    bullet = spec.split("- **Events**:", 1)[1].split("\n- **", 1)[0]
    return set(re.findall(r"`([a-z_]+)`", bullet)) - {"seq"}


def test_the_spec_lists_exactly_the_events_the_code_emits() -> None:
    assert emitted() == listed()


def test_the_scan_finds_events_at_all() -> None:
    # Guards the guard: if the AST scan broke, the equality above could pass on two empty sets.
    assert {"claimed", "submitted", "parked", "dependencies_met"} <= emitted()
