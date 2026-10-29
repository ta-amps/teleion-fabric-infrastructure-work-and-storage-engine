"""Additive local principal/audit schema and periodic anonymous state cleanup."""

from __future__ import annotations

import secrets

from tessera._base import StoreBase


class AccessSchemaOps(StoreBase):
    def _init_schema(self) -> None:
        super()._init_schema()
        self._conn().executescript("""
        CREATE TABLE IF NOT EXISTS access_principals (
          key_id TEXT PRIMARY KEY, principal_kind TEXT NOT NULL, principal_id TEXT NOT NULL,
          account_id TEXT, namespace TEXT NOT NULL, key_prefix TEXT NOT NULL, labels TEXT NOT NULL,
          quota_tier TEXT NOT NULL, last_used_at REAL, purge_at REAL);
        CREATE TABLE IF NOT EXISTS access_limits (
          subject TEXT NOT NULL, window INTEGER NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
          claims INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(subject,window));
        CREATE TABLE IF NOT EXISTS access_events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, ts REAL NOT NULL,
          namespace TEXT NOT NULL, principal_kind TEXT NOT NULL, principal_id TEXT, session_id TEXT,
          key_id TEXT, account_id TEXT, ip_hash TEXT, ua_hash TEXT, surface TEXT, route_or_tool TEXT,
          action TEXT NOT NULL, resource_type TEXT, resource_id TEXT, receipt_id TEXT, status INTEGER,
          bytes_in INTEGER, bytes_out INTEGER, error_code TEXT, labels TEXT NOT NULL, scopes_used TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS access_events_owner ON access_events(principal_id,seq);
        CREATE INDEX IF NOT EXISTS access_events_time ON access_events(ts);
        """)
        with self._tx() as c:
            if "labels" not in {r["name"] for r in c.execute("PRAGMA table_info(results)")}:
                c.execute("ALTER TABLE results ADD COLUMN labels TEXT NOT NULL DEFAULT '{}'")
        self._conn().execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES ('access_hash_salt',?)",
            (secrets.token_hex(32),),
        )

    def purge_expired_principals(self) -> int:
        with self._tx() as c:
            rows = c.execute(
                "SELECT key_id,namespace FROM access_principals WHERE principal_kind='anonymous' AND purge_at<=?",
                (self.clock.now(),),
            ).fetchall()
            tables = (
                "task_deps",
                "task_inputs",
                "leases",
                "results",
                "jobs",
                "events",
                "counts",
                "tasks",
                "queues",
                "records",
                "record_heads",
                "noun_tags",
                "feed_entries",
            )
            for row in rows:
                for table in tables:
                    c.execute(f"DELETE FROM {table} WHERE namespace=?", (row["namespace"],))  # noqa: S608
                c.execute("DELETE FROM access_limits WHERE subject=?", (row["key_id"],))
                c.execute("DELETE FROM access_principals WHERE key_id=?", (row["key_id"],))
                c.execute("DELETE FROM keys WHERE key_id=?", (row["key_id"],))
            c.execute("DELETE FROM access_events WHERE ts<?", (self.clock.now() - 86400,))
        return len(rows)
