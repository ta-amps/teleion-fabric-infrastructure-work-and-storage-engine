import assert from "node:assert/strict";
import { test } from "node:test";
import { ClaimAdmission, ClaimPressure, IdleClaims, claimAdmissionOptions, emptyClaimRetryAfter } from "../deploy/cloudflare/claim_admission.js";

test("healthy keyed traffic has no claims-per-minute ceiling", async () => {
  const gate = new ClaimAdmission({}, () => 0);
  for (let i = 0; i < 1000; i++) (await gate.acquire("same-key")).release();
  assert.equal(gate.active, 0);
  assert.equal(gate.pending, 0);
  assert.equal(gate.queues.size, 0);
});

test("bursts wait, rotating principals without losing FIFO within a principal", async () => {
  const gate = new ClaimAdmission({ initial: 1, maximum: 1 });
  let permit = await gate.acquire("first");
  const a1 = gate.acquire("a"), a2 = gate.acquire("a"), b = gate.acquire("b");
  const order = [];
  a1.then(() => order.push("a1")); a2.then(() => order.push("a2")); b.then(() => order.push("b"));
  permit.release(); permit = await a1;
  permit.release(); permit = await b;
  permit.release(); permit = await a2;
  permit.release(); permit.release();
  assert.deepEqual(order, ["a1", "b", "a2"]);
  assert.equal(gate.active, 0);
});

test("measured latency reduces concurrency, then healthy saturated work recovers", async () => {
  let now = 0;
  const gate = new ClaimAdmission({ initial: 8, maximum: 10, targetMs: 100 }, () => now);
  const first = await gate.acquire("a"); now += 1000; first.release();
  assert.equal(gate.limit, 5);
  for (let i = 0; i < 40; i++) {
    const permits = await Promise.all(Array.from({ length: gate.limit }, () => gate.acquire("a")));
    now += 10;
    permits.forEach(p => p.release()); now += 100;
  }
  assert.equal(gate.limit, 10);
  assert.equal(gate.active, 0);
});

test("pending memory and deadlines are bounded, expired requests never acquire", async () => {
  const gate = new ClaimAdmission({ initial: 1, maximum: 1, maxPending: 1, maxWaitMs: 10 });
  const first = await gate.acquire("a");
  const waiting = gate.acquire("a");
  await assert.rejects(gate.acquire("b"), e => e instanceof ClaimPressure && e.retryAfter >= 1);
  await assert.rejects(waiting, /pacing deadline/);
  first.release();
  assert.equal(gate.active, 0); assert.equal(gate.pending, 0); assert.equal(gate.queues.size, 0);
  (await gate.acquire("b")).release();
});

test("late event-loop resumption cannot grant a stale waiter", async () => {
  let now = 0;
  const gate = new ClaimAdmission({ initial: 1, maximum: 1, maxWaitMs: 100 }, () => now);
  const first = await gate.acquire("a"), waiting = gate.acquire("b");
  now = 101; first.release();
  await assert.rejects(waiting, /pacing deadline/);
  assert.equal(gate.active, 0); assert.equal(gate.pending, 0);
});

test("configuration rejects zero, negative, nonfinite and inverted resource bounds", () => {
  for (const value of ["0", "-1", "NaN", "Infinity", "1.5", ""]) {
    assert.throws(() => claimAdmissionOptions({ CLAIM_MAX_PENDING: value }));
  }
  assert.throws(() => claimAdmissionOptions({ CLAIM_INITIAL_CONCURRENCY: "4", CLAIM_MAX_CONCURRENCY: "3" }));
});

test("empty-claim hints double while a key finds the queue empty and reset on work", () => {
  assert.deepEqual([1, 2, 3, 4, 5, 6, 50].map(emptyClaimRetryAfter), [5, 10, 20, 40, 60, 60, 60]);
  const idle = new IdleClaims();
  assert.deepEqual([1, 2, 3].map(() => idle.empty("main", "q", "k1")), [5, 10, 20]);
  assert.equal(idle.empty("main", "q", "k2"), 5); // each key paces itself
  assert.equal(idle.empty("other", "q", "k1"), 5); // and each namespace
  idle.granted("main", "q", "k1");
  assert.equal(idle.empty("main", "q", "k1"), 5);
  idle.empty("main", "q", "k1");
  idle.work("main", "q");
  assert.equal(idle.empty("main", "q", "k1"), 5);
  assert.equal(idle.empty("main", "q", "k2"), 5);
});

test("one principal cannot hold every waiting slot while another principal waits", async () => {
  const gate = new ClaimAdmission({ initial: 1, maximum: 1, maxPending: 4, maxWaitMs: 5000, targetMs: 500 });
  const held = await gate.acquire("holder");
  const outcome = promise => promise.then(permit => (permit.release(), "granted"), error => error instanceof ClaimPressure ? error.message : "error");
  const noisy = Array.from({ length: 4 }, () => outcome(gate.acquire("noisy")));
  assert.equal(gate.pending, 4);
  // Alone, a principal may still use every slot; the total never grows.
  assert.match(await outcome(gate.acquire("noisy")), /pending queue full/);
  const quiet = outcome(gate.acquire("quiet"));
  assert.equal(gate.pending, 4);
  held.release();
  assert.equal(await quiet, "granted");
  const results = await Promise.all(noisy);
  assert.equal(results.filter(r => r === "granted").length, 3);
  assert.equal(results.filter(r => /fair share/.test(r)).length, 1);
  assert.equal(gate.pending, 0);
  assert.equal(gate.active, 0);
});
