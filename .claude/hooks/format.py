#!/usr/bin/env python3
"""Claude Code PostToolUse hook: format the file that was just edited. Never blocks.

Python files go through `ruff format` and `ruff check --fix`; web files go through the repo's own
prettier when web/node_modules has it. Anything else is left alone.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

WEB_SUFFIXES = {".ts", ".tsx", ".css", ".json", ".html", ".md"}


def run(args: list[str], cwd: Path) -> None:
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(args, cwd=cwd, capture_output=True, timeout=60, check=False)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        tool_input = payload.get("tool_input") or {}
        file_path = Path(str(tool_input.get("file_path", "")))
        root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd())
    except Exception:
        sys.exit(0)
    if not file_path.is_file():
        sys.exit(0)
    if file_path.suffix == ".py":
        run(["uv", "run", "--quiet", "ruff", "format", str(file_path)], root)
        run(["uv", "run", "--quiet", "ruff", "check", "--fix", "--quiet", str(file_path)], root)
    elif file_path.suffix in WEB_SUFFIXES and "web" in file_path.parts:
        prettier = root / "web" / "node_modules" / ".bin" / "prettier"
        if prettier.exists():
            run([str(prettier), "--write", str(file_path)], root / "web")
    sys.exit(0)


if __name__ == "__main__":
    main()
