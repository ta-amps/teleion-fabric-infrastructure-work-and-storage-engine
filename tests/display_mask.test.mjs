// The board hides banned words in stored data at display time; stored records stay as they are. The real patterns
// come from the shared list at deploy time, so this test uses a made-up one.
import assert from "node:assert/strict";
import test from "node:test";
import { renderBoardHtml } from "../deploy/cloudflare/cloud_pages.js";
import { displayMask } from "../deploy/cloudflare/display_mask.js";

const health = { ok: true, queues: [] };
const snapshot = { namespace: "main", events: [{ seq: 7, type: "promoted", queue: "q", task_id: "t-zzneverword" }],
  feed_entries: [{ seq: 1, feed: "ns", kind: "delta", summary: "Promoted zzneverword proposal" }] };

test("stored banned words are masked on the board when the deploy-time list is set", () => {
  const mask = displayMask({ BANNED_TEXT: JSON.stringify({ always: ["\\bzzneverword\\b"], visible: [] }) });
  const html = renderBoardHtml(health, snapshot, "https://example.invalid", mask);
  assert.ok(!html.includes("zzneverword"));
  assert.ok(html.includes("Promoted … proposal") || html.includes("…"));
});

test("without the list nothing is masked, so the Python board stays identical", () => {
  const plain = renderBoardHtml(health, snapshot, "https://example.invalid");
  assert.equal(renderBoardHtml(health, snapshot, "https://example.invalid", displayMask({})), plain);
  assert.equal(renderBoardHtml(health, snapshot, "https://example.invalid", displayMask({ BANNED_TEXT: "not json" })), plain);
});
