# Architecture

Tessera has one store and several doors. Every door ends in the same IMMEDIATE transaction, so the
guarantees in SPEC.md hold whichever way work comes in.

```mermaid
flowchart LR
  subgraph Callers
    CLI["tessera CLI<br/>(shared SQLite file)"]
    HTTP["HTTP clients<br/>(Shamash adapter, scripts)"]
    MCP["MCP clients<br/>(Muse Code, other agents)"]
    W["tools/muse_worker.py"]
  end
  subgraph Service["Tessera service (briefs L6 to L24)"]
    API["FastAPI: keys, POST-only writes,<br/>problem+json errors, rate limits"]
    DOOR["MCP door:<br/>claim, heartbeat, submit, split"]
    REAPER["reaper loop<br/>(every minute)"]
    LANE["model-job lane:<br/>cancel on lapse"]
    BOARD["/board<br/>(read-only)"]
  end
  STORE[("Store: SQLite WAL<br/>queues, tasks, leases, results,<br/>keys, jobs, events, counts,<br/>records, blobs")]
  META["Meta Model API<br/>(background responses)"]
  CLI --> STORE
  W --> CLI
  W --> HTTP
  HTTP --> API --> STORE
  MCP --> DOOR --> STORE
  REAPER --> STORE
  LANE --> STORE
  LANE -. cancel .-> META
  BOARD --> STORE
```

- **Store** (`src/tessera/`): the reference implementation and the only place state changes. One
  connection per thread; every write is one IMMEDIATE transaction that also updates `counts` and appends
  to `events`. Several processes may share the file.
- **Service**: a thin layer that adds keys, scopes, POST-only writes, rate limits and the MCP door. It
  calls the store and nothing else.
- **Model-job lane**: Tessera never calls a model for work; the worker that holds the lease does. Tessera
  records the job against the lease and, when the lease lapses, asks Meta to cancel it.
- **Board**: reads the counts record and nothing else, so every board shows the same numbers.

## Two loops on one fabric

- **Build loop**: briefs for this repo and Shamash sit on queue `build`; Muse Code workers claim them
  with `tools/muse_worker.py`, work in their own git worktree, and submit receipts.
- **Run loop**: Shamash's cases move through `aq02`, `aq85`, `aq04`, `aq05` and `aq86`.

Both loops write the same event log, which is the making-of (docs/making-of.md).
