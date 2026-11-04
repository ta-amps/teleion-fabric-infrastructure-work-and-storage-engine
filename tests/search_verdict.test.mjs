import assert from "node:assert/strict";
import test from "node:test";
import { searchVerdict } from "../deploy/cloudflare/cloud_intelligence.js";

const scent = (key, title = "") => ({ key, title });

test("a name with nothing like it says none, and offers the one call that looks further", () => {
  const out = searchVerdict("someone/vist", [scent("bench.github:other:thing", "Other thing")]);
  assert.equal(out.match, "none");
  assert.match(out.note, /No match/);
  assert.equal(out.repo_lookup, "https://shim.teleionlabs.com/q/repos?repo=someone/vist");
});

test("a key that matches exactly, and a name in the key or title, are matches", () => {
  assert.equal(searchVerdict("models.meta", [scent("models.meta")]).match, "exact_id");
  assert.equal(searchVerdict("oh-my-pi", [scent("bench.github:can1357:oh-my-pi")]).match, "name");
});

test("plain words keep the search's own results and no name lookup", () => {
  const out = searchVerdict("agent memory", [scent("x")]);
  assert.equal(out.match, "words");
  assert.equal(out.repo_lookup, undefined);
});

test("no results is none; an empty query adds nothing; a token shape is never sent on", () => {
  assert.equal(searchVerdict("practice", []).match, "none");
  assert.deepEqual(searchVerdict("", []), {});
  assert.equal(searchVerdict("ghp_abcdefghijklmnop", []).repo_lookup, undefined);
});
