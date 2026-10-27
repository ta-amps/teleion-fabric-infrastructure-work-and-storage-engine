"""Durable principal metadata, bounded anonymous quotas and content-free access audit."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from typing import TYPE_CHECKING, Any, cast

from tessera._access_schema import AccessSchemaOps
from tessera.errors import InvalidRequest, RateLimited, ScopeDenied
from tessera.grants import KeyGrant
from tessera.models import QueueConfig, RecordWrite, TaskSpec

if TYPE_CHECKING:
    from tessera.store import Store

DEMO_QUOTAS = {
    "session_seconds": 3600,
    "retention_seconds": 86400,
    "claims_per_hour": 20,
    "concurrent_claims": 1,
    "storage_bytes": 1048576,
    "requests_per_hour": 300,
    "request_bytes": 65536,
    "sessions_per_ip_hour": 10,
}
ANON_LABELS: dict[str, Any] = {"origin": "anonymous", "demo": True, "untrusted": True}
SCOPES = {
    "claim": "verbs:claim",
    "heartbeat": "verbs:claim",
    "cancel": "verbs:submit",
    "submit": "verbs:submit",
    "enqueue": "verbs:submit",
    "putRecords": "cas:write",
    "putBlob": "cas:write",
    "putObject": "cas:write",
    "defineBucket": "cas:write",
    "publishFeed": "cas:write",
    "handshake": "cas:write",
    "park": "verbs:submit",
    "resume": "verbs:submit",
    "split": "verbs:submit",
    "registerJob": "verbs:submit",
    "updateJob": "verbs:submit",
    "reconcile": "cas:write",
    "mutation": "admin",
}
READ_ACTIONS = {
    "getRecord",
    "listRecords",
    "recordHistory",
    "listStores",
    "searchNouns",
    "task",
    "results",
    "queues",
    "counts",
    "claimEvents",
    "listVerbs",
    "getBlob",
    "getObject",
    "listBuckets",
    "listObjects",
    "readFeed",
    "matchReflex",
    "query",
}
ANON_ACTIONS = {
    "queues",
    "counts",
    "claim",
    "heartbeat",
    "submit",
    "cancel",
    "enqueue",
    "task",
    "results",
    "putRecords",
    "getRecord",
    "listRecords",
    "recordHistory",
    "listStores",
    "searchNouns",
    "claimEvents",
    "listVerbs",
    "me",
    "my_audit",
}
WRITE_ACTIONS = {"putRecords", "submit", "enqueue"}


class DemoPayloadTooLarge(InvalidRequest):
    status = 413
    code = "payload_too_large"


def public_principal(grant: KeyGrant) -> dict[str, Any]:
    return {
        "principal_kind": grant.principal_kind,
        "principal_id": grant.principal_id or grant.key_id,
        "session_id": grant.session_id,
        "key_id": None if grant.anonymous else grant.key_id,
        "account_id": grant.account_id,
        "namespace": grant.namespace,
        "key_prefix": grant.key_prefix,
        "scopes": grant.scopes,
        "labels": grant.labels,
        "expires_at": grant.expires_at,
        "quotas": DEMO_QUOTAS if grant.anonymous else None,
        "quota_reason": None
        if grant.anonymous
        else "Operator-configured queue grants and service request limits apply.",
    }


class PrincipalOps(AccessSchemaOps):
    def request_identity(self, ip: str, ua: str) -> dict[str, str]:
        with self._read() as c:
            salt = c.execute("SELECT value FROM meta WHERE key='access_hash_salt'").fetchone()[0]
        day = int(self.clock.now() // 86400)
        return {
            name: hmac.new(
                salt.encode(), f"{day}:{kind}:{value}".encode(), hashlib.sha256
            ).hexdigest()
            for name, kind, value in (("ip_hash", "ip", ip[:128]), ("ua_hash", "ua", ua[:512]))
        }

    def principal_metadata(self, grant: KeyGrant) -> KeyGrant:
        with self._read() as c:
            row = c.execute(
                "SELECT * FROM access_principals WHERE key_id=?", (grant.key_id,)
            ).fetchone()
        if row:
            meta = dict(row)
            meta["labels"] = json.loads(meta["labels"])
            meta["session_id"] = (
                meta["principal_id"] if meta["principal_kind"] == "anonymous" else None
            )
        else:
            meta = {
                "principal_kind": "service_binding" if grant.bound_surface() else "keyed",
                "principal_id": grant.key_id,
                "account_id": f"legacy:{grant.key_id}",
            }
        return grant.model_copy(update=meta)

    def open_anonymous_session(
        self, agent_label: str = "", *, ip_hash: str = "unknown"
    ) -> dict[str, Any]:
        if len(agent_label) > 64:
            raise InvalidRequest("agent_label must be at most 64 characters")
        label = re.sub(r"[^a-zA-Z0-9 ._:-]", "", agent_label)[:64] or "agent"
        now = self.clock.now()
        window = int(now // 3600)
        with self._tx() as c:
            for subject, maximum in ((f"mint:{ip_hash}", 10), ("mint:global", 1000)):
                c.execute(
                    "INSERT OR IGNORE INTO access_limits(subject,window) VALUES (?,?)",
                    (subject, window),
                )
                row = c.execute(
                    "SELECT requests FROM access_limits WHERE subject=? AND window=?",
                    (subject, window),
                ).fetchone()
                if row[0] >= maximum:
                    raise RateLimited("anonymous session mint quota exceeded", 3600 - now % 3600)
                c.execute(
                    "UPDATE access_limits SET requests=requests+1 WHERE subject=? AND window=?",
                    (subject, window),
                )
        store = cast("Store", self)
        principal_id = "a_" + secrets.token_hex(16)
        namespace = "demo." + principal_id
        labels = {**ANON_LABELS, "agent_label": label}
        secret, grant = store.issue_key(
            label,
            queues=["demo.*"],
            effect_classes=["read", "write-internal"],
            scopes=[
                "store",
                "nouns:read",
                "verbs:claim",
                "verbs:submit",
                "cas:write",
                f"namespace:{namespace}",
            ],
            days=1 / 24,
            principal_kind="anonymous",
            principal_id=principal_id,
            namespace=namespace,
            labels=labels,
            purge_at=now + DEMO_QUOTAS["retention_seconds"],
        )
        tags = ["origin:anonymous", "demo:true", "untrusted:true"]
        with self.scope_context(
            namespace=namespace, key_id=grant.key_id, anonymous=True, labels=labels
        ):
            for name in ("demo.ping", "demo.echo", "demo.situate_sample"):
                store.define_queue(
                    QueueConfig(name=name, lease_seconds=300, heartbeat_seconds=30, max_attempts=3)
                )
                store.enqueue(
                    TaskSpec(
                        queue=name,
                        task_id=f"{principal_id}_{name.replace('.', '_')}",
                        kind="demo",
                        brief={
                            "title": name,
                            "synthetic": True,
                            "instruction": "Read demo.instructions, create a small synthetic artifact in demo.artifacts, then submit its record key and version.",
                            "labels": labels,
                        },
                        visibility_scope="private",
                        tags=tags,
                    )
                )
            store.put_records(
                [
                    RecordWrite(
                        store="demo.instructions",
                        key="welcome",
                        expected_version=0,
                        value={
                            "title": "Synthetic agent practice",
                            "synthetic": True,
                            "goal": "Complete a fenced noun/verb loop without real personal data.",
                            "labels": labels,
                        },
                        visibility_scope="private",
                        tags=tags,
                    )
                ]
            )
        return {
            "session_token": secret,
            **public_principal(grant),
            "persisted": True,
            "retention_expires_at": now + DEMO_QUOTAS["retention_seconds"],
        }

    def enforce_principal(
        self, grant: KeyGrant, action: str, args: dict[str, Any], namespace: str
    ) -> None:
        if not grant.allows_namespace(namespace):
            raise ScopeDenied("this principal may not access this namespace")
        fine = any(":" in s and not s.startswith(("surface:", "namespace:")) for s in grant.scopes)
        required = SCOPES.get(action, "nouns:read" if action in READ_ACTIONS else None)
        if not grant.is_admin and fine and required and required not in grant.scopes:
            raise ScopeDenied(f"missing {required} scope")
        if not grant.anonymous:
            return
        if action not in ANON_ACTIONS:
            raise ScopeDenied("anonymous sessions may only use isolated demo nouns and verbs")
        for name in ("queue", "store", "store_name"):
            if args.get(name) and not str(args[name]).startswith("demo."):
                raise ScopeDenied("anonymous sessions cannot access production nouns or verbs")
        writes = args.get("writes", [])
        if not isinstance(writes, list):
            raise InvalidRequest("writes must be a list")
        for write in writes:
            if not isinstance(write, dict) or not str(write.get("store", "")).startswith("demo."):
                raise ScopeDenied("demo store required")
        payload = (
            args.get("writes", args.get("value"))
            if action == "putRecords"
            else args.get("output")
            if action == "submit"
            else args.get("brief")
            if action == "enqueue"
            else {}
        )
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if action in WRITE_ACTIONS and re.search(
            r"@(?!example\.(?:com|org|net)\b)[a-z0-9.-]+\.[a-z]{2,}|\b\d{3}-\d{2}-\d{4}\b|\b(?:\d[ -]?){13,19}\b",
            encoded,
            re.I,
        ):
            raise InvalidRequest(
                "demo payloads must not contain personal or payment data; use example.com"
            )
        size = len(encoded.encode())
        if size > DEMO_QUOTAS["request_bytes"]:
            raise DemoPayloadTooLarge("demo request exceeds byte limit")
        now = self.clock.now()
        window = int(now // 3600)
        with self._tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO access_limits(subject,window) VALUES (?,?)",
                (grant.key_id, window),
            )
            row = c.execute(
                "SELECT * FROM access_limits WHERE subject=? AND window=?", (grant.key_id, window)
            ).fetchone()
            total = c.execute(
                "SELECT COALESCE(SUM(bytes),0) FROM access_limits WHERE subject=?", (grant.key_id,)
            ).fetchone()[0]
            if (
                row["requests"] >= DEMO_QUOTAS["requests_per_hour"]
                or (action == "claim" and row["claims"] >= DEMO_QUOTAS["claims_per_hour"])
                or (action in WRITE_ACTIONS and total + size > DEMO_QUOTAS["storage_bytes"])
            ):
                raise RateLimited("demo quota exceeded", 3600 - now % 3600)
            c.execute(
                "UPDATE access_limits SET requests=requests+1,claims=claims+?,bytes=bytes+? WHERE subject=? AND window=?",
                (
                    int(action == "claim"),
                    size if action in WRITE_ACTIONS else 0,
                    grant.key_id,
                    window,
                ),
            )

    def audit_access(
        self,
        grant: KeyGrant | None,
        action: str,
        args: dict[str, Any],
        *,
        status: int,
        meta: dict[str, Any],
        output: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        out = output or {}
        nested = out.get("receipt") or out.get("lease") or out
        if not isinstance(nested, dict):
            nested = {}
        writes = args.get("writes") or []
        resource = (
            nested.get("task_id")
            or args.get("task_id")
            or args.get("key")
            or (writes[0].get("key") if writes and isinstance(writes[0], dict) else None)
            or args.get("queue")
        )
        resource_type = (
            "verb"
            if args.get("task_id") or action == "claim"
            else "noun"
            if writes or args.get("key")
            else "access"
        )
        if action in {"mint_key", "revoke_key"}:
            resource = out.get("key_id") or args.get("key_id")
            resource_type = "key"
        now = self.clock.now()
        with self._tx() as c:
            c.execute(
                """INSERT INTO access_events(request_id,ts,namespace,principal_kind,principal_id,session_id,
                key_id,account_id,ip_hash,ua_hash,surface,route_or_tool,action,resource_type,resource_id,receipt_id,
                status,bytes_in,bytes_out,error_code,labels,scopes_used) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    meta.get("request_id") or secrets.token_hex(16),
                    now,
                    meta.get("namespace") or (grant.namespace if grant else "main"),
                    grant.principal_kind if grant else "unknown",
                    (grant.principal_id or grant.key_id) if grant else None,
                    grant.session_id if grant else None,
                    grant.key_id if grant and not grant.anonymous else None,
                    grant.account_id if grant else None,
                    meta.get("ip_hash"),
                    meta.get("ua_hash"),
                    (grant.bound_surface() or "tessera") if grant else "tessera",
                    meta.get("route_or_tool") or action,
                    action,
                    resource_type,
                    resource,
                    nested.get("receipt_hash") or nested.get("claim_receipt"),
                    status,
                    meta.get("bytes_in", 0),
                    meta.get("bytes_out", 0),
                    error_code,
                    json.dumps(grant.labels if grant else {}),
                    json.dumps(
                        [SCOPES[action]]
                        if action in SCOPES
                        else ["nouns:read"]
                        if action in READ_ACTIONS
                        else []
                    ),
                ),
            )
            if grant:
                c.execute(
                    "UPDATE access_principals SET last_used_at=? WHERE key_id=?",
                    (now, grant.key_id),
                )
            c.execute("DELETE FROM access_events WHERE ts<?", (now - 86400,))
            c.execute("DELETE FROM access_limits WHERE window<?", (int(now // 3600) - 24,))

    def own_audit(
        self, grant: KeyGrant, *, after: int = 0, limit: int = 50, admin: bool = False
    ) -> dict[str, Any]:
        if admin and not grant.is_admin:
            raise ScopeDenied("admin scope required")
        with self._read() as c:
            if admin:
                rows = c.execute(
                    "SELECT * FROM access_events WHERE seq>? AND ts>=? ORDER BY seq DESC LIMIT ?",
                    (after, self.clock.now() - 86400, max(1, min(limit, 200))),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM access_events WHERE principal_id=? AND seq>? AND ts>=? ORDER BY seq DESC LIMIT ?",
                    (
                        grant.principal_id or grant.key_id,
                        after,
                        self.clock.now() - 86400,
                        max(1, min(limit, 200)),
                    ),
                ).fetchall()
        return {
            "events": [
                {
                    **dict(row),
                    "labels": json.loads(row["labels"]),
                    "scopes_used": json.loads(row["scopes_used"]),
                }
                for row in rows
            ]
        }

    def public_activity(self) -> dict[str, Any]:
        with self._read() as c:
            rows = c.execute(
                "SELECT ts,principal_kind,action,resource_type,status,labels FROM access_events WHERE principal_kind IN ('anonymous','keyed','service_binding') AND ts>=? ORDER BY seq DESC LIMIT 30",
                (self.clock.now() - 86400,),
            ).fetchall()
        return {
            "events": [
                {
                    **dict(row),
                    "labels": ANON_LABELS if json.loads(row["labels"]).get("demo") else {},
                }
                for row in rows
            ]
        }
