"""External-agent access through real Worker HTTP/MCP handlers and local SQLite bindings."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def test_external_agent_access_worker_contract() -> None:
    node = shutil.which("node")
    assert node is not None, "Node22+ required for Worker SQLite contract tests"
    folder = Path(__file__).resolve().parent
    result = subprocess.run(
        [
            node,
            "--import",
            str(folder / "edge_access_loader.mjs"),
            str(folder / "edge_access_harness.mjs"),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.stdout, result.stderr
    report = json.loads(result.stdout)
    failures = [test for test in report["tests"] if not test["passed"]]
    assert result.returncode == 0, json.dumps(failures, indent=2)
    assert report["passed"] == 37
