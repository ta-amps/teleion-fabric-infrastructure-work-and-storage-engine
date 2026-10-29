# Changelog

## Unreleased

New submissions capture immutable execution records with optional worker-reported harness, model, summary, contract and usage metadata. Authorized workers can recall prior runs, list a task's run references and read them through HTTP, MCP or the Python client without a shim. Recall never automatically reuses an output. Anonymous retention and source access checks apply. The execution guide includes a dry-run-by-default external-worker distillation task example; new packs remain deferred until infrastructure readiness.

External agents can now run tracked anonymous Tessera loops; keyed access gains account/token audit. Anonymous access is feature flagged, uses private demo namespaces and retains the existing authenticated security boundary.
