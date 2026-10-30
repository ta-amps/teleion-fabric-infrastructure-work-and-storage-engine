#!/usr/bin/env python3
"""Check that architecture.json still describes this repo truthfully.

architecture.json is the machine-readable architecture: components, interfaces, data stores, trust
boundaries, invariants with the tests that check them, import rules, claims and extension points. This
script fails when the file and the repo disagree:
  * required fields are present with the right types (schema version 1);
  * every path it names exists (docs, reading order, components, stores, evidence, tests);
  * every "path::name" test reference points at a file that defines that test or class;
  * references between components, interfaces, stores and boundaries point at ids that exist;
  * Python import rules hold: no file under a rule's path imports a forbidden module;
  * the invariants table in the architecture doc lists the same invariants, word for word.

Usage: python3 scripts/check_architecture.py [ROOT] [--summary | --mermaid | --sync-docs]
  --summary    print a short text overview after the check
  --mermaid    print the component graph as a Mermaid flowchart (no check)
  --sync-docs  rewrite the architecture doc's "## Invariants" table from architecture.json
Exit 0 when consistent (or when there is no architecture.json), 1 with a report otherwise.
Stdlib only; copy it into any repo. Schema: agents/schemas/architecture.schema.json in the hackathon/
folder of ta-amps-hackathon.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

MANIFEST = "architecture.json"
REQUIRED: dict[str, type | tuple[type, ...]] = {
    "schema_version": int,
    "name": str,
    "summary": str,
    "status": dict,
    "docs": dict,
    "read_order": list,
    "commands": dict,
    "components": list,
    "invariants": list,
    "claims": list,
}
OPTIONAL_LISTS = (
    "interfaces",
    "data_stores",
    "boundaries",
    "import_rules",
    "deployment",
    "limitations",
    "open_questions",
)
CLAIM_STATUSES = {"built", "partial", "planned"}
TEST_REF = re.compile(r"^(?P<path>[^:]+)::(?P<names>[A-Za-z_][\w]*(?:::[A-Za-z_][\w]*)*)$")


class Report:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def add(self, where: str, message: str) -> None:
        self.problems.append(f"{where}: {message}")


def _path_exists(root: Path, value: str) -> bool:
    target = value.split("#", 1)[0].rstrip("/")
    return bool(target) and (root / target).exists()


def check_path(root: Path, report: Report, where: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        report.add(where, "expected a non-empty path string")
    elif value.startswith(("/", "..")):
        report.add(where, f"path must stay inside the repo: {value}")
    elif not _path_exists(root, value):
        report.add(where, f"missing path: {value}")


def check_ref(root: Path, report: Report, where: str, value: Any) -> None:
    """A test or evidence reference: a repo path, or path::test_name (path::Class::test_name)."""
    if not isinstance(value, str):
        report.add(where, "expected a string")
        return
    match = TEST_REF.match(value)
    if not match:
        check_path(root, report, where, value)
        return
    path = match.group("path")
    check_path(root, report, where, path)
    file = root / path
    if not file.is_file():
        return
    try:
        tree = ast.parse(file.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        report.add(where, f"cannot parse {path} to find {match.group('names')}")
        return
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    for name in match.group("names").split("::"):
        if name not in defined:
            report.add(where, f"{path} defines no test or class named {name}")


def _ids(report: Report, section: str, items: list[Any]) -> set[str]:
    seen: set[str] = set()
    for index, item in enumerate(items):
        where = f"{section}[{index}]"
        if not isinstance(item, dict):
            report.add(where, "expected an object")
            continue
        ident = item.get("id")
        if not isinstance(ident, str) or not ident:
            report.add(where, "needs a string id")
        elif ident in seen:
            report.add(where, f"duplicate id {ident}")
        else:
            seen.add(ident)
    return seen


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def module_name(base_module: str, base_path: Path, file: Path) -> str:
    """Dotted name of a file inside the rule's path, given the dotted name of that path."""
    if file == base_path:
        return base_module
    rel = file.relative_to(base_path).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join([base_module, *parts]) if parts else base_module


def imported_modules(file: Path, dotted: str) -> list[tuple[int, str]]:
    tree = ast.parse(file.read_text(encoding="utf-8"))
    package = dotted if file.name == "__init__.py" else dotted.rpartition(".")[0]
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".") if package else []
                parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                base = ".".join(parts)
                target = f"{base}.{node.module}" if node.module else base
                found.append((node.lineno, target))
                found += [(node.lineno, f"{target}.{alias.name}") for alias in node.names]
            elif node.module:
                found.append((node.lineno, node.module))
                found += [(node.lineno, f"{node.module}.{alias.name}") for alias in node.names]
    return found


def check_import_rules(root: Path, report: Report, rules: list[Any]) -> None:
    for index, rule in enumerate(rules):
        where = f"import_rules[{index}]"
        if not isinstance(rule, dict):
            report.add(where, "expected an object")
            continue
        where += f" {rule.get('id', '')}".rstrip()
        package, package_root = rule.get("package"), rule.get("package_root")
        files, forbidden = _strings(rule.get("files")), _strings(rule.get("must_not_import"))
        if (
            not isinstance(package, str)
            or not isinstance(package_root, str)
            or not files
            or not forbidden
        ):
            report.add(where, "needs package, package_root, files and must_not_import")
            continue
        base = root / package_root
        if not base.is_dir():
            report.add(where, f"package_root is not a directory: {package_root}")
            continue
        targets: list[Path] = []
        for value in files:
            path = root / value
            if not path.exists():
                report.add(where, f"missing path: {value}")
            elif path.is_file():
                targets.append(path)
            else:
                targets += sorted(path.rglob("*.py"))
        for file in targets:
            rel = file.relative_to(root).as_posix()
            if not file.resolve().is_relative_to(base.resolve()):
                report.add(where, f"{rel} is outside package_root {package_root}")
                continue
            try:
                imports = imported_modules(file, module_name(package, base, file))
            except (SyntaxError, UnicodeDecodeError) as exc:
                report.add(where, f"cannot parse {rel}: {exc}")
                continue
            reported: set[tuple[int, str]] = set()
            for line, name in imports:
                for banned in forbidden:
                    matches = name == banned or name.startswith(banned + ".")
                    if matches and (line, banned) not in reported:
                        reported.add((line, banned))
                        report.add(
                            where,
                            f"{rel}:{line} imports {banned}, which this layer must not import",
                        )


def check(root: Path, data: Any) -> list[str]:
    report = Report()
    if not isinstance(data, dict):
        return [f"{MANIFEST}: expected a JSON object"]
    for key, kind in REQUIRED.items():
        if key not in data:
            report.add(MANIFEST, f"missing required field {key}")
        elif not isinstance(data[key], kind):
            report.add(key, f"expected {getattr(kind, '__name__', kind)}")
    if report.problems:
        return report.problems
    if data["schema_version"] != 1:
        report.add("schema_version", "this checker understands version 1")
    for key in OPTIONAL_LISTS:
        if key in data and not isinstance(data[key], list):
            report.add(key, "expected a list")
    for key in ("as_of", "stage"):
        if not isinstance(data["status"].get(key), str):
            report.add("status", f"needs a string {key}")
    for name, value in data["docs"].items():
        check_path(root, report, f"docs.{name}", value)
    for index, value in enumerate(data["read_order"]):
        check_path(root, report, f"read_order[{index}]", value)

    components = _ids(report, "components", data["components"])
    interfaces = _ids(report, "interfaces", data.get("interfaces", []))
    stores = _ids(report, "data_stores", data.get("data_stores", []))
    _ids(report, "boundaries", data.get("boundaries", []))
    _ids(report, "invariants", data["invariants"])
    _ids(report, "claims", data["claims"])

    for index, item in enumerate(data["components"]):
        if not isinstance(item, dict):
            continue
        where = f"components[{index}] {item.get('id')}"
        if not isinstance(item.get("role"), str):
            report.add(where, "needs a role")
        paths = _strings(item.get("path"))
        if not paths:
            report.add(where, "needs a path (a string or a list)")
        for value in paths:
            check_path(root, report, where, value)
        for dep in _strings(item.get("depends_on", [])):
            if dep not in components:
                report.add(where, f"depends_on unknown component {dep}")
        for ref in _strings(item.get("interfaces", [])):
            if ref not in interfaces:
                report.add(where, f"unknown interface {ref}")
        for ref in _strings(item.get("tests", [])):
            check_ref(root, report, where, ref)
    for index, item in enumerate(data.get("interfaces", [])):
        if not isinstance(item, dict):
            continue
        where = f"interfaces[{index}] {item.get('id')}"
        if item.get("served_by") not in components:
            report.add(where, f"served_by must name a component, got {item.get('served_by')!r}")
        if "spec" in item:
            check_path(root, report, where, item["spec"])
    for index, item in enumerate(data.get("data_stores", [])):
        if not isinstance(item, dict):
            continue
        where = f"data_stores[{index}] {item.get('id')}"
        if item.get("owned_by") not in components:
            report.add(where, f"owned_by must name a component, got {item.get('owned_by')!r}")
    for index, item in enumerate(data.get("boundaries", [])):
        if not isinstance(item, dict):
            continue
        where = f"boundaries[{index}] {item.get('id')}"
        if len(_strings(item.get("between"))) != 2 or not isinstance(item.get("rule"), str):
            report.add(where, "needs between (two sides) and a rule")
        for value in _strings(item.get("enforced_by", [])):
            check_path(root, report, where, value)
        for ref in _strings(item.get("checked_by", [])):
            check_ref(root, report, where, ref)
    for index, item in enumerate(data["invariants"]):
        if not isinstance(item, dict):
            continue
        where = f"invariants[{index}] {item.get('id')}"
        if not isinstance(item.get("statement"), str):
            report.add(where, "needs a statement")
        refs = _strings(item.get("checked_by", []))
        if not refs:
            report.add(
                where, "needs at least one checked_by test (an unchecked invariant is a wish)"
            )
        for ref in refs:
            check_ref(root, report, where, ref)
    for index, item in enumerate(data["claims"]):
        if not isinstance(item, dict):
            continue
        where = f"claims[{index}] {item.get('id')}"
        if item.get("status") not in CLAIM_STATUSES:
            report.add(where, f"status must be one of {sorted(CLAIM_STATUSES)}")
        if item.get("status") in {"built", "partial"} and not _strings(item.get("evidence", [])):
            report.add(where, "a built or partial claim needs evidence")
        for ref in _strings(item.get("evidence", [])):
            check_ref(root, report, where, ref)
    for index, item in enumerate(data.get("data_stores", [])):
        if isinstance(item, dict) and item.get("id") in stores and "path" in item:
            check_path(root, report, f"data_stores[{index}]", item["path"])
    extension_points = data.get("generalization", {}).get("extension_points", [])
    for index, item in enumerate(extension_points if isinstance(extension_points, list) else []):
        if isinstance(item, dict):
            for value in _strings(item.get("path", [])):
                check_path(root, report, f"generalization.extension_points[{index}]", value)
    check_import_rules(root, report, data.get("import_rules", []))
    check_docs_table(root, data, report)
    return report.problems


INVARIANTS_HEADING = "## Invariants"
TABLE_HEAD = ["| Id | Always true | Checked by |", "|---|---|---|"]


def _doc_section(text: str) -> tuple[int, int] | None:
    """Line span of the table under the invariants heading, or None if the doc has no such section."""
    lines = text.splitlines()
    if INVARIANTS_HEADING not in lines:
        return None
    start = lines.index(INVARIANTS_HEADING) + 1
    while start < len(lines) and not lines[start].startswith("|"):
        start += 1
    end = start
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    return start, end


def invariants_table(data: dict[str, Any]) -> list[str]:
    rows = list(TABLE_HEAD)
    for item in data["invariants"]:
        tests = ", ".join(
            f"`{ref.rsplit('::', 1)[-1]}`" for ref in _strings(item.get("checked_by"))
        )
        rows.append(f"| {item['id']} | {item['statement']} | {tests} |")
    return rows


def check_docs_table(root: Path, data: dict[str, Any], report: Report) -> None:
    doc = data["docs"].get("architecture")
    if not isinstance(doc, str) or not (root / doc).is_file():
        return
    span = _doc_section((root / doc).read_text(encoding="utf-8"))
    if span is None:
        return  # no invariants section in the doc: nothing to compare
    lines = (root / doc).read_text(encoding="utf-8").splitlines()[span[0] : span[1]]
    if lines != invariants_table(data):
        report.add(doc, "the invariants table differs from architecture.json; run --sync-docs")


def sync_docs(root: Path, data: dict[str, Any]) -> str:
    doc = data["docs"]["architecture"]
    text = (root / doc).read_text(encoding="utf-8")
    span = _doc_section(text)
    if span is None:
        raise SystemExit(f"{doc} has no '{INVARIANTS_HEADING}' heading")
    lines = text.splitlines()
    lines[span[0] : span[1]] = invariants_table(data)
    (root / doc).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return doc


def summary(data: dict[str, Any]) -> str:
    claims: dict[str, int] = {}
    for item in data.get("claims", []):
        if isinstance(item, dict):
            claims[str(item.get("status"))] = claims.get(str(item.get("status")), 0) + 1
    lines = [
        f"{data['name']}: {data['summary']}",
        f"status: {data['status'].get('stage')} as of {data['status'].get('as_of')}",
        f"components: {', '.join(c['id'] for c in data['components'] if isinstance(c, dict))}",
        f"interfaces: {len(data.get('interfaces', []))}, data stores: {len(data.get('data_stores', []))}, "
        f"boundaries: {len(data.get('boundaries', []))}, invariants: {len(data['invariants'])}, "
        f"import rules: {len(data.get('import_rules', []))}",
        "claims: " + ", ".join(f"{n} {s}" for s, n in sorted(claims.items())),
    ]
    return "\n".join(lines)


def _node(ident: str) -> str:
    return re.sub(r"\W", "_", ident)


def mermaid(data: dict[str, Any]) -> str:
    components = [
        c
        for c in data.get("components", [])
        if isinstance(c, dict) and isinstance(c.get("id"), str)
    ]
    lines = ["flowchart LR"]
    for item in components:
        label = str(item.get("title") or item["id"]).replace('"', "'")
        lines.append(f'  {_node(item["id"])}["{label}"]')
    for item in components:
        for dep in _strings(item.get("depends_on", [])):
            lines.append(f"  {_node(item['id'])} --> {_node(dep)}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    root = Path(args[0] if args else ".").resolve()
    manifest = root / MANIFEST
    if not manifest.exists():
        print(f"check_architecture: no {MANIFEST} in {root.name}; nothing to check")
        return 0
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"check_architecture: {MANIFEST} is not valid JSON: {exc}", file=sys.stderr)
        return 1
    if "--mermaid" in argv:
        print(mermaid(data))
        return 0
    if "--sync-docs" in argv:
        print(f"check_architecture: rewrote the invariants table in {sync_docs(root, data)}")
    problems = check(root, data)
    if problems:
        print(f"check_architecture: {len(problems)} problem(s) in {MANIFEST}", file=sys.stderr)
        for problem in problems:
            print("  " + problem, file=sys.stderr)
        return 1
    print(f"check_architecture: {MANIFEST} matches the repo ({root.name})")
    if "--summary" in argv:
        print(summary(data))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
