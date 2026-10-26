#!/usr/bin/env python3
"""Run the whole conformance suite over HTTP against a real `tessera serve` process on this machine.

Starts the service on a free port with a fresh database and three test keys (admin, q_a scoped to
conformance-a, q_b scoped to conformance-b), runs the suite through the HTTP client, scans the
service log for keys (T16), then stops the service. T14 (restart) is skipped here.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from tessera import Store

REPO = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "suite.db")
        store = Store(db)
        keys = {
            "admin": store.issue_key("suite-admin", queues=["*"], scopes=["admin", "store"])[0],
            "q_a": store.issue_key("suite-a", queues=["conformance-a"])[0],
            "q_b": store.issue_key("suite-b", queues=["conformance-b"])[0],
        }
        store.close()
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        log_path = Path(tmp) / "service.log"
        env = {**os.environ, "TESSERA_REAPER_SECONDS": "3600", "TESSERA_PUBLIC_URL": url}
        with log_path.open("w") as log:
            server = subprocess.Popen(
                [sys.executable, "-m", "tessera.cli", "--db", db, "serve", "--port", str(port)],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        try:
            for _ in range(100):
                try:
                    if httpx.get(f"{url}/v1/health", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.1)
            else:
                print("the service did not start; see its log:", log_path.read_text()[-2000:])
                return 1
            suite_env = {
                **os.environ,
                "TESSERA_URL": url,
                "TESSERA_TEST_KEYS": json.dumps(keys),
                "TESSERA_BACKEND_FACTORY": "tessera.testing:create_http_backend",
                "TESSERA_LOG_PATH": str(log_path),
            }
            suite = os.environ.get("SUITE", "conformance-draft")
            return subprocess.call(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(REPO / suite)],
                env=suite_env,
                cwd=REPO,
            )
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
