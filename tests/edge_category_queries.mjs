// Actual coordinator, SQL and Worker ranking handlers; temporary example data only.
import assert from "node:assert/strict";
import {DatabaseSync} from "node:sqlite";
import worker, {TesseraCoordinator} from "../deploy/cloudflare/worker.js";
import {canonicalCategoryTags, canonicalCategoryId} from "../deploy/cloudflare/cloud_intelligence.js";
import {normalizeTag} from "../deploy/cloudflare/ontology_engine.js";

const db = new DatabaseSync(":memory:"), pending = [];
const ctx = {
  blockConcurrencyWhile(fn) { pending.push(Promise.resolve().then(fn)); },
  waitUntil(p) { pending.push(p); },
  storage: {
    async setAlarm() {}, async getAlarm() {return null;},
    transactionSync(fn) {
      db.exec("SAVEPOINT category_test");
      try { const result = fn(); db.exec("RELEASE category_test"); return result; }
      catch(e) { db.exec("ROLLBACK TO category_test; RELEASE category_test"); throw e; }
    },
    sql: {exec(query, ...params) {
      const stmt = db.prepare(query);
      const args = params.map(v => v === undefined ? null : typeof v === "boolean" ? Number(v) : v);
      if (stmt.columns().length) return {toArray: () => stmt.all(...args)};
      stmt.run(...args); return {toArray: () => []};
    }},
  },
};
const env = {STORAGE_MODE:"cloud",PUBLIC_URL:"https://tessera.example"};
const coord = new TesseraCoordinator(ctx, env);
await Promise.all(pending);
env.COORDINATOR = {idFromName:n=>n,get:()=>coord};
const adm = await coord.bootstrapOrIssueKey("", true, {label:"example",queues:["*"],scopes:["admin","store"]});
assert.equal(adm.ok, true);
const token = adm.data.key;
const reader = await coord.bootstrapOrIssueKey(token, false, {label:"example-reader",queues:["conformance-query"],scopes:["store"]});
assert.equal(reader.ok, true);
const call = (action,args,key=token) => coord.rpcCall(key,action,args);
const put = (key,value,tags=[]) => call("putRecords",{writes:[{store:"examples",key,value,tags,expected_version:0}]});
assert.equal(canonicalCategoryId(" CAT:Noun.Code_Node "), "noun.code_node");
assert.equal(normalizeTag("category:noun.code_node"),"category:noun.code_node");
assert.deepEqual(canonicalCategoryTags({category_id:"noun.code_node"},["industry:ngo","domain:legal"]),["category:noun.code_node","industry:ngo","domain:legal"]);
assert.deepEqual(canonicalCategoryTags({category_id:"noun.code_node"},["cat:noun.code_node"]),["category:noun.code_node"]);
for (const [name,value,tags] of [
  ["id",{category_id:"noun.mcp_server"},[]],
  ["alias",{category:"cat:NOUN.MCP_SERVER"},[]],
  ["tag",{},["cat:noun.mcp_server"]],
]) assert.equal((await put(name,value,tags)).ok,true);
for (const [value,tags] of [
  [{category_id:"noun.code_node",category:"noun.feed_source"},[]],
  [{category_id:"noun.code_node"},["cat:noun.feed_source"]],
  [{category_id:"noun.code_node",tags:["category:noun.feed_source"]},[]],
  [{category_id:"noun.code_node",metadata_tags:{category:"noun.feed_source"}},[]],
]) {
  const out = await put("bad",value,tags);
  assert.equal(out.status,400); assert.match(out.detail,/conflicting category/);
}
assert.equal((await call("putRecords",{writes:[
  {store:"examples",key:"rollback",value:{category_id:"noun.code_node"},expected_version:0},
  {store:"examples",key:"bad",value:{category_id:"noun.code_node"},tags:["category:noun.feed_source"],expected_version:0},
]})).status,400);
assert.equal(db.prepare("SELECT COUNT(*) AS n FROM records WHERE key='rollback'").get().n,0);

const common = {example_only:true,trials:1,successes:1};
assert.equal((await put("z-target",{...common,category_id:"noun.mcp_server"})).ok,true);
assert.equal((await put("legacy-conflict",{category_id:"noun.feed_source"})).ok,true);
for (const table of ["records","record_heads"]) {
  db.prepare(`UPDATE ${table} SET tags='[]' WHERE store='examples' AND key='z-target'`).run();
  db.prepare(`UPDATE ${table} SET tags='["category:noun.mcp_server"]' WHERE store='examples' AND key='legacy-conflict'`).run();
}
db.prepare("DELETE FROM noun_tags WHERE container='examples' AND noun_id='z-target'").run();
assert.equal((await call("putRecords",{writes:Array.from({length:1001},(_,i)=>({store:"examples",key:`a-${String(i).padStart(4,"0")}`,value:{...common,category_id:"noun.feed_source"},expected_version:0}))})).ok,true);
assert.equal((await call("putRecords",{writes:[{store:"examples",key:"private",value:{...common,category_id:"noun.mcp_server"},expected_version:0,visibility_scope:"private"}]})).ok,true);
for (const [action,field] of [["listRecords","records"],["searchNouns","scents"]]) {
  const out=await call(action,{store:"examples",category:"noun.mcp_server"},reader.data.key);
  assert.equal(out.ok,true,JSON.stringify(out));
  assert.deepEqual(out.data[field].map(r=>r.key).sort(),["alias","id","tag","z-target"]);
}
const rank = await worker.fetch(new Request("https://tessera.example/v1/rankings/noun.mcp_server?store=examples",{headers:{Authorization:"Bearer "+reader.data.key}}),env,{waitUntil:p=>pending.push(p)});
assert.equal(rank.status,200);
const rankBody=await rank.json();
assert.deepEqual(rankBody.ranked.map(r=>r.id),["z-target"]);

assert.equal((await call("defineBucket",{name:"examples"})).ok,true);
const object = (key,metadata,tags=[]) => call("putObjectMeta",{bucket:"examples",key,metadata,tags,sha256:"a".repeat(64),size:1,expected_version:0});
assert.equal((await object("z-target.txt",{category_id:"noun.code_node"})).ok,true);
assert.equal((await object("bad.txt",{category_id:"noun.code_node"},["cat:noun.feed_source"])).status,400);
for(let i=0;i<1001;i++) assert.equal((await object(`a-${String(i).padStart(4,"0")}.txt`,{category_id:"noun.feed_source"})).ok,true);
for (const [action,field] of [["listObjects","objects"],["searchNouns","scents"]]) {
  const out=await call(action,{bucket:"examples",category:"noun.code_node"},reader.data.key);
  assert.equal(out.ok,true,JSON.stringify(out)); assert.deepEqual(out.data[field].map(r=>r.key),["z-target.txt"]);
}
await Promise.all(pending);
db.close();
process.stdout.write(JSON.stringify({passed:true}));
