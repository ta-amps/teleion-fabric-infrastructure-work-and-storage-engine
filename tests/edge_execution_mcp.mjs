// MCP adapter fixture only: authorization/storage are covered by the real runtime suite.
import assert from "node:assert/strict";
import { handleMcpRequest, TOOLS } from "../deploy/cloudflare/cloud_mcp.js";

const report = {version: 1, summaries: {problem: "SYNTHETIC addition"}};
const calls = [];
const stub = {async rpcCall(_secret, name, args) {
  calls.push({name, args});
  return {ok: true, data: name === "submit" ? {receipt_hash: "fixture"} : {matches: [], reuse_requires_validation: true}};
}};
async function call(name, args) {
  const request = new Request("https://example.com/mcp", {method: "POST", headers: {"content-type": "application/json"}, body: JSON.stringify({jsonrpc: "2.0", id: 1, method: "tools/call", params: {name, arguments: args}})});
  const response = await handleMcpRequest(request, {}, {}, stub, "fixture", false, "conformance-execution");
  const value = await response.json();
  assert.equal(value.result.isError, false);
}
await call("recall_execution", {task_id: "fixture", fencing_token: 2, execution_report: report, limit: 3});
await call("submit", {task_id: "fixture", fencing_token: 2, output: {synthetic: true}, idempotency_key: "fixture:2", execution_report: report});
await call("get_execution", {run_id: "run_fixture"});
await call("list_executions", {task_id: "fixture", after: 2, limit: 3});
assert.deepEqual(calls.map(x => x.name), ["recallExecution", "submit", "getExecution", "listExecutions"]);
assert.deepEqual(calls[0].args.execution_report, report);
assert.equal(calls[0].args.limit, 3);
assert.deepEqual(calls[1].args.execution_report, report);
assert.equal(calls[2].args.run_id, "run_fixture");
assert.equal(calls[3].args.after, 2);
assert.equal(calls[3].args.limit, 3);
assert(calls.every(x => x.args.namespace === "conformance-execution"));
assert.equal(TOOLS.find(t => t.name === "submit").inputSchema.properties.execution_report.type[0], "object");
assert.equal(TOOLS.find(t => t.name === "recall_execution").inputSchema.properties.fencing_token.type, "integer");
assert.deepEqual(TOOLS.find(t => t.name === "get_execution").inputSchema.required, ["run_id"]);
assert.equal(TOOLS.find(t => t.name === "list_executions").inputSchema.properties.limit.maximum, 100);
console.log(JSON.stringify({passed: true, scope: "MCP adapter fixture; no model calls"}));
