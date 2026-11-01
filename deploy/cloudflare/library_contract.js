// The library door's public contract: store and queue names, rates, the MCP tools, the connect.json
// section and the A2A agent card. Storage-free, so discovery stays up when storage is exhausted.
// Every door (HTTP, MCP, A2A) maps to the same propose verb; none of them is a queue or an agent.
import { TESSERA_VERSION } from "./cloud_pages.js";
import { BUNDLE_FORMAT, LIBRARY_LICENSES, LIBRARY_LIMITS } from "./library_checks.js";

export const LIBRARY = Object.freeze({
  proposals: "library.proposals",
  catalog: "library.catalog",
  instructions: "library.instructions",
  propose_queue: "library.propose",
  review_queue: "library.review",
});
export const LIBRARY_RATES = Object.freeze({ anonymous_per_session_hour: 3, anonymous_per_ip_hour: 6, anonymous_per_hour: 60, keyed_per_key_hour: 30 });
export const REVIEW_SCHEMA = "ta-amps.library.review.v1";
export const libraryEnabled = env => String(env?.LIBRARY_PROPOSALS_ENABLED || "false") === "true";

const COMMON = {
  license: { type: "string", enum: [...LIBRARY_LICENSES] },
  summary: { type: "string", maxLength: 2000, description: "Who the item helps and how." },
  provenance: { type: "string", maxLength: 2000, description: "Where the content came from." },
  pack: { type: "string", maxLength: 64, description: "Pack id the item belongs to (default community)." },
  version: { type: "string", maxLength: 14, description: "Version such as 0.1.0 (default 0.1.0)." },
};
const NOTHING_RUNS = "Files a propose verb with a SHA-256 receipt and a library.review verb that a different reviewer claims. Runs, activates and admits nothing.";
export const LIBRARY_TOOLS = Object.freeze([
  { name: "propose_task_object", description: `Propose one task object (noun) for the seed-pack library: {slug, category, title, value}. ${NOTHING_RUNS}`,
    inputSchema: { type: "object", required: ["noun", "license", "summary"], properties: { noun: { type: "object", description: "slug, category (a registered noun category), title, value, optional role" }, ...COMMON }, additionalProperties: false } },
  { name: "propose_task", description: `Propose one task template (verb) for the seed-pack library: {slug, kind, title, steps, acceptance}. ${NOTHING_RUNS}`,
    inputSchema: { type: "object", required: ["verb", "license", "summary"], properties: { verb: { type: "object", description: "slug, kind (a registered verb category), title, steps, acceptance, optional summary and outcome" }, ...COMMON }, additionalProperties: false } },
  { name: "submit_pack", description: `Submit a whole seed pack as one bundle (${BUNDLE_FORMAT}: license, source, files). Code is stored as text and never run. ${NOTHING_RUNS}`,
    inputSchema: { type: "object", required: ["bundle"], properties: { bundle: { type: "object", description: "format, license, source (the pack's source.json), files [{path, content}] with README.md" }, summary: COMMON.summary, provenance: COMMON.provenance }, additionalProperties: false } },
  { name: "proposal_status", description: "Read a library proposal's status, receipts and review decision. Public; no key needed.",
    inputSchema: { type: "object", required: ["proposal_id"], properties: { proposal_id: { type: "string", maxLength: 40 } }, additionalProperties: false } },
]);
export const LIBRARY_TOOL_NAMES = new Set(LIBRARY_TOOLS.map(tool => tool.name));

// The proposal body a tool call stands for; the same body HTTP and A2A send.
export function libraryToolBody(name, args = {}) {
  const pick = keys => Object.fromEntries(keys.filter(k => args[k] !== undefined).map(k => [k, args[k]]));
  if (name === "propose_task_object") return { type: "noun", ...pick(["noun", "license", "summary", "provenance", "pack", "version"]) };
  if (name === "propose_task") return { type: "verb", ...pick(["verb", "license", "summary", "provenance", "pack", "version"]) };
  return { type: "pack", ...pick(["bundle", "summary", "provenance"]) };
}

export function libraryDescription(url, enabled) {
  return {
    enabled,
    stores: { proposals: LIBRARY.proposals, catalog: LIBRARY.catalog, instructions: LIBRARY.instructions },
    verbs: { propose: LIBRARY.propose_queue, review: LIBRARY.review_queue },
    endpoints: {
      propose: { method: "POST", url: `${url}/v1/library/proposals` },
      overview: `${url}/v1/library`, proposals: `${url}/v1/library/proposals`, proposal: `${url}/v1/library/proposals/{proposal_id}`,
      catalog: `${url}/v1/library/catalog`, catalog_item: `${url}/v1/library/catalog/{key}`, instructions: `${url}/v1/library/instructions`,
      a2a: { method: "POST", url: `${url}/a2a`, agent_card: `${url}/.well-known/agent.json` },
    },
    mcp_tools: LIBRARY_TOOLS.map(tool => tool.name),
    types: { noun: "one task object", verb: "one task template", pack: `a whole seed pack bundle (${BUNDLE_FORMAT})` },
    who_may_propose: "a contributor key whose queues include library.propose, or a tracked anonymous session from POST /v1/session/anonymous",
    who_may_review: "a different account-tracked key whose queues include library.review; never the proposer's key or account, an anonymous session or a requester key",
    limits: { ...LIBRARY_LIMITS, rates: LIBRARY_RATES },
    licenses: [...LIBRARY_LICENSES],
    checks: ["schema", "size", "license", "file_type", "hidden_characters", "secrets", "personal_data"],
    review_output: { schema: REVIEW_SCHEMA, fields: ["proposal_id", "item_sha256", "reviewer_key_id", "approved", "reasons", "checks"] },
    guarantees: [
      "Proposing runs nothing, activates nothing and admits nothing.",
      "Submitted code is stored as text and never executed by Tessera.",
      "Only an approving library.review verb completed by a different principal admits items to library.catalog.",
      "A rejected proposal keeps its propose and review receipts.",
    ],
  };
}

const EXAMPLE_NOUN = { type: "noun", license: "MIT", summary: "Helps a food pantry check intake forms before a visit.", noun: { slug: "intake-form-checklist", category: "noun.recovery_playbook", title: "Intake form checklist", value: { steps: ["Check the date", "Check the household size"] } } };
const EXAMPLE_VERB = { type: "verb", license: "MIT", summary: "A reusable check any worker can claim.", verb: { slug: "check-intake-form", kind: "verb.verify_gate", title: "Check an intake form", steps: ["Read the form", "Compare each field with the checklist"], acceptance: [{ id: "report-valid", check: "The report lists every field with pass or fail.", method: "deterministic" }] } };

export function renderA2aCard(url, repoUrl) {
  const json = ["application/json"];
  return {
    protocolVersion: "0.3.0",
    name: "Tessera seed-pack library door",
    description: "Submit seed packs, task objects (nouns) and task templates (verbs) to the seed-pack library, one message at a time. Each submission becomes a propose verb with a SHA-256 receipt and a review verb that a different reviewer claims. Submitting executes, activates and admits nothing.",
    url: `${url}/a2a`,
    preferredTransport: "JSONRPC",
    additionalInterfaces: [{ url: `${url}/a2a`, transport: "JSONRPC" }],
    version: TESSERA_VERSION,
    provider: { organization: "ta-amps", url: repoUrl },
    documentationUrl: `${url}/v1/library`,
    capabilities: { streaming: false, pushNotifications: false, stateTransitionHistory: false },
    securitySchemes: { bearer: { type: "http", scheme: "bearer", description: "A contributor key, or a tracked session token from POST /v1/session/anonymous (no secret needed to mint one)." } },
    security: [{ bearer: [] }],
    defaultInputModes: [...json, "text/plain"],
    defaultOutputModes: json,
    supportsAuthenticatedExtendedCard: false,
    skills: [
      { id: "propose-task-object", name: "Propose a task object", description: "message/send with one data part {type: noun, license, summary, noun: {slug, category, title, value}}.", tags: ["seed-pack", "noun", "proposal"], examples: [JSON.stringify(EXAMPLE_NOUN)], inputModes: json, outputModes: json },
      { id: "propose-task", name: "Propose a task template", description: "message/send with one data part {type: verb, license, summary, verb: {slug, kind, title, steps, acceptance}}.", tags: ["seed-pack", "verb", "proposal"], examples: [JSON.stringify(EXAMPLE_VERB)], inputModes: json, outputModes: json },
      { id: "submit-pack", name: "Submit a seed pack", description: `message/send with one data part {type: pack, bundle: {format: ${BUNDLE_FORMAT}, license, source, files}}. Code is stored as text, never run.`, tags: ["seed-pack", "pack", "proposal"], inputModes: json, outputModes: json },
      { id: "proposal-status", name: "Proposal status", description: "tasks/get with the proposal id: submitted, working (in review), completed (admitted), rejected, or failed (review dead-lettered).", tags: ["seed-pack", "status"], inputModes: json, outputModes: json },
    ],
  };
}
