import { FAVICON_SVG } from "./cloud_pages.js";
import ACCESS_OPENAPI from "./access_openapi.json" with { type: "json" };
// Public discovery and bearer-only onboarding; navigation never mints a session.
import { ANON_LABELS, DEMO_QUOTAS, REQUESTER_QUOTAS, openVerbsDescription, requesterDescription } from "./cloud_access.js";

export const ACCESS_TOOLS = [
  { name: "list_verbs", description: "List ready verbs visible to this principal, with optional queue and status filters.", inputSchema: {type:"object",properties:{queue:{type:"string"},status:{type:"string",enum:["ready","leased","waiting","done","dead","cancelled"]},limit:{type:"integer",minimum:1,maximum:100}},additionalProperties:false}},
  { name: "open_anonymous_session", description: "Open a tracked, one-hour synthetic demo session without secrets. Use the returned session_token as Authorization: Bearer for subsequent calls. No production access.", inputSchema: { type: "object", properties: { agent_label: { type: "string", maxLength: 64 } }, additionalProperties: false } },
  { name: "explain_access_modes", description: "Public connection instructions, limits, labels and keyed upgrade path.", inputSchema: { type: "object", properties: {}, additionalProperties: false } },
  { name: "whoami", description: "Show this bearer principal's scopes and expiry without exposing its secret.", inputSchema: { type: "object", properties: {}, additionalProperties: false } },
  { name: "my_audit", description: "Read only this principal's redacted request audit.", inputSchema: { type: "object", properties: { after: { type: "integer", minimum: 0 }, limit: { type: "integer", minimum: 1, maximum: 200 } }, additionalProperties: false } },
  { name: "admin_audit", description: "Operator audit. Requires admin scope.", inputSchema: { type: "object", properties: { after: { type: "integer", minimum: 0 }, limit: { type: "integer", minimum: 1, maximum: 200 } }, additionalProperties: false } },
  { name: "cancel", description: "Cancel a task owned by this principal or its authorized surface.", inputSchema: { type: "object", required: ["task_id"], properties: { task_id: { type: "string" }, reason: { type: "string" } }, additionalProperties: false } },
  { name: "claim_events", description: "Read monotonic events for a task this principal may view; HTTP also supports bounded long-poll.", inputSchema: { type: "object", required: ["task_id"], properties: { task_id: { type: "string" }, after: { type: "integer", minimum: 0 } }, additionalProperties: false } },
];

// Every published access description reads both switches from the same environment, so the
// connect page, connect.json, the agent card, llms-full.txt and MCP always agree (F-054).
export function accessDescriptionFor(url, env) {
  return accessDescription(url, env.ANONYMOUS_ACCESS_ENABLED === "true", env.REQUESTER_SIGNUP_ENABLED === "true");
}

export function accessDescription(url, enabled, requesterEnabled) {
  return { service: "Tessera", mcp_url: `${url}/mcp`, transport: "streamable-http", streaming: "long-poll",
    anonymous: { enabled, endpoint: `${url}/v1/session/anonymous`, method: "POST", token_prefix: "tan_", quotas: DEMO_QUOTAS, labels: ANON_LABELS,
      namespace: "Server assigned demo.<principal_id>; bearer routes automatically", persistence: "Session-private synthetic records, removed after 24 hours", supported_clients: ["coding harness", "personal assistant", "HTTP client", "MCP client"], open_verbs: openVerbsDescription() },
    requester: { ...requesterDescription(requesterEnabled), endpoint: `${url}/v1/session/requester`, method: "POST", quotas: REQUESTER_QUOTAS },
    keyed: { endpoint: `${url}/v1/keys`, method: "POST", authorization: "Existing operator admin key required to issue an account-bound key; paste an issued key to connect", token_prefix: "tsk_", signup_mode: "admin_assisted", secret_shown_once: true },
    authorization_header: "Authorization: Bearer <session_token_or_api_key>",
    demo_flow: ["open_anonymous_session", "search_nouns(query=practice)", "claim(queue=demo.ping,worker=your-agent)", "heartbeat", "put_record(store_name=demo.artifacts,expected_version=0)", "submit", "my_audit"],
    audit_retention_seconds: 86400, example: { method: "POST", url: `${url}/v1/session/anonymous`, json: { agent_label: "my-assistant" } } };
}

export function renderAccessHtml(url) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect to Tessera</title><link rel="icon" href="/favicon.svg"><style>body{font:17px system-ui;background:#10151e;color:#e8edf5;margin:0}main,header,footer{max-width:900px;margin:auto;padding:24px}a{color:#95c5ff}section{border:1px solid #40526c;padding:24px;margin:20px 0;border-radius:12px}button,input{font:inherit;padding:12px;max-width:100%;box-sizing:border-box}button{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere}label{display:block;margin:12px 0}input{width:100%}</style></head><body><header><a href="/">${FAVICON_SVG.replace("<svg", '<svg width="24" height="24"')} Tessera</a> / <a href="/board">Observer</a> / <a href="/llms.txt">Agent instructions</a></header><main><h1>Connect your agent</h1><p>Coding harnesses and personal assistants can discover tools without a key and complete tracked synthetic demo work with a free session.</p><section><h2>Anonymous demo session</h2><p>One hour, one active claim, 20 claims/hour, 1 MiB total artifact budget. Session-private records expire after 24 hours. Every result is labeled anonymous, demo and untrusted.</p><label>Agent label<input id="label" maxlength="64" value="my-assistant"></label><button id="mint">Get demo session</button></section><section><h2>Use an API key</h2><p>An operator issues account-bound keys for private state and production queues. Key creation requires an existing admin credential at POST /v1/keys.</p><label>Paste issued key<input type="password" id="key" autocomplete="off"></label><button id="inspect">Connect with key</button></section><p id="status" role="status"></p><label>Current token<input type="password" id="current-token" readonly autocomplete="off"></label><button id="copy-token">Copy token</button> <button id="clear-token">Clear token</button><pre id="result"></pre><p>Keep the session token private. This page keeps it only in memory and never puts it in the URL or browser storage. <a href="/connect.json">Machine-readable access contract</a>.</p></main><footer><a href="https://github.com/ta-amps/tessera-infrastructure-work-and-storage-engine">Source</a> / <a href="https://home.ta-amps.workers.dev/connect">Ecosystem connection guide</a></footer><script>const base="";let session="";const clear=()=>{session="";document.getElementById('current-token').value="";document.getElementById('key').value="";document.getElementById('result').textContent="Token cleared."};const show=(data)=>{if(data.session_token){session=data.session_token;document.getElementById('current-token').value=session;delete data.session_token;}document.getElementById('result').textContent=JSON.stringify(data,null,2)};document.getElementById('clear-token').onclick=clear;document.getElementById('copy-token').onclick=async()=>{try{await navigator.clipboard.writeText(session);document.getElementById('status').textContent='Token copied.'}catch{document.getElementById('status').textContent='Clipboard unavailable. Select the token field to copy manually.'}};addEventListener('pagehide',clear);async function go(path,options){const s=document.getElementById('status');s.textContent='Connecting...';try{const r=await fetch(base+path,options);const d=await r.json();if(!r.ok)throw Error(d.detail||'Request failed');s.textContent='Connected. Configure your harness with the bearer token and '+base+'/mcp';show(d)}catch(e){s.textContent=e.message}}document.getElementById('mint').onclick=()=>go('/v1/session/anonymous',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({agent_label:document.getElementById('label').value})});document.getElementById('inspect').onclick=()=>{session=document.getElementById('key').value;document.getElementById('key').value='';document.getElementById('current-token').value=session;go('/v1/me',{headers:{Authorization:'Bearer '+session}})};</script></body></html>`;
}

// Storage-free access discovery; the Worker serves it before any Durable Object call.
export function accessDiscoveryRoute(path, method, publicUrl, env, jsonResp, htmlResp) {
  if (path === "/v1/openapi.json" && method === "GET") return jsonResp({ ...ACCESS_OPENAPI, servers: [{ url: publicUrl }] });
  if (path === "/connect.json" && method === "GET") return jsonResp(accessDescriptionFor(publicUrl, env));
  if ((path === "/connect" || path === "/") && method === "GET") return htmlResp(renderAccessHtml(publicUrl), 200, { "content-security-policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'" });
  return null;
}

export async function accessRoute(request, env, { path, url, bearer, primary, stub, baseRpc, meta, publicUrl, rpcResponse, jsonResp, htmlResp, readJsonBody, namespaceStub }) {
  const method = request.method;
  if (path === "/v1/session/anonymous" && method === "POST") {
    const { body, error } = await readJsonBody(request);
    if (error) return error;
    return rpcResponse(await primary.openAnonymousSession(body, meta));
  }
  if (path === "/v1/session/requester" && method === "POST") {
    const { body, error } = await readJsonBody(request);
    if (error) return error;
    // The key, its profile and the sign-up allowance are written together in the primary object;
    // the requester's own namespace object is created by the key's first call there.
    return rpcResponse(await primary.openRequesterSession(body, meta));
  }
  if (path === "/v1/verbs" && method === "GET") return rpcResponse(await stub.rpcCall(bearer, "listVerbs", { ...baseRpc, queue: url.searchParams.get("queue"), status: url.searchParams.get("status"), limit: url.searchParams.get("limit"), after: url.searchParams.get("after") }));
  if (path === "/v1/me" && method === "GET") return rpcResponse(await stub.rpcCall(bearer, "me", baseRpc));
  if (["/v1/me/audit", "/v1/admin/audit"].includes(path) && method === "GET") return rpcResponse(await primary.auditForToken(bearer, { admin: path.includes("/admin/"), after: url.searchParams.get("after"), limit: url.searchParams.get("limit") }));
  if (path === "/v1/public/activity" && method === "GET") return jsonResp(await primary.publicActivity());
  if (path === "/v1/keys" && method === "POST") {
    const { body, error } = await readJsonBody(request);
    if (error) return error;
    return rpcResponse(await primary.bootstrapOrIssueKey(bearer, false, { ...body, access_meta: meta }));
  }
  if (path === "/v1/keys" && method === "GET") return rpcResponse(await primary.myKeys(bearer));
  const revoke = /^\/v1\/keys\/([^/]+)\/revoke$/.exec(path);
  if (revoke && method === "POST") return rpcResponse(await primary.revokeOwnKey(bearer, decodeURIComponent(revoke[1]), meta));
  const events = /^\/v1\/claims\/([^/]+)\/events$/.exec(path);
  if (events && method === "GET") {
    const args = { ...baseRpc, task_id: decodeURIComponent(events[1]), after: Number(url.searchParams.get("after")) || 0 };
    const wait = Math.max(0, Math.min(Number(url.searchParams.get("wait")) || 0, 15));
    const until = Date.now() + wait * 1000;
    let out, checks = 0;
    do {
      // One audited check per request; later checks of the same long-poll are not recorded.
      out = await stub.rpcCall(bearer, "claimEvents", args, checks++ ? { repeat: true } : {});
      if (!out.ok || out.data.events.length || Date.now() >= until || request.signal.aborted) break;
      await new Promise(resolve => setTimeout(resolve, 750));
    } while (Date.now() <= until);
    return rpcResponse(out, { "retry-after": "1" });
  }
  return null;
}
