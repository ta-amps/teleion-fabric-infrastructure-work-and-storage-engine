#!/usr/bin/env python3
"""Local Meta Model API `/v1/responses` bridge backed by Meta open-weight models (`muse-spark-open`)
on any OpenAI/Ollama-compatible `/chat/completions` endpoint.

Why this exists:
- Product surfaces like Shamash (`shamash/lanes/muse.py`) speak strictly to Meta's Responses API
  (`/v1/responses`, `/v1/responses/{id}`, `/v1/responses/{id}/cancel`, `/v1/models`) in Standard mode.
- Before switching to hosted Meta Model API keys (`https://api.meta.ai/v1`), teams can run the exact
  same client code, background polling, schema formatting, token accounting, and `-contributor`
  refusal against `muse-spark-open` on Ollama Cloud (`https://ollama.com/v1`) or a local Ollama host:

    OLLAMA_API_KEY=<key> uv run python tools/meta_open_bridge.py --port 7799
    MODEL_API_BASE=http://127.0.0.1:7799/v1 MODEL_API_KEY=local-bridge uv run shamash muse-smoke
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request

DEFAULT_UPSTREAM_BASE = "https://ollama.com/v1"
DEFAULT_UPSTREAM_MODEL = "muse-spark-open"
MAX_STORED_RESPONSES = 512


def check_standard_mode(model: str) -> None:
    if "contributor" in model.lower():
        raise HTTPException(
            status_code=400,
            detail=f"{model} is a contributor-tier model; Standard mode only",
        )


def normalize_messages(raw_input: Any, schema_spec: dict[str, Any] | None) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        for item in raw_input:
            if isinstance(item, dict) and "content" in item:
                role = str(item.get("role", "user"))
                content = item["content"]
                if isinstance(content, list):
                    text = "".join(
                        str(part.get("text", "")) for part in content if isinstance(part, dict)
                    )
                else:
                    text = str(content)
                messages.append({"role": role, "content": text})
    else:
        messages.append({"role": "user", "content": json.dumps(raw_input)})

    if schema_spec is not None:
        instruction = "Reply with valid JSON only matching this JSON Schema: " + json.dumps(
            schema_spec, ensure_ascii=False
        )
        messages.insert(0, {"role": "system", "content": instruction})
    return messages


def create_bridge_app(
    *,
    upstream_base: str | None = None,
    upstream_model: str | None = None,
    upstream_api_key: str | None = None,
    dry_upstream: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    base = (
        upstream_base or os.environ.get("BRIDGE_UPSTREAM_BASE") or DEFAULT_UPSTREAM_BASE
    ).rstrip("/")
    target_model = (
        upstream_model or os.environ.get("BRIDGE_UPSTREAM_MODEL") or DEFAULT_UPSTREAM_MODEL
    )
    stored: dict[str, dict[str, Any]] = {}
    app = FastAPI(title="Tessera Meta Responses Open-Weights Bridge")

    def _api_key(req: Request) -> str:
        if upstream_api_key:
            return upstream_api_key
        if env_key := (os.environ.get("OLLAMA_API_KEY") or os.environ.get("BRIDGE_UPSTREAM_KEY")):
            return env_key
        auth = req.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return ""

    def _save(resp_id: str, payload: dict[str, Any]) -> None:
        if len(stored) >= MAX_STORED_RESPONSES:
            oldest = next(iter(stored))
            stored.pop(oldest, None)
        stored[resp_id] = payload

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "upstream_base": base, "upstream_model": target_model}

    @app.get("/v1/models")
    def list_models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {"id": "muse-spark-1.3", "object": "model", "bridged_to": target_model},
                {"id": "muse-wonder-1.0", "object": "model", "bridged_to": target_model},
                {"id": target_model, "object": "model", "bridged_to": target_model},
            ],
        }

    @app.post("/v1/responses")
    def create_response(body: dict[str, Any], request: Request) -> dict[str, Any]:
        requested_model = str(body.get("model") or "muse-spark-1.3")
        check_standard_mode(requested_model)
        check_standard_mode(target_model)

        text_cfg = body.get("text") or {}
        fmt = text_cfg.get("format") if isinstance(text_cfg, dict) else None
        schema_spec = fmt.get("schema") if isinstance(fmt, dict) else None
        want_json = isinstance(fmt, dict) and fmt.get("type") in {"json_schema", "json_object"}

        messages = normalize_messages(body.get("input", ""), schema_spec)
        resp_id = f"resp_{uuid.uuid4().hex[:16]}"

        if dry_upstream:
            text_out = (
                json.dumps({"ok": True, "note": "SYNTHETIC dry bridge response"})
                if want_json
                else "ready"
            )
            in_tok, cached_tok, out_tok = 42, 0, 12
        else:
            key = _api_key(request)
            if not key:
                raise HTTPException(
                    status_code=401,
                    detail="Missing OLLAMA_API_KEY or BRIDGE_UPSTREAM_KEY for upstream open-weights endpoint",
                )
            chat_req: dict[str, Any] = {
                "model": target_model,
                "messages": messages,
                "temperature": 0.1,
            }
            if want_json:
                chat_req["response_format"] = {"type": "json_object"}
            if body.get("max_output_tokens") is not None:
                # Open-weight reasoning models spend ~200-1500 tokens on internal reasoning before content.
                chat_req["max_tokens"] = max(int(body["max_output_tokens"]), 4096)

            with httpx.Client(
                base_url=base,
                headers={"authorization": f"Bearer {key}"},
                timeout=90.0,
                transport=transport,
            ) as http:
                up = http.post("/chat/completions", json=chat_req)
                if up.status_code >= 400:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Upstream chat/completions returned HTTP {up.status_code}",
                    )
                up_body = up.json()

            text_out = str(up_body["choices"][0]["message"]["content"])
            if want_json:
                stripped = text_out.strip()
                start, end = stripped.find("{"), stripped.rfind("}")
                if start != -1 and end > start:
                    text_out = stripped[start : end + 1]
            usage = up_body.get("usage") or {}
            details = usage.get("prompt_tokens_details") or {}
            in_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            cached_tok = int(details.get("cached_tokens") or 0)
            out_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)

        envelope: dict[str, Any] = {
            "id": resp_id,
            "object": "response",
            "model": requested_model,
            "bridged_model": target_model,
            "status": "completed",
            "metadata": body.get("metadata") or {},
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text_out}],
                }
            ],
            "usage": {
                "input_tokens": in_tok,
                "input_tokens_details": {"cached_tokens": cached_tok},
                "output_tokens": out_tok,
                "total_tokens": in_tok + out_tok,
            },
        }
        _save(resp_id, envelope)
        return envelope

    @app.get("/v1/responses/{response_id}")
    def get_response(response_id: str) -> dict[str, Any]:
        if response_id not in stored:
            raise HTTPException(status_code=404, detail=f"Unknown response_id {response_id}")
        return stored[response_id]

    @app.post("/v1/responses/{response_id}/cancel")
    def cancel_response(response_id: str) -> dict[str, Any]:
        if response_id not in stored:
            raise HTTPException(status_code=404, detail=f"Unknown response_id {response_id}")
        stored[response_id] = {**stored[response_id], "status": "cancelled"}
        return stored[response_id]

    return app


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7799)
    p.add_argument(
        "--upstream-base", default=os.environ.get("BRIDGE_UPSTREAM_BASE", DEFAULT_UPSTREAM_BASE)
    )
    p.add_argument(
        "--upstream-model", default=os.environ.get("BRIDGE_UPSTREAM_MODEL", DEFAULT_UPSTREAM_MODEL)
    )
    p.add_argument("--dry-upstream", action="store_true")
    args = p.parse_args()

    app = create_bridge_app(
        upstream_base=args.upstream_base,
        upstream_model=args.upstream_model,
        dry_upstream=args.dry_upstream,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
