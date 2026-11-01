"""Learning reports, the observation read interface and swappable learners, end to end.

Everything runs against a real local Store and HTTP service; no provider or network call.
"""

from __future__ import annotations

import copy
import json
import random
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tessera import QueueConfig, Store, TaskSpec
from tessera.access import ServiceSettings
from tessera.client import Client
from tessera.errors import Forbidden, InvalidRequest, NotFound
from tessera.learners import (
    SPECS,
    Activation,
    LearnerConfig,
    LearnerSpec,
    NounRef,
    RunRequest,
    memory,
    pack,
    path_statistics,
    routing,
    run_spec,
    spec,
    value_digest,
)
from tessera.learners.runtime import load_run
from tessera.learners.worker import RUNNERS, run_claimed
from tessera.service import create_app

NS = "conformance-learning"
SOURCE = "conformance-source"
LEARNERS = "conformance-learners"
GOALS = [
    "EXAMPLE convert an mp4 clip into png frames at 24 fps",
    "EXAMPLE extract png frames from an mp4 video at 30 fps",
    "EXAMPLE summarize statute cards for wage law",
    "EXAMPLE convert a webm clip into png frames",
]


class Harness:
    def __init__(self, store: Store, client: TestClient) -> None:
        self.store, self.http = store, client

    def key(self, queues: list[str], *, surfaces: tuple[str, ...] = ()) -> str:
        token, _ = self.store.issue_key(
            "example-learning",
            queues=queues,
            scopes=[
                "nouns:read",
                "cas:write",
                "verbs:claim",
                "verbs:submit",
                f"namespace:{NS}",
                *surfaces,
            ],
            days=1,
        )
        return token

    def client(self, token: str) -> Client:
        def dispatch(request: httpx.Request) -> httpx.Response:
            query = request.url.query.decode()
            response = self.http.request(
                request.method,
                f"{request.url.path}?{query}" if query else request.url.path,
                content=request.content,
                headers=dict(request.headers),
            )
            return httpx.Response(
                response.status_code, headers=dict(response.headers), content=response.content
            )

        return Client(
            "http://testserver", key=token, namespace=NS, transport=httpx.MockTransport(dispatch)
        )

    def work(
        self, api: Client, goal: str, *, ok: bool = True, learning: dict[str, Any] | None = None
    ) -> str:
        video = "clip" in goal or "video" in goal
        task_id = api.enqueue(
            TaskSpec(
                queue=SOURCE,
                kind="verb.solve_draft",
                brief={"goal": goal, "contract": "tessera.example.v1"},
                visibility_scope="private",
            )
        )
        lease = api.claim(SOURCE, "example-worker")
        assert lease is not None and lease.task_id == task_id
        report = {
            "model": None,
            "summaries": {"problem": goal},
            "learning": learning
            if learning is not None
            else {
                "outcome": "succeeded" if ok else "failed",
                "path": {"shim": "json-native"},
                "types": {"input": "video", "output": "image/png[]"}
                if video
                else {"input": "text", "output": "text"},
            },
        }
        api.submit(
            lease.task_id,
            lease.fencing_token,
            {"example_only": True},
            idempotency_key=f"{lease.task_id}:{lease.fencing_token}",
            ok=ok,
            error=None if ok else "EXAMPLE ffmpeg missing\nstack line",
            execution_report=report,
        )
        return task_id

    def pin(self, api: Client, learner: LearnerSpec) -> NounRef:
        try:
            record = api.get_record("learn.specs", learner.key)
        except NotFound:
            record = api.put_records([learner.record(NS)])[0]
        return NounRef(
            store=record.store,
            key=record.key,
            version=record.version,
            value_sha256=value_digest(record.value),
        )

    def learn(self, api: Client, request: RunRequest, learner: LearnerSpec) -> dict[str, Any]:
        task = run_spec(request, learner)
        assert task.idempotency_key is not None
        api.enqueue(task, idempotency_key=task.idempotency_key)
        lease = api.claim(LEARNERS, "example-learner")
        assert lease is not None and lease.task_id == task.task_id
        return run_claimed(api, lease)


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    store = Store(tmp_path / "learning.db")
    with store.scope_context(namespace=NS):
        for queue in (SOURCE, LEARNERS, "conformance-other"):
            store.define_queue(
                QueueConfig(name=queue, lease_seconds=60, heartbeat_seconds=10, max_attempts=1)
            )
    app = create_app(store, ServiceSettings(db_path=store.path, reaper_seconds=3600))
    with TestClient(app) as client:
        yield Harness(store, client)


def memory_request(pin: NounRef, state_version: int = 0) -> RunRequest:
    return RunRequest(
        namespace=NS,
        queue=LEARNERS,
        spec=pin,
        source_queue=SOURCE,
        output_store="learn.memory",
        state_version=state_version,
    )


def test_learning_block_is_optional_stored_and_adds_no_rows(harness: Harness) -> None:
    api = harness.client(harness.key([SOURCE]))
    connection = harness.store._conn()

    def changes(**kwargs: Any) -> tuple[int, str]:
        before = connection.total_changes
        task_id = harness.work(api, "EXAMPLE count rows", **kwargs)
        return connection.total_changes - before, task_id

    with_learning, task_id = changes()
    without_learning, _ = changes(learning={})
    assert with_learning == without_learning
    run_id = api.list_executions(task_id)["executions"][0]["run_id"]
    report = api.get_execution(run_id)["report"]
    assert report["model"] is None and report["learning"]["outcome"] == "succeeded"
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert not {name for name in tables if "learning" in name or "learner" in name}

    api.enqueue(TaskSpec(queue=SOURCE, brief={"goal": "EXAMPLE invalid"}, task_id="bad"))
    lease = api.claim(SOURCE, "example-worker")
    assert lease is not None
    for learning in ({"outcome": "great"}, {"duration_seconds": None}, {"surprise": 1}):
        with pytest.raises(InvalidRequest, match=r"report\.learning"):
            api.submit(
                "bad",
                lease.fencing_token,
                {},
                idempotency_key="bad:1",
                execution_report={"learning": learning},
            )
    assert api.task("bad").state == "leased"
    assert api.list_executions("bad")["executions"] == []


def test_observations_are_permission_filtered_paginated_and_bounded(harness: Harness) -> None:
    owner = harness.client(harness.key([SOURCE]))
    for goal in GOALS[:3]:
        harness.work(owner, goal)
    first = owner.execution_observations(SOURCE, limit=2)
    assert [o["observation"] for o in first["observations"]] == ["execution-observation-v1"] * 2
    assert first["next_after"] == first["observations"][-1]["seq"]
    rest = owner.execution_observations(SOURCE, after=first["next_after"], limit=2)
    assert len(rest["observations"]) == 1 and rest["next_after"] is None
    seen = [o["run_id"] for o in first["observations"] + rest["observations"]]
    assert len(set(seen)) == 3
    item = first["observations"][0]
    assert item["category"] == "verb.solve_draft"
    assert item["brief"]["template"] == "tessera.example.v1"
    assert item["report"]["learning"]["path"] == {"shim": "json-native"}
    assert item["trust"]["telemetry"] == "worker_reported"
    assert "input_snapshot" not in item and "output" not in item
    assert owner.execution_observations(SOURCE, kind="verb.other")["observations"] == []

    # Another key with the same queue grant cannot see the owner's private runs.
    stranger = harness.client(harness.key([SOURCE]))
    assert stranger.execution_observations(SOURCE)["observations"] == []
    # A key without the queue grant is refused outright.
    elsewhere = harness.client(harness.key(["conformance-other"]))
    with pytest.raises(Forbidden):
        elsewhere.execution_observations(SOURCE)
    response = harness.http.get(f"/ns/{NS}/v1/executions", params={"queue": SOURCE, "after": "x"})
    assert response.status_code in (400, 401, 422)
    with pytest.raises(InvalidRequest):
        harness.store.execution_observations(
            SOURCE, grant=harness.store.find_key(harness.key([SOURCE])), limit=0
        )  # type: ignore[arg-type]


def test_mcp_observation_tool_uses_the_same_permissions(harness: Harness) -> None:
    token = harness.key([SOURCE])
    harness.work(harness.client(token), GOALS[0])
    init = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "example", "version": "0"},
    }
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json, text/event-stream",
        "X-Namespace": NS,
    }
    started = harness.http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init},
        headers=headers,
    )
    version = started.json()["result"]["protocolVersion"]
    headers["MCP-Protocol-Version"] = version
    called = harness.http.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_execution_observations", "arguments": {"queue": SOURCE}},
        },
        headers=headers,
    ).json()
    content = called["result"]["structuredContent"]
    assert len(content["observations"]) == 1 and content["evidence_only"] is True


def test_memory_learner_indexes_asynchronously_and_recalls_with_statistics(
    harness: Harness,
) -> None:
    api = harness.client(harness.key([SOURCE, LEARNERS]))
    for i, goal in enumerate(GOALS):
        harness.work(api, goal, ok=i != 1)
    learner = spec("memory", "minhash-lsh", 1)
    pin = harness.pin(api, learner)
    first = harness.learn(api, memory_request(pin), learner)
    assert first["output"]["outcome"] == "updated"
    assert first["output"]["observations_read"] == 4
    head = api.get_record("learn.memory", memory.head_key(SOURCE))
    assert head.version == 1 and head.value["entries"] == 4 and head.value["cursor"]
    assert head.visibility_scope == "private"

    # The learner's own run is an ordinary execution with an explicit no-model report.
    runs = api.list_executions(run_spec(memory_request(pin), learner).task_id or "")
    own = api.get_execution(runs["executions"][0]["run_id"])
    assert own["report"]["model"] is None and own["report"]["usage"]["total_tokens"] == 0
    assert own["report"]["learning"]["outcome"] == "succeeded"

    harness.work(api, "EXAMPLE convert an mkv clip into png frames")
    second = harness.learn(api, memory_request(pin, state_version=1), learner)
    assert second["output"]["observations_read"] == 1
    head = api.get_record("learn.memory", memory.head_key(SOURCE))
    assert head.version == 2 and len(head.value["segments"]) == 2 and head.value["entries"] == 5

    # The same state version is one verb identity: a duplicate schedule tick adds no work.
    again = run_spec(memory_request(pin, state_version=1), learner)
    assert api.enqueue(again, idempotency_key=again.idempotency_key or "") == again.task_id
    assert api.claim(LEARNERS, "example-learner") is None
    # A different run pinned to an older state cannot overwrite the newer head.
    harness.work(api, "EXAMPLE convert an avi clip into png frames")
    stale_request = memory_request(pin, state_version=1).model_copy(update={"max_observations": 50})
    stale = harness.learn(api, stale_request, learner)
    assert stale["output"]["outcome"] == "superseded"
    assert api.get_record("learn.memory", memory.head_key(SOURCE)).version == 2

    recalled = memory.recall(
        api,
        store="learn.memory",
        queue=SOURCE,
        category="verb.solve_draft",
        brief={
            "goal": "EXAMPLE convert a mov clip into png frames",
            "contract": "tessera.example.v1",
        },
        report={"learning": {"types": {"input": "video", "output": "image/png[]"}}},
    )
    assert recalled["executions"]
    assert all("input_snapshot" not in e["execution"] for e in recalled["executions"])
    assert all(e["ok"] for e in recalled["executions"])
    assert recalled["executions"][0]["similarity"] >= recalled["executions"][-1]["similarity"]
    statistics = recalled["statistics"]
    assert statistics["failed"] == 1 and statistics["failure_reasons"][0]["reason"] == (
        "example ffmpeg missing"
    )
    assert statistics["acceptance_rate"] == statistics["accepted"] / statistics["attempts"]
    assert recalled["advice"]["record_version"] == 2
    assert recalled["evidence_only"] and recalled["reuse_requires_validation"]
    assert all("statute" not in json.dumps(e) for e in recalled["executions"])

    # Recording the advice used is part of the ordinary report.
    harness.work(api, "EXAMPLE uses advice", learning={"advice": [recalled["advice"]]})

    # Index nouns stay private to the learner's principal.
    stranger = harness.client(harness.key([SOURCE]))
    with pytest.raises(NotFound):
        memory.recall(
            stranger, store="learn.memory", queue=SOURCE, category="verb.solve_draft", brief={}
        )


def test_path_statistics_scorecards_are_deterministic(harness: Harness) -> None:
    api = harness.client(harness.key([SOURCE, LEARNERS]))
    for i, goal in enumerate(GOALS):
        harness.work(api, goal, ok=i != 1)
    harness.work(api, "EXAMPLE other shim", learning={"path": {"shim": "xml-tags"}})
    observations = api.execution_observations(SOURCE)["observations"]
    shuffled = copy.deepcopy(observations)
    random.Random(7).shuffle(shuffled)
    assert path_statistics.aggregate(observations, group_by=["shim"]) == path_statistics.aggregate(
        shuffled, group_by=["shim"]
    )
    with pytest.raises(ValueError, match="group_by"):
        path_statistics.aggregate(observations, group_by=["shim", "shim"])
    with pytest.raises(ValueError, match="group_by"):
        path_statistics.aggregate(observations, group_by=["owner"])

    learner = spec("path_statistics", "wilson-grouping", 1)
    request = RunRequest(
        namespace=NS,
        queue=LEARNERS,
        spec=harness.pin(api, learner),
        source_queue=SOURCE,
        output_store="learn.paths",
        options={"group_by": ["category", "shim"]},
    )
    result = harness.learn(api, request, learner)
    assert result["output"]["outcome"] == "updated" and result["output"]["groups"] == 2
    noun = api.get_record("learn.paths", path_statistics.stats_key(SOURCE)).value
    native = next(g for g in noun["groups"] if g["path"]["shim"] == "json-native")
    assert native["statistics"]["attempts"] == 4 and native["statistics"]["accepted"] == 3
    low, high = native["statistics"]["acceptance_wilson_95"]
    assert 0 < low < 0.75 < high <= 1
    assert native["sample"] == "small" and noun["evidence_class"] == "observational"
    assert noun["runs_sha256"] == path_statistics.runs_digest(observations)


def test_learner_specs_config_and_run_pins(harness: Harness) -> None:
    assert len({item.learner for item in SPECS}) == 9
    for item in SPECS:
        LearnerSpec.model_validate(item.model_dump(mode="json"))
        registered = (item.learner, item.strategy, item.version) in RUNNERS
        assert registered == (item.status == "implemented")
        assert item.evaluation.keep_baseline_valid and item.consumers.advice_only
    proposed = spec("phrasing", "acceptance-by-phrasing", 1)
    fake = NounRef(store="learn.specs", key=proposed.key, version=1, value_sha256="0" * 64)
    with pytest.raises(ValueError, match="implemented"):
        run_spec(memory_request(fake), proposed)
    learner = spec("memory", "minhash-lsh", 1)
    wrong = NounRef(store="learn.specs", key=learner.key, version=1, value_sha256="0" * 64)
    with pytest.raises(ValueError, match="digest"):
        run_spec(memory_request(wrong), learner)
    with pytest.raises(ValueError, match="model-call"):
        LearnerSpec.model_validate(
            {
                **learner.model_dump(mode="json"),
                "budget": {**learner.budget.model_dump(), "model_calls": 1},
            }
        )
    good = NounRef(
        store="learn.specs",
        key=learner.key,
        version=1,
        value_sha256=value_digest(learner.model_dump(mode="json")),
    )
    assert run_spec(memory_request(good), learner) == run_spec(memory_request(good), learner)
    assert (
        run_spec(memory_request(good), learner).task_id
        != run_spec(memory_request(good, state_version=1), learner).task_id
    )

    active = Activation(learner="memory", spec=good, mode="active")
    shadow = Activation(learner="memory", spec=good, mode="shadow", categories=["verb.solve_draft"])
    config = LearnerConfig(activations=[shadow, active])
    assert [a.mode for a in config.select("memory", "verb.solve_draft", "surface:x")] == [
        "active",
        "shadow",
    ]
    assert config.select("path_statistics", "verb.solve_draft", "surface:x") == []
    with pytest.raises(ValueError, match="same learner scope"):
        LearnerConfig(activations=[active, active])

    # A tampered spec noun is refused before any observation is read.
    api = harness.client(harness.key([SOURCE, LEARNERS]))
    pin = harness.pin(api, learner)
    task = run_spec(memory_request(pin), learner)
    api.enqueue(task, idempotency_key=task.idempotency_key or "")
    record = api.get_record("learn.specs", learner.key)
    api.put_record(
        "learn.specs",
        learner.key,
        {**record.value, "summary": "changed"},
        expected_version=record.version,
        visibility_scope="private",
    )
    lease = api.claim(LEARNERS, "example-learner")
    assert lease is not None
    tampered = lease.model_copy(
        update={"brief": {**lease.brief, "spec": {**pin.model_dump(), "version": 2}}}
    )
    with pytest.raises(ValueError, match="pin"):
        load_run(api, tampered)


def test_brief_cannot_widen_the_pinned_spec_visibility(harness: Harness) -> None:
    api = harness.client(harness.key([SOURCE, LEARNERS]))
    harness.work(api, GOALS[0], ok=False)
    learner = spec("memory", "minhash-lsh", 1)
    assert learner.consumers.visibility_scope == "private"
    pin = harness.pin(api, learner)
    assert harness.learn(api, memory_request(pin), learner)["output"]["outcome"] == "updated"
    head = api.get_record("learn.memory", memory.head_key(SOURCE))
    assert head.visibility_scope == "private"

    # A brief asking for wider visibility than its pinned spec is refused before any read or write.
    task = run_spec(memory_request(pin, state_version=head.version), learner)
    forged = {**learner.consumers.model_dump(), "visibility_scope": "public_commons"}
    task = task.model_copy(update={"brief": {**task.brief, "consumers": forged}})
    api.enqueue(task, idempotency_key=task.idempotency_key or "")
    lease = api.claim(LEARNERS, "example-learner")
    assert lease is not None and lease.task_id == task.task_id
    with pytest.raises(ValueError, match="consumers"):
        load_run(api, lease)
    after = api.get_record("learn.memory", memory.head_key(SOURCE))
    assert (after.version, after.visibility_scope) == (head.version, "private")
    stranger = harness.client(harness.key([SOURCE]))
    with pytest.raises((Forbidden, NotFound)):
        stranger.get_record("learn.memory", memory.head_key(SOURCE))


def test_seed_pack_export_is_deterministic_and_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("pack export must not open a client")

    monkeypatch.setattr(httpx.Client, "__init__", no_network)
    first, second = pack.export(tmp_path / "a"), pack.export(tmp_path / "b")
    assert first == second
    files = sorted(p.relative_to(tmp_path / "a") for p in (tmp_path / "a").rglob("*.json"))
    assert files == sorted(p.relative_to(tmp_path / "b") for p in (tmp_path / "b").rglob("*.json"))
    for path in files:
        assert (tmp_path / "a" / path).read_bytes() == (tmp_path / "b" / path).read_bytes()
    manifest = json.loads((tmp_path / "a/manifest.json").read_text())
    assert "nothing" in manifest["activation"]
    config = json.loads((tmp_path / "a/nouns/config/default.json").read_text())
    assert config["activations"] == []
    verbs = sorted(str(p) for p in files if str(p).startswith("verbs/"))
    assert verbs == [
        "verbs/memory-minhash-lsh-v1.json",
        "verbs/path_statistics-wilson-grouping-v1.json",
        "verbs/routing_weights-beta-bandit-v1.json",
    ]
    for record in pack.spec_records(NS):
        assert record.visibility_scope == "private" and record.expected_version is None


def test_cli_offline_commands_preview_without_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util
    import sys

    path = Path(__file__).resolve().parents[1] / "tools/learner.py"
    loaded = importlib.util.spec_from_file_location("learner_tool", path)
    assert loaded and loaded.loader
    tool = importlib.util.module_from_spec(loaded)
    sys.modules[loaded.name] = tool
    loaded.loader.exec_module(tool)

    def no_network(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("offline commands must not open a client")

    monkeypatch.setattr(httpx.Client, "__init__", no_network)
    assert tool.main(["specs"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert {row["status"] for row in listed} == {"implemented", "proposed"}
    learner = spec("memory", "minhash-lsh", 1)
    pin = NounRef(
        store="learn.specs",
        key=learner.key,
        version=1,
        value_sha256=value_digest(learner.model_dump(mode="json")),
    )
    request = tmp_path / "run.json"
    request.write_text(memory_request(pin).model_dump_json())
    assert tool.main(["preview", "--request", str(request)]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] is True
    assert preview["task"]["visibility_scope"] == "private"
    assert preview["task"]["brief"]["execution"]["server_fallback"] is False
    assert tool.main(["pack", "--output", str(tmp_path / "pack")]) == 0
    assert (tmp_path / "pack/manifest.json").exists()
    with pytest.raises(SystemExit):
        tool.main(["enqueue", "--request", str(request)])


def test_routing_weights_advise_with_evidence_thresholds_and_exploration(harness: Harness) -> None:
    def observation(
        run: int, shim: str, ok: bool, category: str = "verb.solve_draft"
    ) -> dict[str, Any]:
        return {
            "run_id": f"run_{run:04d}",
            "category": category,
            "ok": ok,
            "brief": None,
            "tags": [],
            "report": {"learning": {"path": {"shim": shim}}},
        }

    rows = [observation(i, "json-native", i % 10 != 0) for i in range(30)]
    rows += [observation(100 + i, "xml-tags", i % 2 == 0) for i in range(30)]
    rows += [observation(200 + i, "new-shim", True) for i in range(3)]
    rows += [observation(300, "unlabelled", True, category="verb.ingest")]
    rows.append({**observation(400, "ignored", True), "report": {"learning": {}}})
    families = routing.weights(rows, alternative="shim", exploration=0.1, min_evidence=20)
    assert families == routing.weights(list(reversed(rows)), alternative="shim")
    solve = next(f for f in families if f["category"] == "verb.solve_draft")
    arms = {row["value"]: row for row in solve["alternatives"]}
    assert set(arms) == {"json-native", "xml-tags", "new-shim"}
    assert abs(sum(row["weight"] for row in arms.values()) - 1) < 1e-12
    assert arms["json-native"]["weight"] > arms["xml-tags"]["weight"]
    # Too little evidence keeps the prior; exploration keeps every known arm reachable.
    assert arms["new-shim"]["evidence"] == "insufficient" and arms["new-shim"]["score"] == 0.5
    assert all(row["weight"] >= 0.1 / 3 for row in arms.values())
    assert arms["json-native"]["posterior"] == {"alpha": 28, "beta": 4, "mean": 28 / 32}
    ingest = next(f for f in families if f["category"] == "verb.ingest")
    assert ingest["ready"] is False and ingest["alternatives"][0]["weight"] == 1.0
    for bad in ({"alternative": "owner"}, {"alternative": "shim", "exploration": 0}):
        with pytest.raises(ValueError, match="must be"):
            routing.weights(rows, **bad)  # type: ignore[arg-type]

    api = harness.client(harness.key([SOURCE, LEARNERS]))
    for i, goal in enumerate(GOALS):
        harness.work(api, goal, ok=i != 1)
    learner = spec("routing_weights", "beta-bandit", 1)
    request = RunRequest(
        namespace=NS,
        queue=LEARNERS,
        spec=harness.pin(api, learner),
        source_queue=SOURCE,
        output_store="learn.routing",
        options={"alternative": "shim", "min_evidence": 2},
    )
    result = harness.learn(api, request, learner)
    assert result["output"]["outcome"] == "updated"
    noun = api.get_record("learn.routing", routing.weights_key(SOURCE, "shim"))
    assert noun.value["advice_only"] is True and noun.visibility_scope == "private"
    family = noun.value["families"][0]
    assert family["alternatives"][0]["value"] == "json-native"
    assert family["alternatives"][0]["attempts"] == 4 and family["ready"] is True
    # A consumer cites the exact weights version it used; the ordinary report accepts it.
    advice = {
        "learner": "routing_weights",
        "strategy": "beta-bandit",
        "version": 1,
        "store": "learn.routing",
        "key": noun.key,
        "record_version": noun.version,
    }
    harness.work(api, "EXAMPLE routed by weights", learning={"advice": [advice]})
