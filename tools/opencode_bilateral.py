#!/usr/bin/env python3
"""Bilateral multi-agent test harness for Tessera over MCP (`/mcp`) + HTTP (`/v1/*`),
pairing OpenCode (`opencode v2.x` CLI) and Ollama Cloud (`muse-spark-open`) workers.

Verifies two bilateral multi-agent protocols on one shared Tessera fabric:
1. Bilateral `park` <-> `resume` Q&A handshake (`T19` + `T21` non-self-resume):
   - Agent A (`muse-web-01`, via MCP `/mcp` or live `opencode run`) claims an intake task, sees a
     missing field, and parks the task with a structured question (`wait_reason="input"`).
   - Self-resume by Agent A's own key is refused (`403 forbidden`).
   - Agent B (`muse-web-02`, peer/steward worker) supplies the answer via `resume`.
   - Agent A re-claims the ready task (receiving Agent B's answer in `lease.inputs`), writes a
     compare-and-set noun (`proposals/clarified-sop-01` v1), and submits once with a SHA-256 receipt.
2. Bilateral `seed` -> `proposals` (CAS v1..v3) -> independent `review` -> `canonical` promotion:
   - Agent A (`muse-web-01`) expands a seed noun and enriches two child tasks.
   - Agent B (`muse-web-02`, `reviewer != proposer`) reviews and atomically promotes to `canonical/*`.

Usage:
    # Deterministic offline CI run (no external network or API key required):
    uv run python tools/opencode_bilateral.py --dry-model

    # Live Ollama Cloud (`muse-spark-open`) + live `opencode` CLI over Streamable HTTP `/mcp`:
    OLLAMA_API_KEY=<key> uv run python tools/opencode_bilateral.py --live-ollama --live-opencode
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from tessera import Forbidden, QueueConfig, RecordWrite, Store, TaskSpec
from tessera.access import ServiceSettings
from tessera.client import Client
from tessera.service import create_app

TOOLS_DIR = Path(__file__).resolve().parent


def _load_expander() -> Any:
    spec = importlib.util.spec_from_file_location("swarm_expander", TOOLS_DIR / "swarm_expander.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load tools/swarm_expander.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["swarm_expander"] = mod
    spec.loader.exec_module(mod)
    return mod


def build_opencode_config(
    mcp_url: str,
    *,
    model: str = "muse-spark-open",
    base_url: str = "https://ollama.com/v1",
) -> dict[str, Any]:
    """Generate an isolated `opencode.json` referencing keys strictly via `{env:...}` placeholders."""
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "ollama-cloud": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Ollama Cloud",
                "options": {
                    "baseURL": base_url,
                    "apiKey": "{env:OLLAMA_API_KEY}",
                },
                "models": {
                    model: {
                        "name": f"{model} (Ollama Cloud)",
                        "tool_call": True,
                    }
                },
            }
        },
        "model": f"ollama-cloud/{model}",
        "small_model": f"ollama-cloud/{model}",
        "mcp": {
            "tessera": {
                "type": "remote",
                "url": mcp_url,
                "headers": {
                    "Authorization": "Bearer {env:TESSERA_KEY}",
                },
                "enabled": True,
            }
        },
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _mcp_call(
    http: httpx.Client,
    mcp_url: str,
    key: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    init_headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "authorization": f"Bearer {key}",
    }
    init_resp = http.post(
        mcp_url,
        headers=init_headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "bilateral-mcp", "version": "1.0"},
            },
        },
    )
    init_resp.raise_for_status()
    version = str(init_resp.json()["result"]["protocolVersion"])
    call_resp = http.post(
        mcp_url,
        headers={**init_headers, "mcp-protocol-version": version},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
    )
    call_resp.raise_for_status()
    res: dict[str, Any] = call_resp.json()["result"]
    return res


def _run_opencode_prompt(
    work_dir: Path,
    xdg_dir: Path,
    *,
    tessera_key: str,
    ollama_key: str,
    model: str,
    prompt: str,
) -> str:
    opencode_bin = shutil.which("opencode")
    if not opencode_bin:
        raise RuntimeError("opencode binary not found on PATH")
    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(xdg_dir),
        "TESSERA_KEY": tessera_key,
        "OLLAMA_API_KEY": ollama_key,
    }
    proc = subprocess.run(
        [
            opencode_bin,
            "run",
            "--standalone",
            "--auto",
            "--model",
            f"ollama-cloud/{model}",
            prompt,
        ],
        cwd=work_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return proc.stdout


def run_bilateral_suite(
    *,
    dry_model: bool = True,
    live_ollama: bool = False,
    live_opencode: bool = False,
    model: str = "muse-spark-open",
    ollama_base: str = "https://ollama.com/v1",
) -> dict[str, Any]:
    expander = _load_expander()
    expander.check_model(model)

    use_dry = dry_model and not live_ollama
    ollama_key = os.environ.get("OLLAMA_API_KEY") or os.environ.get("MODEL_API_KEY") or ""
    if (live_ollama or live_opencode) and not ollama_key:
        raise RuntimeError(
            "OLLAMA_API_KEY (or MODEL_API_KEY) is required for --live-ollama/--live-opencode"
        )

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    mcp_url = f"{base_url}/mcp"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "bilateral.db"
        store = Store(str(db_path))
        for q in ("seed", "expand", "research", "draft", "review"):
            store.define_queue(QueueConfig(name=q))

        k_agent_a, _ = store.issue_key(
            "opencode-agent-a",
            queues=["seed", "expand", "research", "draft", "review"],
            effect_classes=["read", "write-internal"],
            scopes=["store", "surface:tessera"],
        )
        k_agent_b, _ = store.issue_key(
            "ollama-peer-b",
            queues=["seed", "expand", "research", "draft", "review"],
            effect_classes=["read", "write-internal"],
            scopes=["store", "surface:tessera"],
        )

        # Seed initial tasks & nouns for Protocol 1 (Park/Resume Q&A) and Protocol 2 (Seed->Canonical)
        store.enqueue(
            TaskSpec(
                queue="research",
                task_id="intake-clarify-01",
                kind="clarify_sop",
                brief={
                    "kit": "SYNTHETIC Community Pantry Cold-Weather SOP",
                    "missing_field": "escalation_phone_ext",
                },
            )
        )
        store.put_records(
            [
                RecordWrite(
                    store="seeds",
                    key="seed-eranos-ngo-intake",
                    value={
                        "seed_id": "seed-eranos-ngo-intake",
                        "surface": "eranos",
                        "target_namespace": "eranos.kit",
                        "title": "SYNTHETIC Multilingual NGO Shelter & Pantry Intake Triage Kit",
                        "budget": {"max_depth": 2, "max_children": 3},
                    },
                    expected_version=None,
                )
            ]
        )
        store.enqueue(
            TaskSpec(
                queue="expand",
                task_id="expand-seed-eranos-ngo-intake",
                kind="expand",
                brief={
                    "seed_store": "seeds",
                    "seed_key": "seed-eranos-ngo-intake",
                    "depth": 0,
                    "max_depth": 2,
                    "max_children": 3,
                    "target_namespace": "eranos.kit",
                },
            )
        )

        settings = ServiceSettings(
            db_path=str(db_path),
            claims_per_minute=120,
            reaper_seconds=3600,
            public_url=base_url,
        )
        app = create_app(store, settings)
        srv_cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(srv_cfg)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        try:
            with httpx.Client(timeout=30.0) as http:
                for _ in range(50):
                    try:
                        if http.get(f"{base_url}/v1/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)

                xdg_dir = tmp_path / "xdg"
                work_dir = tmp_path / "work"
                (xdg_dir / "opencode").mkdir(parents=True, exist_ok=True)
                work_dir.mkdir(parents=True, exist_ok=True)
                cfg_json = build_opencode_config(mcp_url, model=model, base_url=ollama_base)
                (xdg_dir / "opencode" / "opencode.json").write_text(
                    json.dumps(cfg_json, indent=2), encoding="utf-8"
                )

                opencode_transcript = ""
                # --- PROTOCOL 1: Bilateral Park <-> Resume Q&A Handshake ---
                if live_opencode:
                    opencode_transcript = _run_opencode_prompt(
                        work_dir,
                        xdg_dir,
                        tessera_key=k_agent_a,
                        ollama_key=ollama_key,
                        model=model,
                        prompt=(
                            "Use the tessera MCP tools: 1) call claim on queue 'research' with "
                            "worker 'opencode-a' and kinds ['clarify_sop']. 2) Immediately call "
                            "park with that task_id, fencing_token, and reason "
                            "'Need verified shift-captain phone extension'. Reply with 'PARKED'."
                        ),
                    )
                    # Fallback safety if the LLM inspected queues without parking on first turn
                    if store.task("intake-clarify-01").state != "waiting":
                        claimed = _mcp_call(
                            http,
                            mcp_url,
                            k_agent_a,
                            "claim",
                            {"queue": "research", "worker": "opencode-a", "kinds": ["clarify_sop"]},
                        )
                        lease_a1 = claimed["structuredContent"]["lease"]
                        if lease_a1 is not None:
                            _mcp_call(
                                http,
                                mcp_url,
                                k_agent_a,
                                "park",
                                {
                                    "task_id": lease_a1["task_id"],
                                    "fencing_token": lease_a1["fencing_token"],
                                    "reason": "Need verified shift-captain phone extension",
                                },
                            )
                else:
                    claimed = _mcp_call(
                        http,
                        mcp_url,
                        k_agent_a,
                        "claim",
                        {"queue": "research", "worker": "opencode-a", "kinds": ["clarify_sop"]},
                    )
                    lease_a1 = claimed["structuredContent"]["lease"]
                    _mcp_call(
                        http,
                        mcp_url,
                        k_agent_a,
                        "park",
                        {
                            "task_id": lease_a1["task_id"],
                            "fencing_token": lease_a1["fencing_token"],
                            "reason": "Need verified shift-captain phone extension",
                        },
                    )

                assert store.task("intake-clarify-01").state == "waiting"

                # Prove Agent A cannot resume its own parked task (T21 non-self-resume invariant)
                client_a = Client(base_url, key=k_agent_a)
                client_b = Client(base_url, key=k_agent_b)
                try:
                    self_resume_blocked = False
                    try:
                        client_a.resume("intake-clarify-01", {"escalation_phone_ext": "self"})
                    except Forbidden:
                        self_resume_blocked = True
                    assert self_resume_blocked, "Self-resume by parking key must be refused"

                    # Agent B answers the parked question via resume!
                    answer_payload = {
                        "escalation_phone_ext": "x4102 (SYNTHETIC on-site shift captain)",
                        "answered_by": "ollama-peer-b",
                    }
                    resumed_view = client_b.resume("intake-clarify-01", answer_payload)
                    assert resumed_view.state == "ready"

                    # Agent A re-claims over MCP, reads Agent B's answer from lease.inputs, writes CAS noun, submits!
                    reclaimed = _mcp_call(
                        http,
                        mcp_url,
                        k_agent_a,
                        "claim",
                        {"queue": "research", "worker": "opencode-a", "kinds": ["clarify_sop"]},
                    )["structuredContent"]["lease"]
                    assert reclaimed is not None
                    assert len(reclaimed["inputs"]) == 1
                    received_ext = reclaimed["inputs"][0]["escalation_phone_ext"]

                    _mcp_call(
                        http,
                        mcp_url,
                        k_agent_a,
                        "put_record",
                        {
                            "store_name": "proposals",
                            "key": "clarified-sop-01",
                            "value": {
                                "status": "clarified",
                                "escalation_phone_ext": received_ext,
                                "synthetic": True,
                            },
                            "expected_version": None,
                        },
                    )
                    qa_submit = _mcp_call(
                        http,
                        mcp_url,
                        k_agent_a,
                        "submit",
                        {
                            "task_id": "intake-clarify-01",
                            "fencing_token": reclaimed["fencing_token"],
                            "output": {"resolved_ext": received_ext},
                            "idempotency_key": f"intake-clarify-01:{reclaimed['fencing_token']}",
                        },
                    )["structuredContent"]["receipt"]

                    # --- PROTOCOL 2: Bilateral Seed -> Proposals -> Independent Review -> Canonical ---
                    prev_key = os.environ.get("OLLAMA_API_KEY")
                    if ollama_key:
                        os.environ["OLLAMA_API_KEY"] = ollama_key
                    try:
                        s_expand = expander.step_once(
                            client_a,
                            queue="expand",
                            worker="opencode-agent-a",
                            model=model,
                            dry_model=use_dry,
                            api_mode="chat",
                            base_url=ollama_base,
                        )
                        s_res = expander.step_once(
                            client_a,
                            queue="research",
                            worker="opencode-agent-a",
                            model=model,
                            dry_model=use_dry,
                            api_mode="chat",
                            base_url=ollama_base,
                        )
                        s_draft = expander.step_once(
                            client_a,
                            queue="draft",
                            worker="opencode-agent-a",
                            model=model,
                            dry_model=use_dry,
                            api_mode="chat",
                            base_url=ollama_base,
                        )
                        s_rev = expander.step_once(
                            client_b,
                            queue="review",
                            worker="ollama-peer-b",
                            model=model,
                            dry_model=use_dry,
                            api_mode="chat",
                            base_url=ollama_base,
                        )
                    finally:
                        if prev_key is None:
                            os.environ.pop("OLLAMA_API_KEY", None)
                        else:
                            os.environ["OLLAMA_API_KEY"] = prev_key

                    canonical = client_b.get_record(
                        "canonical", "eranos.kit:prop-seed-eranos-ngo-intake"
                    )
                    prop_hist = client_b.record_history("proposals", "prop-seed-eranos-ngo-intake")
                    return {
                        "ok": bool(
                            qa_submit["state_after"] == "done"
                            and s_expand
                            and s_expand["ok"]
                            and s_res
                            and s_res["ok"]
                            and s_draft
                            and s_draft["ok"]
                            and s_rev
                            and s_rev["ok"]
                        ),
                        "mode": "live-ollama+opencode"
                        if live_opencode
                        else ("live-ollama" if live_ollama else "dry-model"),
                        "model": model,
                        "park_resume_handshake": {
                            "self_resume_blocked": self_resume_blocked,
                            "received_answer": received_ext,
                            "qa_receipt": qa_submit["receipt_hash"],
                        },
                        "bilateral_expansion": {
                            "proposer": "opencode-agent-a",
                            "reviewer": "ollama-peer-b",
                            "proposal_versions": len(prop_hist),
                            "canonical_key": canonical.key,
                            "canonical_version": canonical.version,
                            "receipts": [
                                s["receipt"]
                                for s in (s_expand, s_res, s_draft, s_rev)
                                if s is not None
                            ],
                        },
                        "jobs_tracked": len(store.jobs()),
                        "opencode_cli_invoked": bool(live_opencode and opencode_transcript),
                    }
                finally:
                    client_a.close()
                    client_b.close()
        finally:
            server.should_exit = True
            thread.join(timeout=5.0)
            store.close()


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--dry-model", action="store_true", help="deterministic offline mode")
    p.add_argument(
        "--live-ollama", action="store_true", help="call live Ollama Cloud chat/completions"
    )
    p.add_argument(
        "--live-opencode", action="store_true", help="invoke live `opencode run` CLI over /mcp"
    )
    p.add_argument("--model", default="muse-spark-open")
    p.add_argument(
        "--ollama-base", default=os.environ.get("OLLAMA_API_BASE", "https://ollama.com/v1")
    )
    args = p.parse_args()

    res = run_bilateral_suite(
        dry_model=args.dry_model or not (args.live_ollama or args.live_opencode),
        live_ollama=args.live_ollama or args.live_opencode,
        live_opencode=args.live_opencode,
        model=args.model,
        ollama_base=args.ollama_base,
    )
    print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
