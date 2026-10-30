"""Per-IP limits key on an address the client cannot choose (finding 6), counted in the hour its
daily-rotated hash was computed (finding 14).

The serve tests run the real `serve` command with uvicorn.run captured, then wrap the app exactly
as uvicorn does for the options `serve` chose. Synthetic data only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from tessera import Store, cli
from tessera.access import ServiceSettings
from tessera.clock import FakeClock
from tessera.service import create_app

PROFILE = {"display_name": "Synthetic Food Bank", "organization_type": "Food pantry"}


def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trusted: str) -> Any:
    captured: dict[str, Any] = {}

    def capture(app: Any, **options: Any) -> None:
        captured.update(app=app, **options)

    monkeypatch.setenv("TESSERA_REQUESTER_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("TESSERA_TRUSTED_PROXIES", trusted)
    monkeypatch.delenv("TESSERA_EDGE_SECRET", raising=False)
    monkeypatch.setattr(uvicorn, "run", capture)
    assert cli.main(["--db", str(tmp_path / f"serve-{len(trusted)}.db"), "serve"]) == 0
    app = captured["app"]
    if captured.get("proxy_headers"):
        app = ProxyHeadersMiddleware(app, trusted_hosts=captured["forwarded_allow_ips"])
    return app


def sign_ups(app: Any) -> list[int]:
    with TestClient(app) as client:
        return [
            client.post(
                "/v1/session/requester",
                json={"profile": {**PROFILE, "display_name": f"Synthetic Org {i}"}},
                headers={"X-Forwarded-For": f"203.0.113.{i}"},
            ).status_code
            for i in range(4)
        ]


def test_a_client_cannot_reset_per_ip_limits_by_naming_addresses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Three sign-ups per address an hour: the fourth is refused whatever address it names.
    assert sign_ups(served(tmp_path, monkeypatch, "")) == [201, 201, 201, 429]


def test_a_configured_proxy_is_believed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The test client connects as "testclient"; naming it as the proxy makes its forwarding count.
    assert sign_ups(served(tmp_path, monkeypatch, "testclient")) == [201, 201, 201, 201]


def test_a_request_crossing_midnight_cannot_spend_two_days_allowances(tmp_path: Path) -> None:
    """The IP hash rotates at midnight UTC. A sign-up whose hash is computed just before midnight
    but charged just after counts in the old hour, which the same address has already spent."""
    clock = FakeClock()
    store = Store(str(tmp_path / "midnight.db"), clock=clock)
    settings = ServiceSettings(
        requester_signup_enabled=True, reaper_seconds=86400, public_url="http://testserver"
    )
    midnight = (int(clock.now() // 86400) + 1) * 86400
    clock.advance(midnight - 30 - clock.now())
    identify = store.request_identity

    def slow_identity(ip: str, ua: str) -> dict[str, Any]:
        out = identify(ip, ua)
        clock.advance(latency[0])  # the time between identifying a request and charging it
        return out

    latency = [0.0]
    store.request_identity = slow_identity  # type: ignore[method-assign]
    with TestClient(create_app(store, settings)) as client:

        def sign_up(name: str) -> int:
            profile = {**PROFILE, "display_name": name}
            return client.post("/v1/session/requester", json={"profile": profile}).status_code

        assert [sign_up(f"Synthetic Org {i}") for i in range(3)] == [201, 201, 201]
        clock.advance(midnight - 0.005 - clock.now())
        latency[0] = 0.01  # identified at 23:59:59.995, charged at 00:00:00.005
        assert sign_up("Synthetic Straddler") == 429
        latency[0] = 0.0
        clock.advance(60)  # a new day and hour: a new hash with a fresh allowance, as designed
        assert sign_up("Synthetic Next Day") == 201
    store.close()
