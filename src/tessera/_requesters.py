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
import secrets
import unicodedata
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
# A profile's fields, as /connect.json publishes them (same rules as the Worker). Text is NFC
# normalized; letters, combining marks and digits of any script and the symbols below are kept and
# anything else is removed before the length is counted in code points. Control and format
# characters are refused, except the zero-width joiners that Persian and Indic names need.
PROFILE_FIELDS: dict[str, dict[str, int]] = {
    "display_name": {"min_length": 2, "max_length": 80},
    "organization_type": {"min_length": 3, "max_length": 120},
    "cause": {"min_length": 2, "max_length": 40},
}
MAX_CAUSES = 8
PROFILE_SYMBOLS = "_ .,&'()/:+-"
PROFILE_JOINERS = "\u200c\u200d"
RAW_LENGTH_FACTOR = 4  # input longer than four times a field's limit is refused unread


def profile_rules() -> dict[str, Any]:
    """The rules clean_profile applies, as /connect.json publishes them for clients to check up
    front (H-09). The Worker publishes the same object."""
    return {
        "normalization": "NFC",
        "kept": "letters, combining marks and digits of any script (Unicode L, M and N), the"
        f" zero-width joiners U+200C and U+200D, spaces and"
        f" {' '.join(ch for ch in PROFILE_SYMBOLS if ch != ' ')}",
        "kept_pattern": "[\\p{L}\\p{M}\\p{N}_ .,&'()/:+\\-\\u200C\\u200D]",
        "removed": "every other character, before lengths are counted",
        "refused": "control and format characters (Unicode Cc and Cf) other than U+200C and U+200D,"
        " and personal or payment data",
        "length_unit": "Unicode code points, after normalization and removal",
        "fields": PROFILE_FIELDS,
        "max_causes": MAX_CAUSES,
    }


def _clean(value: Any, field: str) -> str:
    low, high = PROFILE_FIELDS[field]["min_length"], PROFILE_FIELDS[field]["max_length"]
    text = unicodedata.normalize("NFC", str(value or ""))
    if len(text) > high * RAW_LENGTH_FACTOR:
        raise InvalidRequest(f"profile {field} must be {low} to {high} characters")
    if any(unicodedata.category(ch) in {"Cc", "Cf"} and ch not in PROFILE_JOINERS for ch in text):
        raise InvalidRequest(f"profile {field} must not contain control or format characters")
    kept = "".join(
        ch
        for ch in text
        if ch in PROFILE_SYMBOLS or ch in PROFILE_JOINERS or unicodedata.category(ch)[0] in "LMN"
    ).strip()
    if not low <= len(kept) <= high:
        raise InvalidRequest(f"profile {field} must be {low} to {high} characters")
    return kept


def clean_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """An organization profile noun: a display name, an organization type and causes, nothing more."""
    causes = profile.get("causes") or []
    if not isinstance(causes, list) or len(causes) > MAX_CAUSES:
        raise InvalidRequest(f"profile causes must be a list of at most {MAX_CAUSES} short phrases")
    clean = {
        "display_name": _clean(profile.get("display_name"), "display_name"),
        "organization_type": _clean(profile.get("organization_type"), "organization_type"),
        "causes": [_clean(c, "cause") for c in causes],
    }
    if has_personal_data(json.dumps(clean, ensure_ascii=False)):
        raise InvalidRequest("profiles must not contain personal or payment data")
    return clean


class RequesterOps:
    """Mixed into Store; uses its transactions, keys and scope context."""

    def open_requester_session(
        self, profile: dict[str, Any], *, ip_hash: str = "unknown", ip_window: int | None = None
    ) -> dict[str, Any]:
        store = cast("Store", self)
        clean = clean_profile(profile)
        now = store.clock.now()
        window = int(now // 3600)
        # The IP hash's allowance counts in the hour the hash was computed (request_identity).
        limits = (
            (
                f"signup:{ip_hash}",
                REQUESTER_QUOTAS["signups_per_ip_hour"],
                False,
                window if ip_window is None else ip_window,
            ),
            ("signup:global", GLOBAL_SIGNUPS_PER_HOUR, True, window),
        )
        with store._tx() as c:
            for subject, ceiling, paced, hour in limits:
                c.execute(
                    "INSERT OR IGNORE INTO access_limits(subject,window) VALUES (?,?)",
                    (subject, hour),
                )
                used = c.execute(
                    "SELECT requests FROM access_limits WHERE subject=? AND window=?",
                    (subject, hour),
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
                    (subject, hour),
                )
        principal_id = "r_" + secrets.token_hex(16)
        namespace = "org." + principal_id
        labels = {**REQUESTER_LABELS, "display_name": clean["display_name"]}
        try:
            return self._mint_requester(clean, principal_id, namespace, labels)
        except Exception:
            # Keep the key and the spent sign-up allowance only if the profile was stored too.
            with store._tx() as c:
                c.execute(
                    "DELETE FROM keys WHERE key_id IN"
                    " (SELECT key_id FROM access_principals WHERE principal_id=?)",
                    (principal_id,),
                )
                c.execute("DELETE FROM access_principals WHERE principal_id=?", (principal_id,))
                for subject, _, _, hour in limits:
                    c.execute(
                        "UPDATE access_limits SET requests=MAX(requests-1,0)"
                        " WHERE subject=? AND window=?",
                        (subject, hour),
                    )
            raise

    def _mint_requester(
        self, clean: dict[str, Any], principal_id: str, namespace: str, labels: dict[str, Any]
    ) -> dict[str, Any]:
        store = cast("Store", self)
        now = store.clock.now()
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
            # The key's own namespace goes with it when it expires (purge_expired_principals).
            purge_at=now + REQUESTER_QUOTAS["key_days"] * 86400,
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
