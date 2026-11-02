import assert from 'node:assert/strict';
import { initExecutions } from '../deploy/cloudflare/cloud_executions.js';
const report = {version:1,harness:{name:'example-node'},model:{name:'none'},summaries:{problem:'EXAMPLE add two integers',input:'one and two',solution:'three'},contract:{input_schema:{type:'object'},output_schema:{type:'object'},context_hash:'a'.repeat(64),environment_hash:'b'.repeat(64),toolchain_hash:'c'.repeat(64)},usage:{source:'deterministic_fixture',input_tokens:0,output_tokens:0}};
async function setup(h) {
  const operator=await h.admin(),owner=await h.key(operator,'example-owner'),other=await h.key(operator,'example-other');
  assert.equal((await h.post('/v1/queues/define',{name:'conformance-agent'},operator.key)).status,200);
  return {operator,owner,other};
}
async function claim(h,token,queue='conformance-agent',extra={}) {
  const r=await h.post('/v1/tasks',{queue,kind:'example',brief:{example_only:true,a:1,b:2},visibility_scope:'private',...extra},token);
  assert.equal(r.status,201,JSON.stringify(r.data));
  const c=await h.post('/v1/tasks/claim',{queue,worker:'example-node'},token);
  assert.equal(c.status,200,JSON.stringify(c.data));return c.data;
}
const fields=l=>({task_id:l.task_id,fencing_token:l.fencing_token});
async function submit(h,token,l,extra={}) {
  return h.post('/v1/tasks/submit',{...fields(l),output:{sum:3},execution_report:report,...extra},token,{'Idempotency-Key':l.task_id});
}
async function run(h,token,l) {
  const list=await h.req(`/v1/tasks/${l.task_id}/executions`,{token});
  assert.equal(list.status,200,JSON.stringify(list.data));assert.equal(list.data.executions.length,1);
  const get=await h.req('/v1/executions/'+list.data.executions[0].run_id,{token});
  assert.equal(get.status,200,JSON.stringify(get.data));return get.data;
}
export async function executionHistoryTests(test) {
  await test('execution Worker full loop, exact recall, discovery and first report immutable',async h=>{
    const {owner}=await setup(h),l=await claim(h,owner.key);
    assert.equal((await submit(h,owner.key,l)).status,200);
    const first=await run(h,owner.key,l);
    const audit=await h.req('/v1/me/audit',{token:owner.key});
    const event=audit.data.events.find(e=>e.action==='getExecution');assert.equal(event.resource_id,first.run_id);assert.equal(event.resource_type,'noun');assert.equal(event.receipt_id,first.receipt_hash);
    assert.equal(first.report.usage.input_tokens,0);assert.equal(first.trust.correctness,'not_independently_verified');
    const replay=await submit(h,owner.key,l,{execution_report:{summaries:{solution:'replacement'}}});
    assert.equal(replay.status,200);assert.equal(replay.data.replayed,true);
    assert.deepEqual((await run(h,owner.key,l)).report,first.report);
    const next=await claim(h,owner.key),recall=await h.post('/v1/executions/recall',{...fields(next),execution_report:report},owner.key);
    assert.equal(recall.status,200,JSON.stringify(recall.data));
    assert.equal(recall.data.matches[0].match_kind,'exact_input');assert.equal(recall.data.matches[0].execution.run_id,first.run_id);
    assert.equal(recall.data.coverage.complete,false);assert.equal(recall.data.reuse_requires_validation,true);
    const bad=await h.post('/v1/executions/recall',{...fields(next),limit:'5'},owner.key);assert.equal(bad.status,400);
  });
  await test('execution Worker source ownership, queues, namespace and holder isolation',async h=>{
    const {owner,other,operator}=await setup(h),l=await claim(h,owner.key);
    assert.equal((await submit(h,owner.key,l)).status,200);const first=await run(h,owner.key,l);
    assert.equal((await h.req('/v1/executions/'+first.run_id,{token:other.key})).status,404);
    assert.equal((await h.req(`/ns/conformance-other/v1/executions/${first.run_id}`,{token:owner.key})).status,403);
    const reader=await h.key(operator,'example-reader',{queues:['elsewhere']});
    assert.equal((await h.req('/v1/executions/'+first.run_id,{token:reader.key})).status,404);
    const next=await claim(h,owner.key,'conformance-agent',{visibility_scope:'public_commons'});
    assert.equal((await h.post('/v1/executions/recall',{...fields(next),execution_report:report},other.key)).status,403);
  });
  await test('execution Worker stale submission and malformed telemetry leave no history',async h=>{
    const {owner}=await setup(h),l=await claim(h,owner.key);
    assert.equal((await submit(h,owner.key,{...l,fencing_token:l.fencing_token+1})).status,409);
    assert.equal((await submit(h,owner.key,l,{execution_report:{usage:{source:'fixture',input_tokens:true}}})).status,400);
    const list=await h.req(`/v1/tasks/${l.task_id}/executions`,{token:owner.key});assert.deepEqual(list.data.executions,[]);
    assert.equal((await submit(h,owner.key,l)).status,200);
    assert.equal((await h.post('/v1/executions/recall',{...fields(l),execution_report:report},owner.key)).status,409);
  });
  await test('execution Worker private artifact revocation hides history without breaking receipt replay',async h=>{
    const {owner,operator}=await setup(h),l=await claim(h,owner.key);
    const p=await h.post('/v1/stores/history.logs/records/log',{expected_version:0,value:{example_only:true},visibility_scope:'private'},owner.key);assert.equal(p.status,200);
    const logged={...report,artifact_refs:[{store:'history.logs',key:'log',version:1}]};
    assert.equal((await submit(h,owner.key,l,{execution_report:logged})).status,200);const first=await run(h,owner.key,l);
    assert.equal((await h.post('/v1/stores/history.logs/records/log',{expected_version:1,value:{example_only:true,restricted:true},visibility_scope:'private'},operator.key)).status,200);
    assert.equal((await h.req('/v1/executions/'+first.run_id,{token:owner.key})).status,404);
    assert.equal((await submit(h,owner.key,l,{execution_report:logged})).status,200);
  });
  await test('execution Worker anonymous labels, recall and complete namespace purge',async h=>{
    const a=await h.mint(),b=await h.mint(),token=a.session_token;
    const l=await claim(h,token,'demo.echo');
    assert.equal((await submit(h,token,l)).status,200);const first=await run(h,token,l);
    assert.equal(first.labels.origin,'anonymous');assert.ok(first.expires_at>first.created_at);
    assert.equal((await h.req('/v1/executions/'+first.run_id,{token:b.session_token})).status,404);
    const next=await claim(h,token,'demo.echo',{brief:l.brief,kind:l.kind});
    const recall=await h.post('/v1/executions/recall',{...fields(next),execution_report:report},token);assert.equal(recall.status,200,JSON.stringify(recall.data));
    const entry=[...h.entries.values()].find(e=>e.db.prepare('SELECT namespace FROM execution_runs LIMIT 1').get()?.namespace===a.namespace);
    assert.ok(entry);entry.db.prepare("UPDATE meta SET value='1' WHERE key='demo_purge_at'").run();await entry.instance.alarm();
    assert.equal(entry.db.prepare('SELECT COUNT(*) n FROM execution_runs').get().n,0);assert.equal(entry.db.prepare('SELECT COUNT(*) n FROM execution_bands').get().n,0);
  });
  await test('execution Worker failed attempts are warnings, not reusable success',async h=>{
    const {owner}=await setup(h),l=await claim(h,owner.key);
    assert.equal((await submit(h,owner.key,l,{ok:false,error:'example failure'})).status,200);
    const next=await claim(h,owner.key),r=await h.post('/v1/executions/recall',{...fields(next),execution_report:report},owner.key);
    assert.equal(r.status,200,JSON.stringify(r.data));assert.equal(r.data.matches[0].match_kind,'failure_warning');assert.equal(r.data.matches[0].execution.ok,false);
  });
  await test('execution Worker telemetry cannot smuggle PII past anonymous content boundary',async h=>{
    const a=await h.mint(),l=(await h.post('/v1/tasks/claim',{queue:'demo.ping',worker:'example'},a.session_token)).data;
    const bad=await submit(h,a.session_token,l,{execution_report:{summaries:{problem:'example@invalid.test'}}});assert.equal(bad.status,400);assert.equal(bad.data.code,'example_only');
    const list=await h.req(`/v1/tasks/${l.task_id}/executions`,{token:a.session_token});assert.deepEqual(list.data.executions,[]);
  });
  await test('execution Worker upgrade restricts legacy derived heads without deleting content',async h=>{
    const {owner,other,operator}=await setup(h);
    assert.equal((await h.post('/v1/queues/define',{name:'conformance-agent',max_attempts:1},operator.key)).status,200);
    const l=await claim(h,owner.key);
    assert.equal((await submit(h,owner.key,l,{ok:false,error:'EXAMPLE private diagnostic'})).status,200);
    const entry=[...h.entries.values()].find(e=>e.db.prepare("SELECT key FROM record_heads WHERE store='negative.knowledge' AND key=?").get(l.task_id));
    assert.ok(entry);
    entry.db.prepare("UPDATE record_heads SET visibility_scope='public_commons' WHERE store='negative.knowledge' AND key=?").run(l.task_id);
    entry.db.prepare("DELETE FROM meta WHERE key='task_tombstone_privacy_v1'").run();
    initExecutions(entry.instance);initExecutions(entry.instance);
    assert.equal((await h.req('/v1/stores/negative.knowledge/records/'+l.task_id,{token:other.key})).status,404);
    const own=await h.req('/v1/stores/negative.knowledge/records/'+l.task_id,{token:owner.key});assert.equal(own.status,200);assert.equal(own.data.value.reason,'EXAMPLE private diagnostic');
  });
  await test('execution Worker tools discovery includes typed history operations',async h=>{
    const r=await h.post('/mcp',{jsonrpc:'2.0',id:1,method:'tools/list'});
    const names=r.data.result.tools.map(t=>t.name);
    for(const name of ['recall_execution','get_execution','list_executions']) assert.ok(names.includes(name));
    const card=await h.req('/.well-known/agent-card.json');assert.equal(card.status,200);
  });
}
