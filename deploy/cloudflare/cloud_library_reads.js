// Reads of the library stores, with or without a key. Proposals are private: anyone may list them
// and read a status stub (id, state, kind, size, times, digests, receipts); only the proposer, a
// library.review key or an admin sees a proposal's content, decided or not, and no read serves an
// older version. The catalog and the instruction nouns are public.
import { LIBRARY, libraryAnonymousEnabled, libraryBudgets, libraryEnabled } from "./library_contract.js";
import { head, proposalView } from "./cloud_library.js";

const NS = "main";
const PROPOSAL_RE = /^prop-[a-f0-9]{32}$/;
const CATALOG_KEY_RE = /^(?:noun|verb|pack):[a-z0-9][a-z0-9.-]{0,200}$/;
const SLUG = /^[a-z0-9]+(?:[-.][a-z0-9]+)*$/;
const err = (status, code, detail) => Object.assign(new Error(detail), { status, code, detail, extra: {} });

function page(c, store, filters, after, limit, columns, publicOnly = true) {
  const where = ["h.namespace=?", "h.store=?", "h.key>?"], params = [NS, store, after];
  if (publicOnly) where.push("h.visibility_scope='public_commons'");
  for (const tag of filters) { where.push("EXISTS (SELECT 1 FROM noun_tags t WHERE t.namespace=h.namespace AND t.noun_kind='record' AND t.container=h.store AND t.noun_id=h.key AND t.tag=?)"); params.push(tag); }
  const rows = c._all(`SELECT h.key AS key, h.version AS version, r.created_at AS created_at, ${columns} FROM record_heads h JOIN records r ON r.namespace=h.namespace AND r.store=h.store AND r.key=h.key AND r.version=h.version WHERE ${where.join(" AND ")} ORDER BY h.key LIMIT ?`, ...params, limit);
  return { items: rows, next: rows.length === limit ? rows.at(-1).key : null };
}

// Who may see a proposal's content: its proposer, a keyed principal that may claim library.review,
// or an admin. Anyone else, and a request without a key, gets the stub.
async function scopeFor(c, secret, proposal) {
  if (!secret) return "public";
  const raw = await c.resolveKeyGrant(secret).catch(() => null);
  if (!raw) return "public";
  const grant = c._hydrateGrant(raw);
  if (grant.key_id === proposal.proposer?.key_id || grant.is_admin) return "full";
  if (!["anonymous", "requester"].includes(grant.principal_kind) && grant.allowsQueue(LIBRARY.review_queue)) return "full";
  return "public";
}

export async function readLibrary(c, what, args = {}, secret = null) {
  c.namespace = NS;
  c.isPrimary = true;
  try {
    c._ensureSchema();
    const limit = Math.max(1, Math.min(Number(args.limit) || 20, 50)), after = String(args.after || "");
    if (what === "overview") {
      const statuses = Object.fromEntries(c._all("SELECT tag, COUNT(*) n FROM noun_tags WHERE namespace=? AND noun_kind='record' AND container=? AND tag LIKE 'status:%' GROUP BY tag", NS, LIBRARY.proposals).map(r => [r.tag.slice(7), Number(r.n)]));
      const items = Object.fromEntries(c._all("SELECT tag, COUNT(*) n FROM noun_tags WHERE namespace=? AND noun_kind='record' AND container=? AND tag LIKE 'item:%' GROUP BY tag", NS, LIBRARY.catalog).map(r => [r.tag.slice(5), Number(r.n)]));
      const day = Math.floor(Date.now() / 86400000) * 24;
      const used = subject => Number(c._one("SELECT bytes FROM access_limits WHERE subject=? AND window=?", subject, day)?.bytes || 0);
      const refused = Object.fromEntries(c._all("SELECT subject, claims FROM access_limits WHERE subject LIKE 'library-refused:%' AND window=?", day).map(r => [r.subject.slice(16), Number(r.claims)]));
      const limits = libraryBudgets(c.env);
      return { ok: true, status: 200, data: { proposals: statuses, catalog: items, enabled: libraryEnabled(c.env), anonymous_enabled: libraryAnonymousEnabled(c.env),
        budgets_today: { bytes: used("library-bytes:all"), daily_bytes: limits.daily_bytes, anonymous_bytes: used("library-bytes:anonymous"), anonymous_daily_bytes: limits.anonymous_daily_bytes, account_daily_bytes: limits.account_daily_bytes, refused } } };
    }
    if (what === "proposals") {
      const status = args.status ? String(args.status) : null;
      if (status && !["proposed", "admitted", "rejected"].includes(status)) throw err(400, "invalid_request", "status is proposed, admitted or rejected");
      const out = page(c, LIBRARY.proposals, status ? [`status:${status}`] : [], after, limit,
        "json_extract(r.value,'$.status') AS status, json_extract(r.value,'$.type') AS type, json_extract(r.value,'$.item_bytes') AS item_bytes, json_extract(r.value,'$.item_sha256') AS item_sha256, json_extract(r.value,'$.door') AS door, json_extract(r.value,'$.proposer.principal_kind') AS proposer_kind, json_extract(r.value,'$.propose.completed_at') AS proposed_at, json_extract(r.value,'$.decision.decided_at') AS decided_at", false);
      return { ok: true, status: 200, data: { proposals: out.items.map(({ key, version, created_at, ...r }) => ({ proposal_id: key, ...r, url: `/v1/library/proposals/${key}` })), next: out.next } };
    }
    if (what === "proposal") {
      const id = String(args.proposal_id || "");
      const h = PROPOSAL_RE.test(id) ? head(c, LIBRARY.proposals, id) : null;
      if (!h) throw err(404, "not_found", "no such proposal");
      return { ok: true, status: 200, data: proposalView(c, id, await scopeFor(c, secret, h.value)) };
    }
    if (what === "catalog") {
      const filters = [];
      if (args.kind) { if (!["noun", "verb_template", "pack"].includes(String(args.kind))) throw err(400, "invalid_request", "kind is Task Object, verb_template or pack"); filters.push(`item:${args.kind}`); }
      if (args.pack) { if (!SLUG.test(String(args.pack))) throw err(400, "invalid_request", "pack is a pack id"); filters.push(`pack:${args.pack}`); }
      const out = page(c, LIBRARY.catalog, filters, after, limit,
        "json_extract(r.value,'$.item_kind') AS item_kind, json_extract(r.value,'$.pack') AS pack, json_extract(r.value,'$.version') AS item_version, coalesce(json_extract(r.value,'$.item.title'), json_extract(r.value,'$.item.source.title')) AS title, json_extract(r.value,'$.license') AS license, json_extract(r.value,'$.item_sha256') AS item_sha256, json_extract(r.value,'$.proposal_id') AS proposal_id, json_extract(r.value,'$.review_receipt') AS review_receipt, json_extract(r.value,'$.reviewer_key_id') AS reviewer_key_id");
      return { ok: true, status: 200, data: { items: out.items.map(r => ({ ...r, url: `/v1/library/catalog/${encodeURIComponent(r.key)}` })), next: out.next } };
    }
    if (what === "catalog_item") {
      const key = String(args.key || "");
      const h = CATALOG_KEY_RE.test(key) ? head(c, LIBRARY.catalog, key) : null;
      if (!h) throw err(404, "not_found", "no such catalog item");
      return { ok: true, status: 200, data: { ...h.value, catalog_version: h.version, created_at: h.created_at } };
    }
    if (what === "instructions") {
      const out = page(c, LIBRARY.instructions, [], after, limit, "r.value AS value");
      return { ok: true, status: 200, data: { instructions: out.items.map(r => ({ key: r.key, version: Number(r.version), value: JSON.parse(r.value) })), next: out.next } };
    }
    throw err(404, "not_found", "no such library resource");
  } catch (e) { return c._errOut(e); }
}
