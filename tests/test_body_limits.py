"""Python bounds bodies as the Worker does, before authentication (H-60): 64 KiB for anonymous
sessions, 16 MiB for uploads and 1 MiB for everything else. Synthetic data only."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from tessera import QueueConfig, Store
from tessera.access import ServiceSettings
from tessera.clock import FakeClock
from tessera.service import create_app


def test_bodies_over_the_worker_caps_are_refused_before_they_are_used(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "limits.db"), clock=FakeClock())
    secret, _ = store.issue_key("operator", queues=["*"], scopes=["admin", "store"])
    store.define_queue(QueueConfig(name="q"))
    settings = ServiceSettings(
        anonymous_access_enabled=True, reaper_seconds=86400, public_url="http://testserver"
    )
    keyed = {"Authorization": "Bearer " + secret, "Content-Type": "application/json"}
    big = json.dumps({"queue": "q", "brief": {"pad": "x" * (1536 * 1024)}})
    small = json.dumps({"queue": "q", "brief": {"pad": "x" * (512 * 1024)}})
    with TestClient(create_app(store, settings)) as client:
        assert client.post("/v1/tasks", content=small, headers=keyed).status_code == 201
        assert client.post("/v1/tasks", content=big, headers=keyed).status_code == 413
        # Without a key the body is refused for its size, not read and then refused for the key.
        anonymous = {"Content-Type": "application/json"}
        assert client.post("/v1/tasks", content=big, headers=anonymous).status_code == 413
        session = client.post("/v1/session/anonymous", json={"agent_label": "limits"}).json()
        demo = {**anonymous, "Authorization": "Bearer " + session["session_token"]}
        record = json.dumps({"value": {"pad": "x" * 70000}, "expected_version": 0})
        assert (
            client.post(
                "/v1/stores/demo.artifacts/records/k", content=record, headers=demo
            ).status_code
            == 413
        )
        assert store.counts()["q"].ready == 1
    store.close()
