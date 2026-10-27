/**
 * Portable ES-Module Ontology, Ranking, Fragility, Reconciliation, and Profile Engine.
 *
 * Designed for zero-dependency execution inside Cloudflare Workers and standard ESM
 * runtimes, mirroring categories.py, rankings.py, fragility.py, reconcile.py, and profiles.py.
 */

const _TEXT_ENCODER = new TextEncoder();

const _SHA256_K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function _rotr(x, n) {
  return (x >>> n) | (x << (32 - n));
}

/**
 * Compute synchronous FIPS 180-4 SHA-256 lowercase hex digest for UTF-8 strings or Uint8Arrays.
 * @param {string | Uint8Array} input
 * @returns {string}
 */
export function sha256Hex(input) {
  const bytes = typeof input === "string" ? _TEXT_ENCODER.encode(input) : new Uint8Array(input);
  const bitLenHi = Math.floor((bytes.length * 8) / 0x100000000);
  const bitLenLo = (bytes.length * 8) >>> 0;

  const paddedLen = (((bytes.length + 9 + 63) >> 6) << 6);
  const buf = new Uint8Array(paddedLen);
  buf.set(bytes, 0);
  buf[bytes.length] = 0x80;
  const view = new DataView(buf.buffer);
  view.setUint32(paddedLen - 8, bitLenHi, false);
  view.setUint32(paddedLen - 4, bitLenLo, false);

  let h0 = 0x6a09e667;
  let h1 = 0xbb67ae85;
  let h2 = 0x3c6ef372;
  let h3 = 0xa54ff53a;
  let h4 = 0x510e527f;
  let h5 = 0x9b05688c;
  let h6 = 0x1f83d9ab;
  let h7 = 0x5be0cd19;

  const w = new Uint32Array(64);
  for (let offset = 0; offset < paddedLen; offset += 64) {
    for (let i = 0; i < 16; i++) {
      w[i] = view.getUint32(offset + i * 4, false);
    }
    for (let i = 16; i < 64; i++) {
      const s0 = _rotr(w[i - 15], 7) ^ _rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = _rotr(w[i - 2], 17) ^ _rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }

    let a = h0;
    let b = h1;
    let c = h2;
    let d = h3;
    let e = h4;
    let f = h5;
    let g = h6;
    let h = h7;

    for (let i = 0; i < 64; i++) {
      const S1 = _rotr(e, 6) ^ _rotr(e, 11) ^ _rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const temp1 = (h + S1 + ch + _SHA256_K[i] + w[i]) >>> 0;
      const S0 = _rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (S0 + maj) >>> 0;

      h = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }

    h0 = (h0 + a) >>> 0;
    h1 = (h1 + b) >>> 0;
    h2 = (h2 + c) >>> 0;
    h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0;
    h5 = (h5 + f) >>> 0;
    h6 = (h6 + g) >>> 0;
    h7 = (h7 + h) >>> 0;
  }

  return [h0, h1, h2, h3, h4, h5, h6, h7]
    .map((v) => (v >>> 0).toString(16).padStart(8, "0"))
    .join("");
}

export function canonicalJson(value) {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (value instanceof Set) {
    return canonicalJson(Array.from(value).sort((a, b) => String(a).localeCompare(String(b))));
  }
  if (Array.isArray(value)) {
    return "[" + value.map((item) => canonicalJson(item)).join(",") + "]";
  }
  const keys = Object.keys(value)
    .filter((k) => value[k] !== undefined)
    .sort();
  return (
    "{" +
    keys.map((k) => JSON.stringify(k) + ":" + canonicalJson(value[k])).join(",") +
    "}"
  );
}

export function canonicalSha256(value) {
  return sha256Hex(canonicalJson(value));
}

function _round6(num) {
  return Math.round(num * 1e6) / 1e6;
}

function _clamp(val, lo, hi) {
  return Math.max(lo, Math.min(hi, val));
}

// ============================================================================
// 1. CATEGORIES ENGINE
// ============================================================================

export const ALLOWED_RELATION_TYPES = Object.freeze([
  "derives_from",
  "supersedes",
  "contradicts",
  "corroborates",
  "verifies",
  "requires_noun",
  "produces_noun",
  "rebutted_by_tombstone",
  "compiled_into_dag",
  "adapted_for_cohort",
]);

export const VERIFICATION_TIERS = Object.freeze([
  "unverified",
  "deterministic_gate",
  "multi_verifier",
  "steward_attested",
]);

function _buildDimensions(weights, maxLatencyMs = 30000, maxCostUsd = 5.0) {
  const requiredSet = new Set([
    "determinism",
    "evidence_grounding",
    "token_efficiency",
    "safety_compliance",
  ]);
  const defs = {
    determinism: { dtype: "float", min_val: 0, max_val: 1, unit: "ratio", direction: "higher_is_better" },
    evidence_grounding: { dtype: "float", min_val: 0, max_val: 1, unit: "ratio", direction: "higher_is_better" },
    token_efficiency: { dtype: "float", min_val: 0, max_val: 1, unit: "ratio", direction: "higher_is_better" },
    safety_compliance: { dtype: "float", min_val: 0, max_val: 1, unit: "ratio", direction: "higher_is_better" },
    reusability: { dtype: "float", min_val: 0, max_val: 1, unit: "ratio", direction: "higher_is_better" },
    latency_p95_ms: { dtype: "float", min_val: 0, max_val: maxLatencyMs, unit: "ms", direction: "lower_is_better" },
    cost_usd: { dtype: "float", min_val: 0, max_val: maxCostUsd, unit: "usd", direction: "lower_is_better" },
    fragility_score: { dtype: "float", min_val: 0, max_val: 1, unit: "score", direction: "lower_is_better" },
    verification_tier: {
      dtype: "categorical",
      min_val: 0,
      max_val: 3,
      allowed_values: VERIFICATION_TIERS,
      unit: "tier",
      direction: "higher_is_better",
    },
  };
  const out = {};
  for (const [name, defaultWeight] of Object.entries(weights)) {
    const cfg = defs[name];
    out[name] = Object.freeze({
      name,
      dtype: cfg.dtype,
      min_val: cfg.min_val,
      max_val: cfg.max_val,
      allowed_values: cfg.allowed_values || [],
      unit: cfg.unit,
      direction: cfg.direction,
      default_weight: defaultWeight,
      required: requiredSet.has(name),
    });
  }
  return Object.freeze(out);
}

function _spec(
  category_id,
  kind,
  title,
  summary,
  fragility_domain,
  default_half_life_hours,
  human_sensitive,
  dimensions,
  allowed_relations,
  worker_guidance,
  canonical_tags
) {
  return Object.freeze({
    category_id,
    kind,
    title,
    summary,
    fragility_domain,
    default_half_life_hours,
    human_sensitive,
    dimensions,
    allowed_relations: Object.freeze(allowed_relations),
    worker_guidance: Object.freeze({
      preflight_context: Object.freeze(worker_guidance.preflight_context),
      required_mcp_tools: Object.freeze(worker_guidance.required_mcp_tools),
      deterministic_checks_before_llm: Object.freeze(worker_guidance.deterministic_checks_before_llm),
      post_condition_receipt_rules: Object.freeze(worker_guidance.post_condition_receipt_rules),
    }),
    canonical_tags: Object.freeze(canonical_tags),
  });
}

export const NOUN_CATEGORIES = Object.freeze({
  "noun.code_node": _spec(
    "noun.code_node",
    "noun",
    "Verified Code Node",
    "Typed function, class, module, or AST unit with deterministic test coverage.",
    "static_code",
    8760,
    false,
    _buildDimensions(
      {
        determinism: 0.24,
        evidence_grounding: 0.14,
        token_efficiency: 0.14,
        safety_compliance: 0.14,
        reusability: 0.14,
        latency_p95_ms: 0.06,
        cost_usd: 0.04,
        fragility_score: 0.06,
        verification_tier: 0.04,
      },
      5000,
      1.0
    ),
    ["derives_from", "supersedes", "contradicts", "corroborates", "verifies", "requires_noun", "rebutted_by_tombstone", "compiled_into_dag", "adapted_for_cohort"],
    {
      preflight_context: ["Load negative.knowledge tombstones matching symbol signature and package path."],
      required_mcp_tools: ["repo.read_symbol", "ast.check_syntax", "test.run_pytest"],
      deterministic_checks_before_llm: ["Verify exact SHA-256 content match in local store to skip redundant synthesis."],
      post_condition_receipt_rules: ["Attach SHA-256 of normalized source bytes and test suite exit code 0."],
    },
    ["code", "deterministic", "ast"]
  ),
  "noun.statute_card": _spec(
    "noun.statute_card",
    "noun",
    "Regulatory Statute Card",
    "Jurisdiction-scoped statutory or regulatory provision with effective dates and citations.",
    "regulatory_statute",
    2160,
    true,
    _buildDimensions({
      determinism: 0.16,
      evidence_grounding: 0.30,
      token_efficiency: 0.08,
      safety_compliance: 0.22,
      reusability: 0.08,
      latency_p95_ms: 0.04,
      cost_usd: 0.03,
      fragility_score: 0.05,
      verification_tier: 0.04,
    }),
    ["derives_from", "supersedes", "contradicts", "corroborates", "requires_noun", "rebutted_by_tombstone", "adapted_for_cohort"],
    {
      preflight_context: ["Load official statutory citation URI, jurisdiction code, and effective date window."],
      required_mcp_tools: ["corpus.lookup_statute", "citation.verify_hash"],
      deterministic_checks_before_llm: ["Validate jurisdiction ISO code and statutory section identifier regex."],
      post_condition_receipt_rules: ["Include verbatim statutory excerpt hash, jurisdiction tag, and non-legal-advice caveat."],
    },
    ["statute", "compliance", "cited"]
  ),
  "noun.ngo_kit": _spec(
    "noun.ngo_kit",
    "noun",
    "NGO Field Operations Kit",
    "Actionable humanitarian or legal-aid intake checklist, workflow, and safeguard packet.",
    "regulatory_statute",
    2160,
    true,
    _buildDimensions({
      determinism: 0.14,
      evidence_grounding: 0.24,
      token_efficiency: 0.12,
      safety_compliance: 0.26,
      reusability: 0.10,
      latency_p95_ms: 0.04,
      cost_usd: 0.03,
      fragility_score: 0.04,
      verification_tier: 0.03,
    }),
    ["derives_from", "supersedes", "corroborates", "requires_noun", "rebutted_by_tombstone", "adapted_for_cohort"],
    {
      preflight_context: ["Load linked noun.statute_card records and regional language/literacy constraints."],
      required_mcp_tools: ["corpus.lookup_statute", "safety.pii_redactor"],
      deterministic_checks_before_llm: ["Verify zero PII tokens in template placeholders."],
      post_condition_receipt_rules: ["Emit checklist SHA-256, plain-language reading grade metric, and human escalation instructions."],
    },
    ["ngo", "field-kit", "human-safeguard"]
  ),
  "noun.case_bundle": _spec(
    "noun.case_bundle",
    "noun",
    "Structured Case Bundle",
    "PII-redacted case facts, timeline evidence, applicable statute links, and open caveats.",
    "regulatory_statute",
    2160,
    true,
    _buildDimensions({
      determinism: 0.15,
      evidence_grounding: 0.28,
      token_efficiency: 0.08,
      safety_compliance: 0.28,
      reusability: 0.05,
      latency_p95_ms: 0.04,
      cost_usd: 0.04,
      fragility_score: 0.04,
      verification_tier: 0.04,
    }),
    ["derives_from", "supersedes", "contradicts", "corroborates", "requires_noun", "rebutted_by_tombstone", "adapted_for_cohort"],
    {
      preflight_context: ["Run mandatory PII/PHI scrubbing rules and check consent/redaction flags."],
      required_mcp_tools: ["safety.pii_redactor", "evidence.hash_manifest"],
      deterministic_checks_before_llm: ["Execute deterministic regex/NER redaction scan before any model call."],
      post_condition_receipt_rules: ["Record redaction pass receipt, exhibit SHA-256 list, and mandatory human sign-off requirement."],
    },
    ["case-bundle", "redacted", "human-gated"]
  ),
  "noun.shim_provision": _spec(
    "noun.shim_provision",
    "noun",
    "Compatibility Shim Provision",
    "Deterministic protocol, schema, or SDK adapter bridging mismatched tool interfaces.",
    "mcp_api_schema",
    168,
    false,
    _buildDimensions(
      {
        determinism: 0.25,
        evidence_grounding: 0.12,
        token_efficiency: 0.16,
        safety_compliance: 0.12,
        reusability: 0.16,
        latency_p95_ms: 0.06,
        cost_usd: 0.04,
        fragility_score: 0.05,
        verification_tier: 0.04,
      },
      2000,
      0.5
    ),
    ["derives_from", "supersedes", "verifies", "requires_noun", "rebutted_by_tombstone", "compiled_into_dag", "adapted_for_cohort"],
    {
      preflight_context: ["Load source and target JSON schemas plus known breaking-change tombstones."],
      required_mcp_tools: ["schema.validate_json", "test.contract_probe"],
      deterministic_checks_before_llm: ["Diff source and target field maps deterministically before drafting adapter glue."],
      post_condition_receipt_rules: ["Include round-trip contract test receipt and upstream version pin."],
    },
    ["shim", "adapter", "schema-bridge"]
  ),
  "noun.scene_recipe": _spec(
    "noun.scene_recipe",
    "noun",
    "Declarative Scene Recipe",
    "Reproducible 2D/3D spatial layout, lighting, camera rig, and asset binding graph.",
    "mcp_api_schema",
    168,
    false,
    _buildDimensions({
      determinism: 0.20,
      evidence_grounding: 0.12,
      token_efficiency: 0.16,
      safety_compliance: 0.14,
      reusability: 0.18,
      latency_p95_ms: 0.08,
      cost_usd: 0.04,
      fragility_score: 0.04,
      verification_tier: 0.04,
    }),
    ["derives_from", "supersedes", "requires_noun", "rebutted_by_tombstone", "compiled_into_dag", "adapted_for_cohort"],
    {
      preflight_context: ["Inspect coordinate-system convention, bounding boxes, and linked noun.motion_profile assets."],
      required_mcp_tools: ["scene.validate_bounds", "asset.resolve_manifest"],
      deterministic_checks_before_llm: ["Verify asset URIs, frame budgets, and non-overlapping collision bounds."],
      post_condition_receipt_rules: ["Record deterministic seed, scene graph SHA-256, and SYNTHETIC watermark metadata if rendered."],
    },
    ["scene", "spatial", "kinematics"]
  ),
  "noun.motion_profile": _spec(
    "noun.motion_profile",
    "noun",
    "Kinematic Motion Profile",
    "Parameterized keyframe curves, velocity/jerk limits, and rig joint constraints.",
    "mcp_api_schema",
    168,
    false,
    _buildDimensions({
      determinism: 0.24,
      evidence_grounding: 0.12,
      token_efficiency: 0.14,
      safety_compliance: 0.14,
      reusability: 0.16,
      latency_p95_ms: 0.08,
      cost_usd: 0.04,
      fragility_score: 0.04,
      verification_tier: 0.04,
    }),
    ["derives_from", "supersedes", "requires_noun", "rebutted_by_tombstone", "compiled_into_dag", "adapted_for_cohort"],
    {
      preflight_context: ["Load joint degree-of-freedom table, frame rate, and maximum acceleration limits."],
      required_mcp_tools: ["motion.check_kinematics", "schema.validate_json"],
      deterministic_checks_before_llm: ["Check spline continuity (C1/C2) and joint angle bounds analytically."],
      post_condition_receipt_rules: ["Emit trajectory curve digest, peak velocity/jerk metrics, and frame count."],
    },
    ["motion", "trajectory", "rig"]
  ),
  "noun.mcp_server": _spec(
    "noun.mcp_server",
    "noun",
    "MCP Server Capability Card",
    "Model Context Protocol server descriptor, tool JSON schemas, auth scope, and transport spec.",
    "mcp_api_schema",
    168,
    false,
    _buildDimensions(
      {
        determinism: 0.20,
        evidence_grounding: 0.14,
        token_efficiency: 0.16,
        safety_compliance: 0.18,
        reusability: 0.12,
        latency_p95_ms: 0.08,
        cost_usd: 0.03,
        fragility_score: 0.05,
        verification_tier: 0.04,
      },
      10000,
      1.0
    ),
    ["derives_from", "supersedes", "verifies", "requires_noun", "rebutted_by_tombstone", "adapted_for_cohort"],
    {
      preflight_context: ["Load tool input/output JSON schemas, sandbox permission policy, and ETag fingerprint."],
      required_mcp_tools: ["mcp.introspect_tools", "schema.validate_json"],
      deterministic_checks_before_llm: ["Validate every tool schema against JSON Schema Draft 2020-12 and check least-privilege scopes."],
      post_condition_receipt_rules: ["Record tool list SHA-256, transport protocol, and probe HTTP status."],
    },
    ["mcp", "tool-schema", "capability"]
  ),
  "noun.model_spec": _spec(
    "noun.model_spec",
    "noun",
    "Model Capability Specification",
    "Inference model card covering context window, modalities, token pricing, and empirical calibration.",
    "mcp_api_schema",
    168,
    false,
    _buildDimensions({
      determinism: 0.14,
      evidence_grounding: 0.18,
      token_efficiency: 0.18,
      safety_compliance: 0.16,
      reusability: 0.10,
      latency_p95_ms: 0.08,
      cost_usd: 0.08,
      fragility_score: 0.04,
      verification_tier: 0.04,
    }),
    ["derives_from", "supersedes", "corroborates", "requires_noun", "rebutted_by_tombstone", "adapted_for_cohort"],
    {
      preflight_context: ["Load provider pricing table, context-window limits, and benchmark suite scores."],
      required_mcp_tools: ["registry.lookup_model", "benchmark.read_summary"],
      deterministic_checks_before_llm: ["Verify max input/output token bounds and non-negative per-million token rates."],
      post_condition_receipt_rules: ["Attach provider model ID, pricing timestamp, and empirical pass-rate summary."],
    },
    ["model-card", "routing", "pricing"]
  ),
  "noun.benchmark_suite": _spec(
    "noun.benchmark_suite",
    "noun",
    "Deterministic Benchmark Suite",
    "Reproducible evaluation harness, golden fixtures, scoring rubric, and anti-contamination hashes.",
    "static_code",
    8760,
    false,
    _buildDimensions({
      determinism: 0.28,
      evidence_grounding: 0.20,
      token_efficiency: 0.10,
      safety_compliance: 0.12,
      reusability: 0.14,
      latency_p95_ms: 0.05,
      cost_usd: 0.04,
      fragility_score: 0.03,
      verification_tier: 0.04,
    }),
    ["derives_from", "supersedes", "verifies", "requires_noun", "rebutted_by_tombstone", "compiled_into_dag"],
    {
      preflight_context: ["Load fixture manifest hashes and deterministic RNG seed configuration."],
      required_mcp_tools: ["test.run_pytest", "evidence.hash_manifest"],
      deterministic_checks_before_llm: ["Confirm zero network calls and bit-identical repeat scoring on golden fixtures."],
      post_condition_receipt_rules: ["Record fixture manifest SHA-256, rubric version, and self-test pass count."],
    },
    ["benchmark", "eval", "golden-suite"]
  ),
  "noun.feed_source": _spec(
    "noun.feed_source",
    "noun",
    "Live External Feed Source",
    "Volatile advisory stream, public data endpoint, or live pricing feed tracked via ETag/SHA-256.",
    "live_feed",
    12,
    false,
    _buildDimensions({
      determinism: 0.14,
      evidence_grounding: 0.24,
      token_efficiency: 0.12,
      safety_compliance: 0.14,
      reusability: 0.10,
      latency_p95_ms: 0.08,
      cost_usd: 0.04,
      fragility_score: 0.10,
      verification_tier: 0.04,
    }),
    ["derives_from", "supersedes", "contradicts", "corroborates", "rebutted_by_tombstone"],
    {
      preflight_context: ["Load previous HTTP ETag, Last-Modified header, and body SHA-256 digest."],
      required_mcp_tools: ["http.conditional_fetch", "evidence.hash_manifest"],
      deterministic_checks_before_llm: ["Send conditional If-None-Match / HEAD request first; skip parsing on HTTP 304."],
      post_condition_receipt_rules: ["Record HTTP status, response SHA-256, ETag, and UTC observation timestamp."],
    },
    ["live-feed", "volatile", "etag-tracked"]
  ),
  "noun.seed_hypothesis": _spec(
    "noun.seed_hypothesis",
    "noun",
    "Seed Problem Hypothesis",
    "Falsifiable candidate framing, predicted failure mode, and planned verification experiments.",
    "live_feed",
    12,
    false,
    _buildDimensions({
      determinism: 0.12,
      evidence_grounding: 0.24,
      token_efficiency: 0.16,
      safety_compliance: 0.14,
      reusability: 0.12,
      latency_p95_ms: 0.06,
      cost_usd: 0.06,
      fragility_score: 0.06,
      verification_tier: 0.04,
    }),
    ["derives_from", "supersedes", "contradicts", "corroborates", "requires_noun", "rebutted_by_tombstone", "compiled_into_dag"],
    {
      preflight_context: ["Check negative tombstones to ensure this hypothesis has not already been falsified."],
      required_mcp_tools: ["graph.search_neighbors", "tombstone.lookup"],
      deterministic_checks_before_llm: ["Verify presence of explicit falsification criteria and measurable target metric."],
      post_condition_receipt_rules: ["Record falsification predicate, prior confidence, and required verification gate."],
    },
    ["hypothesis", "seed", "falsifiable"]
  ),
  "noun.reflex_procedure": _spec(
    "noun.reflex_procedure",
    "noun",
    "Distilled Reflex Procedure",
    "Zero-call or ultra-low-token deterministic execution recipe distilled from verified trajectories.",
    "mcp_api_schema",
    168,
    false,
    _buildDimensions(
      {
        determinism: 0.28,
        evidence_grounding: 0.14,
        token_efficiency: 0.22,
        safety_compliance: 0.12,
        reusability: 0.10,
        latency_p95_ms: 0.05,
        cost_usd: 0.03,
        fragility_score: 0.03,
        verification_tier: 0.03,
      },
      1000,
      0.05
    ),
    ["derives_from", "supersedes", "verifies", "requires_noun", "rebutted_by_tombstone", "compiled_into_dag", "adapted_for_cohort"],
    {
      preflight_context: ["Verify input precondition schema and confirm zero active tombstones against reflex ID."],
      required_mcp_tools: ["reflex.match_precondition", "schema.validate_json"],
      deterministic_checks_before_llm: ["Execute pure 0-call reflex steps directly; invoke LLM only if precondition guard fails."],
      post_condition_receipt_rules: ["Record 0-call execution latency, output SHA-256, and postcondition pass status."],
    },
    ["reflex", "0-call", "distilled"]
  ),
  "noun.negative_tombstone": _spec(
    "noun.negative_tombstone",
    "noun",
    "Negative Knowledge Tombstone",
    "Verified record of a buggy, unsafe, repealed, or falsified candidate preventing repeat failures.",
    "static_code",
    8760,
    false,
    _buildDimensions(
      {
        determinism: 0.26,
        evidence_grounding: 0.28,
        token_efficiency: 0.10,
        safety_compliance: 0.16,
        reusability: 0.08,
        latency_p95_ms: 0.03,
        cost_usd: 0.02,
        fragility_score: 0.03,
        verification_tier: 0.04,
      },
      2000,
      0.25
    ),
    ["derives_from", "supersedes", "contradicts", "corroborates", "verifies"],
    {
      preflight_context: ["Load failing reproduction receipt, rejected candidate SHA-256, and superseding winner ID."],
      required_mcp_tools: ["evidence.hash_manifest", "tombstone.index_signature"],
      deterministic_checks_before_llm: ["Verify reproduction failure trace or adjudication receipt hash is attached."],
      post_condition_receipt_rules: ["Tag with negative.knowledge and index rejected content digest for preflight blocking."],
    },
    ["negative.knowledge", "tombstone", "do-not-repeat"]
  ),
});

export const VERB_CATEGORIES = Object.freeze({
  "verb.ingest": _spec(
    "verb.ingest",
    "verb",
    "Normalize and Ingest Artifact",
    "Parse raw input bytes, strip unsafe tokens, compute CAS SHA-256, and emit a typed Noun.",
    "mcp_api_schema",
    168,
    false,
    _buildDimensions({
      determinism: 0.26,
      evidence_grounding: 0.20,
      token_efficiency: 0.14,
      safety_compliance: 0.16,
      reusability: 0.08,
      latency_p95_ms: 0.06,
      cost_usd: 0.04,
      fragility_score: 0.03,
      verification_tier: 0.03,
    }),
    ["requires_noun", "produces_noun", "derives_from", "verifies"],
    {
      preflight_context: ["Check CAS store by raw SHA-256 to deduplicate identical submissions."],
      required_mcp_tools: ["cas.lookup_sha256", "safety.pii_redactor", "schema.validate_json"],
      deterministic_checks_before_llm: ["Compute SHA-256, MIME/schema parse, and PII scan deterministically before any LLM extraction."],
      post_condition_receipt_rules: ["Emit ingested Noun ID, canonical SHA-256, and category conformance score."],
    },
    ["ingest", "cas", "normalize"]
  ),
  "verb.fetch_feed": _spec(
    "verb.fetch_feed",
    "verb",
    "Fetch External Feed Conditionally",
    "Poll an external feed source using ETag/If-None-Match headers and record provenance.",
    "live_feed",
    12,
    false,
    _buildDimensions({
      determinism: 0.22,
      evidence_grounding: 0.24,
      token_efficiency: 0.16,
      safety_compliance: 0.12,
      reusability: 0.06,
      latency_p95_ms: 0.08,
      cost_usd: 0.04,
      fragility_score: 0.05,
      verification_tier: 0.03,
    }),
    ["requires_noun", "produces_noun", "corroborates", "supersedes"],
    {
      preflight_context: ["Load target noun.feed_source URI, rate-limit window, and prior ETag/SHA-256."],
      required_mcp_tools: ["http.conditional_fetch", "cas.lookup_sha256"],
      deterministic_checks_before_llm: ["Execute pure HTTP conditional GET; short-circuit with 0 LLM tokens on 304 or unchanged SHA-256."],
      post_condition_receipt_rules: ["Record HTTP status, ETag, body SHA-256, and drift classification."],
    },
    ["fetch", "feed", "conditional-http"]
  ),
  "verb.situate": _spec(
    "verb.situate",
    "verb",
    "Situate in Graph and Jurisdiction",
    "Anchor an artifact or signal inside its graph neighborhood, statutory scope, and prior history.",
    "regulatory_statute",
    2160,
    false,
    _buildDimensions({
      determinism: 0.18,
      evidence_grounding: 0.26,
      token_efficiency: 0.14,
      safety_compliance: 0.16,
      reusability: 0.10,
      latency_p95_ms: 0.06,
      cost_usd: 0.04,
      fragility_score: 0.03,
      verification_tier: 0.03,
    }),
    ["requires_noun", "produces_noun", "corroborates", "contradicts", "adapted_for_cohort"],
    {
      preflight_context: ["Query 2-hop graph neighborhood, active negative tombstones, and jurisdiction filters."],
      required_mcp_tools: ["graph.search_neighbors", "tombstone.lookup"],
      deterministic_checks_before_llm: ["Run exact citation/entity graph traversal before invoking semantic synthesis."],
      post_condition_receipt_rules: ["Attach linked neighbor IDs, tombstones consulted, and cohort/jurisdiction scope."],
    },
    ["situate", "context-graph", "jurisdiction"]
  ),
  "verb.problemize": _spec(
    "verb.problemize",
    "verb",
    "Frame Bounded Problem Statement",
    "Convert noisy observations or user requests into testable constraints, acceptance gates, and seeds.",
    "mcp_api_schema",
    168,
    false,
    _buildDimensions({
      determinism: 0.16,
      evidence_grounding: 0.24,
      token_efficiency: 0.16,
      safety_compliance: 0.18,
      reusability: 0.10,
      latency_p95_ms: 0.05,
      cost_usd: 0.05,
      fragility_score: 0.03,
      verification_tier: 0.03,
    }),
    ["requires_noun", "produces_noun", "derives_from", "rebutted_by_tombstone"],
    {
      preflight_context: ["Load situated neighborhood, caller budget/profile limits, and historical tombstones."],
      required_mcp_tools: ["graph.search_neighbors", "tombstone.lookup"],
      deterministic_checks_before_llm: ["Check if an existing verified reflex procedure already solves the exact problem signature."],
      post_condition_receipt_rules: ["Produce explicit success predicate, hard safety constraints, and candidate seed IDs."],
    },
    ["problemize", "framing", "constraints"]
  ),
  "verb.expand_seed": _spec(
    "verb.expand_seed",
    "verb",
    "Expand Seed Hypothesis",
    "Elaborate a seed hypothesis into concrete sub-tasks, required nouns, and falsification checks.",
    "live_feed",
    12,
    false,
    _buildDimensions({
      determinism: 0.16,
      evidence_grounding: 0.22,
      token_efficiency: 0.18,
      safety_compliance: 0.14,
      reusability: 0.12,
      latency_p95_ms: 0.06,
      cost_usd: 0.05,
      fragility_score: 0.04,
      verification_tier: 0.03,
    }),
    ["requires_noun", "produces_noun", "derives_from", "compiled_into_dag"],
    {
      preflight_context: ["Load target noun.seed_hypothesis, budget ceiling, and available reusable nouns."],
      required_mcp_tools: ["graph.search_neighbors", "schema.validate_json"],
      deterministic_checks_before_llm: ["Prune branches matching known negative.knowledge tombstones before expansion."],
      post_condition_receipt_rules: ["Emit acyclic dependency list, estimated token/USD budget, and verification gate bindings."],
    },
    ["expand", "planning", "hypothesis"]
  ),
  "verb.solve_draft": _spec(
    "verb.solve_draft",
    "verb",
    "Synthesize Candidate Solution Draft",
    "Construct a candidate code node, shim, scene recipe, or brief satisfying problem constraints.",
    "mcp_api_schema",
    168,
    false,
    _buildDimensions({
      determinism: 0.18,
      evidence_grounding: 0.22,
      token_efficiency: 0.16,
      safety_compliance: 0.18,
      reusability: 0.10,
      latency_p95_ms: 0.05,
      cost_usd: 0.05,
      fragility_score: 0.03,
      verification_tier: 0.03,
    }),
    ["requires_noun", "produces_noun", "derives_from", "supersedes", "adapted_for_cohort"],
    {
      preflight_context: ["Load problem spec, target CategorySpec dimensions, and all matching tombstones."],
      required_mcp_tools: ["repo.read_symbol", "schema.validate_json", "safety.pii_redactor"],
      deterministic_checks_before_llm: ["Assemble reusable verified nouns and auto-shims first to minimize fresh generation."],
      post_condition_receipt_rules: ["Emit candidate artifact SHA-256, cited input nouns, and draft status pending verify_gate."],
    },
    ["solve", "draft", "synthesis"]
  ),
  "verb.compile_dag": _spec(
    "verb.compile_dag",
    "verb",
    "Compile Deterministic Execution DAG",
    "Lower expanded plans and candidate steps into a topologically sorted, typed execution graph.",
    "static_code",
    8760,
    false,
    _buildDimensions({
      determinism: 0.28,
      evidence_grounding: 0.16,
      token_efficiency: 0.16,
      safety_compliance: 0.14,
      reusability: 0.10,
      latency_p95_ms: 0.06,
      cost_usd: 0.03,
      fragility_score: 0.03,
      verification_tier: 0.04,
    }),
    ["requires_noun", "produces_noun", "compiled_into_dag", "verifies"],
    {
      preflight_context: ["Load expanded step specs, input/output noun categories, and cost caps."],
      required_mcp_tools: ["dag.toposort_validate", "schema.validate_json"],
      deterministic_checks_before_llm: ["Run cycle detection, type compatibility check, and budget sum strictly in 0-call code."],
      post_condition_receipt_rules: ["Emit topological node order, critical-path latency bound, and DAG manifest SHA-256."],
    },
    ["dag", "compiler", "deterministic"]
  ),
  "verb.verify_gate": _spec(
    "verb.verify_gate",
    "verb",
    "Execute Deterministic Verification Gate",
    "Run unit tests, benchmark assertions, schema validators, and safety checks on a candidate.",
    "static_code",
    8760,
    false,
    _buildDimensions({
      determinism: 0.30,
      evidence_grounding: 0.22,
      token_efficiency: 0.12,
      safety_compliance: 0.16,
      reusability: 0.06,
      latency_p95_ms: 0.05,
      cost_usd: 0.03,
      fragility_score: 0.02,
      verification_tier: 0.04,
    }),
    ["requires_noun", "produces_noun", "verifies", "corroborates", "rebutted_by_tombstone"],
    {
      preflight_context: ["Load candidate artifact bytes, target CategorySpec, and benchmark suite."],
      required_mcp_tools: ["test.run_pytest", "schema.validate_json", "safety.pii_redactor"],
      deterministic_checks_before_llm: ["Execute 100% of test, schema, and leak checks deterministically without LLM calls."],
      post_condition_receipt_rules: ["Emit pass/fail counts, stdout/stderr digest, and noun.negative_tombstone on deterministic failure."],
    },
    ["verify", "gate", "0-call"]
  ),
  "verb.recheck_fragility": _spec(
    "verb.recheck_fragility",
    "verb",
    "Recheck Stale or Fragile Noun",
    "Re-probe decayed artifacts against live endpoints, statutes, or schemas to refresh confidence.",
    "live_feed",
    12,
    false,
    _buildDimensions({
      determinism: 0.24,
      evidence_grounding: 0.24,
      token_efficiency: 0.16,
      safety_compliance: 0.12,
      reusability: 0.06,
      latency_p95_ms: 0.06,
      cost_usd: 0.04,
      fragility_score: 0.04,
      verification_tier: 0.04,
    }),
    ["requires_noun", "produces_noun", "verifies", "supersedes", "rebutted_by_tombstone"],
    {
      preflight_context: ["Load artifact prev_sha256, prev_etag, half_life_hours, and dependent graph edges."],
      required_mcp_tools: ["http.conditional_fetch", "evidence.hash_manifest"],
      deterministic_checks_before_llm: ["Compare HTTP status, ETag, and SHA-256 first; invoke re-ingestion only when status == drifted."],
      post_condition_receipt_rules: ["Record unchanged | drifted | unreachable_degraded status and updated last_verified_at."],
    },
    ["fragility", "recheck", "freshness"]
  ),
  "verb.reconcile_conflict": _spec(
    "verb.reconcile_conflict",
    "verb",
    "Reconcile 3-Way Context/CAS Conflict",
    "Merge disjoint edits, monotonic tag sets, and commutative counters or isolate collisions.",
    "static_code",
    8760,
    true,
    _buildDimensions({
      determinism: 0.30,
      evidence_grounding: 0.20,
      token_efficiency: 0.16,
      safety_compliance: 0.14,
      reusability: 0.06,
      latency_p95_ms: 0.04,
      cost_usd: 0.03,
      fragility_score: 0.03,
      verification_tier: 0.04,
    }),
    ["requires_noun", "produces_noun", "supersedes", "contradicts"],
    {
      preflight_context: ["Load common ancestor base snapshot plus concurrent ours and theirs revisions."],
      required_mcp_tools: ["cas.lookup_sha256", "reconcile.three_way"],
      deterministic_checks_before_llm: ["Execute 0-call structural 3-way merge; never invoke LLM when status == auto_merged."],
      post_condition_receipt_rules: ["Emit merged CAS digest or structured ConflictRecord listing exact conflicting_paths."],
    },
    ["reconcile", "3-way-merge", "cas"]
  ),
  "verb.vote_judge": _spec(
    "verb.vote_judge",
    "verb",
    "Adjudicate Conflict via Calibrated Ballots",
    "Tally Sybil-fenced, calibration-weighted verifier votes and route winner or park for human.",
    "regulatory_statute",
    2160,
    true,
    _buildDimensions({
      determinism: 0.26,
      evidence_grounding: 0.26,
      token_efficiency: 0.10,
      safety_compliance: 0.20,
      reusability: 0.04,
      latency_p95_ms: 0.04,
      cost_usd: 0.03,
      fragility_score: 0.03,
      verification_tier: 0.04,
    }),
    ["requires_noun", "produces_noun", "verifies", "supersedes", "rebutted_by_tombstone"],
    {
      preflight_context: ["Load ConflictRecord, proposer_key_ids, voter calibration history, and human_only flag."],
      required_mcp_tools: ["reconcile.adjudicate_ballots", "tombstone.index_signature"],
      deterministic_checks_before_llm: ["Enforce self-vote block, duplicate-key block, and quorum/margin math strictly in deterministic code."],
      post_condition_receipt_rules: ["Emit SHA-256 ReconciliationReceipt and noun.negative_tombstone for rejected buggy loser."],
    },
    ["voting", "judging", "sybil-fenced"]
  ),
  "verb.distill_reflex": _spec(
    "verb.distill_reflex",
    "verb",
    "Distill Verified Trace into Reflex",
    "Compress repeated high-confidence execution traces into a 0-call noun.reflex_procedure.",
    "mcp_api_schema",
    168,
    false,
    _buildDimensions({
      determinism: 0.26,
      evidence_grounding: 0.20,
      token_efficiency: 0.20,
      safety_compliance: 0.12,
      reusability: 0.10,
      latency_p95_ms: 0.04,
      cost_usd: 0.02,
      fragility_score: 0.03,
      verification_tier: 0.03,
    }),
    ["requires_noun", "produces_noun", "derives_from", "verifies", "compiled_into_dag"],
    {
      preflight_context: ["Load >= 3 corroborated execution traces and check absence of contradictory tombstones."],
      required_mcp_tools: ["trace.extract_invariants", "test.run_pytest"],
      deterministic_checks_before_llm: ["Replay candidate reflex procedure deterministically across historical trace inputs."],
      post_condition_receipt_rules: ["Emit noun.reflex_procedure digest, replay pass rate = 1.0, and token savings ratio."],
    },
    ["distill", "reflex", "token-saving"]
  ),
});

export const CATEGORY_REGISTRY = Object.freeze({
  ...NOUN_CATEGORIES,
  ...VERB_CATEGORIES,
});

export function normalizeTag(rawTag) {
  return String(rawTag)
    .trim()
    .toLowerCase()
    .replace(/[_ ]+/g, "-")
    .replace(/[^a-z0-9._:-]+/g, "")
    .replace(/-{2,}/g, "-")
    .replace(/^-+|-+$/g, "");
}

export function normalizeTags(tags = [], includeCanonical = []) {
  const out = new Set();
  for (const raw of [...includeCanonical, ...tags]) {
    if (typeof raw === "string") {
      const cleaned = normalizeTag(raw);
      if (cleaned) out.add(cleaned);
    }
  }
  return Array.from(out).sort();
}

function _normalizeDimensionValue(dimSpec, rawVal) {
  if (dimSpec.dtype === "categorical") {
    const idx = dimSpec.allowed_values.indexOf(rawVal);
    if (idx === -1) {
      throw new Error(`Invalid categorical value ${rawVal} for ${dimSpec.name}`);
    }
    const ratio = idx / (dimSpec.allowed_values.length - 1);
    return dimSpec.direction === "higher_is_better" ? ratio : 1 - ratio;
  }
  if (typeof rawVal !== "number" || Number.isNaN(rawVal)) {
    throw new Error(`Numeric value required for ${dimSpec.name}`);
  }
  const clamped = _clamp(rawVal, dimSpec.min_val, dimSpec.max_val);
  const span = dimSpec.max_val - dimSpec.min_val;
  return dimSpec.direction === "higher_is_better"
    ? (clamped - dimSpec.min_val) / span
    : (dimSpec.max_val - clamped) / span;
}

export function validateCategoryPayload(categoryId, dimensions = {}, relations = [], tags = []) {
  const errors = [];
  const warnings = [];
  const spec = CATEGORY_REGISTRY[categoryId];
  if (!spec) {
    return {
      category_id: categoryId,
      valid: false,
      conformance_score: 0,
      quality_score: 0,
      normalized_dimensions: {},
      normalized_tags: normalizeTags(tags),
      validated_relations: [],
      errors: [`Unknown category_id '${categoryId}'`],
      warnings: [],
      payload_sha256: canonicalSha256({ category_id: categoryId, dimensions, tags }),
    };
  }

  const normalizedDims = {};
  let weightedSum = 0;
  let presentWeight = 0;

  for (const [axis, dimSpec] of Object.entries(spec.dimensions)) {
    if (!(axis in dimensions) || dimensions[axis] === undefined) {
      if (dimSpec.required) errors.push(`Missing required dimension '${axis}'`);
      continue;
    }
    const rawVal = dimensions[axis];
    if (dimSpec.dtype === "categorical") {
      if (typeof rawVal !== "string" || !dimSpec.allowed_values.includes(rawVal)) {
        errors.push(`Dimension '${axis}' invalid categorical value '${rawVal}'`);
        continue;
      }
    } else {
      if (typeof rawVal !== "number" || Number.isNaN(rawVal)) {
        errors.push(`Dimension '${axis}' requires numeric value`);
        continue;
      }
      if (rawVal < dimSpec.min_val || rawVal > dimSpec.max_val) {
        errors.push(`Dimension '${axis}' value ${rawVal} outside [${dimSpec.min_val}, ${dimSpec.max_val}]`);
        continue;
      }
    }
    const norm = _normalizeDimensionValue(dimSpec, rawVal);
    normalizedDims[axis] = _round6(norm);
    weightedSum += dimSpec.default_weight * norm;
    presentWeight += dimSpec.default_weight;
  }

  for (const extra of Object.keys(dimensions).sort()) {
    if (!(extra in spec.dimensions)) {
      warnings.push(`Unrecognized dimension '${extra}' ignored for ${categoryId}`);
    }
  }

  const allowedSet = new Set(spec.allowed_relations);
  const validatedRelations = [];
  relations.forEach((rawRel, idx) => {
    const edge =
      typeof rawRel === "string"
        ? { relation: rawRel, target_id: `unspecified:${idx}`, target_category: null, note: "" }
        : {
            relation: rawRel.relation || rawRel.rel || rawRel.type,
            target_id: String(rawRel.target_id || rawRel.target || "").trim(),
            target_category: rawRel.target_category ?? null,
            note: String(rawRel.note || ""),
          };
    if (!ALLOWED_RELATION_TYPES.includes(edge.relation)) {
      errors.push(`relations[${idx}]: unknown relation '${edge.relation}'`);
      return;
    }
    if (!edge.target_id) {
      errors.push(`relations[${idx}]: empty target_id`);
      return;
    }
    if (edge.target_category && !CATEGORY_REGISTRY[edge.target_category]) {
      errors.push(`relations[${idx}]: unknown target_category '${edge.target_category}'`);
      return;
    }
    if (!allowedSet.has(edge.relation)) {
      errors.push(`Relation '${edge.relation}' is not permitted for ${categoryId}`);
      return;
    }
    validatedRelations.push(edge);
  });

  const normalizedTags = normalizeTags(tags, spec.canonical_tags);
  const qualityScore = presentWeight > 0 ? weightedSum / presentWeight : 0;
  const reqEntries = Object.values(spec.dimensions).filter((d) => d.required);
  const satCount = reqEntries.filter((d) => d.name in normalizedDims).length;
  const reqCoverage = reqEntries.length > 0 ? satCount / reqEntries.length : 1;
  const penalty = Math.min(1, 0.25 * errors.length + 0.03 * warnings.length);
  const conformanceScore = _round6(_clamp((0.65 * reqCoverage + 0.35 * qualityScore) * (1 - penalty), 0, 1));

  const payloadSha256 = canonicalSha256({
    category_id: categoryId,
    normalized_dimensions: normalizedDims,
    normalized_tags: normalizedTags,
    relations: validatedRelations,
  });

  return {
    category_id: categoryId,
    valid: errors.length === 0,
    conformance_score: conformanceScore,
    quality_score: _round6(qualityScore),
    normalized_dimensions: normalizedDims,
    normalized_tags: normalizedTags,
    validated_relations: validatedRelations,
    errors,
    warnings,
    payload_sha256: payloadSha256,
  };
}

// ============================================================================
// 2. FRAGILITY ENGINE
// ============================================================================

export const HALF_LIFE_HOURS_BY_DOMAIN = Object.freeze({
  static_code: 8760,
  regulatory_statute: 2160,
  mcp_api_schema: 168,
  live_feed: 12,
});

export const BASE_FRAGILITY_BY_DOMAIN = Object.freeze({
  static_code: 0.10,
  regulatory_statute: 0.35,
  mcp_api_schema: 0.65,
  live_feed: 0.90,
});

const _DOMAIN_ALIASES = {
  static_code: "static_code",
  code: "static_code",
  regulatory_statute: "regulatory_statute",
  statute: "regulatory_statute",
  mcp_api_schema: "mcp_api_schema",
  mcp: "mcp_api_schema",
  api_schema: "mcp_api_schema",
  live_feed: "live_feed",
  pricing: "live_feed",
  advisory: "live_feed",
};

function _toEpochSeconds(ts) {
  if (typeof ts === "number") return ts;
  if (typeof ts === "string") return Date.parse(ts) / 1000;
  throw new TypeError(`Unsupported timestamp: ${typeof ts}`);
}

export function resolveFragilityDomain(domainOrCategory) {
  if (CATEGORY_REGISTRY[domainOrCategory]) {
    return CATEGORY_REGISTRY[domainOrCategory].fragility_domain;
  }
  const key = String(domainOrCategory).trim().toLowerCase();
  if (_DOMAIN_ALIASES[key]) return _DOMAIN_ALIASES[key];
  throw new Error(`Unknown fragility domain or category '${domainOrCategory}'`);
}

export function resolveHalfLifeHours(domainOrCategory, overrideHalfLifeHours = null) {
  if (overrideHalfLifeHours !== null && overrideHalfLifeHours !== undefined) {
    if (overrideHalfLifeHours <= 0) throw new Error("overrideHalfLifeHours must be > 0");
    return Number(overrideHalfLifeHours);
  }
  if (CATEGORY_REGISTRY[domainOrCategory]) {
    return CATEGORY_REGISTRY[domainOrCategory].default_half_life_hours;
  }
  return HALF_LIFE_HOURS_BY_DOMAIN[resolveFragilityDomain(domainOrCategory)];
}

export function computeFragilityScore(
  domainOrCategory,
  { historicalDriftRate = 0, externalDependencyCount = 0, upstreamMutability = null } = {}
) {
  const domain = resolveFragilityDomain(domainOrCategory);
  const base = BASE_FRAGILITY_BY_DOMAIN[domain];
  const drift = _clamp(Number(historicalDriftRate), 0, 1);
  const depCount = Math.max(0, Math.trunc(externalDependencyCount));
  const depPressure = 0.15 * (1 - 1 / (1 + 0.25 * depCount));
  const mut = upstreamMutability === null ? base : _clamp(Number(upstreamMutability), 0, 1);
  return _round6(_clamp(0.55 * base + 0.25 * drift + 0.20 * mut + depPressure, 0, 1));
}

export function freshnessConfidence(lastVerifiedAt, nowTs, halfLifeHours) {
  if (halfLifeHours <= 0) throw new Error(`halfLifeHours must be > 0, got ${halfLifeHours}`);
  const ageHours = Math.max(0, _toEpochSeconds(nowTs) - _toEpochSeconds(lastVerifiedAt)) / 3600;
  return _clamp(Math.pow(0.5, ageHours / halfLifeHours), 0, 1);
}

export function needsRecheck(freshness, threshold = 0.5) {
  if (threshold <= 0 || threshold > 1) throw new Error(`Invalid threshold ${threshold}`);
  return freshness < threshold;
}

export function planFragilityRechecks(items, nowTs, maxBatch = 25) {
  if (maxBatch <= 0) return [];
  const nowSec = _toEpochSeconds(nowTs);
  const tasks = [];

  for (const rec of items) {
    const itemId = String(rec.item_id || rec.id || "");
    if (!itemId) throw new Error("Each item in planFragilityRechecks requires item_id or id");
    const categoryId = String(rec.category_id || "noun.feed_source");
    const domainOrCat = String(rec.domain || categoryId);
    const domain = resolveFragilityDomain(domainOrCat);
    const hl = resolveHalfLifeHours(domainOrCat, rec.half_life_hours ?? null);
    const threshold = Number(rec.recheck_threshold ?? 0.5);
    const importance = Math.max(0, Number(rec.importance ?? 1.0));
    const fresh = freshnessConfidence(rec.last_verified_at ?? 0, nowSec, hl);

    if (!needsRecheck(fresh, threshold)) continue;

    const fragScore =
      rec.fragility_score !== undefined && rec.fragility_score !== null
        ? Number(rec.fragility_score)
        : computeFragilityScore(domainOrCat, {
            historicalDriftRate: rec.historical_drift_rate ?? 0,
            externalDependencyCount: rec.external_dependency_count ?? 0,
          });
    const urgency = importance * (1 - fresh);
    const headers = {};
    if (typeof rec.etag === "string" && rec.etag.trim()) headers["If-None-Match"] = rec.etag.trim();
    if (typeof rec.last_modified === "string" && rec.last_modified.trim()) {
      headers["If-Modified-Since"] = rec.last_modified.trim();
    }

    const taskSeed = canonicalJson({
      planned_at: Math.round(nowSec * 1000) / 1000,
      target_item_id: itemId,
      verb: "verb.recheck_fragility",
    });
    tasks.push({
      task_id: "recheck_" + sha256Hex(taskSeed).slice(0, 16),
      verb_category: "verb.recheck_fragility",
      target_item_id: itemId,
      target_category_id: categoryId,
      domain,
      source_uri: rec.source_uri ?? null,
      expected_sha256: rec.content_sha256 || rec.prev_sha256 || null,
      conditional_headers: headers,
      half_life_hours: hl,
      freshness_confidence: _round6(fresh),
      fragility_score: _round6(_clamp(fragScore, 0, 1)),
      importance: _round6(importance),
      priority_urgency: _round6(urgency),
      planned_at: nowSec,
    });
  }

  tasks.sort((a, b) => {
    if (b.priority_urgency !== a.priority_urgency) return b.priority_urgency - a.priority_urgency;
    if (a.freshness_confidence !== b.freshness_confidence) return a.freshness_confidence - b.freshness_confidence;
    return a.target_item_id.localeCompare(b.target_item_id);
  });
  return tasks.slice(0, maxBatch);
}

function _normalizeEtag(etag) {
  if (!etag) return null;
  let s = String(etag).trim();
  if (s.startsWith("W/")) s = s.slice(2).trim();
  s = s.replace(/^"+|"+$/g, "");
  return s || null;
}

export function evaluateRecheckObservation(
  prevSha256,
  newBytes,
  httpStatus,
  { prevEtag = null, newEtag = null } = {}
) {
  const normPrev = String(prevSha256 || "").trim().toLowerCase();
  const pe = _normalizeEtag(prevEtag);
  const ne = _normalizeEtag(newEtag);
  const etagMatched = pe !== null && ne !== null ? pe === ne : null;
  const code = Math.trunc(httpStatus);

  if (code === 304) {
    if (etagMatched === false) {
      return {
        status: "drifted",
        http_status: code,
        prev_sha256: normPrev,
        observed_sha256: null,
        etag_matched: false,
        drift_detected: true,
        degraded: false,
        should_reset_freshness: false,
        should_invalidate_dependents: true,
      };
    }
    return {
      status: "unchanged",
      http_status: code,
      prev_sha256: normPrev,
      observed_sha256: normPrev || null,
      etag_matched: etagMatched === null ? true : etagMatched,
      drift_detected: false,
      degraded: false,
      should_reset_freshness: true,
      should_invalidate_dependents: false,
    };
  }

  if (code < 200 || code >= 400) {
    return {
      status: "unreachable_degraded",
      http_status: code,
      prev_sha256: normPrev,
      observed_sha256: null,
      etag_matched: etagMatched,
      drift_detected: false,
      degraded: true,
      should_reset_freshness: false,
      should_invalidate_dependents: false,
    };
  }

  if (newBytes === null || newBytes === undefined) {
    if (etagMatched === true) {
      return {
        status: "unchanged",
        http_status: code,
        prev_sha256: normPrev,
        observed_sha256: normPrev || null,
        etag_matched: true,
        drift_detected: false,
        degraded: false,
        should_reset_freshness: true,
        should_invalidate_dependents: false,
      };
    }
    if (etagMatched === false) {
      return {
        status: "drifted",
        http_status: code,
        prev_sha256: normPrev,
        observed_sha256: null,
        etag_matched: false,
        drift_detected: true,
        degraded: false,
        should_reset_freshness: false,
        should_invalidate_dependents: true,
      };
    }
    return {
      status: "unreachable_degraded",
      http_status: code,
      prev_sha256: normPrev,
      observed_sha256: null,
      etag_matched: null,
      drift_detected: false,
      degraded: true,
      should_reset_freshness: false,
      should_invalidate_dependents: false,
    };
  }

  const observedSha = sha256Hex(newBytes);
  const unchanged = Boolean(normPrev) && observedSha === normPrev;
  return {
    status: unchanged ? "unchanged" : "drifted",
    http_status: code,
    prev_sha256: normPrev,
    observed_sha256: observedSha,
    etag_matched: etagMatched,
    drift_detected: !unchanged,
    degraded: false,
    should_reset_freshness: unchanged,
    should_invalidate_dependents: !unchanged,
  };
}

// ============================================================================
// 3. RANKINGS ENGINE
// ============================================================================

export const DEFAULT_PARETO_AXES = Object.freeze([
  "quality",
  "token_efficiency",
  "freshness",
  "low_fragility",
]);

const _LOWER_IS_BETTER_AXES = new Set(["fragility_score", "cost_usd", "latency_p95_ms"]);

export function empiricalBayesMean(observedMean, n, priorMean = 0.5, priorWeight = 10.0) {
  if (n < 0) throw new Error(`n must be >= 0, got ${n}`);
  if (priorWeight <= 0) throw new Error(`priorWeight must be > 0, got ${priorWeight}`);
  const obs = _clamp(Number(observedMean), 0, 1);
  const pm = _clamp(Number(priorMean), 0, 1);
  return (priorWeight * pm + n * obs) / (priorWeight + n);
}

export function dirichletPosteriorProbs(counts, priorAlphas = 2.0) {
  const entries = Object.entries(counts || {});
  if (entries.length === 0) throw new Error("counts must not be empty");
  const post = {};
  let total = 0;
  for (const [k, raw] of entries) {
    const c = Number(raw);
    if (c < 0) throw new Error(`Negative count for ${k}`);
    const alpha = typeof priorAlphas === "object" ? Number(priorAlphas[k]) : Number(priorAlphas);
    if (alpha <= 0) throw new Error(`Alpha must be > 0 for ${k}`);
    post[k] = alpha + c;
    total += post[k];
  }
  const probs = {};
  for (const [k, v] of Object.entries(post)) probs[k] = v / total;
  return probs;
}

export function wilsonInterval(successes, trials, z = 1.96) {
  const n = Number(trials);
  if (n < 0) throw new Error("trials must be >= 0");
  if (n === 0) return [0, 0];
  const s = Number(successes);
  if (s < 0 || s > n + 1e-9) throw new Error("0 <= successes <= trials required");
  const pHat = _clamp(s / n, 0, 1);
  const z2 = z * z;
  const denom = 1 + z2 / n;
  const center = pHat + z2 / (2 * n);
  const margin = z * Math.sqrt(Math.max(0, (pHat * (1 - pHat) + z2 / (4 * n)) / n));
  return [_clamp((center - margin) / denom, 0, 1), _clamp((center + margin) / denom, 0, 1)];
}

export function wilsonLowerBound(successes, trials, z = 1.96) {
  return wilsonInterval(successes, trials, z)[0];
}

export function computeCategoryDimensionScore(categoryId, dimensions = {}) {
  const spec = CATEGORY_REGISTRY[categoryId];
  if (!spec) {
    const norm = {};
    const vals = [];
    for (const [k, v] of Object.entries(dimensions)) {
      if (typeof v === "number") {
        const clamped = _clamp(v, 0, 1);
        norm[k] = _LOWER_IS_BETTER_AXES.has(k) ? 1 - clamped : clamped;
        vals.push(norm[k]);
      }
    }
    return [vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0.5, norm];
  }
  const normDims = {};
  let weighted = 0;
  let totalW = 0;
  for (const [axis, dimSpec] of Object.entries(spec.dimensions)) {
    if (!(axis in dimensions) || dimensions[axis] === undefined) continue;
    try {
      const nv = _normalizeDimensionValue(dimSpec, dimensions[axis]);
      normDims[axis] = nv;
      weighted += dimSpec.default_weight * nv;
      totalW += dimSpec.default_weight;
    } catch {
      // ignore invalid axis in ranking fallback
    }
  }
  return [totalW > 0 ? _clamp(weighted / totalW, 0, 1) : 0.5, normDims];
}

export function computeCompositeRank(
  item,
  { nowTs = null, priorMean = 0.5, priorWeight = 10.0, wilsonZ = 1.96 } = {}
) {
  const itemId = String(item.item_id || item.id || "");
  if (!itemId) throw new Error("Rankable item requires item_id or id");
  const categoryId = String(item.category_id || "noun.code_node");
  const dimensions = item.dimensions || {};
  const trials = Math.trunc(item.trials ?? item.sample_size ?? 0);
  let successes =
    item.successes !== undefined && item.successes !== null
      ? Number(item.successes)
      : item.observed_mean !== undefined && item.observed_mean !== null
      ? Number(item.observed_mean) * Math.max(0, trials)
      : trials;
  successes = trials > 0 ? _clamp(successes, 0, trials) : 0;
  const obsPass = trials > 0 ? successes / trials : priorMean;
  const ebPass = empiricalBayesMean(obsPass, trials, priorMean, priorWeight);
  const wLb = trials > 0 ? wilsonLowerBound(successes, trials, wilsonZ) : 0;

  const [rawDimScore, normDims] = computeCategoryDimensionScore(categoryId, dimensions);
  const ebDimScore = empiricalBayesMean(rawDimScore, trials, priorMean, priorWeight);

  let fresh = 1.0;
  if (item.freshness_confidence !== undefined && item.freshness_confidence !== null) {
    fresh = _clamp(Number(item.freshness_confidence), 0, 1);
  } else if (item.freshness !== undefined && item.freshness !== null) {
    fresh = _clamp(Number(item.freshness), 0, 1);
  } else if (nowTs !== null && item.last_verified_at !== undefined) {
    const hl = resolveHalfLifeHours(categoryId, item.half_life_hours ?? null);
    fresh = freshnessConfidence(item.last_verified_at, nowTs, hl);
  }

  const calibration = _clamp(Number(item.calibration_weight ?? 1.0), 0, 1);
  const quality = 0.4 * ebDimScore + 0.35 * ebPass + 0.25 * wLb;
  const composite = quality * fresh * calibration;

  const tokenEff = _clamp(
    Number(item.token_efficiency ?? normDims.token_efficiency ?? dimensions.token_efficiency ?? priorMean),
    0,
    1
  );
  let lowFrag = 0.85;
  if (item.low_fragility !== undefined && item.low_fragility !== null) {
    lowFrag = _clamp(Number(item.low_fragility), 0, 1);
  } else if (normDims.fragility_score !== undefined) {
    lowFrag = _clamp(normDims.fragility_score, 0, 1);
  } else if (item.fragility_score !== undefined) {
    lowFrag = _clamp(1 - Number(item.fragility_score), 0, 1);
  } else if (dimensions.fragility_score !== undefined) {
    lowFrag = _clamp(1 - Number(dimensions.fragility_score), 0, 1);
  }

  const axes = {};
  for (const [k, v] of Object.entries(normDims)) axes[k] = _round6(v);
  axes.quality = _round6(quality);
  axes.token_efficiency = _round6(tokenEff);
  axes.freshness = _round6(fresh);
  axes.low_fragility = _round6(lowFrag);
  axes.wilson_lower_bound = _round6(wLb);
  axes.composite_score = _round6(composite);

  return {
    item_id: itemId,
    category_id: categoryId,
    trials,
    successes: _round6(successes),
    observed_pass_rate: _round6(obsPass),
    eb_pass_rate: _round6(ebPass),
    wilson_lower_bound: _round6(wLb),
    raw_dimension_score: _round6(rawDimScore),
    eb_dimension_score: _round6(ebDimScore),
    freshness_confidence: _round6(fresh),
    calibration_weight: _round6(calibration),
    token_efficiency: _round6(tokenEff),
    low_fragility: _round6(lowFrag),
    quality: _round6(quality),
    composite_score: _round6(composite),
    axes,
  };
}

export function rankItems(items, options = {}) {
  const sortAxis = options.sortAxis || options.sort_axis || "composite_score";
  const topK = options.topK ?? options.top_k ?? null;
  const evaluated = items.map((it) => computeCompositeRank(it, options));
  evaluated.sort((a, b) => {
    const va = a.axes[sortAxis] ?? a[sortAxis] ?? a.composite_score;
    const vb = b.axes[sortAxis] ?? b[sortAxis] ?? b.composite_score;
    if (vb !== va) return vb - va;
    if (b.wilson_lower_bound !== a.wilson_lower_bound) return b.wilson_lower_bound - a.wilson_lower_bound;
    if (b.trials !== a.trials) return b.trials - a.trials;
    return a.item_id.localeCompare(b.item_id);
  });
  return topK !== null ? evaluated.slice(0, topK) : evaluated;
}

function _extractParetoCoord(item, axis) {
  const axesSub = item.axes || {};
  const dimsSub = item.dimensions || {};
  let raw = undefined;
  if (item[axis] !== undefined && item[axis] !== null) raw = item[axis];
  else if (axesSub[axis] !== undefined && axesSub[axis] !== null) raw = axesSub[axis];
  else if (dimsSub[axis] !== undefined && dimsSub[axis] !== null) raw = dimsSub[axis];
  else if (axis === "low_fragility") {
    const f = item.fragility_score ?? dimsSub.fragility_score;
    if (f !== undefined) return 1 - Number(f);
  } else if (axis === "freshness" && item.freshness_confidence !== undefined) {
    return Number(item.freshness_confidence);
  } else if (axis === "quality") {
    const q = item.composite_score ?? item.eb_dimension_score ?? item.quality_score;
    if (q !== undefined) return Number(q);
  }
  if (raw === undefined) throw new Error(`Missing Pareto axis '${axis}'`);
  return _LOWER_IS_BETTER_AXES.has(axis) ? -Number(raw) : Number(raw);
}

export function paretoFrontier(items, axes = DEFAULT_PARETO_AXES) {
  if (!axes || axes.length === 0) throw new Error("axes required");
  if (!items || items.length === 0) return [];
  const eps = 1e-12;
  const coords = items.map((it) => axes.map((ax) => _extractParetoCoord(it, ax)));
  const indices = [];

  for (let i = 0; i < items.length; i++) {
    let dominated = false;
    for (let j = 0; j < items.length; j++) {
      if (i === j) continue;
      let allGeq = true;
      let anyGt = false;
      for (let k = 0; k < axes.length; k++) {
        if (coords[j][k] < coords[i][k] - eps) {
          allGeq = false;
          break;
        }
        if (coords[j][k] > coords[i][k] + eps) anyGt = true;
      }
      if (allGeq && anyGt) {
        dominated = true;
        break;
      }
    }
    if (!dominated) indices.push(i);
  }

  indices.sort((ia, ib) => {
    const meanA = coords[ia].reduce((s, v) => s + v, 0) / axes.length;
    const meanB = coords[ib].reduce((s, v) => s + v, 0) / axes.length;
    if (meanB !== meanA) return meanB - meanA;
    const idA = String(items[ia].item_id || items[ia].id || ia);
    const idB = String(items[ib].item_id || items[ib].id || ib);
    return idA.localeCompare(idB);
  });
  return indices.map((idx) => items[idx]);
}

// ============================================================================
// 4. RECONCILE ENGINE
// ============================================================================

const _DEFAULT_SET_FIELDS = new Set([
  "tags",
  "labels",
  "citations",
  "evidence_ids",
  "corroborated_by",
  "verified_by",
  "aliases",
  "capabilities",
]);

const _DEFAULT_COUNTER_FIELDS = new Set([
  "count",
  "pass_count",
  "fail_count",
  "trial_count",
  "trials",
  "successes",
  "verification_count",
  "recheck_count",
  "citation_count",
  "usage_count",
  "hit_count",
  "token_count",
]);

const _MISSING = Symbol("missing");

function _deepEqual(a, b) {
  if (a === b) return true;
  if (a === _MISSING || b === _MISSING) return false;
  return canonicalJson(a) === canonicalJson(b);
}

function _clone(v) {
  if (v === null || typeof v !== "object") return v;
  if (v instanceof Set) return Array.from(v).sort((a, b) => String(a).localeCompare(String(b)));
  if (Array.isArray(v)) return v.map(_clone);
  const out = {};
  for (const [k, val] of Object.entries(v)) out[k] = _clone(val);
  return out;
}

function _isPlainObj(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v) && !(v instanceof Set);
}

function _isSetField(path, leaf, customSets) {
  return (
    customSets.has(path) ||
    customSets.has(leaf) ||
    _DEFAULT_SET_FIELDS.has(leaf) ||
    leaf.endsWith("_tags") ||
    leaf.endsWith("_set") ||
    leaf.endsWith("_ids")
  );
}

function _isCounterField(path, leaf, customCounters) {
  return (
    customCounters.has(path) ||
    customCounters.has(leaf) ||
    _DEFAULT_COUNTER_FIELDS.has(leaf) ||
    leaf.endsWith("_count") ||
    leaf.endsWith("_counter") ||
    path.startsWith("counters.")
  );
}

export function reconcileThreeWay(
  base,
  ours,
  theirs,
  {
    oursProposerKeyId = null,
    ours_proposer_key_id = null,
    theirsProposerKeyId = null,
    theirs_proposer_key_id = null,
    setFields = [],
    set_fields = [],
    counterFields = [],
    counter_fields = [],
  } = {}
) {
  const customSets = new Set([...setFields, ...set_fields]);
  const customCounters = new Set([...counterFields, ...counter_fields]);
  const propA = oursProposerKeyId || ours_proposer_key_id || ours._proposer_key_id || null;
  const propB = theirsProposerKeyId || theirs_proposer_key_id || theirs._proposer_key_id || null;

  const stripMeta = (obj) => {
    const c = {};
    for (const [k, v] of Object.entries(obj || {})) {
      if (k !== "_proposer_key_id") c[k] = v;
    }
    return c;
  };

  const cleanBase = stripMeta(base);
  const cleanOurs = stripMeta(ours);
  const cleanTheirs = stripMeta(theirs);

  const conflictingPaths = [];
  const candAVals = {};
  const candBVals = {};
  const operations = [];

  function mergeDict(bMap, oMap, tMap, prefix) {
    const partialOut = {};
    const docAOut = {};
    const docBOut = {};
    const keys = Array.from(
      new Set([...Object.keys(bMap), ...Object.keys(oMap), ...Object.keys(tMap)])
    ).sort();

    for (const key of keys) {
      const path = prefix ? `${prefix}.${key}` : key;
      const bVal = key in bMap ? bMap[key] : _MISSING;
      const oVal = key in oMap ? oMap[key] : _MISSING;
      const tVal = key in tMap ? tMap[key] : _MISSING;

      if (_deepEqual(oVal, tVal)) {
        if (oVal !== _MISSING) {
          partialOut[key] = _clone(oVal);
          docAOut[key] = _clone(oVal);
          docBOut[key] = _clone(oVal);
        }
        if (!_deepEqual(bVal, oVal)) operations.push(`convergent_edit:${path}`);
        continue;
      }

      if (_deepEqual(oVal, bVal)) {
        if (tVal !== _MISSING) {
          partialOut[key] = _clone(tVal);
          docAOut[key] = _clone(tVal);
          docBOut[key] = _clone(tVal);
          operations.push(`theirs_edit:${path}`);
        } else {
          operations.push(`theirs_delete:${path}`);
        }
        continue;
      }

      if (_deepEqual(tVal, bVal)) {
        if (oVal !== _MISSING) {
          partialOut[key] = _clone(oVal);
          docAOut[key] = _clone(oVal);
          docBOut[key] = _clone(oVal);
          operations.push(`ours_edit:${path}`);
        } else {
          operations.push(`ours_delete:${path}`);
        }
        continue;
      }

      if (_isPlainObj(oVal) && _isPlainObj(tVal)) {
        const bSub = _isPlainObj(bVal) ? bVal : {};
        const [sp, sa, sb] = mergeDict(bSub, oVal, tVal, path);
        partialOut[key] = sp;
        docAOut[key] = sa;
        docBOut[key] = sb;
        continue;
      }

      // Monotonic set/tag union
      if (
        (oVal instanceof Set && tVal instanceof Set) ||
        (_isSetField(path, key, customSets) && Array.isArray(oVal) && Array.isArray(tVal))
      ) {
        const bArr = bVal === _MISSING ? [] : Array.from(bVal);
        const oSet = new Set(oVal);
        const tSet = new Set(tVal);
        const monotonic = bArr.every((item) => oSet.has(item) && tSet.has(item));
        if (monotonic) {
          const union = Array.from(new Set([...oSet, ...tSet])).sort((a, b) =>
            String(a).localeCompare(String(b))
          );
          partialOut[key] = union;
          docAOut[key] = _clone(union);
          docBOut[key] = _clone(union);
          operations.push(`monotonic_set_union:${path}`);
          continue;
        }
      }

      // Commutative integer counter increment
      if (
        _isCounterField(path, key, customCounters) &&
        Number.isInteger(oVal) &&
        Number.isInteger(tVal) &&
        (bVal === _MISSING || Number.isInteger(bVal))
      ) {
        const bInt = bVal === _MISSING ? 0 : bVal;
        const mergedCount = bInt + (oVal - bInt) + (tVal - bInt);
        partialOut[key] = mergedCount;
        docAOut[key] = mergedCount;
        docBOut[key] = mergedCount;
        operations.push(`commutative_counter:${path}`);
        continue;
      }

      // True field-level value collision
      conflictingPaths.push(path);
      candAVals[path] = oVal === _MISSING ? null : _clone(oVal);
      candBVals[path] = tVal === _MISSING ? null : _clone(tVal);
      if (oVal !== _MISSING) docAOut[key] = _clone(oVal);
      if (tVal !== _MISSING) docBOut[key] = _clone(tVal);
      if (bVal !== _MISSING) partialOut[key] = _clone(bVal);
    }
    return [partialOut, docAOut, docBOut];
  }

  const [partialMerged, docA, docB] = mergeDict(cleanBase, cleanOurs, cleanTheirs, "");
  const proposers = Array.from(new Set([propA, propB].filter(Boolean))).sort();

  if (conflictingPaths.length === 0) {
    const mergedSha = canonicalSha256(partialMerged);
    return {
      conflict_id: "merge_" + mergedSha.slice(0, 16),
      status: "auto_merged",
      merged: partialMerged,
      merged_sha256: mergedSha,
      partial_merged: partialMerged,
      conflicting_paths: [],
      candidate_a: null,
      candidate_b: null,
      proposer_key_ids: proposers,
      applied_operations: operations,
    };
  }

  const shaA = canonicalSha256(docA);
  const shaB = canonicalSha256(docB);
  const conflictId =
    "conflict_" +
    canonicalSha256({
      base_sha256: canonicalSha256(cleanBase),
      candidate_a_sha256: shaA,
      candidate_b_sha256: shaB,
      conflicting_paths: conflictingPaths,
    }).slice(0, 16);

  return {
    conflict_id: conflictId,
    status: "needs_voting_or_steward",
    merged: null,
    merged_sha256: null,
    partial_merged: partialMerged,
    conflicting_paths: conflictingPaths,
    candidate_a: {
      candidate_id: "candidate_a",
      proposer_key_id: propA,
      conflicting_values: candAVals,
      document: docA,
      sha256: shaA,
    },
    candidate_b: {
      candidate_id: "candidate_b",
      proposer_key_id: propB,
      conflicting_values: candBVals,
      document: docB,
      sha256: shaB,
    },
    proposer_key_ids: proposers,
    applied_operations: operations,
  };
}

export function computeWorkerCalibration(verifiedCorrect, verifiedTotal, prior = 5.0) {
  if (prior <= 0) throw new Error("prior must be > 0");
  const c = Math.max(0, Number(verifiedCorrect));
  const t = Math.max(c, Number(verifiedTotal));
  return _clamp(c / (t + prior), 0, 1);
}

export function computeEvidenceCompleteness(ballot) {
  const cites = new Set(
    (ballot.citations || []).map((c) => String(c).trim()).filter(Boolean)
  );
  const citeRatio = Math.min(1, cites.size / 2);
  const hasReceipt = Boolean(
    ballot.verification_receipt_sha256 && String(ballot.verification_receipt_sha256).trim()
  );
  const hasRationale = String(ballot.rationale || "").trim().length >= 12;
  const raw = 0.6 * citeRatio + (hasReceipt ? 0.25 : 0) + (hasRationale ? 0.15 : 0);
  return _round6(_clamp(raw, 0.15, 1.0));
}

function _resolveCalibration(voterId, workerCalibrations, defaultPrior = 5.0) {
  const entry = (workerCalibrations || {})[voterId];
  if (entry === undefined || entry === null) return 0;
  if (typeof entry === "number") return _clamp(entry, 0, 1);
  if (typeof entry === "object") {
    if (entry.calibration_score !== undefined) return _clamp(Number(entry.calibration_score), 0, 1);
    return computeWorkerCalibration(
      entry.verified_correct ?? 0,
      entry.verified_total ?? 0,
      entry.prior ?? defaultPrior
    );
  }
  return 0;
}

export function adjudicateBallots(
  conflict,
  ballots,
  workerCalibrations,
  humanOnly = false,
  {
    defaultPrior = 5.0,
    minQuorum = 3,
    calibratedFastQuorum = 2,
    minCalibratedScore = 0.65,
    marginThreshold = 0.15,
  } = {}
) {
  if (
    !conflict ||
    conflict.status !== "needs_voting_or_steward" ||
    !conflict.candidate_a ||
    !conflict.candidate_b
  ) {
    throw new Error("adjudicateBallots requires a ConflictRecord with status='needs_voting_or_steward'");
  }

  const proposerIds = new Set(conflict.proposer_key_ids || []);
  if (conflict.candidate_a.proposer_key_id) proposerIds.add(conflict.candidate_a.proposer_key_id);
  if (conflict.candidate_b.proposer_key_id) proposerIds.add(conflict.candidate_b.proposer_key_id);

  const seenVoters = new Set();
  const accepted = [];
  const rejected = [];
  let weightA = 0;
  let weightB = 0;
  let weightPark = 0;
  let calA = 0;
  let calB = 0;
  const defectsAgainstA = [];
  const defectsAgainstB = [];

  for (const rawB of ballots || []) {
    const voterId = String(rawB.voter_key_id || "").trim();
    const choice = String(rawB.choice || "");

    if (!voterId) {
      rejected.push({
        voter_key_id: "",
        choice,
        accepted: false,
        rejection_reason: "missing_voter_key",
        calibration_score: 0,
        evidence_completeness: 0,
        effective_weight: 0,
      });
      continue;
    }
    if (proposerIds.has(voterId)) {
      rejected.push({
        voter_key_id: voterId,
        choice,
        accepted: false,
        rejection_reason: "self_vote_forbidden",
        calibration_score: 0,
        evidence_completeness: 0,
        effective_weight: 0,
      });
      continue;
    }
    if (seenVoters.has(voterId)) {
      rejected.push({
        voter_key_id: voterId,
        choice,
        accepted: false,
        rejection_reason: "duplicate_voter_key",
        calibration_score: 0,
        evidence_completeness: 0,
        effective_weight: 0,
      });
      continue;
    }
    if (!["candidate_a", "candidate_b", "park_for_human"].includes(choice)) {
      rejected.push({
        voter_key_id: voterId,
        choice,
        accepted: false,
        rejection_reason: "invalid_choice",
        calibration_score: 0,
        evidence_completeness: 0,
        effective_weight: 0,
      });
      continue;
    }

    seenVoters.add(voterId);
    const calScore = _resolveCalibration(voterId, workerCalibrations, defaultPrior);
    const evComp = computeEvidenceCompleteness(rawB);
    if (calScore <= 0) {
      rejected.push({
        voter_key_id: voterId,
        choice,
        accepted: false,
        rejection_reason: "uncalibrated_zero_weight",
        calibration_score: 0,
        evidence_completeness: evComp,
        effective_weight: 0,
      });
      continue;
    }

    const effWeight = _round6(calScore * evComp);
    accepted.push({
      voter_key_id: voterId,
      choice,
      accepted: true,
      rejection_reason: null,
      calibration_score: _round6(calScore),
      evidence_completeness: evComp,
      effective_weight: effWeight,
    });

    if (choice === "candidate_a") {
      weightA += effWeight;
      if (calScore >= minCalibratedScore) calA += 1;
      if (rawB.defect_summary) defectsAgainstB.push(String(rawB.defect_summary).trim());
    } else if (choice === "candidate_b") {
      weightB += effWeight;
      if (calScore >= minCalibratedScore) calB += 1;
      if (rawB.defect_summary) defectsAgainstA.push(String(rawB.defect_summary).trim());
    } else {
      weightPark += effWeight;
    }
  }

  const decisiveWeight = weightA + weightB;
  const margin = decisiveWeight > 0 ? Math.abs(weightA - weightB) / decisiveWeight : 0;
  const decisiveVoters = accepted.filter(
    (b) => b.choice === "candidate_a" || b.choice === "candidate_b"
  ).length;
  const quorumMet = decisiveVoters >= minQuorum || Math.max(calA, calB) >= calibratedFastQuorum;
  const parkMajority = weightPark >= decisiveWeight && (weightPark > 0 || decisiveWeight === 0);

  let status = "park_for_human";
  let winnerId = null;
  let promotedDoc = null;
  let promotedSha = null;
  let tombstone = null;

  if (!humanOnly && !parkMajority && quorumMet && margin >= marginThreshold && weightA !== weightB) {
    status = "winner_promoted";
    const winnerCand = weightA > weightB ? conflict.candidate_a : conflict.candidate_b;
    const loserCand = weightA > weightB ? conflict.candidate_b : conflict.candidate_a;
    const notes = weightA > weightB ? defectsAgainstB : defectsAgainstA;

    winnerId = winnerCand.candidate_id;
    promotedDoc = winnerCand.document;
    promotedSha = winnerCand.sha256;

    const tombstoneSha = canonicalSha256({
      conflict_id: conflict.conflict_id,
      conflicting_paths: conflict.conflicting_paths,
      rejected_candidate_id: loserCand.candidate_id,
      rejected_sha256: loserCand.sha256,
      superseded_by_sha256: winnerCand.sha256,
    });

    tombstone = {
      tombstone_id: "tombstone_" + tombstoneSha.slice(0, 16),
      category_id: "noun.negative_tombstone",
      tags: ["negative.knowledge", "tombstone", "conflict-rejected"],
      conflict_id: conflict.conflict_id,
      rejected_candidate_id: loserCand.candidate_id,
      rejected_proposer_key_id: loserCand.proposer_key_id,
      rejected_sha256: loserCand.sha256,
      superseded_by_candidate_id: winnerCand.candidate_id,
      superseded_by_sha256: winnerCand.sha256,
      conflicting_paths: conflict.conflicting_paths,
      rejected_values: loserCand.conflicting_values,
      defect_summary:
        notes.length > 0
          ? notes.join("; ")
          : `Rejected ${loserCand.candidate_id} in favor of calibrated winner ${winnerCand.candidate_id}.`,
      tombstone_sha256: tombstoneSha,
    };
  }

  const receiptSha = canonicalSha256({
    accepted_ballots: accepted,
    conflict_id: conflict.conflict_id,
    human_only: Boolean(humanOnly),
    margin: _round6(margin),
    promoted_sha256: promotedSha,
    quorum_met: quorumMet,
    rejected_ballots: rejected,
    status,
    tombstone_sha256: tombstone ? tombstone.tombstone_sha256 : null,
    weight_candidate_a: _round6(weightA),
    weight_candidate_b: _round6(weightB),
    weight_park_for_human: _round6(weightPark),
    winning_candidate_id: winnerId,
  });

  return {
    conflict_id: conflict.conflict_id,
    status,
    human_only: Boolean(humanOnly),
    quorum_met: quorumMet,
    winning_candidate_id: winnerId,
    promoted_document: promotedDoc,
    promoted_sha256: promotedSha,
    weight_candidate_a: _round6(weightA),
    weight_candidate_b: _round6(weightB),
    weight_park_for_human: _round6(weightPark),
    margin: _round6(margin),
    valid_ballot_count: accepted.length,
    calibrated_verifier_count: calA + calB,
    accepted_ballots: accepted,
    rejected_ballots: rejected,
    negative_tombstone: tombstone,
    receipt_sha256: receiptSha,
  };
}

// ============================================================================
// 5. PROFILES ENGINE
// ============================================================================

export const CALLER_PROFILES = Object.freeze({
  autonomous_coding_harness: Object.freeze({
    profile_id: "autonomous_coding_harness",
    title: "Autonomous Coding Harness",
    cohort_persona: "Automated repository engineering harness (e.g. OpenCode)",
    clamped_max_chars: 12000,
    preferred_execution_tier: "0_call_reflex",
    response_format: "compact_signatures",
    auto_shim_categories: [
      "noun.code_node",
      "noun.shim_provision",
      "noun.mcp_server",
      "noun.reflex_procedure",
      "noun.statute_card",
    ],
    require_human_on_routes: ["noun.case_bundle", "verb.vote_judge"],
    max_task_cost_usd: 0.25,
  }),
  chat_research_agent: Object.freeze({
    profile_id: "chat_research_agent",
    title: "Chat Research Agent",
    cohort_persona: "Interactive conversational synthesis assistant (e.g. Muse.AI)",
    clamped_max_chars: 6000,
    preferred_execution_tier: "hybrid_1_call",
    response_format: "plain_language_caveats",
    auto_shim_categories: ["noun.feed_source", "noun.seed_hypothesis", "noun.statute_card"],
    require_human_on_routes: ["noun.case_bundle", "noun.ngo_kit"],
    max_task_cost_usd: 0.15,
  }),
  ngo_caseworker: Object.freeze({
    profile_id: "ngo_caseworker",
    title: "NGO Caseworker Surface",
    cohort_persona: "Frontline humanitarian and legal-aid intake worker",
    clamped_max_chars: 3200,
    preferred_execution_tier: "hybrid_1_call",
    response_format: "plain_language_caveats",
    auto_shim_categories: ["noun.ngo_kit", "noun.statute_card", "noun.case_bundle"],
    require_human_on_routes: [
      "noun.statute_card",
      "noun.ngo_kit",
      "noun.case_bundle",
      "verb.reconcile_conflict",
      "verb.vote_judge",
    ],
    max_task_cost_usd: 0.08,
  }),
  human_steward: Object.freeze({
    profile_id: "human_steward",
    title: "Calibrated Human Steward",
    cohort_persona: "Domain steward adjudicating conflicts and high-stakes releases",
    clamped_max_chars: 16000,
    preferred_execution_tier: "agent_decompose",
    response_format: "plain_language_caveats",
    auto_shim_categories: [],
    require_human_on_routes: ["noun.case_bundle", "verb.vote_judge"],
    max_task_cost_usd: 1.0,
  }),
  deterministic_verifier: Object.freeze({
    profile_id: "deterministic_verifier",
    title: "Deterministic Gate Verifier",
    cohort_persona: "Zero-call CI/sandbox test and hash gate runner",
    clamped_max_chars: 8000,
    preferred_execution_tier: "0_call_reflex",
    response_format: "strict_json_receipts",
    auto_shim_categories: ["noun.benchmark_suite", "noun.code_node", "noun.negative_tombstone"],
    require_human_on_routes: ["noun.case_bundle"],
    max_task_cost_usd: 0.02,
  }),
  low_budget_donor: Object.freeze({
    profile_id: "low_budget_donor",
    title: "Low-Budget Micro-Task Donor",
    cohort_persona: "Cost-capped volunteer worker node contributing spare capacity",
    clamped_max_chars: 2000,
    preferred_execution_tier: "0_call_reflex",
    response_format: "strict_json_receipts",
    auto_shim_categories: ["noun.reflex_procedure", "noun.shim_provision"],
    require_human_on_routes: [
      "noun.statute_card",
      "noun.ngo_kit",
      "noun.case_bundle",
      "verb.vote_judge",
    ],
    max_task_cost_usd: 0.01,
  }),
});

const _CLAMP_SUFFIX = "...[clamped]";

function _clampText(text, maxChars) {
  if (text.length <= maxChars) return [text, false];
  const keep = Math.max(0, maxChars - _CLAMP_SUFFIX.length);
  return [text.slice(0, keep) + _CLAMP_SUFFIX, true];
}

function _resolveProfile(profile) {
  if (typeof profile === "object" && profile !== null && profile.profile_id) return profile;
  const found = CALLER_PROFILES[profile];
  if (!found) throw new Error(`Unknown caller profile '${profile}'`);
  return found;
}

export function adaptNounView(record, profile) {
  const prof = _resolveProfile(profile);
  const nounId = String(record.noun_id || record.item_id || record.id || "unnamed_noun");
  const categoryId = String(record.category_id || "noun.code_node");
  const title = String(record.title || nounId);
  const summary = String(record.summary || "");
  const signature = String(record.signature || record.code_signature || `${categoryId}:${nounId}`);
  const jurisdiction = String(record.jurisdiction || "global");
  const citations = (record.citations || []).map(String);
  const caveats = (record.caveats || []).map(String);
  const checklist = (record.checklist || []).map(String);
  const freshness = Number(record.freshness_confidence ?? 1.0);
  const fragility = Number(record.fragility_score ?? 0.15);
  const contentSha = String(record.sha256 || canonicalSha256(record));

  const requiresHuman =
    prof.require_human_on_routes.includes(categoryId) || Boolean(record.human_only);
  const autoShim = prof.auto_shim_categories.includes(categoryId);
  const shimDesc = autoShim
    ? {
        shim_mode: "auto_provisioned",
        target_category: categoryId,
        target_id: nounId,
        adapter_entrypoint: `shim.bind('${categoryId}', '${nounId}')`,
      }
    : null;

  const structured = {
    noun_id: nounId,
    category_id: categoryId,
    profile_id: prof.profile_id,
    title,
    sha256: contentSha,
    freshness_confidence: Math.round(freshness * 1e4) / 1e4,
    fragility_score: Math.round(fragility * 1e4) / 1e4,
    execution_tier: prof.preferred_execution_tier,
    requires_human_review: requiresHuman,
    auto_shim_applied: autoShim,
    max_task_cost_usd: prof.max_task_cost_usd,
    citations,
  };

  let rendered = "";
  let wasClamped = false;

  if (prof.response_format === "compact_signatures") {
    const lines = [
      `[${categoryId}] id=${nounId} sha256=${contentSha.slice(0, 16)} tier=${prof.preferred_execution_tier}`,
      `sig: ${signature}`,
      `metrics: freshness=${freshness.toFixed(2)} fragility=${fragility.toFixed(2)} budget_usd<=${prof.max_task_cost_usd.toFixed(2)}`,
    ];
    if (autoShim && shimDesc) lines.push(`shim: ${shimDesc.adapter_entrypoint}`);
    if (requiresHuman) lines.push("gate: HUMAN_REVIEW_REQUIRED");
    if (citations.length) lines.push(`refs: ${citations.join(", ")}`);
    [rendered, wasClamped] = _clampText(lines.join("\n"), prof.clamped_max_chars);
  } else if (prof.response_format === "plain_language_caveats") {
    const sections = [];
    if (prof.profile_id === "ngo_caseworker") {
      sections.push(`FIELD CASEWORK BRIEF: ${title} (Jurisdiction: ${jurisdiction})`);
      sections.push(
        "SAFEGUARD NOTICE: Operational reference only - not formal legal representation. " +
          (requiresHuman
            ? "Mandatory human supervisor sign-off required before client action."
            : "Verify local eligibility rules.")
      );
      if (summary) sections.push(`Plain Summary: ${summary}`);
      if (checklist.length) {
        sections.push(`Intake Steps: ${checklist.map((s, i) => `${i + 1}) ${s}`).join("; ")}`);
      }
    } else if (prof.profile_id === "human_steward") {
      sections.push(`STEWARD REVIEW PACKET: ${title} [${categoryId} / ${nounId}] (sha256=${contentSha.slice(0, 16)})`);
      if (summary) sections.push(`Overview: ${summary}`);
    } else {
      sections.push(`Research Summary - ${title} (${jurisdiction}): ${summary || signature}`);
      sections.push(`Confidence & Currency: ${Math.round(freshness * 100)}% freshness confidence (fragility=${fragility.toFixed(2)}).`);
    }
    if (caveats.length) sections.push("Caveats: " + caveats.join(" | "));
    if (citations.length) sections.push("Grounded Citations: " + citations.join(", "));
    [rendered, wasClamped] = _clampText(sections.join("\n"), prof.clamped_max_chars);
  } else {
    [rendered, wasClamped] = _clampText(canonicalJson(structured), prof.clamped_max_chars);
  }

  return {
    noun_id: nounId,
    category_id: categoryId,
    profile_id: prof.profile_id,
    response_format: prof.response_format,
    preferred_execution_tier: prof.preferred_execution_tier,
    requires_human_review: requiresHuman,
    auto_shim_applied: autoShim,
    shim_descriptor: shimDesc,
    max_task_cost_usd: prof.max_task_cost_usd,
    clamped_max_chars: prof.clamped_max_chars,
    was_clamped: wasClamped,
    rendered_text: rendered,
    structured_view: structured,
  };
}

export function adaptTaskGuidance(task, categorySpecOrId, profile) {
  const spec =
    typeof categorySpecOrId === "string"
      ? CATEGORY_REGISTRY[categorySpecOrId]
      : categorySpecOrId;
  if (!spec) throw new Error(`Unknown categorySpec '${categorySpecOrId}'`);
  const prof = _resolveProfile(profile);

  const taskId = String(task.task_id || task.id || "task_unspecified");
  const targetCat = String(task.target_category_id || spec.category_id);
  const objective = String(task.objective || task.prompt || spec.summary);
  const reqCost = Number(task.estimated_cost_usd ?? prof.max_task_cost_usd);

  let effTier = prof.preferred_execution_tier;
  let effBudget = _round6(reqCost);
  let costClamped = false;
  if (reqCost > prof.max_task_cost_usd) {
    effBudget = prof.max_task_cost_usd;
    costClamped = true;
    effTier =
      prof.max_task_cost_usd <= 0.02
        ? "0_call_reflex"
        : prof.preferred_execution_tier === "agent_decompose"
        ? "hybrid_1_call"
        : "0_call_reflex";
  }

  const requiresHuman =
    prof.require_human_on_routes.includes(spec.category_id) ||
    prof.require_human_on_routes.includes(targetCat) ||
    Boolean(task.human_only);
  const autoShim =
    prof.auto_shim_categories.includes(spec.category_id) ||
    prof.auto_shim_categories.includes(targetCat);
  const wg = spec.worker_guidance;

  let rawText = "";
  if (prof.response_format === "compact_signatures") {
    rawText = [
      `TASK ${taskId} [${spec.category_id} -> ${targetCat}] tier=${effTier} budget_usd=${effBudget.toFixed(4)}`,
      `obj: ${objective}`,
      `mcp: ${wg.required_mcp_tools.join(", ")}`,
      `0call_gates: ${wg.deterministic_checks_before_llm.join(" | ")}`,
    ].join("\n");
  } else if (prof.response_format === "plain_language_caveats") {
    rawText = [
      `Task Guidance (${prof.title}): ${spec.title} [${taskId}]`,
      `Goal: ${objective}`,
      `Execution Mode: ${effTier} (budget cap $${effBudget.toFixed(2)} USD).`,
      `Before Calling Any Model: ${wg.deterministic_checks_before_llm.join(" ")}`,
    ].join("\n");
  } else {
    rawText = canonicalJson({
      auto_shim_active: autoShim,
      category_id: spec.category_id,
      cost_clamped: costClamped,
      effective_budget_usd: effBudget,
      execution_tier: effTier,
      profile_id: prof.profile_id,
      requires_human_gate: requiresHuman,
      task_id: taskId,
    });
  }

  const [rendered, wasClamped] = _clampText(rawText, prof.clamped_max_chars);
  return {
    task_id: taskId,
    category_id: spec.category_id,
    profile_id: prof.profile_id,
    response_format: prof.response_format,
    effective_execution_tier: effTier,
    cost_clamped: costClamped,
    effective_budget_usd: effBudget,
    requires_human_gate: requiresHuman,
    auto_shim_active: autoShim,
    required_mcp_tools: wg.required_mcp_tools,
    deterministic_checks_before_llm: wg.deterministic_checks_before_llm,
    post_condition_receipt_rules: wg.post_condition_receipt_rules,
    clamped_max_chars: prof.clamped_max_chars,
    was_clamped: wasClamped,
    rendered_guidance: rendered,
  };
}

// Snake-case aliases matching Python API names
export const validate_category_payload = validateCategoryPayload;
export const empirical_bayes_mean = empiricalBayesMean;
export const dirichlet_posterior_probs = dirichletPosteriorProbs;
export const wilson_lower_bound = wilsonLowerBound;
export const compute_composite_rank = computeCompositeRank;
export const rank_items = rankItems;
export const pareto_frontier = paretoFrontier;
export const freshness_confidence = freshnessConfidence;
export const needs_recheck = needsRecheck;
export const plan_fragility_rechecks = planFragilityRechecks;
export const evaluate_recheck_observation = evaluateRecheckObservation;
export const reconcile_three_way = reconcileThreeWay;
export const compute_worker_calibration = computeWorkerCalibration;
export const adjudicate_ballots = adjudicateBallots;
export const adapt_noun_view = adaptNounView;
export const adapt_task_guidance = adaptTaskGuidance;
