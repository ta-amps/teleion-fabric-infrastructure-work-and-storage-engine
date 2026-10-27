// Tessera Cloud Worker: 24/7 native cloud execution on Cloudflare Durable Object SQLite + D1
// (`STORAGE_MODE = "cloud"`, the default) with optional Fly.io origin reverse-proxy fallback
// (`STORAGE_MODE = "origin"`). Serves all Tessera Spec v0.2 HTTP routes (`/v1/*`), named cloud
// file buckets (`/v1/buckets/*`), Streamable HTTP MCP (`/mcp`), and the agent door (`/connect`,
// `/llms.txt`, `/.well-known/agent-card.json`, `/robots.txt`, `/board`, `/work`).

import { loadCloudBlob, mirrorBucketObjectToD1, storeCloudBlob } from "./cloud_buckets.js";
import { sliceBytesResponse } from "./cloud_intelligence.js";
import { handleMcpRequest } from "./cloud_mcp.js";
import {
  ROBOTS_TXT,
  renderAgentCard,
  renderBoardHtml,
  renderConnectHtml,
  renderLlmsTxt,
  renderWorkClaimForm,
  renderWorkLeaseForm,
  renderWorkSubmitted,
} from "./cloud_pages.js";
import { TesseraCoordinator } from "./cloud_store.js";

export { TesseraCoordinator };

const DEFAULT_ORIGIN = "https://tessera-fabric.fly.dev";
const DEFAULT_PUBLIC_URL = "https://tessera.ta-amps.workers.dev";
const DEFAULT_REPO_URL = "https://github.com/ta-amps/tessera";
const NAMESPACE_RE = /^[a-z0-9][a-z0-9_-]{0,47}$/;
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
  416: "Range Not Satisfiable",
  429: "Too Many Requests",
  500: "Internal Server Error",
  502: "Bad Gateway",
  503: "Service Unavailable",
};

const POST_ONLY_EXACT = new Set([
  "/v1/queues/define",
  "/v1/tasks",
  "/v1/tasks/claim",
  "/v1/tasks/heartbeat",
  "/v1/tasks/submit",
  "/v1/tasks/split",
  "/v1/tasks/park",
  "/v1/tasks/resume",
  "/v1/tasks/cancel",
  "/v1/admin/reap",
  "/v1/stores/batch",
  "/v1/blobs",
  "/v1/buckets/define",
  "/v1/handshake",
  "/work/claim",
  "/work/submit",
  "/work/heartbeat",
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
    if (out.code === "rate_limited" && out.extra?.retry_after != null) {
      hdrs["retry-after"] = String(Math.max(1, Math.ceil(Number(out.extra.retry_after))));
    }
    if (out.code === "version_conflict") {
      extra.current_version = out.extra?.current_version ?? null;
    }
    return problem(out.status || 400, out.code || "invalid_request", out.detail || "error", extra, hdrs);
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
    return problem(502, "unavailable", "The Tessera origin service did not answer.");
  }
  return withSecurity(upstream, { isWrite: !isRead, noStore: !isRead });
}

function resolveNamespace(url, request) {
  let namespace = "primary";
  let path = url.pathname;
  const nsMatch = /^\/ns\/([a-z0-9][a-z0-9_-]{0,47})(\/.*)?$/.exec(path);
  if (nsMatch) {
    namespace = nsMatch[1];
    path = nsMatch[2] || "/";
  } else {
    const hdrNs = (request.headers.get("x-tessera-namespace") || "").trim();
    if (hdrNs && NAMESPACE_RE.test(hdrNs)) namespace = hdrNs;
  }
  return { namespace, path, isPrimary: namespace === "primary" };
}

export default {
  async fetch(request, env, ctx) {
    if ((env.STORAGE_MODE || "cloud") === "origin") {
      return proxyToOrigin(request, env);
    }

    const url = new URL(request.url);
    const { namespace, path, isPrimary } = resolveNamespace(url, request);
    const doName = isPrimary ? "primary" : `ns:${namespace}`;
    const stub = env.COORDINATOR.get(env.COORDINATOR.idFromName(doName));
    const method = request.method.toUpperCase();
    const publicUrl = (env.PUBLIC_URL || `${url.protocol}//${url.host}`).replace(/\/+$/, "") || DEFAULT_PUBLIC_URL;
    const repoUrl = env.REPO_URL || DEFAULT_REPO_URL;
    const bearer = extractBearer(request);

    // 1. Streamable HTTP MCP Door (`/mcp`)
    if (path === "/mcp") {
      const mcpResp = await handleMcpRequest(request, env, ctx, stub, bearer, isPrimary);
      return withSecurity(mcpResp, { isWrite: true });
    }

    // 2. Enforce POST-only write routes before parameterized GET routes
    if (isPostOnlyPath(path) && method !== "POST") {
      return problem(405, "method_not_allowed", "writes are POST only; this path changes nothing on GET");
    }

    // 3. Public health & agent door pages
    if ((path === "/v1/health" || path === "/healthz") && (method === "GET" || method === "HEAD")) {
      const health = await stub.publicHealthAndBoard(false);
      return jsonResp(health);
    }
    if ((path === "/" || path === "/connect") && (method === "GET" || method === "HEAD")) {
      return htmlResp(renderConnectHtml(publicUrl, repoUrl));
    }
    if (path === "/llms.txt" && (method === "GET" || method === "HEAD")) {
      return withSecurity(
        new Response(renderLlmsTxt(publicUrl, repoUrl), {
          status: 200,
          headers: { "content-type": "text/plain; charset=utf-8" },
        }),
        { noStore: false }
      );
    }
    if (path === "/.well-known/agent-card.json" && (method === "GET" || method === "HEAD")) {
      return jsonResp(renderAgentCard(publicUrl, repoUrl));
    }
    if (path === "/robots.txt" && (method === "GET" || method === "HEAD")) {
      return withSecurity(
        new Response(ROBOTS_TXT, {
          status: 200,
          headers: { "content-type": "text/plain; charset=utf-8" },
        }),
        { noStore: false }
      );
    }
    if (path === "/board" && (method === "GET" || method === "HEAD")) {
      const info = await stub.publicHealthAndBoard(true);
      return htmlResp(renderBoardHtml(info.health, info.buckets, info.stores));
    }

    // 4. Keyed browser work forms (`/work`, `/work/claim`, `/work/heartbeat`, `/work/submit`)
    if (path === "/work" && method === "GET") {
      return htmlResp(renderWorkClaimForm(), 200, { "x-robots-tag": "noindex" });
    }
    if (path === "/work/claim" && method === "POST") {
      const form = await request.formData();
      const formKey = String(form.get("key") || "");
      const queue = String(form.get("queue") || "");
      const worker = String(form.get("worker") || "");
      const out = await stub.rpcCall(formKey, "claim", { queue, worker, isPrimary });
      if (!out.ok) {
        return htmlResp(
          renderWorkClaimForm(`${out.code}: ${out.detail}`),
          out.status || 400,
          { "x-robots-tag": "noindex" }
        );
      }
      if (!out.data.lease) {
        return htmlResp(
          renderWorkClaimForm("Nothing is ready in that queue yet."),
          200,
          { "x-robots-tag": "noindex" }
        );
      }
      return htmlResp(renderWorkLeaseForm(out.data.lease), 200, { "x-robots-tag": "noindex" });
    }
    if (path === "/work/heartbeat" && method === "POST") {
      const form = await request.formData();
      const formKey = String(form.get("key") || "");
      const taskId = String(form.get("task_id") || "");
      const token = Number(form.get("fencing_token") || "0");
      const out = await stub.rpcCall(formKey, "heartbeat", {
        task_id: taskId,
        fencing_token: token,
        isPrimary,
      });
      if (!out.ok) {
        return htmlResp(
          renderWorkClaimForm(`${out.code}: ${out.detail}`),
          out.status || 400,
          { "x-robots-tag": "noindex" }
        );
      }
      return htmlResp(renderWorkLeaseForm(out.data), 200, { "x-robots-tag": "noindex" });
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
        return htmlResp(renderWorkClaimForm("invalid_request: output is not valid JSON"), 400, {
          "x-robots-tag": "noindex",
        });
      }
      if (typeof output !== "object" || output === null || Array.isArray(output)) {
        return htmlResp(renderWorkClaimForm("invalid_request: output must be a JSON object"), 400, {
          "x-robots-tag": "noindex",
        });
      }
      const ok = !form.get("failed");
      const errText = !ok ? String(form.get("error") || "") || null : null;
      const out = await stub.rpcCall(formKey, "submit", {
        task_id: taskId,
        fencing_token: token,
        output,
        idempotency_key: idem,
        ok,
        error: errText,
        isPrimary,
      });
      if (!out.ok) {
        return htmlResp(
          renderWorkClaimForm(`${out.code}: ${out.detail}`),
          out.status || 400,
          { "x-robots-tag": "noindex" }
        );
      }
      return htmlResp(renderWorkSubmitted(out.data), 200, { "x-robots-tag": "noindex" });
    }

    // 5. Observation & admin key routes
    if (path === "/v1/counts" && method === "GET") {
      const recount = ["1", "true", "yes"].includes((url.searchParams.get("recount") || "").toLowerCase());
      return rpcResponse(await stub.rpcCall(bearer, "counts", { recount, isPrimary }));
    }
    if (path === "/v1/events" && method === "GET") {
      const after = Number(url.searchParams.get("after") || "0");
      const limit = Number(url.searchParams.get("limit") || "1000");
      return rpcResponse(await stub.rpcCall(bearer, "events", { after, limit, isPrimary }));
    }
    if (path === "/v1/admin/reap" && method === "POST") {
      return rpcResponse(await stub.rpcCall(bearer, "reap", { isPrimary }));
    }
    if (path === "/v1/admin/keys") {
      if (method === "GET") {
        return rpcResponse(await stub.rpcCall(bearer, "listKeys", { isPrimary }));
      }
      if (method === "POST") {
        const { body, error } = await readJsonBody(request);
        if (error) return error;
        const bootHdr = request.headers.get("x-bootstrap-secret") || "";
        const bootstrapAllowed =
          (env.BOOTSTRAP_SECRET && constantTimeEqual(bootHdr, env.BOOTSTRAP_SECRET)) ||
          (env.EDGE_SECRET && constantTimeEqual(bootHdr, env.EDGE_SECRET));
        return rpcResponse(await stub.bootstrapOrIssueKey(bearer, Boolean(bootstrapAllowed), body));
      }
      return problem(405, "method_not_allowed", "method not allowed");
    }
    const revokeMatch = /^\/v1\/admin\/keys\/([^/]+)\/revoke$/.exec(path);
    if (revokeMatch && method === "POST") {
      return rpcResponse(
        await stub.rpcCall(bearer, "revokeKey", { key_id: decodeURIComponent(revokeMatch[1]), isPrimary })
      );
    }

    // 6. Queues & tasks (atomic verbs)
    if (path === "/v1/queues/define" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      return rpcResponse(await stub.rpcCall(bearer, "defineQueue", { ...body, isPrimary }));
    }
    if (path === "/v1/queues" && method === "GET") {
      return rpcResponse(await stub.rpcCall(bearer, "queues", { isPrimary }));
    }
    if (path === "/v1/tasks" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      return rpcResponse(await stub.rpcCall(bearer, "enqueue", { ...body, isPrimary }));
    }
    if (path === "/v1/tasks/claim" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      const out = await stub.rpcCall(bearer, "claim", { ...body, isPrimary });
      if (!out.ok) return rpcResponse(out);
      if (!out.data.lease) {
        return withSecurity(
          new Response(null, {
            status: 204,
            headers: {
              "retry-after": "5",
              "x-heartbeat-seconds": String(out.data.beat || "60"),
            },
          }),
          { isWrite: true }
        );
      }
      return jsonResp(out.data.lease, 200, {
        "x-heartbeat-seconds": String(out.data.beat || "60"),
      });
    }
    if (path === "/v1/tasks/heartbeat" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      return rpcResponse(await stub.rpcCall(bearer, "heartbeat", { ...body, isPrimary }));
    }
    if (path === "/v1/tasks/submit" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      const idem = request.headers.get("idempotency-key") || "";
      if (!idem) return problem(400, "invalid_request", "the Idempotency-Key header is required");
      return rpcResponse(
        await stub.rpcCall(bearer, "submit", { ...body, idempotency_key: idem, isPrimary })
      );
    }
    if (path === "/v1/tasks/split" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      return rpcResponse(await stub.rpcCall(bearer, "split", { ...body, isPrimary }));
    }
    if (path === "/v1/tasks/park" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      return rpcResponse(await stub.rpcCall(bearer, "park", { ...body, isPrimary }));
    }
    if (path === "/v1/tasks/resume" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      return rpcResponse(await stub.rpcCall(bearer, "resume", { ...body, isPrimary }));
    }
    if (path === "/v1/tasks/cancel" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      return rpcResponse(await stub.rpcCall(bearer, "cancel", { ...body, isPrimary }));
    }
    const taskResultsMatch = /^\/v1\/tasks\/([^/]+)\/results$/.exec(path);
    if (taskResultsMatch && method === "GET") {
      return rpcResponse(
        await stub.rpcCall(bearer, "results", {
          task_id: decodeURIComponent(taskResultsMatch[1]),
          isPrimary,
        })
      );
    }
    const taskMatch = /^\/v1\/tasks\/([^/]+)$/.exec(path);
    if (taskMatch && method === "GET") {
      return rpcResponse(
        await stub.rpcCall(bearer, "task", { task_id: decodeURIComponent(taskMatch[1]), isPrimary })
      );
    }

    // 7. Model jobs
    if (path === "/v1/jobs") {
      if (method === "GET") {
        const taskId = url.searchParams.get("task_id");
        return rpcResponse(
          await stub.rpcCall(bearer, "jobs", { task_id: taskId || null, isPrimary })
        );
      }
      if (method === "POST") {
        const { body, error } = await readJsonBody(request);
        if (error) return error;
        return rpcResponse(await stub.rpcCall(bearer, "registerJob", { ...body, isPrimary }));
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
          job_id: decodeURIComponent(jobUpdateMatch[1]),
          isPrimary,
        })
      );
    }

    // 8. Queryable CAS Noun Stores (`/v1/stores/*`)
    if (path === "/v1/stores" && method === "GET") {
      return rpcResponse(await stub.rpcCall(bearer, "stores", { isPrimary }));
    }
    if (path === "/v1/stores/batch" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      return rpcResponse(await stub.rpcCall(bearer, "putRecords", { writes: body.writes, isPrimary }));
    }
    const storeListMatch = /^\/v1\/stores\/([^/]+)\/records$/.exec(path);
    if (storeListMatch && method === "GET") {
      const prefix = url.searchParams.get("prefix") || "";
      const limit = Number(url.searchParams.get("limit") || "100");
      return rpcResponse(
        await stub.rpcCall(bearer, "listRecords", {
          store: decodeURIComponent(storeListMatch[1]),
          prefix,
          limit,
          isPrimary,
        })
      );
    }
    const recHistoryMatch = /^\/v1\/stores\/([^/]+)\/records\/([^/]+)\/history$/.exec(path);
    if (recHistoryMatch && method === "GET") {
      return rpcResponse(
        await stub.rpcCall(bearer, "recordHistory", {
          store: decodeURIComponent(recHistoryMatch[1]),
          key: decodeURIComponent(recHistoryMatch[2]),
          isPrimary,
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
          await stub.rpcCall(bearer, "getRecord", { store: storeName, key: recKey, version, isPrimary })
        );
      }
      if (method === "POST") {
        const { body, error } = await readJsonBody(request);
        if (error) return error;
        const out = await stub.rpcCall(bearer, "putRecords", {
          writes: [
            {
              store: storeName,
              key: recKey,
              value: body.value,
              expected_version: body.expected_version ?? null,
            },
          ],
          isPrimary,
        });
        if (!out.ok) return rpcResponse(out);
        return jsonResp(out.data.records[0], 200);
      }
      return problem(405, "method_not_allowed", "method not allowed");
    }

    // 9. Content-addressed Blobs (`/v1/blobs/*`)
    if (path === "/v1/blobs" && method === "POST") {
      const authCheck = await stub.rpcCall(bearer, "verifyStoreScope", { isPrimary });
      if (!authCheck.ok) return rpcResponse(authCheck);
      const raw = new Uint8Array(await request.arrayBuffer());
      const stored = await storeCloudBlob(env, raw, Date.now() / 1000);
      if (stored.error) return problem(stored.status || 400, stored.error, stored.detail);
      return rpcResponse(
        await stub.rpcCall(bearer, "recordBlobStored", {
          sha256: stored.sha256,
          size: stored.size,
          isPrimary,
        })
      );
    }
    const blobGetMatch = /^\/v1\/blobs\/([^/]+)$/.exec(path);
    if (blobGetMatch && method === "GET") {
      const authCheck = await stub.rpcCall(bearer, "verifyStoreScope", { isPrimary });
      if (!authCheck.ok) return rpcResponse(authCheck);
      const sha256 = decodeURIComponent(blobGetMatch[1]).toLowerCase();
      if (!SHA256_RE.test(sha256)) {
        return problem(404, "not_found", `blob ${sha256} does not exist`);
      }
      const loaded = await loadCloudBlob(env, sha256);
      if (loaded.error) return problem(loaded.status || 404, loaded.error, loaded.detail);
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
      return rpcResponse(await stub.rpcCall(bearer, "defineBucket", { ...body, isPrimary }));
    }
    if (path === "/v1/buckets" && method === "GET") {
      return rpcResponse(await stub.rpcCall(bearer, "buckets", { isPrimary }));
    }
    const bucketListMatch = /^\/v1\/buckets\/([^/]+)\/objects$/.exec(path);
    if (bucketListMatch && method === "GET") {
      const prefix = url.searchParams.get("prefix") || "";
      const limit = Number(url.searchParams.get("limit") || "100");
      return rpcResponse(
        await stub.rpcCall(bearer, "listObjects", {
          bucket: decodeURIComponent(bucketListMatch[1]),
          prefix,
          limit,
          isPrimary,
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
        const authCheck = await stub.rpcCall(bearer, "verifyStoreScope", { isPrimary });
        if (!authCheck.ok) return rpcResponse(authCheck);
        let expectedVersion = null;
        const evParam = url.searchParams.get("expected_version") ?? request.headers.get("x-expected-version");
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
        const contentType = request.headers.get("content-type") || "application/octet-stream";
        const rawBytes = new Uint8Array(await request.arrayBuffer());
        const stored = await storeCloudBlob(env, rawBytes, Date.now() / 1000);
        if (stored.error) return problem(stored.status || 400, stored.error, stored.detail);
        const out = await stub.rpcCall(bearer, "putObjectMeta", {
          bucket: bucketName,
          key: objKey,
          sha256: stored.sha256,
          size: stored.size,
          content_type: contentType,
          metadata,
          expected_version: expectedVersion,
          isPrimary,
        });
        if (!out.ok) return rpcResponse(out);
        if (isPrimary) ctx.waitUntil(mirrorBucketObjectToD1(env, out.data));
        return jsonResp(out.data, 201);
      }
      if (method === "GET") {
        const verParam = url.searchParams.get("version");
        const version = verParam == null ? null : Number(verParam);
        const wantHistory = ["1", "true", "yes"].includes(
          (url.searchParams.get("history") || "").toLowerCase()
        );
        const wantMeta = ["1", "true", "yes"].includes(
          (url.searchParams.get("meta") || "").toLowerCase()
        );
        if (wantHistory) {
          return rpcResponse(
            await stub.rpcCall(bearer, "objectHistory", {
              bucket: bucketName,
              key: objKey,
              isPrimary,
            })
          );
        }
        const metaOut = await stub.rpcCall(bearer, "getObjectMeta", {
          bucket: bucketName,
          key: objKey,
          version,
          isPrimary,
        });
        if (!metaOut.ok || wantMeta) return rpcResponse(metaOut);
        const loaded = await loadCloudBlob(env, metaOut.data.sha256);
        if (loaded.error) return problem(loaded.status || 404, loaded.error, loaded.detail);
        return withSecurity(
          sliceBytesResponse(
            loaded.bytes,
            request.headers.get("range"),
            metaOut.data.content_type || "application/octet-stream",
            {
              etag: `"${metaOut.data.sha256}"`,
              "x-tessera-version": String(metaOut.data.version),
              "x-tessera-sha256": metaOut.data.sha256,
              "x-tessera-object-meta": JSON.stringify(metaOut.data),
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
      return rpcResponse(await stub.rpcCall(bearer, "searchNouns", { query: q, store, bucket, limit, isPrimary }));
    }
    if (path === "/v1/feeds" && method === "GET") {
      return rpcResponse(await stub.rpcCall(bearer, "feeds", { isPrimary }));
    }
    const feedMatch = /^\/v1\/feeds\/([^/]+)$/.exec(path);
    if (feedMatch) {
      const feedName = decodeURIComponent(feedMatch[1]);
      if (method === "GET") {
        const after = Number(url.searchParams.get("after") || "0");
        const limit = Number(url.searchParams.get("limit") || "100");
        return rpcResponse(
          await stub.rpcCall(bearer, "pollFeed", { feed: feedName, after, limit, isPrimary })
        );
      }
      if (method === "POST") {
        const { body, error } = await readJsonBody(request);
        if (error) return error;
        return rpcResponse(
          await stub.rpcCall(bearer, "publishFeed", {
            feed: feedName,
            payload: body.payload,
            kind: body.kind ?? "delta",
            summary: body.summary ?? "",
            isPrimary,
          })
        );
      }
      return problem(405, "method_not_allowed", "method not allowed");
    }
    if (path === "/v1/reflexes/match" && method === "GET") {
      const q = url.searchParams.get("q") || "";
      const limit = Number(url.searchParams.get("limit") || "5");
      return rpcResponse(await stub.rpcCall(bearer, "matchReflex", { query: q, limit, isPrimary }));
    }
    if (path === "/v1/handshake" && method === "POST") {
      const { body, error } = await readJsonBody(request);
      if (error) return error;
      const hdrSurface = (request.headers.get("x-surface-id") || "").trim();
      const out = await stub.rpcCall(bearer, "handshake", {
        ...body,
        header_surface_id: hdrSurface,
        isPrimary,
      });
      if (!out.ok) return rpcResponse(out);
      return jsonResp(out.data, 200, {
        "x-surface-id": out.data.surface_id,
        "x-handshake-id": out.data.handshake_id,
      });
    }

    return problem(404, "not_found", `no route matches ${method} ${path}`);
  },
};
