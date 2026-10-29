"""One-time restriction of automatically derived legacy task tombstones."""

from __future__ import annotations

import json
import sqlite3


def harden_task_tombstones(c: sqlite3.Connection) -> None:
    with c:
        c.execute("BEGIN IMMEDIATE")
        if c.execute("SELECT value FROM meta WHERE key='task_tombstone_privacy_v1'").fetchone():
            return
        rows = c.execute(
            "SELECT h.*,r.value FROM record_heads h JOIN records r ON r.namespace=h.namespace AND r.store=h.store AND r.key=h.key AND r.version=h.version WHERE h.store='negative.knowledge'"
        ).fetchall()
        for row in rows:
            value = json.loads(row["value"])
            if (
                not isinstance(value, dict)
                or value.get("task_id") != row["key"]
                or "dead_lettered_at" not in value
            ):
                continue
            source = c.execute(
                "SELECT * FROM tasks WHERE namespace=? AND id=?", (row["namespace"], row["key"])
            ).fetchone()
            tags = list(
                dict.fromkeys(
                    [
                        tag
                        for tag in [
                            *json.loads(row["tags"]),
                            *(json.loads(source["tags"]) if source else []),
                        ]
                        if tag != "status:verified"
                    ]
                )
            )
            owner = source["owner_key_id"] if source else None
            c.execute(
                "UPDATE record_heads SET visibility_scope='private',allowed_surfaces='[]',owner_key_id=?,tags=? WHERE namespace=? AND store='negative.knowledge' AND key=?",
                (owner or "system:operator-only", json.dumps(tags), row["namespace"], row["key"]),
            )
            c.execute(
                "DELETE FROM noun_tags WHERE namespace=? AND noun_type='record' AND container='negative.knowledge' AND record_id=?",
                (row["namespace"], row["key"]),
            )
            for tag in tags:
                c.execute(
                    "INSERT OR IGNORE INTO noun_tags(namespace,noun_type,container,record_id,tag) VALUES (?,'record','negative.knowledge',?,?)",
                    (row["namespace"], row["key"], tag),
                )
        c.execute("INSERT INTO meta(key,value) VALUES ('task_tombstone_privacy_v1','complete')")
