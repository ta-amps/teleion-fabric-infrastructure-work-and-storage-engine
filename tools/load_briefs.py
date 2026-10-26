#!/usr/bin/env python3
"""Put briefs on the `build` queue once their dependencies are done. Safe to rerun: Tessera's enqueue
is idempotent by id, so a brief already queued is left alone.

    uv run python tools/load_briefs.py --db .tessera/tessera.db [--briefs briefs/]

Briefs owned by a person (owner_type "human") stay off the agent queue; they're listed instead.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from tessera import NotFound, QueueConfig, Store, TaskSpec

BUILD_QUEUE = QueueConfig(name="build", lease_seconds=3600, heartbeat_seconds=60, miss_limit=3)


def done(store: Store, task_id: str, finished: set[str]) -> bool:
    """A dependency is done when its brief sits in briefs/done/ or its task is done in the store."""
    if task_id in finished:
        return True
    try:
        return store.task(task_id).state == "done"
    except NotFound:
        return False


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--db", default=os.environ.get("TESSERA_DB", ".tessera/tessera.db"))
    p.add_argument("--briefs", default="briefs")
    args = p.parse_args()
    store = Store(args.db)
    try:
        store.define_queue(BUILD_QUEUE)
        finished = {p.stem for p in Path(args.briefs, "done").glob("*.json")}
        for path in sorted(Path(args.briefs).glob("*.json")):
            brief = json.loads(path.read_text(encoding="utf-8"))
            task_id = brief["task_id"]
            if brief["owner_type"] == "human":
                print(f"{task_id}: for Taylor, not queued")
                continue
            waiting = [d for d in brief.get("depends_on", []) if not done(store, d, finished)]
            if waiting:
                print(f"{task_id}: waits for {', '.join(waiting)}")
                continue
            store.enqueue(
                TaskSpec(
                    queue="build",
                    task_id=task_id,
                    brief=brief,
                    kind=brief["kind"],
                    effect_class=brief["effect_class"],
                )
            )
            print(f"{task_id}: queued")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
