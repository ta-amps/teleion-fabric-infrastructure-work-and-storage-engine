// Cloudflare edge for Tessera: a reverse proxy in front of the Tessera service on Fly.io.
// Every path goes through (/v1/*, /mcp, /board, /work, /connect, /llms.txt, /.well-known/*, /healthz).
// Authorization passes through untouched; EDGE_SECRET (set with `wrangler secret put EDGE_SECRET`)
// tells the origin a request came through this edge, and the service refuses anything else once
// TESSERA_EDGE_SECRET is set on Fly. Responses to writes are never cached.

const DEFAULT_ORIGIN = "https://tessera-fabric.fly.dev";

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

// Used only when the service sends no policy of its own. Data responses can't load anything; pages
// may use files from this origin and inline styles, but never inline scripts.
const DEFAULT_CSP_DATA = "default-src 'none'; frame-ancestors 'none'";
const DEFAULT_CSP_PAGE = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

const SECURITY_HEADERS = {
  "strict-transport-security": "max-age=31536000; includeSubDomains",
  "x-content-type-options": "nosniff",
  "referrer-policy": "strict-origin-when-cross-origin",
  "permissions-policy": "camera=(), microphone=(), geolocation=()",
  "x-frame-options": "DENY",
};

function stripHopByHop(headers) {
  const listed = (headers.get("connection") || "")
    .split(",")
    .map((name) => name.trim().toLowerCase())
    .filter(Boolean);
  for (const name of [...HOP_BY_HOP, ...listed]) headers.delete(name);
}

function problem(status, title, detail) {
  return new Response(JSON.stringify({ type: "about:blank", title, status, detail }), {
    status,
    headers: { "content-type": "application/problem+json" },
  });
}

export default {
  async fetch(request, env) {
    const incoming = new URL(request.url);
    const target = new URL(incoming.pathname + incoming.search, env.ORIGIN || DEFAULT_ORIGIN);
    const headers = new Headers(request.headers);
    stripHopByHop(headers);
    headers.delete("cookie"); // the service authenticates with bearer keys, never cookies
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
      return problem(502, "Bad Gateway", "The Tessera service did not answer.");
    }

    // Streams pass straight through (MCP over Streamable HTTP uses text/event-stream).
    const response = new Response(upstream.body, upstream);
    stripHopByHop(response.headers);
    for (const [name, value] of Object.entries(SECURITY_HEADERS)) response.headers.set(name, value);
    if (!response.headers.has("content-security-policy")) {
      const isPage = (response.headers.get("content-type") || "").includes("text/html");
      response.headers.set("content-security-policy", isPage ? DEFAULT_CSP_PAGE : DEFAULT_CSP_DATA);
    }
    if (!isRead || !response.headers.has("cache-control")) {
      response.headers.set("cache-control", "no-store"); // queue state is live; writes never cache
    }
    return response;
  },
};
