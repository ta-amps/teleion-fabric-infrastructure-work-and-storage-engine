import test from "node:test";
import assert from "node:assert/strict";
import { publicNames, withPublicNames } from "../deploy/cloudflare/public_names.js";

test("people and agents read the public names and addresses", () => {
  assert.equal(publicNames("Tessera is a queue. HERMA reads Herma's rules."), "Teleion Fabric is a queue. MODEL SHIM reads the model shim's rules.");
  assert.equal(publicNames("curl https://tessera.ta-amps.workers.dev/v1/health"), "curl https://fabric.teleionlabs.com/v1/health");
  assert.equal(publicNames("see https://herma.teleionlabs.com/q/pulse"), "see https://shim.teleionlabs.com/q/pulse");
  assert.equal(publicNames("send X-Tessera-Namespace or X-Herma-Key"), "send X-Teleion-Fabric-Namespace or X-Shim-Key");
});

test("machine names change in keys and listed values only", () => {
  assert.equal(publicNames('{"tessera_access":{"mode":"d1_authoritative_with_tessera_summary_mirror"},"surface":"herma"}'), '{"fabric_access":{"mode":"d1_authoritative_with_fabric_summary_mirror"},"surface":"shim"}');
  assert.equal(publicNames('"herma.priority_work_gateway.grid.v6_8"'), '"shim.priority_work_gateway.grid.v6_8"');
  assert.equal(publicNames("surface:tessera noun:tessera.lease_fence tessera://blobs/x"), "surface:fabric noun:fabric.lease_fence fabric://blobs/x");
});

test("ids an agent sends back, repo addresses and file paths are left alone", () => {
  for (const kept of ["muse-herma-worker-01", "w_agent_muse-herma-worker-01", "peer_review_muse-herma-worker-01_v1", "verb_tessera_door_v1", "https://github.com/ta-amps/tessera-infrastructure-work-and-storage-engine/blob/main/x.md", "/assets/herma-pages.css?v=1", "/kits/herma_shim.py", "data-herma-preset"]) {
    assert.equal(publicNames(kept), kept);
  }
});

test("a valid JSON answer stays valid JSON", () => {
  const body = JSON.stringify({ tessera_x: "Tessera says \"hi\"", list: ["herma", { herma_y: 1 }] });
  assert.doesNotThrow(() => JSON.parse(publicNames(body)));
});

test("a caller with a key gets the stored names; digest routes are skipped; bodies are renamed otherwise", async () => {
  const answer = () => new Response('{"tessera_a":"Tessera"}', { headers: { "content-type": "application/json", "www-authenticate": 'Bearer realm="tessera"' } });
  const open = new Request("https://x/v1/health");
  const keyed = new Request("https://x/v1/health", { headers: { authorization: "Bearer k" } });
  assert.equal(await (await withPublicNames(keyed, answer())).text(), '{"tessera_a":"Tessera"}');
  assert.equal(await (await withPublicNames(new Request("https://x/v1/blobs/a"), answer(), { skip: ["/v1/blobs"] })).text(), '{"tessera_a":"Tessera"}');
  const out = await withPublicNames(open, answer());
  assert.equal(await out.text(), '{"fabric_a":"Teleion Fabric"}');
  assert.ok(!/tessera/i.test(out.headers.get("www-authenticate")));
});
