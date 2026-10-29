"""Worker keys (SPEC §7): random bearer secrets stored only as SHA-256 hashes, scoped to queue patterns,
task kinds, effect classes and scopes, expiring after 14 days by default. No key holds `public`."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Sequence
from typing import Any

from tessera._base import new_id
from tessera._principals import PrincipalOps
from tessera.errors import InvalidRequest, NotFound
from tessera.grants import EFFECTS, KeyGrant, is_valid_scope

KEY_PREFIX = "tsk_"


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class KeyOps(PrincipalOps):
    def issue_key(
        self,
        label: str,
        *,
        queues: Sequence[str],
        kinds: Sequence[str] = ("*",),
        effect_classes: Sequence[str] = EFFECTS,
        scopes: Sequence[str] = (),
        days: float = 14.0,
        account_id: str | None = None,
        principal_kind: str = "keyed",
        principal_id: str | None = None,
        namespace: str = "main",
        labels: dict[str, Any] | None = None,
        purge_at: float | None = None,
    ) -> tuple[str, KeyGrant]:
        """Create a key and return (secret, grant). The secret is shown once and never stored."""
        if not queues:
            raise InvalidRequest("a key needs at least one queue pattern")
        if "public" in effect_classes:
            raise InvalidRequest("no key may hold the public effect class")
        unknown_effects = set(effect_classes) - set(EFFECTS)
        bad_scopes = {s for s in scopes if not is_valid_scope(s)}
        if unknown_effects or bad_scopes:
            raise InvalidRequest(
                f"unknown effect classes or scopes: {sorted(unknown_effects | bad_scopes)}"
            )
        if not 0 < days <= 365:
            raise InvalidRequest("days must be between 0 and 365")
        secret = ("tan_" if principal_kind == "anonymous" else KEY_PREFIX) + secrets.token_urlsafe(
            32
        )
        now = self.clock.now()
        key_id = (
            principal_id if principal_kind == "anonymous" and principal_id else new_id("k")[:14]
        )
        with self._tx() as c:
            c.execute(
                "INSERT INTO keys (key_id, key_hash, label, queues, kinds, effect_classes, scopes,"
                " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key_id,
                    hash_secret(secret),
                    label,
                    json.dumps(list(queues)),
                    json.dumps(list(kinds)),
                    json.dumps(list(effect_classes)),
                    json.dumps(list(scopes)),
                    now,
                    now + days * 86400,
                ),
            )
            kind = (
                "service_binding"
                if principal_kind == "keyed" and any(s.startswith("surface:") for s in scopes)
                else principal_kind
            )
            c.execute(
                "INSERT INTO access_principals(key_id,principal_kind,principal_id,account_id,namespace,key_prefix,labels,quota_tier,last_used_at,purge_at) VALUES (?,?,?,?,?,?,?,?,NULL,?)",
                (
                    key_id,
                    kind,
                    principal_id or key_id,
                    None if kind == "anonymous" else account_id or f"operator:{key_id}",
                    namespace,
                    secret[:8],
                    json.dumps(labels or {}),
                    "demo" if kind == "anonymous" else "standard",
                    purge_at,
                ),
            )
            self._event(
                c,
                "key_issued",
                now,
                detail={
                    "key_id": key_id,
                    "label": label,
                    "queues": list(queues),
                    "scopes": list(scopes),
                },
            )
        return secret, self.get_key(key_id)

    def find_key(self, secret: str) -> KeyGrant | None:
        """The live grant for a presented secret, or None (unknown, expired or revoked)."""
        with self._read() as c:
            row = c.execute(
                "SELECT * FROM keys WHERE key_hash = ?", (hash_secret(secret),)
            ).fetchone()
        if row is None:
            return None
        grant = self.principal_metadata(self._grant(row))
        if grant.revoked_at is not None or grant.expires_at <= self.clock.now():
            return None
        return grant

    def get_key(self, key_id: str) -> KeyGrant:
        with self._read() as c:
            row = c.execute("SELECT * FROM keys WHERE key_id = ?", (key_id,)).fetchone()
        if row is None:
            raise NotFound(f"key {key_id} does not exist")
        return self.principal_metadata(self._grant(row))

    def list_keys(self) -> list[KeyGrant]:
        with self._read() as c:
            rows = c.execute("SELECT * FROM keys ORDER BY created_at").fetchall()
        return [self.principal_metadata(self._grant(r)) for r in rows]

    def revoke_key(self, key_id: str) -> KeyGrant:
        now = self.clock.now()
        with self._tx() as c:
            cur = c.execute(
                "UPDATE keys SET revoked_at = ? WHERE key_id = ? AND revoked_at IS NULL",
                (now, key_id),
            )
            if cur.rowcount:
                self._event(c, "key_revoked", now, detail={"key_id": key_id})
        return self.get_key(key_id)

    @staticmethod
    def _grant(row: sqlite3.Row) -> KeyGrant:
        return KeyGrant(
            key_id=row["key_id"],
            label=row["label"],
            queues=json.loads(row["queues"]),
            kinds=json.loads(row["kinds"]),
            effect_classes=json.loads(row["effect_classes"]),
            scopes=json.loads(row["scopes"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )
