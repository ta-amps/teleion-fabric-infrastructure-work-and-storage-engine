// Execution nouns are immutable result evidence, never generic CAS rows or an alternate queue.
import { canonicalJson, sha256Hex } from './cloud_buckets.js';
import { normalize_report, fingerprints, compare } from './execution_matching.js';
import { categoryFromTag } from './cloud_intelligence.js';
const LIMIT = 500;
const GOV = ['id','queue','producer_surface','visibility_scope','allowed_surfaces','target_cohorts','tags','owner_key_id'];
const enc = new TextEncoder();
const hash = value => sha256Hex(enc.encode(canonicalJson(value)));
const err = (status, code, detail) => Object.assign(new Error(detail), {status, code, detail});
const missing = () => err(404,'not_found','execution does not exist');
const bad = detail => err(400,'invalid_request',detail);
export function initExecutions(c) {
  for (const sql of [
    `CREATE TABLE IF NOT EXISTS execution_runs(namespace TEXT NOT NULL,run_id TEXT NOT NULL,task_id TEXT NOT NULL,fencing_token INTEGER NOT NULL,receipt_hash TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL,input_hash TEXT,exact_hash TEXT,shape_hash TEXT,envelope TEXT NOT NULL,PRIMARY KEY(namespace,run_id),UNIQUE(namespace,task_id,fencing_token))`,
    `CREATE INDEX IF NOT EXISTS execution_exact ON execution_runs(namespace,exact_hash)`,
    `CREATE INDEX IF NOT EXISTS execution_input ON execution_runs(namespace,input_hash)`,
    `CREATE INDEX IF NOT EXISTS execution_shape ON execution_runs(namespace,shape_hash)`,
    `CREATE TABLE IF NOT EXISTS execution_bands(namespace TEXT NOT NULL,band TEXT NOT NULL,run_id TEXT NOT NULL,PRIMARY KEY(namespace,band,run_id))`,
  ]) c.ctx.storage.sql.exec(sql);
  hardenLegacyTombstones(c);
}
export function checkedReport(value) {
  try { return normalize_report(value == null ? {} : value); }
  catch(e) { throw bad(`invalid execution_report: ${e.message}`); }
}
function snapshot(c, task) {
  const ns = c.namespace;
  const related = c._all('SELECT * FROM tasks WHERE namespace=? AND (parent_id=? OR id IN (SELECT depends_on FROM task_deps WHERE namespace=? AND task_id=?)) ORDER BY id',ns,task.id,ns,task.id);
  const sources = [task,...related].map(row => Object.fromEntries(GOV.map(key => [key,row[key]])));
  const values = related.map(row => {
    const result = c._one('SELECT output,receipt_hash FROM results WHERE namespace=? AND task_id=? AND ok=1 ORDER BY created_at DESC LIMIT 1',ns,row.id);
    return {task_id:row.id,state:row.state,output:result ? JSON.parse(result.output) : null,receipt_hash:result?.receipt_hash || null};
  });
  let value = {kind:task.kind,effect_class:task.effect_class,brief:JSON.parse(task.brief),inputs:c._inputs(task.id),dependencies_and_children:values};
  if (enc.encode(canonicalJson(value)).length > 131072) value = {unavailable:'input snapshot exceeds 128 KiB; exact matching disabled'};
  return {value,sources};
}
async function snapshotFingerprints(value,report) {
  let fp;
  try { fp = await fingerprints(value,report); }
  catch { fp=await fingerprints({unavailable:'input outside matching profile'},report); fp.input_hash=null;fp.exact_hash=null;fp.input_fingerprint_reason='input outside matching profile'; }
  if (Object.hasOwn(value,'unavailable')) { fp.input_hash=null; fp.exact_hash=null; }
  return fp;
}
function referencesVisible(c,report,view) {
  return (report.artifact_refs || []).every(ref => {
    const head=c._one('SELECT * FROM record_heads WHERE namespace=? AND store=? AND key=?',c.namespace,ref.store,ref.key);
    const row=c._one('SELECT * FROM records WHERE namespace=? AND store=? AND key=? AND version=?',c.namespace,ref.store,ref.key,ref.version);
    return head && row && c._canReadVersion(row,head,view);
  });
}
export async function prepareExecution(c,taskId,token,receiptHash,report,view) {
  const task=c._taskRow(taskId), ns=c.namespace;
  if (!referencesVisible(c,report,view)) throw bad('execution artifact reference is unavailable in this scope');
  const captured=snapshot(c,task);
  const fp=await snapshotFingerprints(captured.value,report);
  const runId='run_'+await hash({namespace:ns,task_id:taskId,fencing_token:token,receipt_hash:receiptHash});
  const reportHash=await hash(report);
  return {ns,captured,fp,runId,reportHash,report};
}
export function validatePrepared(c,prepared,task,view) {
  if (c.namespace!==prepared.ns || canonicalJson(snapshot(c,task))!==canonicalJson(prepared.captured))
    throw err(409,'execution_context_changed','task context changed during report hashing; retry with the same submission');
  if (!referencesVisible(c,prepared.report,view)) throw bad('execution artifact reference is unavailable in this scope');
}
export function captureExecution(c,p,task,lease,receiptHash,now,labels) {
  const expiry=c._one("SELECT purge_at FROM access_principals WHERE key_id=? AND principal_kind='anonymous'",lease.key_id);
  const expiresAt=expiry?.purge_at ?? null;
  const tags=[...new Set([...JSON.parse(task.tags).filter(t=>t!=='status:verified' && !categoryFromTag(t)), 'category:noun.eval_run','status:worker_reported'])];
  const envelope={run_id:p.runId,record_kind:'execution_run',category_id:'noun.eval_run',namespace:p.ns,task_id:task.id,queue:task.queue,fencing_token:lease.fencing_token,receipt_hash:receiptHash,created_at:now,expires_at:expiresAt,producer_surface:task.producer_surface,visibility_scope:task.visibility_scope,allowed_surfaces:JSON.parse(task.allowed_surfaces),target_cohorts:JSON.parse(task.target_cohorts),tags,missing_measurements:Object.fromEntries(['harness','model','usage','embedding'].filter(k=>!Object.hasOwn(p.report,k)).map(k=>[k,{value:null,reason:'worker did not report this field'}])),input_snapshot:p.captured.value,source_governance:p.captured.sources,evidence:{worker:lease.worker,key_id:lease.key_id,claimed_at:lease.claimed_at,server_duration_seconds:Math.max(0,now-lease.claimed_at)},report:p.report,report_sha256:p.reportHash,fingerprints:p.fp,labels,trust:{submission:'server_receipted',telemetry:'worker_reported',correctness:'not_independently_verified',reuse_requires_validation:true}};
  c.ctx.storage.sql.exec('INSERT INTO execution_runs(namespace,run_id,task_id,fencing_token,receipt_hash,created_at,expires_at,input_hash,exact_hash,shape_hash,envelope) VALUES (?,?,?,?,?,?,?,?,?,?,?)',p.ns,p.runId,task.id,lease.fencing_token,receiptHash,now,expiresAt,p.fp.input_hash,p.fp.exact_hash,p.fp.shape_hash,canonicalJson(envelope));
  for(const band of p.fp.bands) c.ctx.storage.sql.exec('INSERT INTO execution_bands(namespace,band,run_id) VALUES (?,?,?)',p.ns,band,p.runId);
}
function allowed(c,envelope,grant,view) {
  if(envelope.namespace!==c.namespace || (envelope.expires_at!=null && envelope.expires_at<=Date.now()/1000)) return false;
  for(const original of envelope.source_governance) {
    const current=c._one('SELECT * FROM tasks WHERE namespace=? AND id=?',c.namespace,original.id);
    if(!current || !c._taskVisible(current,grant,view) || !c._taskVisible(original,grant,view)) return false;
    try { c._requireQueue(grant,current.queue); c._requireQueue(grant,original.queue); } catch {return false;}
  }
  return referencesVisible(c,envelope.report,view);
}
export function executionView(c,row,grant,view,includeOutput=true) {
  const e=JSON.parse(row.envelope);
  if(!allowed(c,e,grant,view)) throw missing();
  const result=c._one((includeOutput?'SELECT *':'SELECT ok')+' FROM results WHERE namespace=? AND task_id=? AND fencing_token=? AND receipt_hash=?',row.namespace,row.task_id,row.fencing_token,row.receipt_hash);
  if(!result) throw missing();
  delete e.source_governance;
  return {...e,ok:!!result.ok,...(includeOutput?{output:JSON.parse(result.output),error:result.error}:{})};
}
export function getExecution(c,args,grant,view) {
  if(typeof args.run_id!=='string') throw bad('run_id must be a string');
  const row=c._one('SELECT * FROM execution_runs WHERE namespace=? AND run_id=?',c.namespace,args.run_id);
  if(!row) throw missing();
  return executionView(c,row,grant,view);
}
function requireLease(c,args,grant,view) {
  const task=c._taskRow(args.task_id);
  if(!c._taskVisible(task,grant,view)) throw err(404,'not_found','task does not exist');
  c._requireQueue(grant,task.queue);
  const lease=c._one('SELECT * FROM leases WHERE namespace=? AND task_id=?',c.namespace,args.task_id),now=Date.now()/1000;
  if(!lease || lease.fencing_token!==args.fencing_token || task.state!=='leased' || Math.min(lease.expires_at,lease.hard_expires_at)<=now)
    throw err(409,'stale_lease','recall requires a live lease and current fencing token');
  if(lease.key_id!==grant.key_id) throw err(403,'forbidden','only the current lease holder may recall executions');
  return task;
}
export async function recallExecution(c,args,grant,view) {
  const limit=args.limit===undefined ? 5 : args.limit;
  if(!Number.isInteger(limit) || limit<1 || limit>20) throw bad('limit must be an integer from 1 to 20');
  if(!Number.isInteger(args.fencing_token) || args.fencing_token<1 || typeof args.task_id!=='string') throw bad('task_id and positive integer fencing_token required');
  const report=checkedReport(args.execution_report), task=requireLease(c,args,grant,view), ns=c.namespace;
  if(!referencesVisible(c,report,view)) throw bad('execution artifact reference is unavailable in this scope');
  const before=snapshot(c,task);
  if(!allowed(c,{namespace:ns,expires_at:null,source_governance:before.sources,report},grant,view)) throw err(404,"not_found","task context is unavailable in this scope");
  const fp=await snapshotFingerprints(before.value,report);
  const current=requireLease(c,args,grant,view);
  if(!allowed(c,{namespace:ns,expires_at:null,source_governance:before.sources,report},grant,view)) throw err(404,"not_found","task context is unavailable in this scope");
  if(c.namespace!==ns || canonicalJson(snapshot(c,current))!==canonicalJson(before)) throw err(409,'execution_context_changed','context changed during recall; retry');
  const queries=[],params=[];
  ['exact_hash','input_hash','shape_hash'].forEach((field,priority)=>{
    if(fp[field]) { queries.push(`SELECT run_id,${priority} AS priority FROM execution_runs WHERE namespace=? AND ${field}=?`); params.push(ns,fp[field]); }
  });
  // One indexed band lookup keeps the compound query within the Workers SQLite limit.
  if(fp.bands.length) {
    queries.push('SELECT run_id,3 AS priority FROM execution_bands WHERE namespace=? AND band IN ('+fp.bands.map(()=>'?').join(',')+')');
    params.push(ns,...fp.bands);
  }
  const rows=queries.length ? c._all('SELECT r.run_id FROM execution_runs r JOIN (SELECT run_id,MIN(priority) priority FROM ('+queries.join(' UNION ALL ')+') GROUP BY run_id) x ON x.run_id=r.run_id WHERE r.namespace=? AND (r.expires_at IS NULL OR r.expires_at>?) ORDER BY x.priority,r.created_at DESC,r.run_id LIMIT ?',...params,ns,Date.now()/1000,LIMIT+1) : [];
  const matches=[];let considered=0;
  for(const candidate of rows.slice(0,LIMIT)) {
    const row=c._one('SELECT * FROM execution_runs WHERE namespace=? AND run_id=?',ns,candidate.run_id);
    if(!row) continue;
    let execution;
    try {execution=executionView(c,row,grant,view,false);} catch(e) {if(e.status===404) continue;throw e;}
    considered++;
    let match=compare(execution.fingerprints,fp,execution.report,report);
    if(match) {
      if(!execution.ok) match={...match,underlying_match_kind:match.match_kind,match_kind:'failure_warning'};
      matches.push({...match,run_id:execution.run_id});
    }
  }
  const order={exact_input:0,compatible_method:1,similar_example:2,failure_warning:3};
  matches.sort((a,b)=>order[a.match_kind]-order[b.match_kind] || b.score-a.score || (a.run_id<b.run_id?-1:a.run_id>b.run_id?1:0));
  const hydrated=[];let outputBytes=0;
  for(const match of matches) {
    let execution;try{execution=getExecution(c,{run_id:match.run_id},grant,view);}catch(e){if(e.status===404)continue;throw e;}
    const size=enc.encode(JSON.stringify(execution.output)).length;
    if(outputBytes+size>1048576){execution.output=null;execution.output_reason='recall output byte budget reached; use get_execution';}else outputBytes+=size;
    const {run_id,...details}=match;hydrated.push({...details,execution});if(hydrated.length===limit)break;
  }
  return {matches:hydrated,query_fingerprints:fp,coverage:{candidate_limit:LIMIT,considered,complete:false,reason:'Bounded exact/input/schema/LSH candidate index; vectors only rerank these candidates. No global semantic completeness claim.'},reuse_requires_validation:true};
}

export function listExecutions(c,args,grant,view) {
  const after=args.after===undefined?0:args.after,limit=args.limit===undefined?20:args.limit;
  if(typeof args.task_id!=='string' || !Number.isSafeInteger(after) || after<0 || !Number.isInteger(limit) || limit<1 || limit>100) throw bad('after must be a nonnegative integer and limit an integer from 1 to 100');
  const task=c._taskRow(args.task_id);
  if(!c._taskVisible(task,grant,view)) throw err(404,'not_found','task does not exist');
  c._requireQueue(grant,task.queue);
  const rows=c._all('SELECT * FROM execution_runs WHERE namespace=? AND task_id=? AND fencing_token>? ORDER BY fencing_token LIMIT ?',c.namespace,args.task_id,after,limit),executions=[];
  for(const row of rows) {
    try {const e=executionView(c,row,grant,view,false);executions.push(Object.fromEntries(['run_id','task_id','fencing_token','receipt_hash','ok','created_at','expires_at'].map(k=>[k,e[k]])));}
    catch(e) {if(e.status!==404) throw e;}
  }
  return {executions,next_after:rows.length===limit?rows.at(-1).fencing_token:null};
}

function hardenLegacyTombstones(c) {
  if(c._one("SELECT value FROM meta WHERE key='task_tombstone_privacy_v1'")) return;
  c.ctx.storage.transactionSync(()=>{
    const rows=c._all("SELECT h.*,r.value FROM record_heads h JOIN records r ON r.namespace=h.namespace AND r.store=h.store AND r.key=h.key AND r.version=h.version WHERE h.store='negative.knowledge'");
    for(const row of rows) {
      const value=JSON.parse(row.value);
      if(!value || value.task_id!==row.key || !Object.hasOwn(value,'dead_lettered_at')) continue;
      const source=c._one('SELECT * FROM tasks WHERE namespace=? AND id=?',row.namespace,row.key);
      const tags=[...new Set([...JSON.parse(row.tags),...(source?JSON.parse(source.tags):[])].filter(t=>t!=='status:verified'))];
      c.ctx.storage.sql.exec("UPDATE record_heads SET visibility_scope='private',allowed_surfaces='[]',owner_key_id=?,tags=? WHERE namespace=? AND store='negative.knowledge' AND key=?",source?.owner_key_id || 'system:operator-only',JSON.stringify(tags),row.namespace,row.key);
      c.ctx.storage.sql.exec("DELETE FROM noun_tags WHERE namespace=? AND noun_kind='record' AND container='negative.knowledge' AND noun_id=?",row.namespace,row.key);
      for(const tag of tags) c.ctx.storage.sql.exec("INSERT OR IGNORE INTO noun_tags(namespace,noun_kind,container,noun_id,tag) VALUES (?,'record','negative.knowledge',?,?)",row.namespace,row.key,tag);
    }
    c.ctx.storage.sql.exec("INSERT INTO meta(key,value) VALUES ('task_tombstone_privacy_v1','complete')");
  });
}
