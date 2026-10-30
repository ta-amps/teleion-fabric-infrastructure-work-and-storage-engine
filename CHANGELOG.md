# Changelog

## Unreleased

Agent pages now render every tool list and tool count from the registry that MCP `tools/list` serves, on the Worker and the Python service: `llms.txt` lists every served tool, `llms-full.txt` adds a generated one-line reference per tool, and the agent card carries the served tool objects. The hosted `llms.txt` had named 26 of its 37 tools. The Python agent card's tool objects now use the `tools/list` field names (`inputSchema`, `outputSchema`) and the card reports `tool_count`.

New submissions capture immutable execution records with optional worker-reported harness, model, summary, contract and usage metadata. Authorized workers can recall prior runs, list a task's run references and read them through HTTP, MCP or the Python client without a shim. Recall never automatically reuses an output. Anonymous retention and source access checks apply. The execution guide includes a dry-run-by-default external-worker distillation task example; new packs remain deferred until infrastructure readiness.

External agents can now run tracked anonymous Tessera loops; keyed access gains account/token audit. Anonymous access is feature flagged, uses private demo namespaces and retains the existing authenticated security boundary.

A key can now always read back raw blob bytes it uploaded, on the Worker and the Python service, even when another key stored the same bytes privately first; before, it got `201` and then `404`, which showed that someone held those bytes. Python object reads now follow the object's own ACL, as on the Worker, instead of the raw blob row written by the bytes' first uploader. The rest of first-uploader blob governance is still open; see `docs/raw-blob-governance.md`.
