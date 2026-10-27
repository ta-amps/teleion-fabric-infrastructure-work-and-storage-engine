# Bilateral swarm testing with OpenCode & Ollama Cloud (`muse-spark-open`)

Before switching to hosted Meta Model API keys (`https://api.meta.ai/v1`), you can exercise every
Tessera, Eranos, and Shamash workflow locally and bilaterally using **OpenCode** (`opencode v2.x`
over Streamable HTTP `/mcp`) and **Meta's open-weight `muse-spark-open`** on **Ollama Cloud**
(`https://ollama.com/v1`) or a local Ollama server.

## 1. Connect OpenCode directly to Tessera `/mcp`

Start Tessera and seed worker keys:

```bash
make serve          # 127.0.0.1:7700 (or `make dev` for 127.0.0.1:7701 behind the Cloudflare edge)
make seed           # writes scoped keys to ~/.config/tessera/keys-tessera.json (mode 0600)
```

Create a project-scoped `opencode.json` that references secrets only via `{env:...}` placeholders
(never commit literal keys) and sets `"tool_call": true` on `muse-spark-open`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "ollama-cloud": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Ollama Cloud",
      "options": {
        "baseURL": "https://ollama.com/v1",
        "apiKey": "{env:OLLAMA_API_KEY}"
      },
      "models": {
        "muse-spark-open": {
          "name": "Muse Spark Open",
          "tool_call": true
        }
      }
    }
  },
  "model": "ollama-cloud/muse-spark-open",
  "small_model": "ollama-cloud/muse-spark-open",
  "mcp": {
    "tessera": {
      "type": "remote",
      "url": "http://127.0.0.1:7700/mcp",
      "headers": {
        "Authorization": "Bearer {env:TESSERA_KEY}"
      },
      "enabled": true
    }
  }
}
```

OpenCode immediately sees all 12 scoped Tessera MCP tools (`queues`, `enqueue`, `claim`,
`heartbeat`, `submit`, `split`, `park`, `resume`, `put_record`, `put_records`, `get_record`,
`record_history`).

## 2. Run the bilateral two-agent test suite

`tools/opencode_bilateral.py` runs **two distinct agents** (`opencode-agent-a` and `ollama-peer-b`,
each with its own `tsk_*` key) across two bilateral protocols:

1. **`park` ↔ `resume` Q&A handshake (`T19` + `T21`):** Agent A claims an intake task over `/mcp`
   and parks it with a question; self-resume by Agent A is refused (`403`); Agent B answers via
   `resume`; Agent A re-claims the task with `lease.inputs`, writes a compare-and-set noun, and
   submits with a SHA-256 receipt.
2. **Seed → Proposals (`v1..v3`) → Independent Review → Canonical (`v1`):** Agent A expands a seed
   noun and enriches two child tasks using `muse-spark-open`; Agent B (`reviewer != proposer`)
   verifies `SYNTHETIC` marks and atomically promotes to `canonical/*`.

```bash
# Deterministic offline verification (runs in CI without network or keys):
make bilateral-demo

# Live Ollama Cloud (`muse-spark-open`) + live `opencode` CLI over `/mcp`:
OLLAMA_API_KEY=<your-ollama-key> \
  uv run python tools/opencode_bilateral.py --live-ollama --live-opencode
```

## 3. Test Meta `/v1/responses` callers (including Shamash) via `meta_open_bridge.py`

Product surfaces such as [Shamash](https://github.com/ta-amps/shamash) speak strictly to Meta's
`/v1/responses` contract in Standard mode. `tools/meta_open_bridge.py` exposes `/v1/responses`,
`/v1/responses/{id}`, `/v1/responses/{id}/cancel`, and `/v1/models` locally on `127.0.0.1:7799` and
bridges requests to `muse-spark-open` (still refusing any `-contributor` model id):

```bash
# Terminal 1: start the local Meta Responses -> muse-spark-open bridge
OLLAMA_API_KEY=<your-ollama-key> uv run python tools/meta_open_bridge.py --port 7799

# Terminal 2: run Shamash's unmodified Meta Model API smoke call against the bridge
cd ../shamash
MODEL_API_BASE=http://127.0.0.1:7799/v1 MODEL_API_KEY=local-bridge uv run shamash muse-smoke
```

See [expansion.md](expansion.md) for the staged noun-store design (`seeds` → `proposals` →
`canonical`).
