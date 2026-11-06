import { agentStartAsset, withAgentStart } from "./agent_start.js";
import { discoveryFile, SITEMAP_XML, wantsPlainText } from "./agent_connect.js";
import { withBannedWordsOnly, withPublicNames } from "./public_names.js";
import { storageQuotaFailure } from "./storage_quota.js";
import { PUBLIC_COPY_NOTE, publicBytes } from "./public_read.js";
import { readClientPrefs } from "./client_prefs.js";
import { ipGroup, isOpenFreeWindow, namespaceReachable, openGrantFor, publicPrincipal } from "./cloud_access.js";
import { edgeFromCf, publicActivityRoute } from "./edge_activity.js";
import { accessDiscoveryRoute, accessRoute, accessDescriptionFor } from "./cloud_access_routes.js";
import { handleA2a } from "./cloud_a2a.js";
import { displayMask } from "./display_mask.js";
import { HUMAN_LINK_INSTRUCTIONS, TOKEN_HEADER } from "./human_links.js";
import { LIBRARY_CHECKER, libraryEnabled, renderA2aCard } from "./library_contract.js";
import { LIBRARY_BODY_LIMIT, parseJsonBody, proposeThroughDoors, readCappedBody } from "./library_io.js";
// Tessera Cloud Worker: 24/7 native cloud execution on Cloudflare Durable Object SQLite + D1
// (`STORAGE_MODE = "cloud"`, the default) with optional Fly.io origin reverse-proxy fallback
// (`STORAGE_MODE = "origin"`). Serves all Tessera Spec v0.2 HTTP routes (`/v1/*` and
// `/ns/{namespace}/v1/*`), named cloud file buckets (`/v1/buckets/*`), Streamable HTTP MCP (`/mcp`),
// and the agent door (`/connect`, `/llms.txt`, `/llms-full.txt`, `/favicon.svg`,
// `/.well-known/agent-card.json`, `/robots.txt`, `/board`, `/work`).

import { loadCloudBlob, mirrorBucketObjectToD1, sha256Hex, storeCloudBlob, validateWireNumbers } from "./cloud_buckets.js";
import { normalizeNamespace, sliceBytesResponse, tagMatchesCategory } from "./cloud_intelligence.js";
import { govFields, handleMcpRequest, mcpToolsFor } from "./cloud_mcp.js";
import {
  ROBOTS_TXT,
  renderAgentCard,
  renderBoardHtml,
  renderConnectHtml,
  renderFaviconSvg,
  renderLlmsFullTxt,
  renderLlmsTxt,
  renderWorkClaimForm,
  renderWorkLeaseForm,
  renderWorkResumed,
  renderWorkSubmitted,
} from "./cloud_pages.js";
import { TesseraCoordinator } from "./cloud_store.js";
import {
  ALLOWED_RELATION_TYPES,
  CALLER_PROFILES,
  CATEGORY_REGISTRY,
  NOUN_CATEGORIES,
  VERB_CATEGORIES,
  adaptNounView,
  adjudicateBallots,
  canonicalSha256,
  paretoFrontier,
  planFragilityRechecks,
  rankItems,
  reconcileThreeWay,
} from "./ontology_engine.js";

export { TesseraCoordinator };

const DEFAULT_ORIGIN = "https://tessera-fabric.fly.dev";
const DEFAULT_PUBLIC_URL = "https://fabric.teleionlabs.com";
const DEFAULT_REPO_URL = "https://github.com/ta-amps/teleion-fabric-infrastructure-work-and-storage-engine";
const NAMESPACE_RE = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
const SHA256_RE = /^[0-9a-f]{64}$/;

const HOP_BY_HOP = [
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
];

const DEFAULT_CSP_DATA = "default-src 'none'; frame-ancestors 'none'";
const DEFAULT_CSP_PAGE = [
  "default-src 'none'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "form-action 'self'",
  "base-uri 'none'",
  "frame-ancestors 'none'",
].join("; ");

const SECURITY_HEADERS = {
  "strict-transport-security": "max-age=31536000; includeSubDomains",
  "x-content-type-options": "nosniff",
  "referrer-policy": "strict-origin-when-cross-origin",
  "permissions-policy": "camera=(), microphone=(), geolocation=()",
  "x-frame-options": "DENY",
};

const STATUS_TITLES = {
  400: "Bad Request",
  401: "Unauthorized",
  403: "Forbidden",
  404: "Not Found",
  405: "Method Not Allowed",
  409: "Conflict",
  413: "Content Too Large",
  416: "Range Not Satisfiable",
  422: "Unprocessable Content",
  429: "Too Many Requests",
  500: "Internal Server Error",
  502: "Bad Gateway",
  503: "Service Unavailable",
};

const POST_ONLY_EXACT = new Set([
  "/v1/session/anonymous",
  "/v1/session/requester",
  "/v1/queues/define",
  "/v1/tasks",
  "/v1/tasks/claim",
  "/v1/executions/recall",
  "/v1/tasks/heartbeat",
  "/v1/tasks/submit",
  "/v1/tasks/split",
  "/v1/tasks/park",
  "/v1/tasks/resume",
  "/v1/tasks/cancel",
  "/v1/admin/reap",
  "/v1/admin/activity-map/clear",
  "/v1/stores/batch",
  "/v1/blobs",
  "/v1/buckets/define",
  "/v1/handshake",
  "/v1/reconcile",
  "/v1/reconcile/adjudicate",
  "/work/claim",
  "/work/submit",
  "/work/heartbeat",
  "/work/resume",
]);

function isPostOnlyPath(path) {
  if (POST_ONLY_EXACT.has(path)) return true;
  if (/^\/v1\/admin\/keys\/[^/]+\/revoke$/.test(path)) return true;
  if (/^\/v1\/jobs\/[^/]+$/.test(path)) return true;
  return false;
}

function stripHopByHop(headers) {
  const listed = (headers.get("connection") || "")
    .split(",")
    .map((name) => name.trim().toLowerCase())
    .filter(Boolean);
  for (const name of [...HOP_BY_HOP, ...listed]) headers.delete(name);
}

function withSecurity(response, { isWrite = false, noStore = true } = {}) {
  const headers = new Headers(response.headers);
  stripHopByHop(headers);
  for (const [k, v] of Object.entries(SECURITY_HEADERS)) headers.set(k, v);
  if (!headers.has("content-security-policy")) {
    const isHtml = (headers.get("content-type") || "").includes("text/html");
    headers.set("content-security-policy", isHtml ? DEFAULT_CSP_PAGE : DEFAULT_CSP_DATA);
  }
  if (isWrite || noStore || !headers.has("cache-control")) {
    headers.set("cache-control", "no-store");
  }
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function problem(status, code, detail, extra = {}, extraHeaders = {}) {
  const headers = { "content-type": "application/problem+json", ...extraHeaders };
  if (status === 401) headers["www-authenticate"] = 'Bearer realm="tessera"';
  return withSecurity(
    new Response(
      JSON.stringify({
        type: "about:blank",
        title: STATUS_TITLES[status] || "Error",
        status,
        detail,
        code,
        ...extra,
      }),
      { status, headers }
    ),
    { isWrite: true }
  );
}

function jsonResp(data, status = 200, extraHeaders = {}) {
  return withSecurity(
    new Response(JSON.stringify(data), {
      status,
      headers: { "content-type": "application/json", ...extraHeaders },
    }),
    { noStore: true }
  );
}

function htmlResp(html, status = 200, extraHeaders = {}) {
  return withSecurity(
    new Response(html, {
      status,
      headers: { "content-type": "text/html; charset=utf-8", ...extraHeaders },
    }),
    { noStore: true }
  );
}

function rpcResponse(out, extraHeaders = {}) {
  if (!out.ok) {
    const hdrs = { ...extraHeaders };
    const extra = {};
    if (["rate_limited", "storage_quota_exhausted"].includes(out.code) && out.extra?.retry_after != null) {
      hdrs["retry-after"] = String(Math.max(1, Math.ceil(Number(out.extra.retry_after))));
    }
    if (out.code === "storage_quota_exhausted") Object.assign(extra, out.extra);
    if (out.code === "version_conflict") {
      extra.current_version = out.extra?.current_version ?? null;
    }
    return problem(
      out.status || 400,
      out.code || "invalid_request",
      out.detail || "error",
      extra,
      hdrs
    );
  }
  return jsonResp(out.data, out.status || 200, extraHeaders);
}

function extractBearer(request) {
  const hdr = request.headers.get("authorization") || "";
  const m = /^Bearer\s+(\S+)$/i.exec(hdr.trim());
  return m ? m[1] : "";
}

function constantTimeEqual(a, b) {
  if (!a || !b) return false;
  const enc = new TextEncoder();
  const ba = enc.encode(String(a));
  const bb = enc.encode(String(b));
  if (ba.byteLength !== bb.byteLength) return false;
  let diff = 0;
  for (let i = 0; i < ba.byteLength; i++) diff |= ba[i] ^ bb[i];
  return diff === 0;
}

async function readJsonBody(request) {
  try {
    const parsed = await request.json();
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return { error: problem(400, "invalid_request", "JSON body must be an object") };
    }
    return { body: parsed };
  } catch {
    return { error: problem(400, "invalid_request", "request body must be valid JSON") };
  }
}

function parseBoolParam(val) {
  return ["1", "true", "yes"].includes(String(val || "").trim().toLowerCase());
}

// A worker's submit goes through with whichever key it sent; the reply's warnings say which one was used.
function forgivingIdempotency(request, body) {
  const hdrIdem = (request.headers.get("idempotency-key") || "").trim();
  const sentInBody = body.idempotency_key !== undefined && body.idempotency_key !== null && typeof body.idempotency_key !== "object";
  const bodyIdem = sentInBody ? String(body.idempotency_key).trim() : "";
  const warnings = [];
  if (hdrIdem && bodyIdem && hdrIdem !== bodyIdem) warnings.push("the Idempotency-Key header and the body's idempotency_key differ; using the body's");
  return { ...body, idempotency_key: bodyIdem || hdrIdem || null, request_warnings: warnings };
}

function mergeIdempotencyHeader(request, body) {
  const hdrIdem = (request.headers.get("idempotency-key") || "").trim();
  if (body.idempotency_key !== undefined && body.idempotency_key !== null && typeof body.idempotency_key !== "string") {
    return { error: problem(400, "invalid_request", "idempotency_key must be text") };
  }
  const bodyIdem = typeof body.idempotency_key === "string" ? body.idempotency_key.trim() : "";
  if (hdrIdem && bodyIdem && hdrIdem !== bodyIdem) {
    return { error: problem(400, "invalid_request", "Idempotency-Key header and body idempotency_key must match") };
  }
  const effective = bodyIdem || hdrIdem;
  return { body: effective ? { ...body, idempotency_key: effective } : body };
}

function parseLayer2Params(searchParams) {
  return {
    prefs: readClientPrefs(searchParams),
    after: searchParams.get("after") || null,
    q: searchParams.get("q") || "",
    tag: searchParams.get("tag") || null,
    category: searchParams.get("category") || null,
    cohort: searchParams.get("cohort") || null,
    surface: searchParams.get("surface") || null,
    include_restricted: parseBoolParam(searchParams.get("include_restricted")),
  };
}

// Only governance fields come from the upload header (the same list MCP accepts).
// Content identity is computed from the received bytes; a claimed sha256 or size is
// an integrity assertion that must match exactly, never a value the server stores.
function parseHeaderGovernance(request, searchParams) {
  let gov = {};
  const claimed = {};
  const rawGov = request.headers.get("x-tessera-governance");
  if (rawGov) {
    try {
      const parsed = JSON.parse(rawGov);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        return { error: problem(400, "invalid_request", "x-tessera-governance must be a JSON object") };
      }
      gov = govFields(parsed);
      for (const field of ["sha256", "size"]) if (parsed[field] != null) claimed[field] = parsed[field];
    } catch {
      return { error: problem(400, "invalid_request", "x-tessera-governance is not valid JSON") };
    }
  }
  const qVis = searchParams.get("visibility_scope");
  if (qVis) gov.visibility_scope = qVis;
  const qProd = searchParams.get("producer_surface");
  if (qProd) gov.producer_surface = qProd;
  const qTags = searchParams.getAll("tag").filter(Boolean);
  if (qTags.length) {
    gov.tags = Array.isArray(gov.tags) ? [...gov.tags, ...qTags] : qTags;
  }
  const qCohorts = searchParams.getAll("cohort").filter(Boolean);
  if (qCohorts.length) {
    gov.target_cohorts = Array.isArray(gov.target_cohorts)
      ? [...gov.target_cohorts, ...qCohorts]
      : qCohorts;
  }
  return { gov, claimed };
}

async function contentIdentityMismatch(claimed, bytes) {
  if (claimed.size != null && (!Number.isSafeInteger(claimed.size) || claimed.size !== bytes.byteLength)) {
    return problem(400, "invalid_request", "the claimed size does not match the uploaded bytes");
  }
  if (claimed.sha256 != null && (typeof claimed.sha256 !== "string" || claimed.sha256.toLowerCase() !== await sha256Hex(bytes))) {
    return problem(400, "invalid_request", "the claimed digest does not match the uploaded bytes");
  }
  return null;
}

async function proxyToOrigin(request, env) {
  const incoming = new URL(request.url);
  const target = new URL(incoming.pathname + incoming.search, env.ORIGIN || DEFAULT_ORIGIN);
  const headers = new Headers(request.headers);
  stripHopByHop(headers);
  headers.delete("cookie");
  headers.set("x-forwarded-host", incoming.host);
  headers.set("x-forwarded-proto", incoming.protocol.replace(":", ""));
  if (env.EDGE_SECRET) headers.set("x-edge-auth", env.EDGE_SECRET);

  const isRead = request.method === "GET" || request.method === "HEAD";
  let upstream;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      body: isRead ? undefined : request.body,
      redirect: "manual",
    });
  } catch {
    return problem(502, "unavailable", "The Teleion Fabric origin service did not answer.");
  }
  return withSecurity(upstream, { isWrite: !isRead, noStore: !isRead });
}

const SHARED_FALLBACK_ACTIONS = new Set(["heartbeat", "submit", "task", "results"]);
function namesSharedThing(action, args) {
  if (action === "claim") return Boolean(args.queue) && !String(args.queue).startsWith("demo.");
  if (["getRecord", "listRecords", "recordHistory"].includes(action)) return Boolean(args.store) && !String(args.store).startsWith("demo.");
  // A no-key caller has no buckets of its own, so these always read the shared space.
  return ["buckets", "listObjects", "getObjectMeta", "objectHistory", "authorizeBlobRead"].includes(action);
}

function resolveNamespace(url, request) {
  let pathNs = null;
  let path = url.pathname;
  if (path === "/ns" || path.startsWith("/ns/")) {
    const rest = path.slice(4);
    const slashIdx = rest.indexOf("/");
    const rawSeg = slashIdx === -1 ? rest : rest.slice(0, slashIdx);
    const decodedSeg = decodeURIComponent(rawSeg);
    if (!decodedSeg || !NAMESPACE_RE.test(decodedSeg)) {
      return { error: problem(400, "invalid_request", `invalid namespace '${decodedSeg}'`) };
    }
    pathNs = normalizeNamespace(decodedSeg);
    path = slashIdx === -1 ? "/" : rest.slice(slashIdx) || "/";
  }

  const hdrRaw = (
    request.headers.get("x-namespace") ||
    request.headers.get("x-teleion-fabric-namespace") ||
    request.headers.get("x-tessera-namespace") ||
    ""
  ).trim();
  let hdrNs = null;
  if (hdrRaw) {
    if (!NAMESPACE_RE.test(hdrRaw)) {
      return { error: problem(400, "invalid_request", `invalid X-Namespace '${hdrRaw}'`) };
    }
    hdrNs = normalizeNamespace(hdrRaw);
  }

  if (pathNs && hdrNs && pathNs !== hdrNs) {
    return {
      error: problem(
        400,
        "invalid_request",
        `path namespace '${pathNs}' conflicts with X-Namespace '${hdrNs}'`
      ),
    };
  }

  const namespace = pathNs || hdrNs || "main";
  return { namespace, path, isPrimary: namespace === "main" };
}

// Storage-free discovery: pages, agent instructions, contracts and the agent card. These
// responses depend only on configuration, never on a principal or a Durable Object.
function discoveryResponse(path, method, env, publicUrl, repoUrl) {
  if (method !== "GET" && method !== "HEAD") return null;
  const access = accessDiscoveryRoute(path, method, publicUrl, env, jsonResp, htmlResp);
  if (access) return access;
  const discoveryJson = discoveryFile(path);
  if (discoveryJson) return jsonResp(discoveryJson);
  if (path === "/.well-known/openapi.json") return Response.redirect(`${publicUrl}/openapi.json`, 307);
  if (path === "/docs" || path === "/docs/" || path === "/docs.txt") return Response.redirect(`https://teleionlabs.com${path}`, 307);
  if (path === "/index.txt") return discoveryResponse("/llms.txt", method, env, publicUrl, repoUrl);
  if (path === "/sitemap.xml") {
    return withSecurity(new Response(SITEMAP_XML, { status: 200, headers: { "content-type": "application/xml; charset=utf-8" } }), { noStore: false });
  }
  if (path === "/.well-known/security.txt") {
    return withSecurity(new Response("Contact: https://teleionlabs.com/security/\nExpires: 2027-10-31T23:59:59.000Z\nPolicy: https://teleionlabs.com/security/\n", { status: 200, headers: { "content-type": "text/plain; charset=utf-8" } }), { noStore: false });
  }
  if (path === "/" || path === "/connect") {
    return htmlResp(renderConnectHtml(publicUrl, repoUrl, mcpToolsFor(env)));
  }
  if (path === "/favicon.svg") {
    return withSecurity(
      new Response(renderFaviconSvg(), {
        status: 200,
        headers: {
          "content-type": "image/svg+xml; charset=utf-8",
          "cache-control": "public, max-age=86400",
        },
      }),
      { noStore: false }
    );
  }
  if (path === "/llms.txt") {
    const openWindow = isOpenFreeWindow(env);
    const accessProse = openWindow
      ? "\n\nDirect open access through December 31, 2026: any agent may call `/mcp` and `/v1/...` directly with no login, credential, session setup or rate-limit gate (optional `X-Agent-Label` names your agent, and your practice space follows your label and address, or `POST /v1/session/anonymous` for an isolated session token). See `/connect.json` and `/openapi.json`. `tools/list` is public; tool counts and schemas are authoritative there. Operator and admin routes remain protected.\n"
      : "\n\nExternal harnesses and personal assistants: `POST /v1/session/anonymous` without secrets, then use `Authorization: Bearer <session_token>` for practice Tasks and Task Objects. See `/connect.json` and `/openapi.json`. `tools/list` is public; tool counts and schemas are authoritative there. Keyed production work remains protected.\n";
    return withSecurity(
      new Response(renderLlmsTxt(publicUrl, repoUrl, mcpToolsFor(env), openWindow) + "\n\n" + HUMAN_LINK_INSTRUCTIONS + " `POST /v1/human-links` (optional HTTP header `Idempotency-Key`), then `GET /v1/human-links/<id>`." + accessProse + (libraryEnabled(env) ? "\nSeed-pack library: propose one Task Object (`noun`), one Task template (`verb`) or a whole pack with `POST /v1/library/proposals`, the MCP library submission operations or A2A `message/send` at `/a2a` (agent card `/.well-known/agent.json`). A different reviewer claims the `library.review` Task; proposing runs, activates and admits nothing. Status: `GET /v1/library/proposals/<proposal_id>`.\n" : ""), {
        status: 200,
        headers: { "content-type": "text/plain; charset=utf-8" },
      }),
      { noStore: false }
    );
  }
  if (path === "/llms-full.txt") {
    const openWindow = isOpenFreeWindow(env);
    return withSecurity(
      new Response(renderLlmsFullTxt(publicUrl, repoUrl, mcpToolsFor(env), openWindow) + "\n\n" + JSON.stringify(accessDescriptionFor(publicUrl, env), null, 2), {
        status: 200,
        headers: { "content-type": "text/plain; charset=utf-8" },
      }),
      { noStore: false }
    );
  }
  if (path === "/.well-known/agent.json") return libraryEnabled(env) ? jsonResp(renderA2aCard(publicUrl, repoUrl, env)) : problem(404, "not_found", "the library door is not enabled on this fabric");
  if (path === "/.well-known/agent-card.json") {
    return jsonResp({ ...renderAgentCard(publicUrl, repoUrl, mcpToolsFor(env)), authentication: { discovery: "none", demo: isOpenFreeWindow(env) ? "none required through 2026-12-31 (optional X-Agent-Label or tan_ bearer session)" : "tan_ bearer session", keyed: "tsk_ bearer key" }, capabilities: { streaming: "long-poll", tools: true }, access: accessDescriptionFor(publicUrl, env) });
  }
  if (path === "/robots.txt") {
    return withSecurity(
      new Response(ROBOTS_TXT, {
        status: 200,
        headers: { "content-type": "text/plain; charset=utf-8" },
      }),
      { noStore: false }
    );
  }
  return null;
}

// MCP discovery (initialize, ping, tools/list, notifications and explain_access_modes) is
// answered without storage. A body that is not JSON gets its parse error the same way.
const STORAGELESS_MCP_METHODS = new Set(["initialize", "ping", "tools/list"]);
async function mcpNeedsNoStorage(request) {
  let payload;
  try {
    payload = await request.clone().json();
  } catch {
    return true;
  }
  const messages = Array.isArray(payload) ? payload : [payload];
  return messages.every((m) => m && typeof m === "object" && (m.id === undefined || STORAGELESS_MCP_METHODS.has(m.method) || (m.method === "tools/call" && m.params?.name === "explain_access_modes")));
}
const STORAGELESS_STUB = new Proxy({}, { get: (_target, name) => () => { throw new Error(`storage-free MCP discovery must not call ${String(name)}`); } });


// The seed-pack library door. POST /v1/library/proposals files one proposal (a noun, a verb template
// or a whole pack) under the caller's own key or tracked session; the GET routes read proposal
// status stubs, the public catalog and the instruction nouns without a key, and a proposal's content
// with its proposer's or a reviewer's key. /a2a maps A2A message/send to the same path.
function libraryOut(out) {
  if (out.ok) return jsonResp(out.data, out.status || 200);
  const extra = {};
  if (out.extra?.findings) Object.assign(extra, { findings: out.extra.findings, refusal_sha256: out.extra.refusal_sha256 });
  if (out.extra?.budget) extra.budget = out.extra.budget;
  if (out.code === "version_conflict") extra.current_version = out.extra?.current_version ?? null;
  const headers = out.code === "rate_limited" && out.extra?.retry_after != null ? { "retry-after": String(Math.max(1, Math.ceil(Number(out.extra.retry_after)))) } : {};
  if (out.code === "rate_limited") extra.retry_after = out.extra?.retry_after ?? null;
  return problem(out.status || 400, out.code || "invalid_request", out.detail || "error", extra, headers);
}
async function libraryRoute(request, path, url, method, bearer, stub, publicUrl, openWindow = false) {
  if (path === "/a2a") {
    if (method !== "POST") return problem(405, "method_not_allowed", "A2A JSON-RPC accepts POST /a2a", {}, { allow: "POST" });
    return withSecurity(await handleA2a(request, { bearer, openWindow, propose: body => stub.libraryPropose(bearer, body, "a2a"), read: (what, args) => stub.libraryRead(what, args, bearer), sessionUrl: `${publicUrl}/v1/session/anonymous` }), { isWrite: true });
  }
  if (path !== "/v1/library" && !path.startsWith("/v1/library/")) return null;
  if (path === "/v1/library/proposals" && method === "POST") {
    const read = await readCappedBody(request);
    if (read.status) return problem(read.status, read.code, read.detail);
    const parsed = parseJsonBody(read.text);
    if (parsed.status) return problem(parsed.status, parsed.code, parsed.detail);
    return libraryOut(await stub.libraryPropose(bearer, parsed.body, "http"));
  }
  if (method !== "GET" && method !== "HEAD") return problem(405, "method_not_allowed", "library reads are GET; proposals are POST /v1/library/proposals", {}, { allow: path === "/v1/library/proposals" ? "GET, HEAD, POST" : "GET, HEAD" });
  const q = name => url.searchParams.get(name);
  if (path === "/v1/library") return libraryOut(await stub.libraryRead("overview", {}));
  if (path === "/v1/library/proposals") return libraryOut(await stub.libraryRead("proposals", { status: q("status"), after: q("after"), limit: q("limit") }));
  if (path === "/v1/library/catalog") return libraryOut(await stub.libraryRead("catalog", { kind: q("kind"), pack: q("pack"), after: q("after"), limit: q("limit") }));
  if (path === "/v1/library/instructions") return libraryOut(await stub.libraryRead("instructions", { after: q("after"), limit: q("limit") }));
  const one = /^\/v1\/library\/(proposals|catalog)\/([^/]+)$/.exec(path);
  if (one) {
    let id;
    try { id = decodeURIComponent(one[2]); } catch { return problem(400, "invalid_request", "bad escape in path"); }
    return libraryOut(await stub.libraryRead(one[1] === "proposals" ? "proposal" : "catalog_item", one[1] === "proposals" ? { proposal_id: id } : { key: id }, bearer));
  }
  return problem(404, "not_found", "no such library resource");
}

const worker = {
  async fetch(request, env, ctx) {
    if ((env.STORAGE_MODE || "cloud") === "origin") {
      return proxyToOrigin(request, env);
    }

    const url = new URL(request.url);
    const nsResolved = resolveNamespace(url, request);
    if (nsResolved.error) return nsResolved.error;
    let { namespace, path, isPrimary } = nsResolved;
    const method = request.method.toUpperCase();
    const publicUrl =
      (env.PUBLIC_URL || `${url.protocol}//${url.host}`).replace(/\/+$/, "") || DEFAULT_PUBLIC_URL;
    const repoUrl = env.REPO_URL || DEFAULT_REPO_URL;
    // Discovery needs no storage. Answer it before any Durable Object call, so pages, agent
    // instructions and MCP tool discovery stay up while the storage allowance is exhausted.
    // People reach Fabric through its page on teleionlabs.com; agents keep /connect, /connect.json and /llms.txt here.
    if (path === "/" && method === "GET" && (request.headers.get("accept") || "").includes("text/html")) {
      return Response.redirect("https://teleionlabs.com/fabric/", 302);
    }
    const discovery = discoveryResponse(path === "/" && wantsPlainText(request) ? "/llms.txt" : path, method, env, publicUrl, repoUrl);
    if (discovery) return discovery;
    if (path === "/mcp" && method === "POST" && (await mcpNeedsNoStorage(request))) {
      return withSecurity(await handleMcpRequest(request, env, ctx, STORAGELESS_STUB, "", true, namespace), { isWrite: true });
    }
    const coordBinding = env.COORDINATOR || env.TESSERA_COORDINATOR;
    const primary = coordBinding.get(coordBinding.idFromName("primary"));
    const openWindow = isOpenFreeWindow(env);
    // The public activity map needs no principal: aggregated, cached and limited per address.
    const activity = await publicActivityRoute(request, { path, method, namespace, env, primary, problem, withSecurity, address: ipGroup(request.headers.get("cf-connecting-ip")), enforceRateLimit: !openWindow });
    if (activity) return activity;
    // A person answering a human link has no key: the token in the header is the whole credential.
    if (path === "/v1/human-links/public") {
      const token = request.headers.get(TOKEN_HEADER) || "";
      const noindex = { "x-robots-tag": "noindex" };
      if (method === "GET") return jsonResp(await primary.humanLinkRead(token), 200, noindex);
      if (method === "POST") {
        const { body, error } = await readJsonBody(request);
        if (error) return error;
        const merged = mergeIdempotencyHeader(request, body);
        if (merged.error) return merged.error;
        const out = await primary.humanLinkAnswer(token, merged.body);
        return jsonResp(out.data, out.status, { ...noindex, ...(out.headers || {}) });
      }
      return problem(405, "method_not_allowed", "method not allowed");
    }
    const requestBearer = extractBearer(request);
    // Unauthenticated public reads of the main namespace need no identity or key lookup.
    if (!requestBearer && namespace === "main" && (method === "GET" || method === "HEAD")) {
      if (["/v1/health", "/api/health", "/healthz", "/health"].includes(path)) return jsonResp(await primary.publicHealthAndBoard(false, "main"));
      if (path === "/board") {
        const info = await primary.publicHealthAndBoard(true, "main");
        return htmlResp(renderBoardHtml(info.health, info.snapshot, repoUrl, displayMask(env)));
      }
      if (path === "/v1/public/activity" && method === "GET") return jsonResp(await primary.publicActivity());
    }
    const agentLabel = (request.headers.get("x-agent-label") || "").trim() || null;
    const adminOrKeyPath = path === "/v1/keys" || path.startsWith("/v1/keys/") || path.startsWith("/v1/admin/");
    const identity = await primary.requestIdentity(request.headers.get("cf-connecting-ip"), request.headers.get("user-agent"));
    const principal = requestBearer ? await primary.resolveKeyGrant(requestBearer) : (!adminOrKeyPath && openWindow ? openGrantFor(env, agentLabel, undefined, identity.open_hash) : null);
    const explicitNamespace = url.pathname.startsWith("/ns/") || request.headers.has("x-namespace") || request.headers.has("x-teleion-fabric-namespace") || request.headers.has("x-tessera-namespace");
    // A no-key caller reads public Task Objects and feeds from the shared space without naming it; its writes and
    // practice Tasks stay in its own practice space.
    const publicRead = !requestBearer && (method === "GET" || method === "HEAD") && (path === "/v1/search" || path === "/v1/feeds" || path.startsWith("/v1/feeds/") || path === "/v1/reflexes/match");
    if (["anonymous", "requester"].includes(principal?.principal_kind) && !explicitNamespace && !publicRead) namespace = principal.namespace;
    isPrimary = namespace === "main";
    const rawStub = coordBinding.get(coordBinding.idFromName(isPrimary ? "primary" : `ns:${namespace}`));
    // edge: where Cloudflare received this request (data center and continent), from request.cf
    // only. It is set after every client field, so no body, header or tool argument can name a place.
    const meta = { ...identity, agent_label: agentLabel, request_id: request.headers.get("x-request-id") || crypto.randomUUID(), route: path, bytes_in: Number(request.headers.get("content-length") || 0), edge: edgeFromCf(request.cf) };
    // Authentication and namespace authorization happen here, before any namespace object is
    // touched: a caller without a valid key, or without a grant for the namespace it named,
    // neither writes a row nor creates a coordinator object for that name. The coordinator
    // still re-checks the full grant on every call.
    const principals = new Map([[requestBearer, principal]]);
    const principalFor = async (secret) => {
      if (!principals.has(secret)) principals.set(secret, secret ? await primary.resolveKeyGrant(secret) : (!adminOrKeyPath && openWindow ? openGrantFor(env, agentLabel, undefined, identity.open_hash) : null));
      return principals.get(secret);
    };
    const stub = {
      rpcCall: async (secret, action, args = {}, accessFlags = {}) => {
        const grant = await principalFor(secret);
        if (!grant) return { ok: false, status: 401, code: "unauthorized", detail: secret ? "the key is unknown, expired or revoked" : "a bearer key is required", extra: {} };
        // A no-key caller that names no space reaches the shared Tasks and Task Objects it asks for by name; its own
        // practice space keeps its practice Tasks, and a Task that isn't there is looked for in the shared space.
        const implicitNoKey = openWindow && !secret && !explicitNamespace && grant.principal_kind === "anonymous" && normalizeNamespace(args.namespace || "main") === grant.namespace;
        if (implicitNoKey && namesSharedThing(action, args)) args = { ...args, namespace: "main", isPrimary: true };
        const target = normalizeNamespace(args.namespace || "main");
        if (!namespaceReachable(grant, target)) {
          const denied = { ok: false, status: 403, code: "scope_denied", detail: "this principal may not access this namespace", extra: {} };
          ctx.waitUntil(primary.countAccessEvent(publicPrincipal(grant), action, { namespace: target }, denied, meta));
          return denied;
        }
        // A no-key public read over MCP names the shared space, whichever practice space this request routes to.
        const coordinator = target === namespace ? rawStub : coordBinding.get(coordBinding.idFromName(target === "main" ? "primary" : `ns:${target}`));
        const out = await coordinator.rpcCall(secret, action, { ...args, access_meta: { ...meta, ...accessFlags } });
        if (implicitNoKey && target !== "main" && !out.ok && out.status === 404 && SHARED_FALLBACK_ACTIONS.has(action)) {
          return primary.rpcCall(secret, action, { ...args, namespace: "main", isPrimary: true, access_meta: { ...meta, ...accessFlags } });
        }
        return out;
      },
      publicHealthAndBoard: (...args) => rawStub.publicHealthAndBoard(...args),
      bootstrapOrIssueKey: (secret, bootstrap, body) => primary.bootstrapOrIssueKey(secret, bootstrap, { ...body, access_meta: meta }),
      openAnonymousSession: body => primary.openAnonymousSession(body, meta),
      humanLink: (secret, op, args) => primary.humanLink(secret, op, args, meta),
      auditForToken: (secret, args) => primary.auditForToken(secret, { ...args, agent_label: args?.agent_label || agentLabel, open_hash: identity.open_hash }),
      // The library door always works in the shared namespace, under the caller's own principal;
      // its content checks run in the separate library-checks object, never in the primary one.
      libraryPropose: (secret, body, door) => proposeThroughDoors({ primary, checker: coordBinding.get(coordBinding.idFromName(LIBRARY_CHECKER)), secret, body, meta: { ...meta, door }, openWindow }),
      libraryRead: (what, args, secret = null) => primary.libraryRead(what, args, secret),
    };
    const bearer = requestBearer;
    const surfaceHeader = (request.headers.get("x-surface-id") || "").trim();
    const baseRpc = { namespace, isPrimary, surfaceHeader };
    if (namespace !== "main" && ["/board", "/healthz", "/health", "/v1/health", "/api/health"].includes(path)) {
      const access = await stub.rpcCall(bearer, "verifyKey", baseRpc);
      if (!access.ok) return rpcResponse(access);
    }
    const namespaceStub = ns => coordBinding.get(coordBinding.idFromName(ns === "main" ? "primary" : `ns:${ns}`));
    const accessResponse = await accessRoute(request, env, { path, url, bearer, primary, stub, baseRpc, meta, publicUrl, rpcResponse, jsonResp, htmlResp, readJsonBody, namespaceStub });
    if (accessResponse) return accessResponse;
    const libraryResponse = await libraryRoute(request, path, url, method, bearer, stub, publicUrl, openWindow);
    if (libraryResponse) return libraryResponse;
    if (path === "/v1/human-links") {
      if (method !== "POST") return problem(405, "method_not_allowed", "method not allowed");
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      const merged = mergeIdempotencyHeader(request, body);
      if (merged.error) return merged.error;
      return rpcResponse(await stub.humanLink(bearer, "issue", merged.body));
    }
    if (path.startsWith("/v1/human-links/")) {
      const id = decodeURIComponent(path.slice("/v1/human-links/".length));
      if (method === "GET") return rpcResponse(await stub.humanLink(bearer, "get", { id }));
      if (method === "DELETE") return rpcResponse(await stub.humanLink(bearer, "cancel", { id }));
      return problem(405, "method_not_allowed", "method not allowed");
    }


    // 1. Streamable HTTP MCP Door (`/mcp`)
    if (path === "/mcp") {
      const mcpResp = await handleMcpRequest(
        request,
        env,
        ctx,
        stub,
        bearer,
        isPrimary,
        namespace
      );
      return withSecurity(mcpResp, { isWrite: true });
    }

    // 2. Enforce POST-only write routes before parameterized GET routes
    if (isPostOnlyPath(path) && method !== "POST") {
      return problem(
        405,
        "method_not_allowed",
        "writes are POST only; this path changes nothing on GET. If your tool can only open URLs, do the same work with GET at https://shim.teleionlabs.com/q/tasks?state=ready and https://shim.teleionlabs.com/q/claim/<your-name> . Instructions: https://teleionlabs.com/llms.txt",
        {},
        { allow: "POST" }
      );
    }

    // 3. Public health & agent door pages
    if (
      (path === "/v1/health" || path === "/api/health" || path === "/healthz" || path === "/health") &&
      (method === "GET" || method === "HEAD")
    ) {
      const health = await stub.publicHealthAndBoard(false, namespace);
      return jsonResp(health);
    }
    if (path === "/board" && (method === "GET" || method === "HEAD")) {
      const info = await stub.publicHealthAndBoard(true, namespace);
      return htmlResp(renderBoardHtml(info.health, info.snapshot, repoUrl, displayMask(env)));
    }

    // 4. Keyed browser work forms (`/work`, `/work/claim`, `/work/heartbeat`, `/work/submit`, `/work/resume`)
    if (path === "/work" && method === "GET") {
      return htmlResp(renderWorkClaimForm("", repoUrl), 200, { "x-robots-tag": "noindex" });
    }
    if (path === "/work/claim" && method === "POST") {
      const form = await request.formData();
      const formKey = String(form.get("key") || "");
      const queue = String(form.get("queue") || "");
      const worker = String(form.get("worker") || "");
      const out = await stub.rpcCall(formKey, "claim", { ...baseRpc, queue, worker });
      if (!out.ok) {
        return htmlResp(
          renderWorkClaimForm(`${out.code}: ${out.detail}`, repoUrl),
          out.status || 400,
          { "x-robots-tag": "noindex" }
        );
      }
      if (!out.data.lease) {
        return htmlResp(
          renderWorkClaimForm("Nothing is ready in that queue yet.", repoUrl),
          200,
          { "x-robots-tag": "noindex" }
        );
      }
      return htmlResp(renderWorkLeaseForm(out.data.lease, repoUrl), 200, {
        "x-robots-tag": "noindex",
      });
    }
    if (path === "/work/heartbeat" && method === "POST") {
      const form = await request.formData();
      const formKey = String(form.get("key") || "");
      const taskId = String(form.get("task_id") || "");
      const token = Number(form.get("fencing_token") || "0");
      const out = await stub.rpcCall(formKey, "heartbeat", {
        ...baseRpc,
        task_id: taskId,
        fencing_token: token,
      });
      if (!out.ok) {
        return htmlResp(
          renderWorkClaimForm(`${out.code}: ${out.detail}`, repoUrl),
          out.status || 400,
          { "x-robots-tag": "noindex" }
        );
      }
      return htmlResp(renderWorkLeaseForm(out.data, repoUrl), 200, {
        "x-robots-tag": "noindex",
      });
    }
    if (path === "/work/submit" && method === "POST") {
      const form = await request.formData();
      const formKey = String(form.get("key") || "");
      const taskId = String(form.get("task_id") || "");
      const token = Number(form.get("fencing_token") || "0");
      const idem = String(form.get("idempotency_key") || "");
      let output;
      try {
        output = JSON.parse(String(form.get("output") || "{}"));
      } catch {
        return htmlResp(
          renderWorkClaimForm("invalid_request: output is not valid JSON", repoUrl),
          400,
          { "x-robots-tag": "noindex" }
        );
      }
      if (typeof output !== "object" || output === null || Array.isArray(output)) {
        return htmlResp(
          renderWorkClaimForm("invalid_request: output must be a JSON object", repoUrl),
          400,
          { "x-robots-tag": "noindex" }
        );
      }
      const ok = !form.get("failed");
      const errText = !ok ? String(form.get("error") || "") || null : null;
      const out = await stub.rpcCall(formKey, "submit", {
        ...baseRpc,
        task_id: taskId,
        fencing_token: token,
        output,
        idempotency_key: idem,
        ok,
        error: errText,
      });
      if (!out.ok) {
        return htmlResp(
          renderWorkClaimForm(`${out.code}: ${out.detail}`, repoUrl),
          out.status || 400,
          { "x-robots-tag": "noindex" }
        );
      }
      return htmlResp(renderWorkSubmitted(out.data, repoUrl), 200, {
        "x-robots-tag": "noindex",
      });
    }
    if (path === "/work/resume" && method === "POST") {
      const form = await request.formData();
      const formKey = String(form.get("key") || "");
      const taskId = String(form.get("task_id") || "").trim();
      let inputObj;
      try {
        inputObj = JSON.parse(String(form.get("input") || "{}"));
      } catch {
        return htmlResp(
          renderWorkClaimForm("invalid_request: input is not valid JSON", repoUrl),
          400,
          { "x-robots-tag": "noindex" }
        );
      }
      const rawPark = String(form.get("park") || "").trim();
      let parkNum = null;
      if (rawPark) {
        parkNum = Number(rawPark);
        if (!Number.isInteger(parkNum)) {
          return htmlResp(
            renderWorkClaimForm("invalid_request: park must be an integer", repoUrl),
            400,
            { "x-robots-tag": "noindex" }
          );
        }
      }
      const out = await stub.rpcCall(formKey, "resume", {
        ...baseRpc,
        task_id: taskId,
        input: inputObj,
        park: parkNum,
      });
      if (!out.ok) {
        return htmlResp(
          renderWorkClaimForm(`${out.code}: ${out.detail}`, repoUrl),
          out.status || 400,
          { "x-robots-tag": "noindex" }
        );
      }
      return htmlResp(renderWorkResumed(out.data, repoUrl), 200, {
        "x-robots-tag": "noindex",
      });
    }

    // 5. Observation & admin key routes
    if (path === "/v1/counts" && method === "GET") {
      const recount = parseBoolParam(url.searchParams.get("recount"));
      return rpcResponse(await stub.rpcCall(bearer, "counts", { ...baseRpc, recount }));
    }
    if (path === "/v1/events" && method === "GET") {
      const after = Number(url.searchParams.get("after") || "0");
      const limit = Number(url.searchParams.get("limit") || "1000");
      return rpcResponse(await stub.rpcCall(bearer, "events", { ...baseRpc, after, limit }));
    }
    if (path === "/v1/admin/reap" && method === "POST") {
      return rpcResponse(await stub.rpcCall(bearer, "reap", { ...baseRpc }));
    }
    if (path === "/v1/admin/activity-map/clear" && method === "POST") {
      return rpcResponse(await stub.rpcCall(bearer, "clearActivityMap", { ...baseRpc }));
    }
    if (path === "/v1/admin/keys") {
      if (method === "GET") {
        return rpcResponse(await stub.rpcCall(bearer, "listKeys", { ...baseRpc }));
      }
      if (method === "POST") {
        const { body, error } = await readJsonBody(request);
        if (error) return error;
        const bootHdr = request.headers.get("x-bootstrap-secret") || "";
        const bootstrapAllowed =
          (env.BOOTSTRAP_SECRET && constantTimeEqual(bootHdr, env.BOOTSTRAP_SECRET)) ||
          (env.EDGE_SECRET && constantTimeEqual(bootHdr, env.EDGE_SECRET));
        return rpcResponse(
          await stub.bootstrapOrIssueKey(bearer, Boolean(bootstrapAllowed), body)
        );
      }
      return problem(405, "method_not_allowed", "method not allowed");
    }
    const revokeMatch = /^\/v1\/admin\/keys\/([^/]+)\/revoke$/.exec(path);
    if (revokeMatch && method === "POST") {
      return rpcResponse(
        await stub.rpcCall(bearer, "revokeKey", {
          ...baseRpc,
          key_id: decodeURIComponent(revokeMatch[1]),
        })
      );
    }

    // 6. Queues & tasks (atomic verbs)
    if (path === "/v1/queues/define" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      return rpcResponse(await stub.rpcCall(bearer, "defineQueue", { ...body, ...baseRpc }));
    }
    if (path === "/v1/queues" && method === "GET") {
      return rpcResponse(await stub.rpcCall(bearer, "queues", { ...baseRpc }));
    }
    if (path === "/v1/tasks" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      const hdrIdem = (request.headers.get("idempotency-key") || "").trim();
      const idempotencyKey = body.idempotency_key ?? (hdrIdem || null);
      return rpcResponse(
        await stub.rpcCall(bearer, "enqueue", {
          ...body,
          idempotency_key: idempotencyKey,
          ...baseRpc,
        })
      );
    }
    if (/^\/v1\/tasks\/[^/]+\/executions$/.test(path) && method === "GET") {
      return rpcResponse(await stub.rpcCall(bearer,"listExecutions",{task_id:decodeURIComponent(path.split("/")[3]),after:Number(url.searchParams.get("after") || 0),limit:Number(url.searchParams.get("limit") || 20),...baseRpc}));
    }
    if (path === "/v1/executions/recall" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      return rpcResponse(await stub.rpcCall(bearer,"recallExecution",{...body,...baseRpc}));
    }
    if (path.startsWith("/v1/executions/") && method === "GET") {
      return rpcResponse(await stub.rpcCall(bearer,"getExecution",{run_id:decodeURIComponent(path.slice("/v1/executions/".length)),...baseRpc}));
    }
    if (path === "/v1/tasks/claim" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      // source_kinds is the shared name for the kinds a client wants; kinds keeps working.
      const out = await stub.rpcCall(bearer, "claim", { ...body, kinds: body.kinds ?? (typeof body.source_kinds === "string" ? body.source_kinds.split(",").map((k) => k.trim()).filter(Boolean) : body.source_kinds) ?? null, ...baseRpc });
      if (!out.ok) return rpcResponse(out);
      if (!out.data.lease) {
        return withSecurity(
          new Response(null, {
            status: 204,
            headers: {
              ...out.headers,
              "retry-after": String(out.data.retry_after ?? 5),
              "x-heartbeat-seconds": String(out.data.beat || "60"),
            },
          }),
          { isWrite: true }
        );
      }
      return jsonResp(out.data.lease, 200, {
        ...out.headers,
        "x-heartbeat-seconds": String(out.data.beat || "60"),
      });
    }
    if (path === "/v1/tasks/heartbeat" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      return rpcResponse(await stub.rpcCall(bearer, "heartbeat", { ...body, ...baseRpc }));
    }
    if (path === "/v1/tasks/submit" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      return rpcResponse(await stub.rpcCall(bearer, "submit", { ...forgivingIdempotency(request, body), ...baseRpc }));
    }
    if (path === "/v1/tasks/split" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      return rpcResponse(await stub.rpcCall(bearer, "split", { ...body, ...baseRpc }));
    }
    if (path === "/v1/tasks/park" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      return rpcResponse(await stub.rpcCall(bearer, "park", { ...body, ...baseRpc }));
    }
    if (path === "/v1/tasks/resume" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      return rpcResponse(await stub.rpcCall(bearer, "resume", { ...body, ...baseRpc }));
    }
    if (path === "/v1/tasks/cancel" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      return rpcResponse(await stub.rpcCall(bearer, "cancel", { ...body, ...baseRpc }));
    }
    const taskResultsMatch = /^\/v1\/tasks\/([^/]+)\/results$/.exec(path);
    if (taskResultsMatch && method === "GET") {
      return rpcResponse(
        await stub.rpcCall(bearer, "results", {
          finished: parseBoolParam(url.searchParams.get("finished")),
          ...baseRpc,
          task_id: decodeURIComponent(taskResultsMatch[1]),
        })
      );
    }
    const taskMatch = /^\/v1\/tasks\/([^/]+)$/.exec(path);
    if (taskMatch && method === "GET") {
      return rpcResponse(
        await stub.rpcCall(bearer, "task", {
          ...baseRpc,
          task_id: decodeURIComponent(taskMatch[1]),
        })
      );
    }

    // 7. Model jobs
    if (path === "/v1/jobs") {
      if (method === "GET") {
        const taskId = url.searchParams.get("task_id");
        return rpcResponse(
          await stub.rpcCall(bearer, "jobs", { ...baseRpc, task_id: taskId || null })
        );
      }
      if (method === "POST") {
        const { body, error } = await readJsonBody(request);
        if (error) return error;
        return rpcResponse(await stub.rpcCall(bearer, "registerJob", { ...body, ...baseRpc }));
      }
      return problem(405, "method_not_allowed", "method not allowed");
    }
    const jobUpdateMatch = /^\/v1\/jobs\/([^/]+)$/.exec(path);
    if (jobUpdateMatch && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      return rpcResponse(
        await stub.rpcCall(bearer, "updateJob", {
          ...body,
          ...baseRpc,
          job_id: decodeURIComponent(jobUpdateMatch[1]),
        })
      );
    }

    // 8. Queryable CAS Noun Stores (`/v1/stores/*`)
    if (path === "/v1/stores" && method === "GET") {
      const own = await stub.rpcCall(bearer, "stores", { ...baseRpc });
      // A no-key caller who names no space sees its practice stores and the public ones.
      if (own.ok && !requestBearer && !explicitNamespace && namespace !== "main" && openWindow) {
        const shared = await stub.rpcCall(bearer, "stores", { ...baseRpc, namespace: "main", isPrimary: true });
        if (shared.ok) own.data = { ...own.data, stores: [...own.data.stores, ...shared.data.stores] };
      }
      return rpcResponse(own);
    }
    if (path === "/v1/stores/batch" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      return rpcResponse(
        await stub.rpcCall(bearer, "putRecords", { ...baseRpc, writes: body.writes })
      );
    }
    const storeListMatch = /^\/v1\/stores\/([^/]+)\/records$/.exec(path);
    if (storeListMatch && method === "GET") {
      const prefix = url.searchParams.get("prefix") || "";
      const limit = Number(url.searchParams.get("limit") || "100");
      return rpcResponse(
        await stub.rpcCall(bearer, "listRecords", {
          ...baseRpc,
          store: decodeURIComponent(storeListMatch[1]),
          prefix,
          limit,
          ...parseLayer2Params(url.searchParams),
        })
      );
    }
    if (storeListMatch && method === "POST") {
      const storeName = decodeURIComponent(storeListMatch[1]);
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      if (typeof body.key !== "string" || !body.key.trim()) {
        return problem(400, "invalid_request", "key must be a non-empty string (or POST /v1/stores/{store}/records/{key})");
      }
      let expectedVersion = body.expected_version ?? null;
      const matchVersion = request.headers.get("if-match");
      if (matchVersion != null) {
        const rawVersion = matchVersion.replace(/^"|"$/g, "");
        if (!/^\d+$/.test(rawVersion) || !Number.isSafeInteger(Number(rawVersion))) return problem(400, "invalid_request", "If-Match must be a nonnegative integer version");
        if (expectedVersion != null && expectedVersion !== Number(rawVersion)) return problem(400, "invalid_request", "If-Match and expected_version disagree");
        expectedVersion = Number(rawVersion);
      }
      const out = await stub.rpcCall(bearer, "putRecords", {
        ...baseRpc,
        writes: [
          {
            store: storeName,
            key: body.key,
            value: body.value,
            expected_version: expectedVersion,
            producer_surface: body.producer_surface,
            visibility_scope: body.visibility_scope,
            allowed_surfaces: body.allowed_surfaces,
            target_cohorts: body.target_cohorts,
            tags: body.tags,
            owner_key_id: body.owner_key_id,
          },
        ],
      });
      if (!out.ok) return rpcResponse(out);
      return jsonResp(out.data.records[0], 200, { etag: `"${out.data.records[0].version}"` });
    }
    const recHistoryMatch = /^\/v1\/stores\/([^/]+)\/records\/([^/]+)\/history$/.exec(path);
    if (recHistoryMatch && method === "GET") {
      return rpcResponse(
        await stub.rpcCall(bearer, "recordHistory", {
          ...baseRpc,
          store: decodeURIComponent(recHistoryMatch[1]),
          key: decodeURIComponent(recHistoryMatch[2]),
        })
      );
    }
    const recMatch = /^\/v1\/stores\/([^/]+)\/records\/([^/]+)$/.exec(path);
    if (recMatch) {
      const storeName = decodeURIComponent(recMatch[1]);
      const recKey = decodeURIComponent(recMatch[2]);
      if (method === "GET") {
        const verParam = url.searchParams.get("version");
        const version = verParam == null ? null : Number(verParam);
        return rpcResponse(
          await stub.rpcCall(bearer, "getRecord", {
            ...baseRpc,
            store: storeName,
            key: recKey,
            version,
            prefs: readClientPrefs(url.searchParams),
          })
        );
      }
      if (method === "POST") {
        const { body, error } = await readJsonBody(request);
        if (error) return error;
        let expectedVersion = body.expected_version ?? null;
        const matchVersion = request.headers.get("if-match");
        if (matchVersion != null) {
          const rawVersion=matchVersion.replace(/^"|"$/g, "");
          if (!/^\d+$/.test(rawVersion) || !Number.isSafeInteger(Number(rawVersion))) return problem(400,"invalid_request","If-Match must be a nonnegative integer version");
          if (expectedVersion != null && expectedVersion !== Number(rawVersion)) return problem(400,"invalid_request","If-Match and expected_version disagree");
          expectedVersion=Number(rawVersion);
        }
        const out = await stub.rpcCall(bearer, "putRecords", {
          ...baseRpc,
          writes: [
            {
              store: storeName,
              key: recKey,
              value: body.value,
              expected_version: expectedVersion,
              producer_surface: body.producer_surface,
              visibility_scope: body.visibility_scope,
              allowed_surfaces: body.allowed_surfaces,
              target_cohorts: body.target_cohorts,
              tags: body.tags,
              owner_key_id: body.owner_key_id,
            },
          ],
        });
        if (!out.ok) return rpcResponse(out);
        return jsonResp(out.data.records[0], 200, {etag: `"${out.data.records[0].version}"`});
      }
      return problem(405, "method_not_allowed", "method not allowed");
    }

    // 9. Content-addressed Blobs (`/v1/blobs/*`)
    if (path === "/v1/blobs" && method === "POST") {
      const authCheck = await stub.rpcCall(bearer, "verifyStoreScope", { ...baseRpc });
      if (!authCheck.ok) return rpcResponse(authCheck);
      const { gov, claimed, error: govErr } = parseHeaderGovernance(request, url.searchParams);
      if (govErr) return govErr;
      const raw = new Uint8Array(await request.arrayBuffer());
      const mismatch = await contentIdentityMismatch(claimed, raw);
      if (mismatch) return mismatch;
      const permit = await stub.rpcCall(bearer, "authorizeBlobWrite", { ...gov, ...baseRpc });
      if (!permit.ok) return rpcResponse(permit);
      const stored = await storeCloudBlob(env, raw, Date.now() / 1000, namespace, permit.data.governance);
      if (stored.error) return problem(stored.status || 400, stored.error, stored.detail);
      // Server-computed identity is spread last so no client field can replace it.
      return rpcResponse(
        await stub.rpcCall(bearer, "recordBlobStored", {
          ...gov,
          ...baseRpc,
          sha256: stored.sha256,
          size: stored.size,
        })
      );
    }
    const blobGetMatch = /^\/v1\/blobs\/([^/]+)$/.exec(path);
    if (blobGetMatch && method === "GET") {
      const sha256 = decodeURIComponent(blobGetMatch[1]).toLowerCase();
      if (!SHA256_RE.test(sha256)) {
        const authCheck = await stub.rpcCall(bearer, "verifyStoreScope", { ...baseRpc });
        if (!authCheck.ok) return rpcResponse(authCheck);
        return problem(404, "not_found", `blob ${sha256} does not exist`);
      }
      const authz = await stub.rpcCall(bearer, "authorizeBlobRead", {
        ...baseRpc,
        sha256,
      });
      if (!authz.ok) return rpcResponse(authz);
      const loaded = await loadCloudBlob(env, sha256);
      if (loaded.error) return problem(loaded.status || 404, loaded.error, loaded.detail);
      // A no-key reader asked for bytes by their digest: a copy with something removed wouldn't be those bytes, so it reads as absent.
      if (authz.public_only && (publicBytes(loaded.bytes).changed || publicBytes(loaded.bytes).tooLarge)) return problem(404, "not_found", `blob ${sha256} does not exist`);
      return withSecurity(
        sliceBytesResponse(loaded.bytes, request.headers.get("range"), "application/octet-stream", {
          etag: `"${sha256}"`,
        }),
        { noStore: true }
      );
    }

    // 10. Persistent Named File Buckets (`/v1/buckets/*`)
    if (path === "/v1/buckets/define" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      return rpcResponse(await stub.rpcCall(bearer, "defineBucket", { ...body, ...baseRpc }));
    }
    if (path === "/v1/buckets" && method === "GET") {
      return rpcResponse(await stub.rpcCall(bearer, "buckets", { ...baseRpc }));
    }
    const bucketListMatch = /^\/v1\/buckets\/([^/]+)\/objects$/.exec(path);
    if (bucketListMatch && method === "GET") {
      const prefix = url.searchParams.get("prefix") || "";
      const limit = Number(url.searchParams.get("limit") || "100");
      return rpcResponse(
        await stub.rpcCall(bearer, "listObjects", {
          ...baseRpc,
          bucket: decodeURIComponent(bucketListMatch[1]),
          prefix,
          limit,
          ...parseLayer2Params(url.searchParams),
        })
      );
    }
    const bucketObjMatch = /^\/v1\/buckets\/([^/]+)\/objects\/(.+)$/.exec(path);
    if (bucketObjMatch) {
      const bucketName = decodeURIComponent(bucketObjMatch[1]);
      const objKey = bucketObjMatch[2]
        .split("/")
        .map((seg) => decodeURIComponent(seg))
        .join("/");
      if (method === "POST") {
        const authCheck = await stub.rpcCall(bearer, "verifyStoreScope", { ...baseRpc });
        if (!authCheck.ok) return rpcResponse(authCheck);
        let expectedVersion = null;
        const evParam =
          url.searchParams.get("expected_version") ?? request.headers.get("x-expected-version");
        if (evParam != null && evParam !== "") {
          const parsedEv = Number(evParam);
          if (!Number.isInteger(parsedEv)) {
            return problem(400, "invalid_request", "expected_version must be an integer");
          }
          expectedVersion = parsedEv;
        }
        let metadata = {};
        const rawMeta = request.headers.get("x-tessera-metadata");
        if (rawMeta) {
          try {
            const parsed = JSON.parse(rawMeta);
            if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
              return problem(400, "invalid_request", "x-tessera-metadata must be a JSON object");
            }
            metadata = parsed;
          } catch {
            return problem(400, "invalid_request", "x-tessera-metadata is not valid JSON");
          }
        }
        const { gov, claimed, error: govErr } = parseHeaderGovernance(request, url.searchParams);
        if (govErr) return govErr;
        const contentType = request.headers.get("content-type") || "application/octet-stream";
        const rawBytes = new Uint8Array(await request.arrayBuffer());
        const mismatch = await contentIdentityMismatch(claimed, rawBytes);
        if (mismatch) return mismatch;
        const permit = await stub.rpcCall(bearer, "authorizeObjectWrite", { ...gov, ...baseRpc, bucket:bucketName, key:objKey, expected_version:expectedVersion, metadata });
        if (!permit.ok) return rpcResponse(permit);
        const stored = await storeCloudBlob(env, rawBytes, Date.now() / 1000, namespace, permit.data.governance);
        if (stored.error) return problem(stored.status || 400, stored.error, stored.detail);
        const out = await stub.rpcCall(bearer, "putObjectMeta", {
          ...gov,
          ...baseRpc,
          bucket: bucketName,
          key: objKey,
          sha256: stored.sha256,
          size: stored.size,
          content_type: contentType,
          metadata,
          expected_version: expectedVersion,
        });
        if (!out.ok) return rpcResponse(out);
        ctx.waitUntil(mirrorBucketObjectToD1(env, out.data));
        return jsonResp(out.data, 201);
      }
      if (method === "GET") {
        const verParam = url.searchParams.get("version");
        const version = verParam == null ? null : Number(verParam);
        const wantHistory = parseBoolParam(url.searchParams.get("history"));
        const wantMeta = parseBoolParam(url.searchParams.get("meta"));
        if (wantHistory) {
          return rpcResponse(
            await stub.rpcCall(bearer, "objectHistory", {
              ...baseRpc,
              bucket: bucketName,
              key: objKey,
            })
          );
        }
        const objectPrefs = readClientPrefs(url.searchParams);
        const metaOut = await stub.rpcCall(bearer, "getObjectMeta", {
          ...baseRpc,
          bucket: bucketName,
          key: objKey,
          version,
          prefs: objectPrefs,
        });
        if (!metaOut.ok) return rpcResponse(metaOut);
        const loaded = wantMeta ? null : await loadCloudBlob(env, metaOut.data.sha256);
        if (loaded?.error) return problem(loaded.status || 404, loaded.error, loaded.detail);
        // public_read.js: a no-key reader gets the exact bytes unless the display step changed something; the same for HTTP, MCP and every version.
        if (wantMeta) return rpcResponse(metaOut);
        const shown = metaOut.public_only ? publicBytes(loaded.bytes) : { bytes: loaded.bytes, changed: false };
        if (shown.tooLarge) return problem(413, "too_large_for_public_read", "This file is too large to serve in public form.");
        const cutTo = objectPrefs.max_bytes != null && !request.headers.get("range") && shown.bytes.byteLength > objectPrefs.max_bytes ? objectPrefs.max_bytes : null;
        return withSecurity(
          sliceBytesResponse(
            cutTo == null ? shown.bytes : shown.bytes.subarray(0, cutTo),
            request.headers.get("range"),
            metaOut.data.content_type || "application/octet-stream",
            {
              ...(shown.changed ? { "x-personal-data-note": PUBLIC_COPY_NOTE } : { etag: `"${metaOut.data.sha256}"` }),
              "x-tessera-version": String(metaOut.data.version),
              "x-tessera-sha256": metaOut.data.sha256,
              "x-tessera-object-meta": JSON.stringify(metaOut.data),
              ...(metaOut.data.review_state ? { "x-review-state": metaOut.data.review_state } : {}),
              ...(cutTo == null ? {} : { "x-truncated-to-bytes": String(cutTo), "x-total-bytes": String(shown.bytes.byteLength) }),
            }
          ),
          { noStore: true }
        );
      }
      return problem(405, "method_not_allowed", "method not allowed");
    }

    // 11. 0-Call L0 Scent Search, Live Delta Feeds & Reflex Recall
    if (path === "/v1/search" && method === "GET") {
      const q = url.searchParams.get("q") || "";
      const store =
        url.searchParams.get("store") ||
        url.searchParams.get("store_name") ||
        url.searchParams.get("store_filter") ||
        null;
      const bucket = url.searchParams.get("bucket") || null;
      const limit = Number(url.searchParams.get("limit") || "20");
      return rpcResponse(
        await stub.rpcCall(bearer, "searchNouns", {
          ...baseRpc,
          query: q,
          store,
          bucket,
          limit,
          offset: url.searchParams.get("offset"),
          ...parseLayer2Params(url.searchParams),
        })
      );
    }
    if (path === "/v1/feeds" && method === "GET") {
      return rpcResponse(await stub.rpcCall(bearer, "feeds", { ...baseRpc }));
    }
    const feedMatch = /^\/v1\/feeds\/([^/]+)$/.exec(path);
    if (feedMatch) {
      const feedName = decodeURIComponent(feedMatch[1]);
      if (method === "GET") {
        const after = Number(url.searchParams.get("after") || "0");
        const limit = Number(url.searchParams.get("limit") || "100");
        return rpcResponse(
          await stub.rpcCall(bearer, "pollFeed", {
            ...baseRpc,
            feed: feedName,
            after,
            limit,
            ...parseLayer2Params(url.searchParams),
          })
        );
      }
      if (method === "POST") {
        const { body, error } = await readJsonBody(request);
        if (error) return error;
        return rpcResponse(
          await stub.rpcCall(bearer, "publishFeed", {
            ...baseRpc,
            feed: feedName,
            payload: body.payload,
            kind: body.kind ?? "delta",
            summary: body.summary ?? "",
            producer_surface: body.producer_surface,
            visibility_scope: body.visibility_scope,
            allowed_surfaces: body.allowed_surfaces,
            target_cohorts: body.target_cohorts,
            tags: body.tags,
            owner_key_id: body.owner_key_id,
          })
        );
      }
      return problem(405, "method_not_allowed", "method not allowed");
    }
    if (path === "/v1/reflexes/match" && method === "GET") {
      const q = url.searchParams.get("q") || "";
      const limit = Number(url.searchParams.get("limit") || "5");
      return rpcResponse(
        await stub.rpcCall(bearer, "matchReflex", {
          ...baseRpc,
          query: q,
          limit,
          ...parseLayer2Params(url.searchParams),
        })
      );
    }
    if (path === "/v1/handshake" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      const out = await stub.rpcCall(bearer, "handshake", {
        ...body,
        ...baseRpc,
        header_surface_id: surfaceHeader,
      });
      if (!out.ok) return rpcResponse(out);
      return jsonResp(out.data, 200, {
        "x-surface-id": out.data.surface_id,
        "x-handshake-id": out.data.handshake_id,
      });
    }

    // 12. Wave-2 Categorized Ontology, Empirical-Bayes Rankings, Fragility Decay & 3-Way Reconciliation
    if (path === "/v1/categories" && method === "GET") {
      const authCheck = await stub.rpcCall(bearer, "verifyStoreScope", { ...baseRpc });
      if (!authCheck.ok) return rpcResponse(authCheck);
      const profile = url.searchParams.get("profile") || null;
      let activeProfile = null;
      if (profile) {
        if (!CALLER_PROFILES[profile]) {
          return problem(400, "invalid_request", `unknown caller profile '${profile}'`);
        }
        activeProfile = CALLER_PROFILES[profile];
      }
      return jsonResp({
        noun_categories: Object.keys(NOUN_CATEGORIES)
          .sort()
          .map((cid) => NOUN_CATEGORIES[cid]),
        verb_categories: Object.keys(VERB_CATEGORIES)
          .sort()
          .map((cid) => VERB_CATEGORIES[cid]),
        allowed_relations: [...ALLOWED_RELATION_TYPES].sort(),
        caller_profiles: Object.keys(CALLER_PROFILES)
          .sort()
          .map((pid) => CALLER_PROFILES[pid]),
        active_profile: activeProfile,
      });
    }

    const rankMatch = /^\/v1\/rankings\/([^/]+)$/.exec(path);
    if (rankMatch && method === "GET") {
      const authCheck = await stub.rpcCall(bearer, "verifyStoreScope", { ...baseRpc });
      if (!authCheck.ok) return rpcResponse(authCheck);
      const categoryId = decodeURIComponent(rankMatch[1]);
      if (!CATEGORY_REGISTRY[categoryId]) {
        return problem(400, "invalid_request", `unknown category '${categoryId}'`);
      }
      const profile = url.searchParams.get("profile") || null;
      if (profile && !CALLER_PROFILES[profile]) {
        return problem(400, "invalid_request", `unknown caller profile '${profile}'`);
      }
      const targetStore =
        url.searchParams.get("store") || url.searchParams.get("store_name") || "tessera.seeds";
      const axis = url.searchParams.get("axis") || "composite_score";
      const limit = Math.max(1, Math.min(Number(url.searchParams.get("limit") || "50"), 200));
      const recOut = await stub.rpcCall(bearer, "listRecords", {
        ...baseRpc,
        store: targetStore,
        prefix: "",
        limit: 1000,
        ...parseLayer2Params(url.searchParams),
        category: categoryId,
      });
      if (!recOut.ok) return rpcResponse(recOut);
      const nowTs = Date.now() / 1000;
      const items = [];
      const rawById = new Map();
      const unranked = [];
      for (const rec of recOut.data.records || []) {
        const val =
          rec.value && typeof rec.value === "object" && !Array.isArray(rec.value)
            ? rec.value
            : { value: rec.value };
        if (!tagMatchesCategory(rec.tags || [], categoryId)) continue;
        const iid = String(val.item_id || val.id || rec.key);
        const fragSub =
          val.fragility && typeof val.fragility === "object" ? val.fragility : {};
        const trials = val.trials, successes = val.successes;
        if (!Number.isInteger(trials) || trials < 0 || typeof successes !== "number" || !Number.isFinite(successes) || successes < 0 || successes > trials) {
          unranked.push({ id: iid, trials: null, successes: null, score: null, reason: "Reproducible trial and success counts are unavailable or invalid" });
          continue;
        }
        const normItem = {
          item_id: iid,
          id: iid,
          category_id: categoryId,
          title: String(val.title || iid),
          dimensions: val.dimensions && typeof val.dimensions === "object" ? val.dimensions : {},
          trials,
          successes,
          fragility_score: fragSub.fragility_score ?? val.fragility_score,
          half_life_hours: fragSub.half_life_hours ?? val.half_life_hours,
          last_verified_at:
            val.last_verified_at ||
            val.last_checked_at ||
            fragSub.last_verified_at ||
            rec.created_at ||
            rec.updated_at,
        };
        items.push(normItem);
        rawById.set(iid, { ...val, ...normItem });
      }
      const ranked = rankItems(items, { sort_axis: axis, now_ts: nowTs, top_k: limit });
      const frontierIds = new Set(
        (ranked.length ? paretoFrontier(ranked) : []).map((r) => r.item_id)
      );
      const outRows = ranked.map((r) => {
        const entry = {
          ...r,
          id: r.item_id,
          pareto_optimal: frontierIds.has(r.item_id),
        };
        if (profile && rawById.has(r.item_id)) {
          entry.adapted_view = adaptNounView(rawById.get(r.item_id), profile);
        }
        return entry;
      });
      return jsonResp({
        category_id: categoryId,
        store: targetStore,
        sort_axis: axis,
        profile,
        candidate_limit: 1000,
        pareto_frontier_ids: [...frontierIds].sort(),
        ranked: outRows,
        unranked,
      });
    }

    if (path === "/v1/fragility/plan" && method === "GET") {
      const authCheck = await stub.rpcCall(bearer, "verifyStoreScope", { ...baseRpc });
      if (!authCheck.ok) return rpcResponse(authCheck);
      const targetStore =
        url.searchParams.get("store") || url.searchParams.get("store_name") || "registry.feeds";
      const budget = Math.max(1, Math.min(Number(url.searchParams.get("budget") || "25"), 100));
      const simHours = Math.max(
        0,
        Number(url.searchParams.get("simulate_hours_ahead") ?? "16")
      );
      const recOut = await stub.rpcCall(bearer, "listRecords", {
        ...baseRpc,
        store: targetStore,
        prefix: "",
        limit: 200,
        ...parseLayer2Params(url.searchParams),
      });
      if (!recOut.ok) return rpcResponse(recOut);
      const nowTs = Date.now() / 1000 + simHours * 3600;
      const items = (recOut.data.records || []).map((rec) => {
        const val =
          rec.value && typeof rec.value === "object" && !Array.isArray(rec.value) ? rec.value : {};
        const fragSub =
          val.fragility && typeof val.fragility === "object" ? val.fragility : {};
        return {
          item_id: String(val.item_id || val.id || rec.key),
          category_id: String(val.category_id || val.category || "noun.feed_source"),
          source_uri: val.url || val.source_uri || null,
          expected_sha256: val.content_sha256 || val.sha256 || null,
          etag: val.etag || null,
          last_modified: val.last_modified || null,
          half_life_hours: fragSub.half_life_hours ?? val.half_life_hours ?? 12.0,
          fragility_score: fragSub.fragility_score ?? val.fragility_score ?? 0.85,
          last_verified_at:
            val.last_verified_at ||
            val.last_checked_at ||
            fragSub.last_verified_at ||
            rec.created_at ||
            rec.updated_at,
          importance: Number(val.importance ?? 1.0),
        };
      });
      const plan = planFragilityRechecks(items, nowTs, budget);
      return jsonResp({
        store: targetStore,
        evaluated_at: nowTs,
        plan,
      });
    }

    if (path === "/v1/reconcile" && method === "POST") {
      const authCheck = await stub.rpcCall(bearer, "verifyStoreScope", { ...baseRpc });
      if (!authCheck.ok) return rpcResponse(authCheck);
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      const baseDoc =
        body.base && typeof body.base === "object" ? body.base : body.ancestor;
      const oursDoc =
        body.ours && typeof body.ours === "object" ? body.ours : body.left;
      const theirsDoc =
        body.theirs && typeof body.theirs === "object" ? body.theirs : body.right;
      if (
        !baseDoc ||
        typeof baseDoc !== "object" ||
        !oursDoc ||
        typeof oursDoc !== "object" ||
        !theirsDoc ||
        typeof theirsDoc !== "object"
      ) {
        return problem(
          400,
          "invalid_request",
          "base/ancestor, ours/left, and theirs/right must be JSON objects"
        );
      }
      const setFields = Array.isArray(body.set_fields)
        ? body.set_fields
        : Array.isArray(body.monotonic_set_fields)
          ? body.monotonic_set_fields
          : [];
      const counterFields = Array.isArray(body.counter_fields) ? body.counter_fields : [];
      const conflict = reconcileThreeWay(baseDoc, oursDoc, theirsDoc, {
        ours_proposer_key_id: body.ours_proposer_key_id || null,
        theirs_proposer_key_id: body.theirs_proposer_key_id || null,
        set_fields: setFields,
        counter_fields: counterFields,
      });
      return jsonResp({ ...conflict, clean: conflict.status === "auto_merged" });
    }

    if (path === "/v1/reconcile/adjudicate" && method === "POST") {
      const authCheck = await stub.rpcCall(bearer, "verifyStoreScope", { ...baseRpc });
      if (!authCheck.ok) return rpcResponse(authCheck);
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      const aliasMap = { cand_a: "candidate_a", cand_b: "candidate_b" };
      const revAlias = {};
      let conflictArg;
      if (body.conflict && typeof body.conflict === "object") {
        conflictArg = { ...body.conflict };
      } else {
        const cands = body.candidates || {};
        const docA = { ...(cands.candidate_a || cands.cand_a || { option: "a" }) };
        const docB = { ...(cands.candidate_b || cands.cand_b || { option: "b" }) };
        if ("cand_a" in cands) revAlias.candidate_a = "cand_a";
        if ("cand_b" in cands) revAlias.candidate_b = "cand_b";
        const proposer = body.proposer_id || body.proposer_key_id || null;
        conflictArg = {
          conflict_id: String(body.conflict_id || "conflict-adjudication"),
          status: "needs_voting_or_steward",
          conflicting_paths: Array.isArray(body.conflicting_paths)
            ? body.conflicting_paths
            : ["summary"],
          proposer_key_ids: proposer ? [proposer] : [],
          candidate_a: {
            candidate_id: "candidate_a",
            proposer_key_id: body.candidate_a_proposer || null,
            conflicting_values: docA,
            document: docA,
            sha256: canonicalSha256(docA),
          },
          candidate_b: {
            candidate_id: "candidate_b",
            proposer_key_id: proposer,
            conflicting_values: docB,
            document: docB,
            sha256: canonicalSha256(docB),
          },
        };
      }
      const rawBallots = Array.isArray(body.ballots) ? body.ballots : [];
      const normBallots = [];
      const calibrations =
        body.worker_calibrations && typeof body.worker_calibrations === "object"
          ? { ...body.worker_calibrations }
          : {};
      for (const b of rawBallots) {
        if (!b || typeof b !== "object") continue;
        let voterKey = String(b.voter_key_id || b.key_prefix || b.voter_id || "");
        if (body.proposer_id && b.voter_id === body.proposer_id) {
          voterKey = String(body.proposer_id);
        }
        const rawChoice = String(b.choice || "");
        const mappedChoice = aliasMap[rawChoice] || rawChoice;
        normBallots.push({
          voter_key_id: voterKey,
          choice: mappedChoice,
          citations: Array.isArray(b.citations) ? b.citations : [],
          verification_receipt_sha256:
            b.verification_receipt_sha256 || null,
          rationale: String(b.rationale || ""),
          defect_summary: String(b.defect_summary || ""),
        });
        if (voterKey && !(voterKey in calibrations) && "verified_passes" in b) {
          const passes = Number(b.verified_passes || 0);
          const fails = Number(b.verified_failures || 0);
          calibrations[voterKey] = {
            worker_key_id: voterKey,
            verified_correct: passes,
            verified_total: passes + fails,
          };
        }
      }
      const receipt = adjudicateBallots(
        conflictArg,
        normBallots,
        calibrations,
        Boolean(body.human_only)
      );
      const win = receipt.winning_candidate_id;
      return jsonResp({
        ...receipt,
        simulated_status: receipt.status, status: "simulation_result", persisted: false,
        verified: false, reason: "Caller-supplied ballots are a local calculation, not authenticated independent votes",
        winner: win ? revAlias[win] || win : null,
      });
    }

    // Echo at most 200 characters of an unknown path (H-55).
    return problem(404, "not_found", `no route matches ${method} ${path.length > 200 ? `${path.slice(0, 200)}...` : path}. Start at ${publicUrl}/llms.txt`);
  },

  async scheduled(event, env, ctx) {
    const primary = env.COORDINATOR.get(env.COORDINATOR.idFromName("primary"));
    // Lease reaping is infrastructure. Running it on the cron restarts an alarm chain lost
    // during a storage outage and keeps reaper_last_run at most one cron interval old.
    ctx.waitUntil(primary.runReaper());
    if (String(env.FRAGILITY_PLANNER_ENABLED || "false") !== "true") return;
    ctx.waitUntil(primary.planMaintenance());
  },
};


// What a route's body holds. Blob and bucket-object uploads are raw bytes and /work forms are
// form posts; every other body is parsed as JSON whatever its Content-Type says, so its numbers
// are checked whatever it says too (H-07). The 16 MiB cap (the storage limit) belongs to the upload
// routes themselves, not to any path that contains "/blobs" (H-65); Python applies the same caps.
export function bodyRoute(pathname) {
  const path = pathname.replace(/^\/ns\/[^/]+(?=\/)/, "");
  if (path === "/v1/library/proposals" || path === "/a2a") return { kind: "json", limit: LIBRARY_BODY_LIMIT };
  if (path === "/v1/blobs") return { kind: "bytes", limit: 16 * 1024 * 1024 };
  if (/^\/v1\/buckets\/[^/]+\/objects\/./.test(path)) return { kind: "bytes", limit: 16 * 1024 * 1024 };
  if (path === "/work" || path.startsWith("/work/")) return { kind: "form", limit: 1024 * 1024 };
  return { kind: "json", limit: 1024 * 1024 };
}

const onboardingApplication = {
  scheduled: worker.scheduled,
  async fetch(request, env, ctx) {
    const requestId = /^[a-zA-Z0-9._:-]{1,100}$/.test(request.headers.get("x-request-id") || "") ? request.headers.get("x-request-id") : crypto.randomUUID();
    // A client still sending the old namespace header keeps working; only it is told the name.
    const oldNamespaceHeader = /x-tessera-namespace/i.test(request.headers.get("access-control-request-headers") || "") ? ", X-Tessera-Namespace" : "";
    const headers = { "access-control-allow-origin": "*", "access-control-allow-methods": "GET, HEAD, POST, PUT, DELETE, OPTIONS", "access-control-allow-headers": "Authorization, Content-Type, If-Match, X-Request-Id, X-Agent-Label, X-Human-Link-Token, Idempotency-Key, X-Namespace, X-Teleion-Fabric-Namespace, X-Surface-Id, MCP-Protocol-Version, MCP-Session-Id" + oldNamespaceHeader, "access-control-expose-headers": "ETag, Retry-After, X-Request-Id, MCP-Session-Id, Age", "x-request-id": requestId, "cache-control": "no-store" };
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers });
    try {
      // A broken percent escape (H-54) or a control character (H-65) anywhere in the path is
      // refused up front, as Python refuses them.
      if (/[\u0000-\u001f\u007f]/.test(decodeURIComponent(new URL(request.url).pathname))) {
        return problem(400, "invalid_request", "the path has a control character", {}, headers);
      }
      const h = new Headers(request.headers); h.set("x-request-id", requestId);
      if (request.body) {
        const route = bodyRoute(new URL(request.url).pathname);
        const limit = (h.get("authorization") || "").startsWith("Bearer tan_") ? 65536 : route.limit;
        // A declared length over the limit is refused before a byte is read.
        if (h.has("content-length") && !(Number(h.get("content-length")) <= limit)) return problem(413, "payload_too_large", "request exceeds byte limit", {}, headers);
        const reader = request.body.getReader(); const chunks = []; let size = 0;
        while (true) {
          const { done, value } = await reader.read(); if (done) break;
          size += value.byteLength;
          if (size > limit) { await reader.cancel(); return problem(413, "payload_too_large", "request exceeds byte limit", {}, headers); }
          chunks.push(value);
        }
        h.set("content-length", String(size));
        const bytes = new Uint8Array(size); let offset = 0;
        for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
        if (route.kind === "json") validateWireNumbers(new TextDecoder().decode(bytes));
        h.set("content-length", String(size));
        request = new Request(request, { headers: h, body: bytes });
      } else request = new Request(request, { headers: h });
      const response = await worker.fetch(request, env, ctx);
      const outHeaders = new Headers(response.headers);
      // A read that chose to be publicly cacheable (the favicon, the public activity map) keeps its
      // cache-control; every other answer, and every write, is never stored.
      const cacheable = (request.method === "GET" || request.method === "HEAD") && /^public\b/.test(response.headers.get("cache-control") || "");
      for (const [k,v] of Object.entries(headers)) if (!(cacheable && k === "cache-control")) outHeaders.set(k,v);
      return new Response(response.body, { status: response.status, headers: outHeaders });
    } catch (e) {
      // A path segment with a broken percent escape is the client's mistake, not a fault (H-54).
      if (e instanceof URIError) return problem(400, "invalid_request", "the path has a malformed percent escape", {}, headers);
      const quota = storageQuotaFailure(e);
      if (quota) return problem(quota.status, quota.code, quota.detail, quota.extra, { ...headers, "retry-after": String(quota.extra.retry_after) });
      return problem(e.status || 500, e.code || "internal_error", e.detail || "request could not be completed", {}, headers);
    }
  },
};

async function serve(request, env, ctx) {
  const answer = agentStartAsset(request) || withAgentStart(request, await onboardingApplication.fetch(request, env, ctx));
  if (env.PUBLIC_NAMES_FILTER !== "on") return answer;
  // Stored bytes and ids stay exact for everyone. A no-key reader of records, lists and metadata gets the shared word list applied to
  // words only: names stay, or an id it reads back would no longer resolve.
  const requestUrl = new URL(request.url);
  const noKeyContainerRead = /^(?:\/ns\/[^/]+)?\/v1\/(?:buckets|stores)(?:\/|$)/.test(requestUrl.pathname) && ["GET", "HEAD"].includes(request.method) && (!/\/objects\/.+/.test(requestUrl.pathname) || requestUrl.searchParams.has("meta") || requestUrl.searchParams.has("history"));
  if (noKeyContainerRead) return withBannedWordsOnly(request, await answer);
  return withPublicNames(request, await answer, { skip: ["/v1/blobs", "/v1/buckets", "/v1/stores/", "/v1/tasks/", "/ns/"] });
}

export default {
  ...onboardingApplication,
  // HEAD answers wherever GET does, with GET's status and headers and no body (H-53). /mcp keeps
  // its own method rules, since a GET there opens an event stream.
  async fetch(request, env, ctx) {
    if (request.method === "HEAD" && !/^(?:\/ns\/[^/]+)?\/mcp$/.test(new URL(request.url).pathname)) {
      const got = await serve(new Request(request, { method: "GET" }), env, ctx);
      return new Response(null, { status: got.status, statusText: got.statusText, headers: got.headers });
    }
    return serve(request, env, ctx);
  },
};
