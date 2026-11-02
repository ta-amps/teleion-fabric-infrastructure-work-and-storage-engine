// The Worker side of the library door: bodies are measured in bytes before they are buffered, and
// a proposal makes three object calls (attempt and write in the primary object, inspection in the
// separate library-checks object under a wall-clock timeout). Inside the objects the runtime keeps
// the clock still while code runs, so this timeout is the budget that can fire; it fails closed.
import { LIBRARY_LIMITS, withinDepth } from "./library_checks.js";
import { CHECK_TIMEOUT_MS } from "./library_contract.js";

export const LIBRARY_BODY_LIMIT = 600 * 1024;

// Refuses a declared Content-Length over the limit without reading, then reads the stream and stops
// at the limit. Returns {text} or {status, code, detail}; the text must be UTF-8.
export async function readCappedBody(request, limit = LIBRARY_BODY_LIMIT) {
  const declared = request.headers.get("content-length");
  if (declared != null && !(Number(declared) <= limit)) return { status: 413, code: "payload_too_large", detail: `a request holds at most ${limit} bytes` };
  if (!request.body) return { text: "" };
  const reader = request.body.getReader(), chunks = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > limit) {
      await reader.cancel().catch(() => {});
      return { status: 413, code: "payload_too_large", detail: `a request holds at most ${limit} bytes` };
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let at = 0;
  for (const chunk of chunks) { bytes.set(chunk, at); at += chunk.byteLength; }
  try { return { text: new TextDecoder("utf-8", { fatal: true }).decode(bytes) }; }
  catch { return { status: 400, code: "invalid_request", detail: "the body must be UTF-8 text" }; }
}

export function parseJsonBody(text) {
  try {
    const body = JSON.parse(text);
    if (body === null || typeof body !== "object" || Array.isArray(body)) return { status: 400, code: "invalid_request", detail: "the body must be a JSON object" };
    if (!withinDepth(body)) return { status: 400, code: "invalid_request", detail: `JSON nests at most ${LIBRARY_LIMITS.json_depth} levels deep` };
    return { body };
  } catch { return { status: 400, code: "invalid_request", detail: "request body must be valid JSON" }; }
}

function withTimeout(promise, ms) {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("timeout")), ms); })]).finally(() => clearTimeout(timer));
}

const tooComplex = () => ({ ok: false, status: 422, code: "proposal_refused", detail: "the proposal could not be checked within the door's time budget; nothing was stored",
  extra: { findings: [{ rule: "complexity", where: "body", detail: "too large or too complex to check in time; split it or send less" }], refusal_sha256: null } });

// One proposal through any door. The body was parsed by the door; nesting is checked again here
// because an object call clones it, and cloning a very deep value would throw.
export async function proposeThroughDoors({ primary, checker, secret, body, meta, timeoutMs = CHECK_TIMEOUT_MS }) {
  if (!secret) return { ok: false, status: 401, code: "unauthorized", detail: "send Authorization: Bearer <contributor key or tracked session token>; POST /v1/session/anonymous opens a tracked session without secrets", extra: {} };
  if (body === null || typeof body !== "object" || Array.isArray(body) || !withinDepth(body))
    return { ok: false, status: 400, code: "invalid_request", detail: `a proposal is a JSON object nesting at most ${LIBRARY_LIMITS.json_depth} levels deep`, extra: {} };
  const attempt = await primary.libraryAttempt(secret, meta);
  if (!attempt.ok) return attempt;
  let inspected;
  try { inspected = await withTimeout(checker.libraryInspect(body, { anonymous: attempt.data.anonymous }), timeoutMs); }
  catch { inspected = tooComplex(); }
  if (!inspected.ok) {
    await primary.libraryRefusal(secret, inspected, meta);
    return inspected;
  }
  return primary.libraryPropose(secret, inspected.data, meta);
}
