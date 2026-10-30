"""Requester keys: self-serve sign-up for organizations that ask for work, with no operator step.

A sign-up mints an ordinary hashed key with one narrow, fixed scope. Its holder writes nouns only in
its own `org.<principal_id>` namespace (its profile, requests and received deliveries), files
`request` verbs into the shared request queue, and follows the request verbs it filed. Everything
else is denied. Sign-ups are capped per IP hash and globally; writes are screened for common
personal and payment data and bounded in size and rate. This is not a second auth system: it is the
existing key, principal, audit and limit machinery with one more principal kind.
"""

from __future__ import annotations

import json
import re
import secrets
from typing import TYPE_CHECKING, Any, cast

from tessera._principals import (
    DemoPayloadTooLarge,
    has_personal_data,
    mint_allowance,
    stored_payload,
)
from tessera.errors import InvalidRequest, NotFound, RateLimited, ScopeDenied
from tessera.grants import REQUEST_KIND, REQUEST_QUEUE, REQUESTER_STORE_PREFIX, KeyGrant
from tessera.models import RecordWrite

if TYPE_CHECKING:
    from tessera.store import Store

REQUESTER_QUOTAS = {
    "key_days": 30,
    "signups_per_ip_hour": 3,
    "requests_per_hour": 300,
    "request_verbs_per_hour": 5,
    "request_bytes": 32768,
    "storage_bytes": 262144,
}
GLOBAL_SIGNUPS_PER_HOUR = 100
REQUESTER_LABELS: dict[str, Any] = {"origin": "requester", "untrusted": True}
OWN_ACTIONS = frozenset(
    {
        "putRecords",
        "getRecord",
        "listRecords",
        "recordHistory",
        "listStores",
        "searchNouns",
        "me",
        "my_audit",
    }
)
SHARED_ACTIONS = frozenset({"enqueue", "task", "results", "claimEvents", "me", "my_audit"})
# In the shared namespace these read one request verb, which must be one the key filed.
FOLLOW_ACTIONS = frozenset({"task", "results", "claimEvents"})
_TEXT = re.compile(r"[^\w .,&'()/:+-]", re.UNICODE)


def _clean(value: Any, field: str, low: int, high: int) -> str:
    text = _TEXT.sub("", str(value or "")).strip()
    if not low <= len(text) <= high:
        raise InvalidRequest(f"profile {field} must be {low} to {high} characters")
    return text


def clean_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """An organization profile noun: a display name, an organization type and causes, nothing more."""
    causes = profile.get("causes") or []
    if not isinstance(causes, list) or len(causes) > 8:
        raise InvalidRequest("profile causes must be a list of at most 8 short phrases")
    clean = {
        "display_name": _clean(profile.get("display_name"), "display_name", 2, 80),
        "organization_type": _clean(profile.get("organization_type"), "organization_type", 3, 120),
        "causes": [_clean(c, "cause", 2, 40) for c in causes],
    }
    if has_personal_data(json.dumps(clean, ensure_ascii=False)):
        raise InvalidRequest("profiles must not contain personal or payment data")
    return clean


class RequesterOps:
    """Mixed into Store; uses its transactions, keys and scope context."""

    def open_requester_session(
        self, profile: dict[str, Any], *, ip_hash: str = "unknown"
    ) -> dict[str, Any]:
        store = cast("Store", self)
        clean = clean_profile(profile)
        now = store.clock.now()
        window = int(now // 3600)
        with store._tx() as c:
            for subject, ceiling, paced in (
                (f"signup:{ip_hash}", REQUESTER_QUOTAS["signups_per_ip_hour"], False),
                ("signup:global", GLOBAL_SIGNUPS_PER_HOUR, True),
            ):
                c.execute(
                    "INSERT OR IGNORE INTO access_limits(subject,window) VALUES (?,?)",
                    (subject, window),
                )
                used = c.execute(
                    "SELECT requests FROM access_limits WHERE subject=? AND window=?",
                    (subject, window),
                ).fetchone()[0]
                allowed, wait = (
                    mint_allowance(ceiling, now % 3600, used)
                    if paced
                    else (ceiling, 3600 - now % 3600)
                )
                if used >= allowed:
                    raise RateLimited("requester sign-up quota exceeded", wait)
                c.execute(
                    "UPDATE access_limits SET requests=requests+1 WHERE subject=? AND window=?",
                    (subject, window),
                )
        principal_id = "r_" + secrets.token_hex(16)
        namespace = "org." + principal_id
        labels = {**REQUESTER_LABELS, "display_name": clean["display_name"]}
        secret, grant = store.issue_key(
            clean["display_name"],
            queues=[REQUEST_QUEUE],
            kinds=[REQUEST_KIND],
            # A request asks for internal writes (kits, reports); the key itself never claims.
            effect_classes=["read", "write-internal"],
            scopes=["nouns:read", "cas:write", "verbs:submit", f"namespace:{namespace}"],
            days=REQUESTER_QUOTAS["key_days"],
            account_id=f"requester:{principal_id}",
            principal_kind="requester",
            principal_id=principal_id,
            namespace=namespace,
            labels=labels,
        )
        grant = store.principal_metadata(grant)
        with store.scope_context(
            namespace=namespace, key_id=grant.key_id, requester=True, labels=labels
        ):
            store.put_records(
                [
                    RecordWrite(
                        store=f"{REQUESTER_STORE_PREFIX}profile",
                        key="profile",
                        expected_version=0,
                        value={"schema_version": 1, **clean},
                        tags=["kind:organization_profile"],
                    )
                ]
            )
        from tessera._principals import public_principal

        return {"key": secret, **public_principal(grant), "profile": clean}

    def enforce_requester(
        self, grant: KeyGrant, action: str, args: dict[str, Any], namespace: str
    ) -> None:
        """Deny by default: own-namespace nouns, and filing and following request verbs."""
        store = cast("Store", self)
        shared = grant.files_requests(namespace)
        if action not in (SHARED_ACTIONS if shared else OWN_ACTIONS):
            raise ScopeDenied(
                "a requester key writes nouns in its own namespace and files request verbs"
            )
        if shared and action == "enqueue":
            if args.get("queue") != REQUEST_QUEUE or (args.get("kind") or "task") != REQUEST_KIND:
                raise ScopeDenied(
                    f"requesters may only file {REQUEST_KIND} verbs in {REQUEST_QUEUE}"
                )
            if args.get("depends_on"):
                raise ScopeDenied("a request verb cannot depend on other verbs")
        elif shared and action in FOLLOW_ACTIONS:
            with store._read() as c:
                own = c.execute(
                    "SELECT 1 FROM tasks WHERE namespace=? AND id=? AND owner_key_id=?"
                    " AND parent_id IS NULL",
                    (namespace, str(args.get("task_id") or ""), grant.key_id),
                ).fetchone()
            if own is None:
                raise NotFound("task does not exist")
        elif not shared:
            names = [args.get("store"), args.get("store_name")]
            names += [w.get("store") for w in args.get("writes") or [] if isinstance(w, dict)]
            if any(n and not str(n).startswith(REQUESTER_STORE_PREFIX) for n in names):
                raise ScopeDenied("requester nouns live in org. stores of their own namespace")
        encoded = stored_payload(action, args)
        writing = action in {"putRecords", "enqueue"}
        if writing and has_personal_data(encoded):
            raise InvalidRequest("requests and nouns must not contain personal or payment data")
        size = len(encoded.encode())
        if size > REQUESTER_QUOTAS["request_bytes"]:
            raise DemoPayloadTooLarge("requester request exceeds byte limit")
        now = store.clock.now()
        window = int(now // 3600)
        with store._tx() as c:
            for subject in (grant.key_id, f"request:{grant.key_id}"):
                c.execute(
                    "INSERT OR IGNORE INTO access_limits(subject,window) VALUES (?,?)",
                    (subject, window),
                )
            row = c.execute(
                "SELECT * FROM access_limits WHERE subject=? AND window=?", (grant.key_id, window)
            ).fetchone()
            filed = c.execute(
                "SELECT claims FROM access_limits WHERE subject=? AND window=?",
                (f"request:{grant.key_id}", window),
            ).fetchone()[0]
            total = c.execute(
                "SELECT COALESCE(SUM(bytes),0) FROM access_limits WHERE subject=?", (grant.key_id,)
            ).fetchone()[0]
            if (
                row["requests"] >= REQUESTER_QUOTAS["requests_per_hour"]
                or (action == "enqueue" and filed >= REQUESTER_QUOTAS["request_verbs_per_hour"])
                or (writing and total + size > REQUESTER_QUOTAS["storage_bytes"])
            ):
                raise RateLimited("requester quota exceeded", 3600 - now % 3600)
            c.execute(
                "UPDATE access_limits SET requests=requests+1,bytes=bytes+? WHERE subject=? AND window=?",
                (size if writing else 0, grant.key_id, window),
            )
            if action == "enqueue":
                c.execute(
                    "UPDATE access_limits SET claims=claims+1 WHERE subject=? AND window=?",
                    (f"request:{grant.key_id}", window),
                )
