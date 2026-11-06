#!/usr/bin/env python3
"""Claim library.review verbs with a reviewer key and submit the library's offline decision.

    TESSERA_URL=https://fabric.teleionlabs.com TESSERA_KEY_FILE=~/.config/tessera/reviewer.key \\
        python3 tools/library_reviewer.py --library ../seed-packs-library-of-nouns-and-verbs --once

One pass per verb: claim from `library.review`, read the proposal the brief names
(GET /v1/library/proposals/<id>), run the library's `scripts/review_proposal.py` on it, heartbeat
while it runs, and submit its ta-amps.library.review.v1 output. When the library's reviewer says
the proposal cannot be decided offline (exit 2), the verb is parked for a person instead of being
decided. The reviewer key must be a different key and account from every proposer's; Tessera
refuses the submit otherwise. The submitted pack's code is never run: the library's reviewer
reads and hashes it. Stdlib only, so a volunteer worker loop can call `review_once` directly.
Keys come from TESSERA_KEY or a mode-600 TESSERA_KEY_FILE and are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

QUEUE = "library.review"
USER_AGENT = "tessera-library-reviewer/1.0"
REVIEW_TIMEOUT = 300


class Fabric:
    """The few Tessera HTTP calls a reviewer makes, with the key held in memory only."""

    def __init__(self, base: str, key: str) -> None:
        if urllib.parse.urlsplit(base).scheme not in {"http", "https"}:
            raise SystemExit("library_reviewer: the fabric URL must be http or https")
        self.base = base.rstrip("/")
        self._key = key

    def call(
        self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(  # noqa: S310 (http(s) only, checked above)
            self.base + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 (http(s) only, checked above)
                raw, status = response.read(), response.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        return status, (json.loads(raw) if raw else None)


def load_key() -> str:
    if os.environ.get("TESSERA_KEY"):
        return os.environ["TESSERA_KEY"].strip()
    path = Path(os.environ.get("TESSERA_KEY_FILE", "")).expanduser()
    if not path.is_file():
        raise SystemExit(
            "library_reviewer: set TESSERA_KEY or TESSERA_KEY_FILE to the reviewer key"
        )
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SystemExit(f"library_reviewer: {path} must be mode 600")
    text = path.read_text().strip()
    if text.startswith("{"):
        text = str(json.loads(text).get("key", ""))
    return text


def run_review(
    library: Path, proposal: dict[str, Any], lease: dict[str, Any], key_id: str, beat: Any
) -> tuple[int, str, str]:
    with tempfile.TemporaryDirectory(prefix="library-review-") as tmp:
        prop, brief = Path(tmp) / "proposal.json", Path(tmp) / "brief.json"
        prop.write_text(json.dumps(proposal))
        brief.write_text(json.dumps(lease.get("brief", {})))
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "LC_ALL"}}
        proc = subprocess.Popen(
            [
                sys.executable,
                "-s",
                "-E",
                str(library / "scripts" / "review_proposal.py"),
                "--proposal",
                str(prop),
                "--brief",
                str(brief),
                "--reviewer-key-id",
                key_id,
            ],
            cwd=library,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        started = time.monotonic()
        interval = max(5.0, float(lease.get("heartbeat_seconds") or 60) / 3)
        while True:
            try:
                out, err = proc.communicate(timeout=interval)
                return proc.returncode, out, err
            except subprocess.TimeoutExpired:
                if time.monotonic() - started > REVIEW_TIMEOUT:
                    proc.kill()
                    proc.communicate()
                    return 2, "", "the offline review timed out"
                beat()


def review_once(
    fabric: Fabric, library: Path, worker: str = "library-reviewer"
) -> dict[str, Any] | None:
    """Claim one library.review verb and settle it. Returns a short report, or None when idle."""
    status, me = fabric.call("GET", "/v1/me")
    if status != 200:
        raise SystemExit(f"library_reviewer: the key was refused ({status})")
    status, lease = fabric.call("POST", "/v1/tasks/claim", {"queue": QUEUE, "worker": worker})
    if status == 204 or lease is None:
        return None
    if status != 200:
        return {"event": "claim_refused", "status": status, "detail": (lease or {}).get("detail")}
    ref = {"task_id": lease["task_id"], "fencing_token": lease["fencing_token"]}
    brief = lease.get("brief", {})
    status, proposal = fabric.call(
        "GET", str(brief.get("proposal_url") or f"/v1/library/proposals/{brief.get('proposal_id')}")
    )
    code, out, err = (
        (2, "", f"proposal unreadable ({status})")
        if status != 200
        else run_review(
            library,
            proposal,
            lease,
            me["key_id"],
            lambda: fabric.call("POST", "/v1/tasks/heartbeat", ref),
        )
    )
    if code != 0:
        reason = (err.strip().splitlines() or ["the offline review could not decide"])[-1][:300]
        status, _ = fabric.call(
            "POST", "/v1/tasks/park", {**ref, "reason": f"needs a person: {reason}"}
        )
        return {
            "event": "parked",
            "task_id": ref["task_id"],
            "proposal_id": brief.get("proposal_id"),
            "status": status,
            "reason": reason,
        }
    output = json.loads(out)
    status, receipt = fabric.call(
        "POST",
        "/v1/tasks/submit",
        {**ref, "output": output},
        {"Idempotency-Key": f"{ref['task_id']}:{ref['fencing_token']}"},
    )
    if status == 409 and output["approved"]:
        # The catalog changed after the proposal (another proposal took a key with other
        # content), so the approval cannot be admitted: record why as a rejection instead of
        # letting the lease lapse into retries.
        detail = str((receipt or {}).get("detail") or "a catalog key is taken")[:300]
        output = {
            **output,
            "approved": False,
            "reasons": [
                f"Fits the library's rules but cannot be admitted: {detail}. Propose a new version."
            ],
            "checks": [*output["checks"], {"id": "catalog", "passed": False, "detail": detail}][
                :50
            ],
        }
        status, receipt = fabric.call(
            "POST",
            "/v1/tasks/submit",
            {**ref, "output": output},
            {"Idempotency-Key": f"{ref['task_id']}:{ref['fencing_token']}:catalog"},
        )
    return {
        "event": "submitted" if status == 200 else "submit_refused",
        "task_id": ref["task_id"],
        "proposal_id": output["proposal_id"],
        "approved": output["approved"],
        "status": status,
        "receipt_hash": (receipt or {}).get("receipt_hash"),
        "detail": None if status == 200 else (receipt or {}).get("detail"),
        "reasons": output["reasons"][:3],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--library", type=Path, required=True, help="a checkout of the pack library"
    )
    parser.add_argument("--base", default=os.environ.get("TESSERA_URL", "http://127.0.0.1:7700"))
    parser.add_argument("--once", action="store_true", help="settle at most one verb")
    parser.add_argument("--max", type=int, default=20, help="settle at most this many verbs")
    parser.add_argument("--worker", default="library-reviewer")
    args = parser.parse_args(argv)
    if not (args.library / "scripts" / "review_proposal.py").is_file():
        raise SystemExit("library_reviewer: --library must be a pack library checkout")
    fabric = Fabric(args.base, load_key())
    for _ in range(1 if args.once else args.max):
        report = review_once(fabric, args.library.resolve(), args.worker)
        if report is None:
            print(json.dumps({"event": "idle", "queue": QUEUE}))
            break
        print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
