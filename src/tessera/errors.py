"""Errors every Tessera backend raises. Each maps to one HTTP status and problem code (SPEC §8)."""

from __future__ import annotations


class TesseraError(Exception):
    status = 500
    code = "internal"

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.code)
        self.detail = detail or self.code


class InvalidRequest(TesseraError):
    status = 400
    code = "invalid_request"


class Unauthorized(TesseraError):
    status = 401
    code = "unauthorized"


class Forbidden(TesseraError):
    status = 403
    code = "forbidden"


class ScopeDenied(Forbidden):
    """The caller's key or surface identity lacks permission for the requested effect class or store."""

    status = 403
    code = "scope_denied"


class NotFound(TesseraError):
    status = 404
    code = "not_found"


class MethodNotAllowed(TesseraError):
    status = 405
    code = "method_not_allowed"


class StaleLease(TesseraError):
    """The caller's lease lapsed, was re-leased (higher fencing token) or never existed."""

    status = 409
    code = "stale_lease"


class VersionConflict(TesseraError):
    """A compare-and-set write expected a version that is no longer the head."""

    status = 409
    code = "version_conflict"

    def __init__(self, detail: str = "", current_version: int | None = None) -> None:
        super().__init__(detail)
        self.current_version = current_version


class IdempotencyConflict(TesseraError):
    """The idempotency key was already used for a different task."""

    status = 409
    code = "idempotency_conflict"


class InvalidState(TesseraError):
    """The task isn't in a state that allows this, for example resuming a task that isn't parked."""

    status = 409
    code = "invalid_state"


class RateLimited(TesseraError):
    status = 429
    code = "rate_limited"

    def __init__(self, detail: str = "", retry_after: float = 1.0) -> None:
        super().__init__(detail)
        self.retry_after = retry_after


class Unavailable(TesseraError):
    status = 503
    code = "unavailable"

    def __init__(self, detail: str = "", retry_after: float = 5.0) -> None:
        super().__init__(detail)
        self.retry_after = retry_after


class StorageQuotaExhausted(Unavailable):
    """The hosting provider's storage allowance is spent; retry after `reset_at` (UTC)."""

    code = "storage_quota_exhausted"

    def __init__(
        self, detail: str = "", retry_after: float = 5.0, reset_at: str | None = None
    ) -> None:
        super().__init__(detail, retry_after=retry_after)
        self.reset_at = reset_at


BY_CODE: dict[str, type[TesseraError]] = {
    cls.code: cls
    for cls in (
        TesseraError,
        InvalidRequest,
        Unauthorized,
        Forbidden,
        ScopeDenied,
        NotFound,
        MethodNotAllowed,
        StaleLease,
        VersionConflict,
        IdempotencyConflict,
        InvalidState,
        RateLimited,
        Unavailable,
        StorageQuotaExhausted,
    )
}
# The Worker refuses personal or payment data in demo and requester writes with example_only. Its
# former code is still understood, so a client keeps working against an older Worker.
BY_CODE["example_only"] = InvalidRequest
BY_CODE["synthetic_only"] = InvalidRequest  # compat-alias: tessera-error-code
