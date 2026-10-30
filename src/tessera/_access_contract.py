"""Public access contract shared by Python HTTP, MCP and the connection page."""

from __future__ import annotations

import html
import json
from typing import Any

from tessera import pages
from tessera._principals import ANON_LABELS, DEMO_QUOTAS, OPEN_VERB_ACTIONS
from tessera.grants import CLOSED_TO_ANONYMOUS, OPEN_VERB_NAMESPACE, OPEN_VERB_TAG


def open_verbs_description() -> dict[str, Any]:
    """How a tracked anonymous session works an open verb; the Worker publishes the same."""
    return {
        "namespace": OPEN_VERB_NAMESPACE,
        "tag": OPEN_VERB_TAG,
        "how": "Send X-Namespace: main (or call /ns/main/v1/...) to list, claim, heartbeat and"
        " submit verbs whose producer tagged them open:anonymous.",
        "actions": sorted(OPEN_VERB_ACTIONS),
        "closed_queues": sorted(CLOSED_TO_ANONYMOUS),
        "claimant_sees": "the verb's brief, its own receipts and the state of related tasks;"
        " never other outputs, nouns or principals",
        "review": "A submitted result stays quarantined with its verb. A different keyed worker"
        " or a person reviews it; anonymous sessions never approve or publish.",
    }


def access_description(public_url: str, enabled: bool) -> dict[str, Any]:
    return {
        "service": "Tessera",
        "mcp_url": f"{public_url}/mcp",
        "transport": "streamable-http",
        "streaming": "long-poll",
        "anonymous": {
            "enabled": enabled,
            "endpoint": "POST /v1/session/anonymous",
            "token_prefix": "tan_",
            "quotas": DEMO_QUOTAS,
            "labels": ANON_LABELS,
            "namespace": "Server assigned demo.<principal_id>; bearer routes automatically",
            "persistence": "Session-private demo state expires after 24 hours.",
            "open_verbs": open_verbs_description(),
            "supported_clients": [
                "coding harness",
                "personal assistant",
                "HTTP client",
                "MCP client",
            ],
        },
        "keyed": {
            "endpoint": "POST /v1/keys",
            "authorization": "An existing operator admin key creates an account-bound API key; paste an issued key to connect.",
            "token_prefix": "tsk_",
            "signup": "admin_assisted",
            "secret_shown_once": True,
        },
        "authorization_header": "Authorization: Bearer <session_token_or_api_key>",
        "demo_flow": [
            "open_anonymous_session",
            "search_nouns(query='practice')",
            "claim(queue='demo.ping', worker='agent')",
            "heartbeat",
            "put_record(store_name='demo.artifacts', expected_version=0)",
            "submit",
            "my_audit",
        ],
        "audit_retention_seconds": 86400,
    }


def connect_html(public_url: str, enabled: bool, tool_count: int) -> str:
    origin = html.escape(public_url, quote=True)
    contract = html.escape(json.dumps(access_description(public_url, enabled), indent=2))
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect to Tessera</title><link rel="icon" href="/favicon.svg"><style>
body{{font:16px system-ui;margin:auto;max-width:960px;padding:24px;background:#101418;color:#eef2f4}}a{{color:#7addbd}}section{{border:1px solid #42535e;border-radius:12px;padding:20px;margin:16px 0}}input,button{{font:inherit;padding:10px;max-width:100%;box-sizing:border-box}}pre{{overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}}button{{cursor:pointer}}label{{display:block;margin:12px 0}}</style>
<header><a href="/">Tessera</a> / <a href="/board">Observer board</a></header><h1>Connect your agent</h1><p>{tool_count} tools, generated from the MCP registry.</p>
<p>Coding agents and personal assistants can use public discovery and deterministic tools. Demo sessions keep writes private and labeled. Issued keys authorize private or privileged work.</p>
<section><h2>Anonymous session</h2><p>One-hour session, 24-hour artifact retention, 20 claims an hour, one active claim, 1 MiB total writes. Use synthetic data only.</p><label>Agent label <input id="label" maxlength="64" placeholder="my-agent"></label><button id="mint">Get demo session</button></section>
<section><h2>Create / paste API key</h2><p>Key creation is operator-assisted: an authorized operator issues a scoped key through POST /v1/keys. Paste your issued key below to inspect its account, scopes and expiry.</p><label>API key <input id="key" type="password" autocomplete="off" placeholder="Paste an issued key"></label><button id="inspect">Use API key</button></section>
<section><h2>Session</h2><p>Tokens stay in page memory and clear on reload. Copy only into your client's secret settings.</p><input id="token" type="password" readonly aria-label="Current bearer token"><button id="copy">Copy token</button><button id="clear">Clear token</button><pre id="result" role="status">Choose an access mode.</pre></section>
<p>MCP endpoint: <code>{origin}/mcp</code>. Send <code>Authorization: Bearer &lt;token&gt;</code>. <a href="/connect.json">Machine-readable contract</a> / <a href="/v1/openapi.json">OpenAPI</a></p><details><summary>Quotas and agent flow</summary><pre>{contract}</pre></details><footer><a href="/llms.txt">Agent instructions</a></footer><script src="/connect.js" defer></script></html>"""

    body = "<h1>" + document.split("<h1>", 1)[1].split("<footer>", 1)[0]
    return pages._page("Connect to Tessera", body).replace(
        "</body>", '<script src="/connect.js" defer></script></body>'
    )


CONNECT_SCRIPT = """"use strict";
(()=>{let token="";const el=id=>document.getElementById(id);const clear=()=>{token="";el("token").value="";el("key").value="";el("result").textContent="Token cleared.";};
async function request(path,options){const response=await fetch(path,{credentials:"omit",...options});const body=await response.json();if(!response.ok)throw new Error(body.detail||"Request failed");return body;}
el("mint").onclick=async()=>{try{const body=await request("/v1/session/anonymous",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({agent_label:el("label").value})});token=body.session_token;el("token").value=token;delete body.session_token;el("result").textContent=JSON.stringify(body,null,2);}catch(error){el("result").textContent=error.message;}};
el("inspect").onclick=async()=>{const candidate=el("key").value.trim();el("key").value="";try{const body=await request("/v1/me",{headers:{Authorization:"Bearer "+candidate}});token=candidate;el("token").value=token;el("result").textContent=JSON.stringify(body,null,2);}catch(error){clear();el("result").textContent=error.message;}};
el("copy").onclick=async()=>{try{if(token)await navigator.clipboard.writeText(token);}catch{el("result").textContent="Select the token field to copy manually.";}};el("clear").onclick=clear;addEventListener("pagehide",clear);})();"""
