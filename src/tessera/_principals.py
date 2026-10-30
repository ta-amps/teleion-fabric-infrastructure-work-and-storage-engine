"""Durable principal metadata, bounded anonymous quotas and content-free access audit."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import re
import secrets
from typing import TYPE_CHECKING, Any, cast

from tessera._access_schema import AccessSchemaOps
from tessera.errors import InvalidRequest, NotFound, RateLimited, ScopeDenied
from tessera.grants import CLOSED_TO_ANONYMOUS, OPEN_VERB_NAMESPACE, KeyGrant
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
    "open_claims_per_hour": 5,
    "open_claims_per_ip_hour": 10,
}
ANON_LABELS: dict[str, Any] = {"origin": "anonymous", "demo": True, "untrusted": True}
# A limited deterministic screen for common personal and payment data (same rules as the Worker):
# email addresses outside the example domains, identity and payment card numbers, IBANs, phone
# numbers and street addresses. It is not a claim that all personal data can be detected.
_EMAIL = re.compile(r"@(?!example\.(?:com|org|net)\b)[a-z0-9.-]+\.[a-z]{2,}", re.I)
_IDENTITY = re.compile(r"\b\d{3}-\d{2}-\d{4}\b|\b(?:\d[ -]?){13,19}\b")
# An IBAN carries at least ten digits, so a bill name such as "AB12 RENT CAPS" is not one.
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
_CONTACT = re.compile(
    r"(?<![\w+])(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)[ .-]?|\d{3}[ .-])\d{3}[ .-]\d{4}(?![\w-])"
    r"|(?<![\w+])\+\d{1,3}(?:[ .-]?\d{2,4}){2,5}(?![\w-])"
    r"|\b\d{1,5}\s+(?:[A-Z][a-z]+\s+){1,3}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Dr|Boulevard|Blvd|Court|Ct|Way|Place|Pl)\b"
)
# A quote of at least this many characters counts as a cited source quote.
MIN_CITED_QUOTE = 20
# Every field an action stores is screened and counted, ids, keys and tags included.
STORED_FIELDS: dict[str, tuple[str, ...]] = {
    "putRecords": ("writes", "store", "store_name", "key", "value", "tags"),
    "enqueue": ("brief", "task_id", "idempotency_key", "tags"),
    "submit": ("output", "execution_report", "error", "idempotency_key"),
}


def _identity_or_payment(text: str) -> bool:
    return bool(
        _EMAIL.search(text)
        or _IDENTITY.search(text)
        or any(sum(ch.isdigit() for ch in m.group(0)) >= 10 for m in _IBAN.finditer(text))
    )


def has_personal_data(text: str) -> bool:
    return _identity_or_payment(text) or bool(_CONTACT.search(text))


def _cited_quotes(value: Any) -> list[str]:
    """Every string under a `quote` key, at any depth, long enough to be a cited source quote."""
    if isinstance(value, dict):
        found = [v for k, v in value.items() if k == "quote" and isinstance(v, str)]
        return [q for q in found if len(q.strip()) >= MIN_CITED_QUOTE] + [
            q for v in value.values() for q in _cited_quotes(v)
        ]
    if isinstance(value, list):
        return [q for item in value for q in _cited_quotes(item)]
    return []


def submission_has_personal_data(encoded: str, output: Any) -> bool:
    """Research outputs may cite public contact details: a phone number or street address that
    appears in a cited source quote (or in text repeating that quote) passes. Email addresses,
    identity, card and IBAN numbers never do, and request inputs get no such allowance. The quote
    itself is checked against its source by the verb's reviewer, not here."""
    quotes = sorted(set(_cited_quotes(output)), key=len, reverse=True)
    text = encoded
    for quote in quotes:
        text = text.replace(json.dumps(quote, ensure_ascii=False)[1:-1], " ")
    return has_personal_data(text) or any(_identity_or_payment(q) for q in quotes)


def stored_payload(action: str, args: dict[str, Any]) -> str:
    """The fields an action would store, as the JSON text that is screened and counted."""
    stored = {name: args.get(name) for name in STORED_FIELDS.get(action, ()) if name in args}
    return json.dumps(stored, ensure_ascii=False, separators=(",", ":"))


# Successful actions keep one audit row each, reads included. Denied, failed and no-op
# requests and internal pre-checks are counted per minute instead (same rule as the Worker).
PRECHECK_ACTIONS = frozenset(
    {"verifyKey", "verifyStoreScope", "authorizeBlobWrite", "authorizeObjectWrite"}
)
COUNTER_WINDOW_SECONDS = 60
COUNTERS_NOTE = (
    "Denied, failed and no-op requests and internal pre-checks are counted per minute instead of "
    "one row each; counts not yet written may be lost if the object restarts."
)


def audit_mode(action: str, status: int, output: dict[str, Any], *, has_grant: bool) -> str:
    """Return row, count or skip for one access event."""
    if not has_grant:
        return "skip"
    if status >= 400 or action in PRECHECK_ACTIONS:
        return "count"
    if action == "claim" and not (output.get("task_id") or output.get("lease")):
        return "count"
    return "row"


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
    "recallExecution",
    "getExecution",
    "listExecutions",
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
    "recallExecution",
    "getExecution",
    "listExecutions",
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
# Outside its own namespace an anonymous session may only work an open verb and read its own work:
# the task it claimed and its own submissions to it. Claim events would show other attempts, so
# they stay closed.
OPEN_VERB_ACTIONS = frozenset(
    {"claim", "heartbeat", "submit", "task", "results", "listVerbs", "me", "my_audit"}
)
OWN_WORK_ACTIONS = frozenset({"task", "results"})


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
        "queues": grant.queues,
        "kinds": grant.kinds,
        "effect_classes": grant.effect_classes,
        "labels": grant.labels,
        "expires_at": grant.expires_at,
        "quotas": DEMO_QUOTAS
        if grant.anonymous
        else _requester_quotas()
        if grant.requester
        else None,
        "quota_reason": None
        if grant.anonymous or grant.requester
        else "Operator-configured queue grants and service request limits apply.",
    }


GLOBAL_SESSIONS_PER_HOUR = 1000


def _requester_quotas() -> dict[str, int]:
    from tessera._requesters import REQUESTER_QUOTAS

    return REQUESTER_QUOTAS


def ip_group(ip: str) -> str:
    """Attribute IPv6 callers by /64 and IPv4-mapped addresses by IPv4 (same rule as the Worker)."""
    raw = ip.strip().lower().strip("[]").split("%")[0]
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return raw
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


ClaimCap = tuple[str, int, str, int | None]


def claim_caps(
    key_id: str, namespace: str, ip_hash: str | None, ip_window: int | None = None
) -> list[ClaimCap]:
    """An anonymous session's hourly claim limits: subject, ceiling, refusal and the hour it
    counts in (None: the claim's own; the IP hash's: the hour the hash was computed). Same rule as
    the Worker. They count granted leases only; an empty poll costs a request, never a claim."""
    caps: list[ClaimCap] = [(key_id, DEMO_QUOTAS["claims_per_hour"], "demo quota exceeded", None)]
    if namespace == OPEN_VERB_NAMESPACE:
        # Open verbs are shared work: also capped per session and per IP hash.
        refusal = "open verb claim quota exceeded"
        caps += [
            (f"open:{key_id}", DEMO_QUOTAS["open_claims_per_hour"], refusal, None),
            (
                f"open-ip:{ip_hash or 'unknown'}",
                DEMO_QUOTAS["open_claims_per_ip_hour"],
                refusal,
                ip_window,
            ),
        ]
    return caps


def check_claim_caps(c: Any, caps: list[ClaimCap], now: float) -> None:
    """Inside the claim transaction, before a lease is chosen."""
    for subject, ceiling, refusal, hour in caps:
        row = c.execute(
            "SELECT claims FROM access_limits WHERE subject=? AND window=?",
            (subject, int(now // 3600) if hour is None else hour),
        ).fetchone()
        if row is not None and row[0] >= ceiling:
            raise RateLimited(refusal, 3600 - now % 3600)


def charge_claim_caps(c: Any, caps: list[ClaimCap], now: float) -> None:
    """Inside the claim transaction, once a lease is granted."""
    for subject, _, _, hour in caps:
        c.execute(
            "INSERT INTO access_limits(subject,window,claims) VALUES (?,?,1)"
            " ON CONFLICT(subject,window) DO UPDATE SET claims=claims+1",
            (subject, int(now // 3600) if hour is None else hour),
        )


def mint_allowance(ceiling: int, seconds_into_hour: float, used: int) -> tuple[int, float]:
    """Sessions allowed so far this hour and, when exhausted, seconds until the next one.

    The hourly allowance accrues evenly after a burst of a tenth, so one caller cannot spend it
    in minutes and lock everyone else out until the next hour (same rule as the Worker).
    """
    burst = math.ceil(ceiling / 10)
    allowed = min(ceiling, burst + math.floor(ceiling * seconds_into_hour / 3600))
    if used < ceiling:
        return allowed, max(1.0, math.ceil((used - burst + 1) * 3600 / ceiling - seconds_into_hour))
    return allowed, max(1.0, math.ceil(3600 - seconds_into_hour))


class PrincipalOps(AccessSchemaOps):
    def request_identity(self, ip: str, ua: str) -> dict[str, Any]:
        """Daily-rotated HMACs of the caller's address group and agent, and `ip_window`, the hour
        they were computed in. Per-IP limits count in that hour, never a later one, so a request
        that crosses midnight cannot charge the new day's hash to the old day's allowance."""
        with self._read() as c:
            salt = c.execute("SELECT value FROM meta WHERE key='access_hash_salt'").fetchone()[0]
        now = self.clock.now()
        day = int(now // 86400)
        hashes: dict[str, Any] = {
            name: hmac.new(
                salt.encode(), f"{day}:{kind}:{value}".encode(), hashlib.sha256
            ).hexdigest()
            for name, kind, value in (
                ("ip_hash", "ip", ip_group(ip)[:128]),
                ("ua_hash", "ua", ua[:512]),
            )
        }
        return {**hashes, "ip_window": int(now // 3600)}

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
        self, agent_label: str = "", *, ip_hash: str = "unknown", ip_window: int | None = None
    ) -> dict[str, Any]:
        if len(agent_label) > 64:
            raise InvalidRequest("agent_label must be at most 64 characters")
        label = re.sub(r"[^a-zA-Z0-9 ._:-]", "", agent_label)[:64] or "agent"
        now = self.clock.now()
        window = int(now // 3600)
        with self._tx() as c:
            for subject, maximum, paced, hour in (
                (f"mint:{ip_hash}", 10, False, window if ip_window is None else ip_window),
                ("mint:global", GLOBAL_SESSIONS_PER_HOUR, True, window),
            ):
                c.execute(
                    "INSERT OR IGNORE INTO access_limits(subject,window) VALUES (?,?)",
                    (subject, hour),
                )
                row = c.execute(
                    "SELECT requests FROM access_limits WHERE subject=? AND window=?",
                    (subject, hour),
                ).fetchone()
                allowed, wait = (
                    mint_allowance(maximum, now % 3600, row[0])
                    if paced
                    else (maximum, 3600 - now % 3600)
                )
                if row[0] >= allowed:
                    raise RateLimited("anonymous session mint quota exceeded", wait)
                c.execute(
                    "UPDATE access_limits SET requests=requests+1 WHERE subject=? AND window=?",
                    (subject, hour),
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
        self,
        grant: KeyGrant,
        action: str,
        args: dict[str, Any],
        namespace: str,
        *,
        ip_hash: str | None = None,
        ip_window: int | None = None,
    ) -> None:
        open_verbs = grant.uses_open_verbs(namespace)
        reachable = open_verbs or grant.files_requests(namespace)
        if not (reachable or grant.allows_namespace(namespace)):
            raise ScopeDenied("this principal may not access this namespace")
        fine = any(":" in s and not s.startswith(("surface:", "namespace:")) for s in grant.scopes)
        required = SCOPES.get(action, "nouns:read" if action in READ_ACTIONS else None)
        if not grant.is_admin and fine and required and required not in grant.scopes:
            raise ScopeDenied(f"missing {required} scope")
        if grant.requester:
            cast("Store", self).enforce_requester(grant, action, args, namespace)
            return
        if not grant.anonymous:
            return
        if open_verbs:
            self._enforce_open_verb(grant, action, args, namespace)
        elif action not in ANON_ACTIONS:
            raise ScopeDenied("anonymous sessions may only use isolated demo nouns and verbs")
        else:
            for name in ("queue", "store", "store_name"):
                if args.get(name) and not str(args[name]).startswith("demo."):
                    raise ScopeDenied("anonymous sessions cannot access production nouns or verbs")
        writes = args.get("writes", [])
        if not isinstance(writes, list):
            raise InvalidRequest("writes must be a list")
        for write in writes:
            if not isinstance(write, dict) or not str(write.get("store", "")).startswith("demo."):
                raise ScopeDenied("demo store required")
        encoded = stored_payload(action, args)
        screened = (
            submission_has_personal_data(encoded, args.get("output"))
            if action == "submit"
            else has_personal_data(encoded)
        )
        if action in WRITE_ACTIONS and screened:
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
            if row["requests"] >= DEMO_QUOTAS["requests_per_hour"] or (
                action in WRITE_ACTIONS and total + size > DEMO_QUOTAS["storage_bytes"]
            ):
                raise RateLimited("demo quota exceeded", 3600 - now % 3600)
            if action == "claim":
                # Claims are charged by the claim itself, only when it grants a lease.
                check_claim_caps(c, claim_caps(grant.key_id, namespace, ip_hash, ip_window), now)
            c.execute(
                "UPDATE access_limits SET requests=requests+1,bytes=bytes+? WHERE subject=? AND window=?",
                (size if action in WRITE_ACTIONS else 0, grant.key_id, window),
            )

    def _enforce_open_verb(
        self, grant: KeyGrant, action: str, args: dict[str, Any], namespace: str
    ) -> None:
        """Deny by default: claim, heartbeat and submit open verbs, and read only one's own work."""
        if action not in OPEN_VERB_ACTIONS:
            raise ScopeDenied(
                "outside its own namespace an anonymous session may only claim, heartbeat and"
                " submit open verbs"
            )
        if args.get("queue") and str(args["queue"]) in CLOSED_TO_ANONYMOUS:
            raise ScopeDenied("review, approval and publication verbs are closed to anonymous")
        if action in OWN_WORK_ACTIONS:
            task_id = str(args.get("task_id") or "")
            with self._read() as c:
                own = c.execute(
                    "SELECT 1 FROM leases WHERE namespace=? AND task_id=? AND key_id=?"
                    " UNION ALL SELECT 1 FROM results"
                    " WHERE namespace=? AND task_id=? AND completed_by_key=? LIMIT 1",
                    (namespace, task_id, grant.key_id, namespace, task_id, grant.key_id),
                ).fetchone()
            if own is None:
                raise NotFound("task does not exist")

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
        run_id = args.get("run_id")
        if not isinstance(run_id, str) or not re.fullmatch(r"run_[a-f0-9]{64}", run_id):
            run_id = None
        resource = (
            run_id
            or nested.get("task_id")
            or args.get("task_id")
            or args.get("key")
            or (writes[0].get("key") if writes and isinstance(writes[0], dict) else None)
            or args.get("queue")
        )
        resource_type = (
            "noun"
            if run_id
            else "verb"
            if args.get("task_id") or action == "claim"
            else "noun"
            if writes or args.get("key")
            else "access"
        )
        if action in {"mint_key", "revoke_key"}:
            resource = out.get("key_id") or args.get("key_id")
            resource_type = "key"
        now = self.clock.now()
        mode = audit_mode(action, status, out, has_grant=grant is not None)
        if mode == "skip" or grant is None:
            return
        if mode == "count":
            window = int(now // COUNTER_WINDOW_SECONDS)
            outcome = (
                f"{status}:{error_code or 'unknown'}"
                if status >= 400
                else "empty"
                if action == "claim"
                else str(status)
            )
            with self._tx() as c:
                c.execute(
                    "INSERT INTO access_counters(window,principal_id,namespace,action,outcome,"
                    "principal_kind,count,first_ts,last_ts) VALUES (?,?,?,?,?,?,1,?,?)"
                    " ON CONFLICT(window,principal_id,namespace,action,outcome)"
                    " DO UPDATE SET count=count+1,last_ts=excluded.last_ts",
                    (
                        window,
                        grant.principal_id or grant.key_id,
                        meta.get("namespace") or grant.namespace,
                        action,
                        outcome,
                        grant.principal_kind,
                        now,
                        now,
                    ),
                )
                c.execute(
                    "DELETE FROM access_counters WHERE window<?",
                    (int((now - 86400) // COUNTER_WINDOW_SECONDS),),
                )
            return
        with self._tx() as c:
            c.execute(
                """INSERT INTO access_events(request_id,ts,namespace,principal_kind,principal_id,session_id,
                key_id,account_id,ip_hash,ua_hash,surface,route_or_tool,action,resource_type,resource_id,receipt_id,
                status,bytes_in,bytes_out,error_code,labels,scopes_used) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    meta.get("request_id") or secrets.token_hex(16),
                    now,
                    meta.get("namespace") or (grant.namespace if grant else "main"),
                    grant.principal_kind,
                    grant.principal_id or grant.key_id,
                    grant.session_id,
                    grant.key_id if not grant.anonymous else None,
                    grant.account_id,
                    meta.get("ip_hash"),
                    meta.get("ua_hash"),
                    grant.bound_surface() or "tessera",
                    meta.get("route_or_tool") or action,
                    action,
                    resource_type,
                    resource,
                    nested.get("receipt_hash") or nested.get("claim_receipt"),
                    status,
                    meta.get("bytes_in", 0),
                    meta.get("bytes_out", 0),
                    error_code,
                    json.dumps(grant.labels),
                    json.dumps(
                        [SCOPES[action]]
                        if action in SCOPES
                        else ["nouns:read"]
                        if action in READ_ACTIONS
                        else []
                    ),
                ),
            )
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
            since = int((self.clock.now() - 86400) // COUNTER_WINDOW_SECONDS)
            capped = max(1, min(limit, 200))
            counters = (
                c.execute(
                    "SELECT * FROM access_counters WHERE window>=?"
                    " ORDER BY window DESC, last_ts DESC LIMIT ?",
                    (since, capped),
                ).fetchall()
                if admin
                else c.execute(
                    "SELECT * FROM access_counters WHERE principal_id=? AND window>=?"
                    " ORDER BY window DESC, last_ts DESC LIMIT ?",
                    (grant.principal_id or grant.key_id, since, capped),
                ).fetchall()
            )
        return {
            "events": [
                {
                    **dict(row),
                    "labels": json.loads(row["labels"]),
                    "scopes_used": json.loads(row["scopes_used"]),
                }
                for row in rows
            ],
            "counters": [
                {
                    **dict(row),
                    "window_start": row["window"] * COUNTER_WINDOW_SECONDS,
                    "window_seconds": COUNTER_WINDOW_SECONDS,
                }
                for row in counters
            ],
            "counters_note": COUNTERS_NOTE,
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
