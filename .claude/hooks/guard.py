#!/usr/bin/env python3
"""Claude Code PreToolUse guard: enforces the hard rules in every permission mode.

Reads the hook payload (JSON) on stdin and the rules in `.claude/guard.json`.
Exit 0 allows the call. Exit 2 blocks it; the reason on stderr goes back to Claude.
Fails closed: if the payload or the config can't be read, the call is blocked.

guard.json keys (all optional):
  deny_read      globs no tool may read (Read, Grep, Glob, NotebookRead), relative to the repo root
  deny_write     globs no tool may write (Edit, Write, MultiEdit, NotebookEdit)
  allow          globs that override both lists (for example ".env.example")
  deny_commands  [{"pattern": regex, "reason": text}] checked against every Bash command
Globs: `*` matches within one path segment, `**` matches across segments.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

READ_TOOLS = {"Read", "Grep", "Glob", "NotebookRead"}
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def block(reason: str) -> None:
    print(f"Blocked by .claude/hooks/guard.py: {reason}", file=sys.stderr)
    sys.exit(2)


def glob_to_regex(glob: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def matches(rel: str, globs: list[str]) -> bool:
    """True when `rel` matches a glob; `dir/**` also covers the directory itself."""
    return any(glob_to_regex(g).match(rel) or (g.endswith("/**") and rel == g[:-3]) for g in globs)


def relative(path_text: str, root: Path, cwd: Path) -> str | None:
    """Repo-relative POSIX path, or None when the path lies outside the repo."""
    path = Path(os.path.expanduser(path_text))
    if not path.is_absolute():
        path = cwd / path
    try:
        resolved = path.resolve(strict=False)
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def target_paths(tool: str, tool_input: dict[str, object]) -> list[str]:
    keys = ("file_path", "notebook_path", "path")
    return [
        str(tool_input[k]) for k in keys if isinstance(tool_input.get(k), str) and tool_input[k]
    ]


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        tool = str(payload.get("tool_name", ""))
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            raise ValueError("tool_input is not an object")
        root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd())
        cwd = Path(payload.get("cwd") or root)
        config_path = Path(__file__).resolve().parent.parent / "guard.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        block(f"could not read the hook payload or .claude/guard.json ({exc}); failing closed")
        return

    allow = list(config.get("allow", []))
    deny_read = list(config.get("deny_read", []))
    deny_write = list(config.get("deny_write", []))

    if tool in READ_TOOLS | WRITE_TOOLS:
        for raw in target_paths(tool, tool_input):
            rel = relative(raw, root, cwd)
            if rel is None or matches(rel, allow):
                continue
            if matches(rel, deny_read) or (tool in WRITE_TOOLS and matches(rel, deny_write)):
                block(
                    f"{tool} on '{rel}' is a protected path in this repo (see AGENTS.md hard rules)"
                )

    if tool == "Bash":
        command = str(tool_input.get("command", ""))
        for rule in config.get("deny_commands", []):
            if re.search(rule["pattern"], command):
                block(rule.get("reason", "command matches a denied pattern"))

    sys.exit(0)


if __name__ == "__main__":
    main()
