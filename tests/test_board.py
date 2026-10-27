"""Console Board v2 (/board, L19), UI chrome, and agent discovery routes."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tessera import ChildSpec, QueueConfig, Store, TaskSpec
from tessera.access import ServiceSettings
from tessera.service import create_app

_ROW_RE = re.compile(r"<tr data-queue='([^']+)'>(.*?)</tr>", re.DOTALL)
_COL_RE = re.compile(r"<td data-col='([^']+)'>([^<]*)</td>")


def _parse_board_queue_counts(html_text: str) -> dict[str, dict[str, int]]:
    parsed: dict[str, dict[str, int]] = {}
    for q_name, inner in _ROW_RE.findall(html_text):
        cols = dict(_COL_RE.findall(inner))
        parsed[q_name] = {
            state: int(cols[state])
            for state in ("ready", "leased", "waiting", "done", "dead", "cancelled")
        }
    return parsed


def test_board_matches_counts_on_empty_and_populated_fabric(tmp_path: Path) -> None:
    db_path = tmp_path / "board.db"
    store = Store(db_path)
    admin_secret, _ = store.issue_key("admin", queues=["*"], scopes=["admin", "store"])
    worker_a_secret, worker_a_grant = store.issue_key(
        "worker-a", queues=["*"], effect_classes=["read", "write-internal", "write-repo"]
    )
    app = create_app(store, ServiceSettings(db_path=str(db_path), reaper_seconds=3600))

    with TestClient(app) as client:
        # 1. Empty board renders cleanly with zero queues and zero write controls.
        empty_resp = client.get("/board")
        assert empty_resp.status_code == 200
        empty_html = empty_resp.text
        assert "No queues yet." in empty_html
        assert "Live Verb Queues" in empty_html
        assert "Task Dependency &amp; Split Wave DAG" in empty_html
        assert "Parked Tiles" in empty_html
        assert "Monotonic Cursor Event Feed" in empty_html
        assert ".table-wrap{overflow-x:auto" in empty_html

        counts_empty = client.get(
            "/v1/counts", headers={"authorization": f"Bearer {admin_secret}"}
        ).json()["queues"]
        assert _parse_board_queue_counts(empty_html) == counts_empty

        # 2. Populate all states, split waves, dependencies, parked tiles, and delta feeds.
        store.define_queue(QueueConfig(name="alpha", max_attempts=1))
        store.define_queue(QueueConfig(name="beta", max_attempts=3))

        # Done task + dependent task (waiting on another unresolved task)
        t_done = store.enqueue(TaskSpec(queue="alpha", brief={"step": "done"}))
        l_done = store.claim("alpha", "w1", key_id=worker_a_grant.key_id)
        assert l_done is not None
        store.submit(
            t_done,
            l_done.fencing_token,
            {"ok": 1},
            idempotency_key="b-done",
            completed_by_key=worker_a_grant.key_id,
        )

        # Dead task (max_attempts=1 on alpha)
        t_dead = store.enqueue(TaskSpec(queue="alpha", brief={"step": "fail"}))
        l_dead = store.claim("alpha", "w1", key_id=worker_a_grant.key_id)
        assert l_dead is not None
        store.submit(
            t_dead,
            l_dead.fencing_token,
            {"err": 1},
            idempotency_key="b-dead",
            ok=False,
            error="boom",
            completed_by_key=worker_a_grant.key_id,
        )

        # Cancelled task
        t_cancel = store.enqueue(TaskSpec(queue="alpha", brief={"step": "cancel"}))
        store.cancel(t_cancel, "no longer needed")

        # Parked task (waiting for input)
        t_park = store.enqueue(TaskSpec(queue="beta", brief={"step": "park"}))
        l_park = store.claim("beta", "w1", key_id=worker_a_grant.key_id)
        assert l_park is not None
        store.park(t_park, l_park.fencing_token, reason="awaiting reviewer", resume_within=600)

        # Split parent + wave children (child 0 ready, child 1 waiting on child 0)
        t_parent = store.enqueue(TaskSpec(queue="beta", brief={"step": "split"}))
        l_parent = store.claim("beta", "w1", key_id=worker_a_grant.key_id)
        assert l_parent is not None
        kids = store.split(
            t_parent,
            l_parent.fencing_token,
            [
                ChildSpec(brief={"wave": 1}),
                ChildSpec(brief={"wave": 2}, after=[0]),
            ],
        )

        # Leased task (claim child 0) + ready task + dependent task
        l_kid0 = store.claim("beta", "w2", key_id=worker_a_grant.key_id)
        assert l_kid0 is not None and l_kid0.task_id == kids[0]
        t_ready = store.enqueue(TaskSpec(queue="beta", brief={"step": "ready"}))
        t_dep = store.enqueue(
            TaskSpec(queue="alpha", brief={"step": "dep"}, depends_on=[t_done, t_ready])
        )

        # Monotonic feed entry
        store.publish_feed("ops.live", {"tile": t_ready}, summary="queued ready tile")

        # 3. Verify every queue's counts on /board match GET /v1/counts (L19).
        board_resp = client.get("/board")
        assert board_resp.status_code == 200
        board_html = board_resp.text
        api_counts: dict[str, Any] = client.get(
            "/v1/counts", headers={"authorization": f"Bearer {admin_secret}"}
        ).json()["queues"]
        board_counts = _parse_board_queue_counts(board_html)
        assert board_counts == api_counts

        # Verify DAG, Parked Tiles, and Feed entries appear in the read-only HTML.
        assert t_parent in board_html and t_dep in board_html
        assert t_park in board_html and "input" in board_html
        assert "ops.live" in board_html and "queued ready tile" in board_html

        # Strictly read-only: zero forms, inputs, textareas, or buttons on /board, and zero leaked secrets.
        for forbidden_tag in ("<form", "<input", "<textarea", "<button"):
            assert forbidden_tag not in board_html.lower()
        assert admin_secret not in board_html
        assert worker_a_secret not in board_html


def test_ui_chrome_and_discovery_endpoints_parity(tmp_path: Path) -> None:
    db_path = tmp_path / "chrome.db"
    store = Store(db_path)
    app = create_app(store, ServiceSettings(db_path=str(db_path), reaper_seconds=3600))

    with TestClient(app) as client:
        for route in ("/", "/connect", "/board", "/work"):
            resp = client.get(route)
            assert resp.status_code == 200, route
            assert "<header class='site-hdr'>" in resp.text
            assert "<nav class='site-nav'" in resp.text
            assert "<footer class='site-ftr'>" in resp.text
            assert '<svg class="brand-mark"' in resp.text

        connect_text = client.get("/connect").text
        assert "26 tools" in connect_text
        assert "25 tools" not in connect_text

        fav = client.get("/favicon.svg")
        assert fav.status_code == 200
        assert "image/svg+xml" in fav.headers["content-type"]
        assert "<svg" in fav.text

        llms = client.get("/llms.txt")
        assert llms.status_code == 200
        assert "26 tools" in llms.text

        llms_full = client.get("/llms-full.txt")
        assert llms_full.status_code == 200
        assert "text/plain" in llms_full.headers["content-type"]
        assert "Tessera" in llms_full.text

        h_v1 = client.get("/v1/health")
        h_api = client.get("/api/health")
        assert h_v1.status_code == 200 and h_api.status_code == 200
        assert h_api.json()["ok"] is True
        assert h_api.json()["version"] == "0.2.0"
        assert h_api.json()["queues"] == h_v1.json()["queues"]


def test_cloudflare_pages_parity() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = """
import {
  TESSERA_VERSION,
  renderBoardHtml,
  renderConnectHtml,
  renderFaviconSvg,
  renderLlmsFullTxt,
  renderLlmsTxt,
} from "./deploy/cloudflare/cloud_pages.js";

if (TESSERA_VERSION !== "0.2.0") throw new Error("bad version: " + TESSERA_VERSION);
const fav = renderFaviconSvg();
if (!fav.includes("<svg")) throw new Error("missing svg in favicon");
const connect = renderConnectHtml("https://example.test", "https://github.com/ta-amps/tessera");
if (!connect.includes("26 tools") || connect.includes("25 tools")) throw new Error("tool count mismatch");
if (!connect.includes("<header class='site-hdr'>") || !connect.includes("<footer class='site-ftr'>")) {
  throw new Error("missing semantic chrome");
}
const board = renderBoardHtml(
  {
    reaper_last_run: 1793070000,
    queues: [{ name: "solve", counts: { ready: 2, leased: 1, waiting: 3, done: 4, dead: 0, cancelled: 1 }, oldest_ready_age_seconds: 12, expired_leases: 0 }],
  },
  {
    namespace: "main",
    dag_nodes: [{ task_id: "t_1", queue: "solve", state: "waiting", parent_id: null, depends_on: ["t_0"], pending_deps: 1, pending_children: 0 }],
    parked_tiles: [{ task_id: "t_2", queue: "solve", wait_reason: "input", parked_by: "k_1", wake_at: null, parks: 1 }],
    events: [{ seq: 5, ts: 1793070001, type: "parked", queue: "solve", task_id: "t_2" }],
    latest_event_seq: 5,
    feed_entries: [{ seq: 1, feed: "ops", kind: "delta", summary: "ok", created_at: 1793070002 }],
  }
);
for (const heading of ["Live Verb Queues", "Task Dependency &amp; Split Wave DAG", "Parked Tiles", "Monotonic Cursor Event Feed"]) {
  if (!board.includes(heading)) throw new Error("missing heading: " + heading);
}
if (board.toLowerCase().includes("<form")) throw new Error("board must not contain forms");
const llms = renderLlmsTxt("https://example.test", "https://github.com/ta-amps/tessera");
const llmsFull = renderLlmsFullTxt("https://example.test", "https://github.com/ta-amps/tessera");
if (!llms.includes("26 tools") || !llmsFull.includes("0.2.0")) throw new Error("llms text mismatch");
"""
    res = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr + res.stdout
