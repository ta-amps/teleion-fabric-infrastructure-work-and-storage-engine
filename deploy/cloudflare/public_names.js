// Public output says Teleion Fabric and the shim's public address. This is the last step before a response leaves,
// only for callers with no key: our own workers keep reading the stored values and names. It changes names, never
// ids: lowercase ids an agent sends back (a Task id, an agent name, a kit file) are left alone, or a later call would
// not find them. Bodies people verify by digest are not passed through it.
const KEPT = /https?:\/\/github\.com\/ta-amps\/[\w.-]+(?:\/[^\s"'<>)]*)?|\/assets\/herma-[\w.?=-]+|\/kits\/herma[\w.-]*|data-herma-[\w-]+|herma-(?:pages|agent-kit)[\w.-]*|\bherma_shim\.py\b|\b(?:noun|verb|detail|n|v):(?:tessera|herma)\.[\w.]+/g;
const EXACT_VALUES = new Map([
  ["tessera_durable_object_cas", "fabric_durable_object_cas"],
  ["d1_authoritative_with_tessera_summary_mirror", "d1_authoritative_with_fabric_summary_mirror"],
  ["tessera-cloud", "fabric-cloud"],
  ["public_tessera_synced_rows", "public_fabric_synced_rows"],
]);
const RULES = [
  [/https?:\/\/tessera\.(?:ta-amps\.workers\.dev|teleionlabs\.com)/gi, "https://fabric.teleionlabs.com"],
  [/https?:\/\/herma\.(?:ta-amps\.workers\.dev|teleionlabs\.com)/gi, "https://shim.teleionlabs.com"],
  [/\btessera\.(?:ta-amps\.workers\.dev|teleionlabs\.com)/gi, "fabric.teleionlabs.com"],
  [/\bherma\.(?:ta-amps\.workers\.dev|teleionlabs\.com)/gi, "shim.teleionlabs.com"],
  [/X-Tessera-Namespace/g, "X-Teleion-Fabric-Namespace"],
  [/x-tessera-namespace/g, "x-teleion-fabric-namespace"],
  [/X-Herma-/g, "X-Shim-"],
  [/x-herma-/g, "x-shim-"],
  [/tessera:\/\//g, "fabric://"],
  [/\bsurface:tessera\b/g, "surface:fabric"],
  [/\bsurface:herma\b/g, "surface:shim"],
  [/"(?:tessera)_([a-z0-9_]+)"(\s*:)/g, '"fabric_$1"$2'],
  [/"(?:herma)_([a-z0-9_]+)"(\s*:)/g, '"shim_$1"$2'],
  [/(:\s*)"tessera"/g, '$1"fabric"'],
  [/(:\s*)"herma"/g, '$1"shim"'],
  [/\bherma\.(?=[a-z_.0-9]*\bv\d)/g, "shim."],
  [/\btessera\.(?=[a-z_.0-9]*\bv\d)/g, "fabric."],
  [/\bHERMA\b/g, "MODEL SHIM"],
  [/\bTESSERA\b/g, "TELEION FABRIC"],
  [/\bHerma's\b/g, "the model shim's"],
  [/\bHerma\b/g, "Teleion Fabric's model shim"],
  [/\bTessera's\b/g, "Teleion Fabric's"],
  [/\bTessera\b/g, "Teleion Fabric"],
  [/(?<![\w-])herma(?![\w-])/g, "the model shim"],
  [/(?<![\w-])tessera(?![\w-])/g, "Teleion Fabric"],
];

export function publicNames(text) {
  const kept = [];
  let out = String(text ?? "").replace(KEPT, (m) => {
    kept.push(m);
    return `\u0000${kept.length - 1}\u0000`;
  });
  for (const [from, to] of EXACT_VALUES) out = out.split(from).join(to);
  for (const [re, to] of RULES) out = out.replace(re, to);
  return out.replace(/\u0000(\d+)\u0000/g, (_, i) => kept[Number(i)]);
}

const TEXT_TYPE = /^(?:text\/(?!event-stream)|application\/(?:json|problem\+json|x-ndjson|javascript))/i;
const MAX_BYTES = 8 * 1024 * 1024;

/** The response a caller with no key receives. `skip` lists path prefixes whose bodies are read by digest. */
export async function withPublicNames(request, response, { skip = [], keyHeaders = ["authorization"] } = {}) {
  if (request.method === "OPTIONS" || response.status === 204 || response.status === 304 || !response.body) return renamedHeaders(response);
  if (keyHeaders.some((h) => request.headers.has(h))) return response;
  const path = new URL(request.url).pathname;
  if (skip.some((p) => path.startsWith(p))) return renamedHeaders(response);
  const type = response.headers.get("content-type") || "";
  if (!TEXT_TYPE.test(type) || Number(response.headers.get("content-length") || 0) > MAX_BYTES) return renamedHeaders(response);
  const text = await response.text();
  const renamed = publicNames(text);
  const headers = new Headers(response.headers);
  headers.delete("content-length");
  headers.delete("etag");
  return renamedHeaders(new Response(renamed, { status: response.status, statusText: response.statusText, headers }));
}

export function renamedHeaders(response) {
  const headers = new Headers(response.headers);
  let changed = false;
  for (const [name, value] of response.headers) {
    if (/^x-herma-/i.test(name)) {
      headers.delete(name);
      headers.set(name.replace(/^x-herma-/i, "x-shim-"), publicNames(value));
      changed = true;
    } else if (/tessera|herma/i.test(value) && !/^(?:set-cookie|access-control-)/i.test(name)) {
      const next = publicNames(value);
      if (next !== value) {
        headers.set(name, next);
        changed = true;
      }
    }
  }
  return changed ? new Response(response.body, { status: response.status, statusText: response.statusText, headers }) : response;
}
