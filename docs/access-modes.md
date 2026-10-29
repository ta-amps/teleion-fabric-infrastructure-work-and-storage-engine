# External agent access

Coding agents, personal assistants and HTTP clients can discover Tessera without
credentials. `initialize` and `tools/list` on `/mcp` are public. Real writes use
one of two tracked principals: a short-lived anonymous session or an issued API
key. `/connect` presents both choices and `/connect.json` describes the current
service contract and whether anonymous minting is enabled.

## Enable and connect

The code defaults off; the checked-in cloud deployment explicitly enables it. Set `ANONYMOUS_ACCESS_ENABLED=true` on the Cloudflare
Worker or `TESSERA_ANONYMOUS_ACCESS_ENABLED=true` for the local Python service.
Existing keyed access continues with the flag off. Enabling anonymous access
allows session minting; it does not remove authorization from work routes.

| Mode | Obtain a credential | Work scope | Retention |
| --- | --- | --- | --- |
| Anonymous | `POST /v1/session/anonymous` or MCP `open_anonymous_session` | Server-assigned `demo.<principal_id>` namespace, fixed demo queues and private nouns | Token: one hour; demo state: 24 hours, then periodic cleanup |
| Keyed | Operator-assisted `POST /v1/keys`, existing admin key routes or `make seed` | Issued queue, effect, noun and namespace grants | Key expiry is configurable; regular records and receipts remain durable |

Key issuance uses an existing operator identity. There is no secret-free admin
signup, email login or OAuth claim. `/connect` lets a user paste an issued key
and inspect `/v1/me`. Tokens stay in page memory, clear on reload and never enter
URLs, local storage or analytics. The full secret is returned only at mint time.

Both modes send `Authorization: Bearer <token>` to HTTP or MCP. Configure the
header using the client's secret mechanism, not a committed project file:

```json
{
  "mcpServers": {
    "fabric": {
      "url": "https://tessera.ta-amps.workers.dev/mcp",
      "headers": {"Authorization": "Bearer <session_token_or_api_key>"}
    }
  }
}
```

## Anonymous limits

| Limit | Value |
| --- | --- |
| Session lifetime | 3,600 seconds |
| Demo state and access audit retention | 86,400 seconds |
| Claims per session per hour | 20 |
| Concurrent live claims | 1 |
| Requests per session per hour | 300 |
| Cumulative submitted payload bytes | 1,048,576 |
| Request bytes | 65,536 |
| Session mints per IP hash per hour | 10 |
| Global session mints per hour | 1,000 |

Limits use persistent atomic counters. Rejected or repeated write attempts can
consume quota. A limit returns `429` with `Retry-After`; an oversized anonymous
request returns `413`. Keyed quota values are operator-defined and remain `null`
in the public contract when no fixed numeric limit is known.

Each session gets `demo.ping`, `demo.echo`, `demo.situate_sample` and the
`demo.instructions/welcome` noun. Anonymous records and verbs are always private
and have immutable `origin:anonymous`, `demo:true`, `untrusted:true` provenance.
Receipts also carry these labels. An optional sanitized agent label is limited
to 64 characters. A client cannot promote a demo artifact by changing its tags,
visibility, namespace or surface header.

Anonymous callers can search/list their demo nouns and verbs, claim, heartbeat,
write CAS records, submit, cancel and inspect their own audit. Bucket/blob writes,
admin calls, production queues and other principals' namespaces are denied.
Use synthetic examples and `example.com` addresses. Demo writes reject common
personal/payment-data patterns; this is a limited deterministic filter, not a
claim that arbitrary sensitive content can be detected reliably.

## A complete HTTP loop

Use a private terminal and temporary directory. The session token is written to
a mode-600 header file so it does not appear in the curl argument list. Replace
`BASE` with a local service address when testing locally.

```bash
BASE=https://tessera.ta-amps.workers.dev
umask 077
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
curl --fail-with-body -sS "$BASE/v1/session/anonymous" \
  -H 'Content-Type: application/json' -d '{"agent_label":"external-agent"}' > "$scratch/session.json"
python3 - "$scratch" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
session = json.loads((p / 'session.json').read_text())
(p / 'authorization').write_text('Authorization: Bearer ' + session['session_token'] + '\n')
PY
curl --fail-with-body -sS "$BASE/v1/search?q=practice" -H @"$scratch/authorization"
curl --fail-with-body -sS "$BASE/v1/verbs?status=ready" -H @"$scratch/authorization"
curl --fail-with-body -sS "$BASE/v1/tasks/claim" -H @"$scratch/authorization" \
  -H 'Content-Type: application/json' -d '{"queue":"demo.ping","worker":"external-agent"}' > "$scratch/lease.json"
python3 - "$scratch" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]); lease = json.loads((p / 'lease.json').read_text())
ref = {k: lease[k] for k in ('task_id', 'fencing_token')}
(p / 'heartbeat.json').write_text(json.dumps(ref))
(p / 'submit.json').write_text(json.dumps({**ref, 'output': {'record': 'result', 'version': 1}}))
(p / 'idempotency').write_text('Idempotency-Key: ' + lease['task_id'] + ':' + str(lease['fencing_token']) + '\n')
PY
curl --fail-with-body -sS "$BASE/v1/tasks/heartbeat" -H @"$scratch/authorization" \
  -H 'Content-Type: application/json' --data-binary @"$scratch/heartbeat.json"
curl --fail-with-body -sS "$BASE/v1/stores/demo.artifacts/records/result" -H @"$scratch/authorization" \
  -H 'Content-Type: application/json' -d '{"expected_version":0,"value":{"synthetic":true,"message":"ping"}}'
curl --fail-with-body -sS "$BASE/v1/tasks/submit" -H @"$scratch/authorization" \
  -H @"$scratch/idempotency" -H 'Content-Type: application/json' --data-binary @"$scratch/submit.json"
curl --fail-with-body -sS "$BASE/v1/me/audit" -H @"$scratch/authorization"
```

For keyed work, replace the header file with an operator-issued key and choose
an authorized queue and noun store. The same claim, heartbeat, CAS and submit
schemas apply. `If-Match: <version>` is also accepted for an HTTP record write.
A competing CAS update returns `409`, as does a stale fencing token.

The equivalent MCP sequence is `open_anonymous_session`, then reconnect with
the returned token and call `search_nouns`, `list_verbs`, `claim`, `heartbeat`,
`put_record`, `submit`, `my_audit`. Read `tools/list` for the exact current input
schemas and `/.well-known/agent-card.json` for the same registry. The connection
page and agent text derive the advertised tool count from that registry.

## Key scope and lifecycle

Minimum fine-grained scopes are `nouns:read`, `verbs:claim`, `verbs:submit`,
`cas:write`, `admin:keys` and `namespace:<exact-name>`; `namespace:*` explicitly
permits all namespaces. Legacy non-admin keys without namespace scopes remain
limited to `main`. Existing `store` and `admin` scopes remain supported.

`GET /v1/keys` lists the caller's account keys, including key ID, first-eight-
character prefix, account, label, scopes, creation/use/expiry/revocation dates
and quota tier. `POST /v1/keys/{id}/revoke` requires the owner or an admin.
Delegated issuers cannot widen scopes, queue/kind/effect grants, expiry,
account or surface authority. Rotate by minting a replacement, switching the
client, then revoking the previous key.

## Audit, events and browser clients

`GET /v1/me/audit` and MCP `my_audit` return only the current principal's events.
`GET /v1/admin/audit` and MCP `admin_audit` require admin scope. Events share
request ID, timestamp, namespace, route/tool, principal kind and ID, optional
session/key/account IDs, daily HMAC IP/UA hashes, action, resource type/ID,
receipt ID, status, input/output sizes, error code, immutable labels and used
scopes. Raw tokens, raw IP addresses, prompts and artifact contents are excluded.
Service-bound keys use `principal_kind=service_binding`.

`GET /v1/public/activity` exposes only timestamp, principal kind, action,
resource type, status and fixed demo labels. It omits resource/account/session
identifiers and client labels. The portal uses this redacted endpoint.

`GET /v1/claims/{task_id}/events?after=<seq>&wait=15` provides authorized,
bounded long-poll. `claim_events` offers the MCP counterpart. Event cursors are
monotonic storage sequences. This release advertises `streaming: long-poll`;
it does not claim SSE, WebSocket or callback delivery.

MCP and HTTP agent routes answer CORS preflight with `204`. They allow bearer
headers with `Access-Control-Allow-Origin: *`, without cookies or
`Allow-Credentials`. Allowed headers include Authorization, Content-Type,
If-Match, X-Request-Id and X-Agent-Label. ETag, Retry-After and X-Request-Id are
exposed. Python retains configured MCP Host checks while permitting valid
external HTTP(S) origins. Origin is never a source of authorization.

The local HTTP schema is generated at `/v1/openapi.json`. Regression coverage:
`tests/test_access_modes.py` and `tests/edge_access_harness.mjs` exercise complete
flows, private-state isolation, scope denial, races, quota and browser behavior.

## Custom HTTP clients

Send a descriptive application header, for example `User-Agent: my-agent/1.0`,
when using a stdlib HTTP client. The workers.dev platform rejects some default
Python urllib signatures with error 1010 before the Worker receives the request.
Browsers and the tested MCP SDK use their existing client headers. This is
separate from bearer authorization and does not require changing account security.
See [Cloudflare error 1010](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/).
