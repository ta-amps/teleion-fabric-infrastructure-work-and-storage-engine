# Teleion Fabric: comparison

Where Teleion Fabric sits among established work queues and workflow engines, as of **Oct 26, 2026**. Facts about other
systems come from their official documentation or their own source code at a tagged release, each with a numbered
source below; behaviour read from code rather than docs is marked "(source)". The research was an AI-assisted pass
over those pages on Oct 26, 2026; quotes were located in the page text. Teleion Fabric's own rows are self-reported from
this repo and checked by its test suite, not by the researcher. Nothing here judges another project; it compares
documented behaviour.

## The short version

- **Where Teleion Fabric is genuinely different.** A repeated submit returns the stored, hashed record of the first one;
  every other system found removes duplicates when work is added, not when results come back. Its MCP door and
  web forms let agents claim, heartbeat and submit leased work directly; official MCP servers exist for Redis, SQS,
  Cloudflare and Temporal, but none found exposes lease-aware work tools, and none offers forms for agents without
  tools. And its queue, versioned records, blobs and event log commit in one transaction.
- **Where it is one of a few, not the only one.** Refusing a stale worker's late submit: pg-boss (per attempt),
  Temporal (per-attempt task token) and BullMQ (lock token) also reject or discard late results. A hard cap on lease
  time (SQS: 12 hours from first receive). A parent that waits for its children (BullMQ flows, Celery chords,
  Temporal child workflows, pg-boss flows). SQLite (River has experimental support).
- **Where established systems are stronger.** Transactions with your own data: pg-boss, Graphile Worker, River and
  plain `SKIP LOCKED` commit jobs (and in pg-boss and River, completions) in the application's own database
  transaction; Teleion Fabric can't join it. Scale and operations: SQS, Cloud Tasks, Cloudflare Queues and Temporal Cloud
  are hosted services; Teleion Fabric is one SQLite file on one machine. History: Temporal keeps a complete, durable event
  history per workflow, with replay.

## When to use something else

| If you need | Look at |
|---|---|
| Jobs that commit in the same transaction as your application's Postgres writes | pg-boss, River, Graphile Worker, or `SKIP LOCKED` |
| Long-running workflows with durable timers, signals and replay | Temporal |
| A managed queue with no servers to run, at large scale | SQS, Cloud Tasks, Cloudflare Queues |
| A Python task queue inside an existing Celery or Redis stack | Celery, RQ |
| Many AI agents, some web-only, sharing a backlog with leases, fencing, records and an audit log, on one small machine | Teleion Fabric |

## Comparison, part 1: claim, liveness, stale workers, idempotency, retries

| System | 1. Claim mechanism and default | 2. Heartbeat / extension API | 3. Stale-worker guard; late finish | 4. Idempotency / dedup | 5. Retries, backoff, dead letter |
|---|---|---|---|---|---|
| **Teleion Fabric** (self-reported) | Lease; 60 s heartbeat, 3 misses lapse it; hard cap 600 s (up to 3,600 s) [4] | `heartbeat` (HTTP and MCP) [4] | Fencing token; stale submit refused [4] | Idempotency key on submit; first record returned; sha256 record hash [4] | 3 attempts, jittered exponential (30 s base, 15 min cap), then dead letter [4] |
| **Amazon SQS** (standard, FIFO) | Visibility timeout; default 30 s, max 12 h [5][6] | `ChangeMessageVisibility`; 12 h limit counted from first receive, not reset by extending [5] | None. Delete with old record handle "will succeed, but the message might not be deleted" [7] | Standard: none, at-least-once [8]. FIFO: dedup ID or SHA-256 of body, 5-min window [9] | Message reappears after timeout; `maxReceiveCount` moves it to a DLQ; DLQ redrive [10]. Backoff setting: none found |
| **Google Cloud Tasks** | Push to your HTTP or App Engine handler; `dispatchDeadline` default 10 min (HTTP), range 15 s to 30 min [15] | None found (push model) | None. Request cancelled at deadline and retried; "whether the worker stops processing depends on the worker" [15]; duplicates occur [19] | Named tasks: ID reused only after up to 24 h (9 days for queue.yaml queues) [16] | `RetryConfig`: attempts, min/max backoff, doublings, max duration; after limits "the task is deleted" [17][18]. DLQ: none found |
| **Celery** (Redis or RabbitMQ broker) | Acks just before execution by default [27]. Redis: `visibility_timeout` 1 h [26]. RabbitMQ: ack timeout 30 min [34] | None found per task (Redis timeout is a broker-wide transport option) [26] | None. Redis: unacked task "redelivered to another worker and executed" [26]. RabbitMQ: channel closed, deliveries requeued [34] | None built in; tasks should be idempotent [27]; duplicate task IDs "undefined" [30] | `retry`/`autoretry_for`; `max_retries` 3; exponential backoff, cap 600 s, jitter on by default [27]. DLQ: RabbitMQ DLX via `Reject` [27]; quorum queues delivery limit 20 [35] |
| **RQ** 2.12 | Job moved to `StartedJobRegistry` [38]; `job_timeout` default 180 s (source) [42] | Automatic while running (`job.last_heartbeat`) [37]; no user extend API found | None found. Dead worker's job gets `AbandonedJobError`, then retry or `FailedJobRegistry` [39][43]. Late finish: not documented | `unique=True` with `job_id` (since 2.8), until job deleted [36] | `Retry(max, interval=[...])`, needs `--with-scheduler` [39]; failed jobs kept 1 year [36]. No backoff formula |
| **Redis Streams** consumer groups | `XREADGROUP` adds entry to pending list (PEL); no timeout; others claim after a min idle time they choose [44][45][46] | None dedicated; claiming resets idle time [46] | None. `XACK` takes key, group, IDs only [47]; "multiple processing is possible and unavoidable" [46] | `XADD IDMP` / `IDMPAUTO` (8.6+); IDs kept 100 s, 100 per producer by default [48][49] | Delivery counter; `XNACK` SILENT/FAIL/FATAL (8.8+) [50]; dead letter = app moves entry to another stream [44] |
| **BullMQ** 6.4.2 | Job lock; `lockDuration` 30 s [56][57] | Auto-renew at half `lockDuration`; `job.extendLock(token, ms)` [56] | Lock token: finishing without the lock fails with "Missing lock" / "Lock mismatch" (source) [58][59]; stalled job re-run by another worker [54] | Custom `jobId`: duplicate add ignored until removed [60]; dedup modes simple, throttle, debounce [61] | `attempts`; fixed or exponential backoff, optional jitter [62]; failed set. DLQ: none found |
| **pg-boss** 12.38 | `SKIP LOCKED` fetch [69]; `expireInSeconds` default 15 min, max 24 h [70] | `heartbeatSeconds` (off by default, min 10 s); `touch()` [70][71] | Yes, per attempt (`retryCount`): late result discarded, `{ affected: 0 }` [70][71] | Queue policies + `singletonKey`; throttle and debounce; duplicate `id` skipped [70][71] | `retryLimit` 2, `retryDelay` 0, exponential with jitter, `retryDelayMax`; `deadLetter` queue + `redrive()` [70][71] |
| **Graphile Worker** 0.18 | Job row locked; a crashed worker's jobs stay locked at least 4 h [77] | None in open source; Worker Pro (proprietary) adds heartbeats [81][82] | None documented; Pro docs warn a job can be re-attempted while still running [82]. Completion deletes by job id, no owner check (source) [85] | `job_key` modes `replace`, `preserve_run_at`, `unsafe_dedupe` [80] | `max_attempts` 25 [79]; delay `exp(least(10, attempt))` s [78]; failed jobs stay in table. DLQ: none found |
| **River** v0.49 | Job set `running`; `JobTimeout` default 1 min; rescued after 1 h [88][89] | None found; per-worker `Timeout()` override [88][89] | None documented; rescued job may run "alongside an existing execution attempt" [89]. Completion checks only `state = 'running'` (source) [98] | Unique jobs (`ByArgs`, `ByPeriod`, ...) [91] | 25 attempts; `attempt^4` s plus or minus 10% [90]; then `discarded` (kept 7 days) [89]; DLQ table in River Pro [94] |
| **Postgres `FOR UPDATE SKIP LOCKED`** (pattern) | Row lock until the transaction ends [99][100]; no default timeout (`transaction_timeout` default off) [101] | Not applicable: the lock lasts while the transaction stays open [100] | Lock released when the holder's transaction ends [100]; no token in the pattern | Not part of the pattern | Not part of the pattern |
| **Temporal** (Server 1.32) | Activity Start-To-Close timeout, no default (must set it or Schedule-To-Close) [102] | Activity Heartbeat + Heartbeat Timeout (`activity.RecordHeartbeat` Go, `activity.heartbeat()` Python) [102][109][110] | Per-attempt Task Token; a retry leaves an earlier token "invalid" [103]. Timed-out activity not told unless it heartbeats [102] | Workflow ID unique among open runs; reuse and conflict policies [106]; activities should be idempotent [105] | Default retry: 1 s, x2, max 100 s, unlimited attempts [104]. DLQ: none found |
| **Cloudflare Queues** | Push: batch to a consumer Worker (15 min wall time) [122]. Pull: `visibility_timeout` default 30 s, max 12 h, `lease_id` [118] | None found for pull leases [118] | None. Ack after timeout is accepted; the next holder can also ack "without an error" [118] | None; at-least-once; docs suggest your own idempotency key [119] | `max_retries` 3 (limit 100); delays up to 24 h; exponential backoff is an example, not built in [120][122]; optional DLQ, else deleted [121] |

## Comparison, part 2: joins, transactions, history, agent access, hosting

| System | 6. Parent/child or fan-out then join | 7. Same transaction as app data | 8. Built-in audit / event history | 9. Official agent-facing access | 10. Hosting footprint |
|---|---|---|---|---|---|
| **Teleion Fabric** (self-reported) | Split keeps the parent waiting, then returns it with the children's results [4] | Only Teleion Fabric's own store (versioned records, blobs, atomic batches) [4] | Append-only event log; counts record updated in the same transaction [4] | MCP door over Streamable HTTP; web forms for agents without tools [4] | Single node; one SQLite file (WAL); Python service [4] |
| **Amazon SQS** | None in SQS; docs point to AWS Step Functions for work over 12 h [5] | None documented; hosted service [12] | CloudTrail data events (send, receive, delete, visibility); off by default, extra charge [11] | AWS MCP (preview, "all AWS services") [13]; Amazon SNS / SQS MCP Server 1.0.0, open source [14] | Fully hosted by AWS [12] |
| **Google Cloud Tasks** | None found | No: "does not support enqueuing a task as part of a Cloud Datastore transaction" [20] | Cloud Audit Logs [22]; task logging off by default [21]; task keeps only first and last attempt [15] | None found; not on Google Cloud's remote MCP list (Pub/Sub is) [25] | Hosted by Google; handlers on any HTTP endpoint or App Engine [23] |
| **Celery** | Chords: callback runs after a whole group finishes; needs a result backend [29] | No. Docs recommend `delay_on_commit` (sends after Django commit) [32] | Task events (sent, received, started, succeeded, failed, retried...); off by default [28][31] | None found | Your workers + broker (RabbitMQ, Redis, SQS are "Stable") [33] + result backend for chords [29] |
| **RQ** | `depends_on` one job or a list [36]; `Group` to track jobs [36] | Redis only: `enqueue_many` inside a Redis pipeline [36] | Registries [41]; up to 10 most recent results per job [40] | None found | Redis 5+ or Valkey 7.2+; Python workers [36] |
| **Redis Streams** | None | Redis only: `MULTI`/`EXEC`, no rollbacks [51] | The stream is an append-only log until trimmed [44] | Official Redis MCP server; stream tools include consumer groups and ack [52][53] | Redis server; message safety needs AOF with strong fsync; async replication can lose entries on failover [44] |
| **BullMQ** | Flows: parent waits until all children succeed; reads their results [63][64] | Redis backend: no. Postgres backend: same database, but no API documented to join your transaction [66] | Event stream auto-trimmed to about 10,000 events [65] | None found (official) | Redis with `maxmemory-policy noeviction` [67]; or PostgreSQL 13+ backend in v6 [66]; Node.js |
| **pg-boss** | `flow()`: dependents blocked until all parents complete [71] | Yes: `db` option and ORM adapters; transactional workers commit handler writes with completion [72][73] | Job rows kept (completed: 7 days default); dead-letter lineage via `sourceRootId` [70][71]; in-process events [74] | None found | PostgreSQL 13+ (also CockroachDB, Citus, PGlite); Node 22.12+ or Bun [68] |
| **Graphile Worker** | None found | Yes: `graphile_worker.add_job()` from SQL, triggers or functions [79] | In-process events [83]; successful jobs are deleted [76] | None found | PostgreSQL 12+, Node 22.18+ [84] |
| **River** | River Pro workflows: DAG with fan-out and fan-in (commercial) [95] | Yes: transactional enqueueing; `JobCompleteTx` [92][93] | Job-persisted logs [96]; finished jobs kept 24 h, discarded 7 days [89] | None found | PostgreSQL (three latest majors) [87]; experimental SQLite since 0.23.0 [97] |
| **Postgres `SKIP LOCKED`** | Not part of the pattern | Yes by construction: claim and app writes share one transaction [100] | Not part of the pattern | Not applicable | PostgreSQL [99] |
| **Temporal** | Child Workflows; parent can await child results [107] | No: separate service; activities should be idempotent [105][116] | Event History: "complete and durable"; run terminated past 51,200 events [108] | `temporalio/temporal-mcp` (pre-release, read-only) [111]; docs MCP server [112]; MCP client integration (Python, pre-release) [113]; OpenAI Agents SDK integration [114] | Temporal Cloud [117], or self-host 4 services + Cassandra, MySQL or PostgreSQL (SQLite for dev only) [115][116] |
| **Cloudflare Queues** | None in Queues | None documented | Aggregated metrics only; no per-message history found [124] | Cloudflare API MCP server covers the whole API; Queues not named [125] | Hosted by Cloudflare; Workers Free and Paid plans [123] |

## Fencing tokens and MCP

- A fencing token is "simply a number that increases ... every time a client acquires the lock", and the storage
  server must check it (Kleppmann, 2016 [1]). Teleion Fabric's store is that server: every write carries the token and a
  stale one is refused (SPEC §5.2, test T05).
- The current MCP protocol version is 2026-07-28 [2]. That revision removed protocol-level sessions and the GET
  stream from Streamable HTTP [3]. Teleion Fabric's door already runs stateless (`stateless_http=True`, MCP Python SDK
  2.3.0); which protocol version clients negotiate with it hasn't been checked yet.

## What the research could not verify

- **SQS:** whether a late `DeleteMessage` with an old record handle deletes the message. The docs say it "might not be deleted" and also that SQS "can delete a message ... locked by another consumer" [7]. No backoff setting on the queue was found.
- **Cloud Tasks:**
  - Exact default `RetryConfig` values. The docs say the service "will pick the default"; the values on [17] are from an example output.
  - Whether Data Access audit logs are on by default (not stated on [22]).
  - Any lease extension for push tasks (none found).
- **Celery and RQ:** what happens when the original worker finishes after its task was redelivered (Celery with Redis) or collected as abandoned (RQ). Not documented.
- **Redis Streams:** that a late `XACK` from the original consumer removes an entry another consumer now holds. This is inferred from the command syntax [47], not stated.
- **BullMQ:**
  - The late-finish errors come from source [58][59], not docs prose.
  - No dead-letter queue found in the docs pages read.
  - Whether the Postgres backend can join an application's transaction is not documented [66].
- **River and Graphile Worker:** late-completion behaviour is a source reading at the cited tags, not documented behaviour.
- **Temporal:**
  - The exact server response to an old task token. The docs say only "invalid" [103].
  - Whether completing by Activity ID + Workflow ID checks the attempt.
  - Whether retry delays include jitter (not documented).
- **Cloudflare:**
  - Whether the Cloudflare API MCP server covers Queues operations. The docs say "entire Cloudflare API" but do not name Queues [125].
  - What happens to a push batch when the 15-minute invocation limit is hit.
- **AWS MCP (preview):** whether it covers SQS. The docs say "all AWS services" but do not name SQS [13].
- **Official MCP or agent integrations for Celery, RQ, BullMQ, pg-boss, Graphile Worker and River:** none found in their docs, their GitHub organisations, or a web search. BullMQ has a community MCP server, which is excluded as not official.
- **Teleion Fabric:** all Teleion Fabric facts are self-reported [4] and were not tested here.

## Sources

All pages read on 2026-10-26.

General
1. Martin Kleppmann, "How to do distributed locking", 2016-02-08. https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html
2. MCP, Versioning ("The current protocol version is 2026-07-28"). https://modelcontextprotocol.io/specification/versioning
3. MCP spec 2026-07-28, Transports: Streamable HTTP. https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http (changelog: https://modelcontextprotocol.io/specification/2026-07-28/changelog)
4. Teleion Fabric README and SPEC.md (self-reported, not tested by the researcher). https://github.com/ta-amps/teleion-fabric-infrastructure-work-and-storage-engine

Amazon SQS
5. Visibility timeout. https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html
6. ChangeMessageVisibility API. https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_ChangeMessageVisibility.html
7. DeleteMessage API. https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_DeleteMessage.html
8. At-least-once delivery. https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/standard-queues-at-least-once-delivery.html
9. Exactly-once processing (FIFO). https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/FIFO-queues-exactly-once-processing.html
10. Dead-letter queues. https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html
11. Logging SQS API calls with CloudTrail. https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/logging-using-cloudtrail.html
12. What is Amazon SQS. https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/welcome.html
13. AWS MCP Servers catalog (Essential: AWS MCP, preview). https://awslabs.github.io/mcp/
14. Amazon SNS / SQS MCP Server. https://awslabs.github.io/mcp/servers/amazon-sns-sqs-mcp-server

Google Cloud Tasks
15. Task resource (`dispatchDeadline`, attempts). https://docs.cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues.tasks
16. `tasks.create` (task ID de-duplication). https://docs.cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues.tasks/create
17. Configure Cloud Tasks queues (retry parameters). https://docs.cloud.google.com/tasks/docs/configuring-queues
18. RetryConfig reference. https://docs.cloud.google.com/tasks/docs/reference/rest/v2/RetryConfig
19. Common pitfalls (duplicate executions). https://docs.cloud.google.com/tasks/docs/common-pitfalls
20. Migrate from Task Queues to Cloud Tasks. https://docs.cloud.google.com/tasks/docs/migrating
21. Cloud Tasks logging. https://docs.cloud.google.com/tasks/docs/logging
22. Cloud Tasks audit logging. https://docs.cloud.google.com/tasks/docs/audit-logging
23. Cloud Tasks overview. https://docs.cloud.google.com/tasks/docs/dual-overview
24. Create HTTP target tasks (handler timeout default and max). https://docs.cloud.google.com/tasks/docs/creating-http-target-tasks
25. Google Cloud remote MCP servers, supported products (updated 2026-10-26). https://docs.cloud.google.com/mcp/supported-products

Celery and RabbitMQ
26. Using Redis (visibility timeout, caveats). https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html
27. Tasks user guide (acks_late, retries, Reject, database transactions). https://docs.celeryq.dev/en/stable/userguide/tasks.html
28. Configuration (`task_acks_late`, `worker_send_task_events`). https://docs.celeryq.dev/en/stable/userguide/configuration.html
29. Canvas: chords. https://docs.celeryq.dev/en/stable/userguide/canvas.html#chords
30. FAQ (natural task IDs; retry vs acks_late). https://docs.celeryq.dev/en/stable/faq.html
31. Monitoring and events. https://docs.celeryq.dev/en/stable/userguide/monitoring.html
32. First steps with Django (`delay_on_commit`). https://docs.celeryq.dev/en/stable/django/first-steps-with-django.html
33. Backends and brokers overview. https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/index.html
34. RabbitMQ consumers: delivery acknowledgement timeout. https://www.rabbitmq.com/docs/consumers#acknowledgement-timeout (checked via https://github.com/rabbitmq/rabbitmq-website/blob/main/docs/consumers.md)
35. RabbitMQ quorum queues: poison message handling. https://www.rabbitmq.com/docs/quorum-queues#poison-message-handling (checked via https://github.com/rabbitmq/rabbitmq-website/blob/main/docs/quorum-queues/index.md)

RQ
36. Queues (job options, dependencies, unique jobs, pipelines). https://python-rq.org/docs/
37. Jobs. https://python-rq.org/docs/jobs/
38. Workers. https://python-rq.org/docs/workers/
39. Exceptions and retries. https://python-rq.org/docs/exceptions/
40. Results. https://python-rq.org/docs/results/
41. Job registries. https://python-rq.org/docs/job_registries/
42. Source v2.12: `Queue.DEFAULT_TIMEOUT`. https://github.com/rq/rq/blob/v2.12/rq/queue.py
43. Source v2.12: `StartedJobRegistry.cleanup`. https://github.com/rq/rq/blob/v2.12/rq/registry.py

Redis
44. Redis Streams. https://redis.io/docs/latest/develop/data-types/streams/
45. XREADGROUP. https://redis.io/docs/latest/commands/xreadgroup/
46. XCLAIM. https://redis.io/docs/latest/commands/xclaim/
47. XACK. https://redis.io/docs/latest/commands/xack/
48. XADD. https://redis.io/docs/latest/commands/xadd/
49. XCFGSET. https://redis.io/docs/latest/commands/xcfgset/
50. XNACK. https://redis.io/docs/latest/commands/xnack/
51. Transactions. https://redis.io/docs/latest/develop/using-commands/transactions/
52. Redis MCP. https://redis.io/docs/latest/integrate/redis-mcp/
53. `redis/mcp-redis` README. https://github.com/redis/mcp-redis

BullMQ
54. Stalled jobs (workers guide). https://docs.bullmq.io/guide/workers/stalled-jobs
55. Stalled (jobs guide). https://docs.bullmq.io/guide/jobs/stalled
56. Manually processing jobs (lock, token, `extendLock`). https://docs.bullmq.io/patterns/manually-fetching-jobs
57. WorkerOptions (v6 API reference). https://docs.bullmq.io/api/interfaces/v6.WorkerOptions.html
58. Source v6.4.2: `removeLock.lua`. https://github.com/taskforcesh/bullmq/blob/v6.4.2/src/commands/includes/removeLock.lua
59. Source v6.4.2: `finished-errors.ts`. https://github.com/taskforcesh/bullmq/blob/v6.4.2/src/classes/finished-errors.ts
60. Job IDs. https://docs.bullmq.io/guide/jobs/job-ids
61. Deduplication. https://docs.bullmq.io/guide/jobs/deduplication
62. Retrying failing jobs. https://docs.bullmq.io/guide/retrying-failing-jobs
63. Flows. https://docs.bullmq.io/guide/flows
64. Fail parent. https://docs.bullmq.io/guide/flows/fail-parent
65. Events. https://docs.bullmq.io/guide/events
66. PostgreSQL backend. https://docs.bullmq.io/guide/postgresql
67. Going to production. https://docs.bullmq.io/guide/going-to-production

pg-boss
68. pg-boss home (summary, requirements). https://pgboss.io/
69. Introduction. https://pgboss.io/introduction
70. Queues API (policies, retries, dead letter, heartbeat vs expiration). https://pgboss.io/api/queues
71. Jobs API (flows, redrive, complete/fail/touch by attempt). https://pgboss.io/api/jobs
72. ORM transaction adapters. https://pgboss.io/api/adapters
73. Workers (transactional workers). https://pgboss.io/api/workers
74. Events. https://pgboss.io/api/events

Graphile Worker
75. Introduction. https://worker.graphile.org/docs
76. Task executors. https://worker.graphile.org/docs/tasks
77. Error handling. https://worker.graphile.org/docs/error-handling
78. Exponential backoff. https://worker.graphile.org/docs/exponential-backoff
79. Adding jobs through SQL. https://worker.graphile.org/docs/sql-add-job
80. Job key. https://worker.graphile.org/docs/job-key
81. Worker Pro (proprietary). https://worker.graphile.org/docs/pro
82. Worker Pro: crashed worker recovery. https://worker.graphile.org/docs/pro/recovery
83. Worker events. https://worker.graphile.org/docs/worker-events
84. Requirements. https://worker.graphile.org/docs/requirements
85. Source v0.18.0: `completeJobs.ts`. https://github.com/graphile/worker/blob/v0.18.0/src/sql/completeJobs.ts
86. Source v0.18.0: `failJobs.ts`. https://github.com/graphile/worker/blob/v0.18.0/src/sql/failJobs.ts

River
87. Getting started. https://riverqueue.com/docs
88. Writing reliable workers. https://riverqueue.com/docs/reliable-workers
89. Maintenance services (rescuer, cleaner). https://riverqueue.com/docs/maintenance-services
90. Job retries. https://riverqueue.com/docs/job-retries
91. Unique jobs. https://riverqueue.com/docs/unique-jobs
92. Transactional enqueueing. https://riverqueue.com/docs/transactional-enqueueing
93. Transactional job completion. https://riverqueue.com/docs/transactional-job-completion
94. Dead letter queue (River Pro). https://riverqueue.com/docs/pro/dead-letter-queue
95. Workflows (River Pro). https://riverqueue.com/docs/pro/workflows
96. Job-persisted logging. https://riverqueue.com/docs/job-logging
97. Using with SQLite. https://riverqueue.com/docs/sqlite
98. Source v0.49.0: `river_job.sql` (`JobSetStateIfRunningMany`). https://github.com/riverqueue/river/blob/v0.49.0/riverdriver/riverpgxv5/internal/dbsqlc/river_job.sql

PostgreSQL
99. SELECT, locking clause. https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE
100. Explicit locking, row-level locks. https://www.postgresql.org/docs/current/explicit-locking.html#LOCKING-ROWS
101. Client connection defaults (`transaction_timeout`, `idle_in_transaction_session_timeout`). https://www.postgresql.org/docs/current/runtime-config-client.html

Temporal
102. Detecting Activity failures (timeouts, heartbeats). https://docs.temporal.io/encyclopedia/detecting-activity-failures
103. Activity Execution: Task Token. https://docs.temporal.io/activity-execution#task-token
104. Retry Policies. https://docs.temporal.io/encyclopedia/retry-policies
105. Activity Definition: idempotency. https://docs.temporal.io/activity-definition#idempotency
106. Workflow ID and Run ID (reuse and conflict policies). https://docs.temporal.io/workflow-execution/workflowid-runid
107. Child Workflows. https://docs.temporal.io/child-workflows
108. Events and Event History (limits). https://docs.temporal.io/workflow-execution/event#event-history-limits
109. Go SDK: Activity timeouts and heartbeats. https://docs.temporal.io/develop/go/activities/timeouts
110. Python SDK: Activity timeouts and heartbeats. https://docs.temporal.io/develop/python/activities/timeouts
111. `temporalio/temporal-mcp` (pre-release, read-only). https://github.com/temporalio/temporal-mcp
112. Develop with AI (Knowledge Base MCP server). https://docs.temporal.io/with-ai
113. Python MCP integration (pre-release). https://docs.temporal.io/develop/python/integrations/mcp
114. OpenAI Agents SDK integration (Python). https://docs.temporal.io/develop/python/integrations/openai-agents
115. Persistence. https://docs.temporal.io/temporal-service/persistence
116. Temporal Server (four services). https://docs.temporal.io/temporal-service/temporal-server
117. Temporal Cloud guide. https://docs.temporal.io/cloud

Cloudflare Queues
118. Pull consumers. https://developers.cloudflare.com/queues/configuration/pull-consumers/
119. Delivery guarantees. https://developers.cloudflare.com/queues/reference/delivery-guarantees/
120. Batching, retries and delays. https://developers.cloudflare.com/queues/configuration/batching-retries/
121. Dead letter queues. https://developers.cloudflare.com/queues/configuration/dead-letter-queues/
122. Limits. https://developers.cloudflare.com/queues/platform/limits/
123. Pricing. https://developers.cloudflare.com/queues/platform/pricing/
124. Metrics. https://developers.cloudflare.com/queues/observability/metrics/
125. Cloudflare's own MCP servers. https://developers.cloudflare.com/agents/model-context-protocol/mcp-servers-for-cloudflare/
