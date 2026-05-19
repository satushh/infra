# Ethereum infrastructure

Docker Compose stack to run an Ethereum node (execution + consensus) alongside an observability stack.

## Services

### `nethermind`
Execution layer client. Processes transactions, executes the EVM, and exposes the JSON-RPC and Engine API used by the consensus client.

### `beacon`
Consensus layer client (Prysm). Drives consensus, follows the beacon chain, and instructs `nethermind` what to build/validate via the Engine API. Authenticates to `nethermind` with a shared JWT secret mounted from `./data/${NETWORK}/ipc`.

### `alloy`
Grafana Alloy agent. Tails the beacon node logs and ships them to `loki`.

### `loki`
Log aggregation backend. Stores logs forwarded by `alloy` so they can be queried from Grafana.

### `prometheus`
Metrics database. Scrapes metrics endpoints (Nethermind, beacon monitoring, etc.) defined in `configuration/prometheus.yml`.

### `pyroscope`
Continuous profiling backend. Receives pprof profiles (the beacon node exposes pprof on `:6060`).

### `peer-geo-exporter`
Small Go service to a geographic location using a local DB-IP Lite City database, and exposes the `beacon_peer_geo` Prometheus gauge that feeds the world-map panel on the beacon dashboard.

### `grafana`
Dashboards and UI for the data stored in Loki, Prometheus, and Pyroscope. Datasources and dashboards are provisioned from `configuration/grafana/`.

## Ports exposed to the world

Only ports bound to `0.0.0.0` (the default when no host IP is given) are reachable from outside the host. Everything bound to `127.0.0.1` is local-only.

| Port | Proto | Service | Reason |
|------|-------|---------|--------|
| 30303 | TCP/UDP | nethermind | Execution layer P2P (devp2p). Required for peer discovery and block/tx gossip. |
| 12000 | UDP | beacon | Consensus layer discv5 discovery. Required to find beacon peers. |
| 13000 | TCP+UDP | beacon | Consensus layer libp2p. Required for block/attestation gossip and sync. |
| 3000 | TCP | grafana | Web UI. Exposed so the dashboards can be reached from a browser. |

All other ports (Nethermind JSON-RPC `8545` and Engine API `8551`; beacon REST `3500`, gRPC `4000`, monitoring `8080`, pprof `6060`; Loki `3100`; Prometheus `9090`; Pyroscope `4040`) are bound to `127.0.0.1` and only reachable from the host itself — typically via SSH port-forwarding.

## `.env` file

`docker-compose.yaml` interpolates the following variables, which must be defined in a `.env` file next to it:

| Variable | Description |
|----------|-------------|
| `NETWORK` | Ethereum network name (e.g. `mainnet`, `sepolia`, `holesky`). Used as the Nethermind `--config` value, the beacon `--<network>` flag, and to namespace the on-disk data directory under `./data/${NETWORK}/`. |
| `NETHERMIND_IMAGE` | Docker image (with tag) to use for the `nethermind` service, e.g. `nethermind/nethermind:1.34.0`. Pinned here so upgrades are explicit. |
| `BEACON_IMAGE` | Docker image (with tag) to use for the `beacon` service, e.g. `gcr.io/prysmaticlabs/prysm/beacon-chain:v6.0.4`. |
| `CHECKPOINT_SYNC_URL` | URL of a trusted beacon checkpoint-sync provider. Lets the beacon node start from a recent finalized state instead of syncing from genesis. |
| `P2P_HOST_IP` | Public IP address of the host, advertised by the beacon node to peers (`--p2p-host-ip`). Required so inbound libp2p connections from the rest of the network can reach this node. |


## Pausing and resuming the stack

To take the node offline for a while without losing chain or observability data, use `stop` (or `down`) — **never** `down -v`.

```sh
# pause
docker compose stop

# resume (later)
docker compose start
```

`docker compose down` is also safe — it removes the containers but keeps the bind mounts under `./data/` and the named volumes (`loki`, `prometheus`, `grafana`, `tempo`, `peer-geo`, `fork-choice`). Resume with `docker compose up -d`. The `-v` flag is what deletes named volumes, so don't pass it.

What happens when you resume after a few days of downtime:
- **EL** resumes from its last block and snap-syncs the missed range (minutes to a couple of hours depending on how far behind).
- **beacon** resumes from its last finalized state and fetches missing slots from peers. Blob retention is ~18 days, so blob backfill works fine for pauses shorter than that — no need to redo a checkpoint sync.
- **Prometheus / Loki / Grafana** keep their existing data; you just have a gap on the graphs covering the downtime.

One thing to check before resuming: if your host's public IP changed during the downtime (VPN, ISP lease), update `P2P_HOST_IP` in `.env` first. Otherwise the beacon advertises a stale address and inbound libp2p connections fail.

Quick post-resume sanity check:
```sh
docker compose ps
curl -s http://127.0.0.1:3500/eth/v1/node/syncing
```

## Links
- [http://<P2P_HOST_IP>:3000/d/adnmforf/beacon-node](http://<P2P_HOST_IP>:3000/d/adnmforf/beacon-node) - The beacon node Grafana Dashboard

## `infra-ai` — observability assistant

A small FastAPI service that turns the metrics/logs in this stack into natural-language briefings. You ask "how is my node doing?" in a browser and it queries Prometheus, Loki, and the beacon REST API to produce a short status report.

Built and wired into the alternate compose file `docker-compose.hoodi-2026-05-18.yaml`. The original `docker-compose.yaml` does **not** include it (deliberately — that file is the stable mainnet setup; `infra-ai` lives with the Hoodi/testing setup until it's been used long enough to harden).

### What it is

- A FastAPI process in `./infra-ai/` (Python 3.12, single container).
- Speaks an OpenAI-compatible chat-completions + tool-use API against whichever LLM provider you select.
- Serves a static chat UI at `/` so you can use it from a browser.
- Reachable at `http://127.0.0.1:7777` (bound to loopback — SSH-forward it like the rest of the stack).

### LLM providers

Selectable per-request via the UI dropdowns or in the JSON body of `POST /ask`. Configure each in `.env`:

| Provider | Endpoint | When to pick it |
|----------|----------|-----------------|
| `lmstudio` | `http://host.docker.internal:1234/v1` | Local model on your workstation. Zero egress, no per-token cost, slower (~100s/turn for a 30B model on consumer hw). |
| `groq` | `https://api.groq.com/openai/v1` | Cloud. Fastest path to a briefing (~2–5s/turn). Free tier has a 100K-tokens-per-day-per-model cap. |
| `cerebras` | `https://api.cerebras.ai/v1` | Cloud. Comparable speed to Groq, separate quota. Has `gpt-oss-120b` which is good for the reasoning-heavy queries. |

Each provider has its own `*_API_KEY` and `*_MODEL` env var; see `.env` for the layout. `LLM_PROVIDER=...` in `.env` only sets the default — the UI always lets you switch per-message.

The available-models list in the UI is fetched live from each provider's `/v1/models` endpoint when the page loads. For Groq, the list is filtered to a curated set of tool-capable models (some Groq models don't support OpenAI tool-use); the others go through unfiltered.

### Tools the model can call

The model produces briefings by calling these tools (defined in `infra-ai/app/tools.py`). It cannot do anything else — there is no shell access, no write paths, no docker socket.

| Tool | Wraps | What it returns |
|------|-------|-----------------|
| `prom_metric_search(substring)` | `/api/v1/label/__name__/values` | Metric names matching a substring. Used when the model isn't sure what metric exists. |
| `prom_instant(query)` | `/api/v1/query` | Current sample(s) for a PromQL expression. |
| `prom_range(query, minutes_back, step)` | `/api/v1/query_range` | A time-series, summarised to first/last/min/max per series (max 10 series). |
| `loki_query(logql, minutes_back, limit)` | `/loki/api/v1/query_range` | Recent log lines matching a LogQL expression. Hard cap 50 lines, each clipped to 220 chars. |
| `beacon_get(path)` | `http://beacon:3500{path}` | Any beacon REST endpoint — sync, peers, finality, fork-choice. |

All responses are clipped before they reach the model (per-result JSON capped at 3.5 KB) so the context window doesn't blow up on log-heavy queries.

### Briefing format

The system prompt instructs the model to lead with one plain-English sentence summarising overall state, then emit only the labelled sections it has real data for. Empty sections are omitted entirely — no "unknown" or "N/A" lines. Bytes are formatted as KB/MB/GB.

```
The node is healthy and at head.

HEAD          slot 3075194, sync_distance 0, is_syncing false, is_optimistic false
PEERS         65 connected
RESOURCE      4.91 GB beacon RSS, 588 goroutines, 213 MB geth RSS
EXECUTION     geth head block 2839691, header 2839691, finalized 2839610, 16 peers, 52.6 GB disk
ANOMALIES     no errors in last 30m (loki_query)
```

The frontend detects the labelled-section layout and renders the answer in a monospace block.

### HTTP API

`GET /healthz` — liveness; returns the configured default provider.

`GET /providers` — probes each configured provider and returns `{available, default_model, models[]}` per provider. The UI uses this to populate dropdowns.

`POST /ask` — non-streaming. Body: `{q, history?, provider?, model?}`. Returns `{answer, trace[], provider, model}`. Errors are surfaced as JSON with `error_type` and a readable message (rate limit, bad request, connection failure).

`POST /ask/stream` — Server-Sent Events with `tool_start`, `tool_end`, `meta`, `error`, and `final` events. The frontend uses this; you'd also use it from any other client that wants to show progress live.

### Config (in `.env`)

```sh
LLM_PROVIDER=groq            # default; per-request override always wins

LMSTUDIO_URL=http://host.docker.internal:1234/v1
LMSTUDIO_MODEL=qwen/qwen3-coder-30b

GROQ_API_KEY=...
GROQ_MODEL=llama-3.3-70b-versatile

CEREBRAS_API_KEY=...
CEREBRAS_MODEL=gpt-oss-120b
```

The container picks these up at startup. Adding a new OpenAI-compatible provider is a ~20-line patch to `agent.py` (a new branch in `_resolve()` and a probe in `list_providers()`).

### Hooking it into another node

The assistant is decoupled from this specific stack via four env vars:

```
PROM_URL    default http://prometheus:9090
LOKI_URL    default http://loki:3100
BEACON_URL  default http://beacon:3500
```

Point them at any reachable Prometheus / Loki / beacon REST endpoint and the tools work unchanged. To use it on a node that doesn't run this compose:

1. Copy `infra-ai/` to that node.
2. `docker build -t infra-ai .`
3. `docker run --rm -p 127.0.0.1:7777:7777 \
     -e PROM_URL=http://your-prom:9090 \
     -e LOKI_URL=http://your-loki:3100 \
     -e BEACON_URL=http://your-beacon:3500 \
     -e LLM_PROVIDER=groq -e GROQ_API_KEY=... \
     infra-ai`

If Loki isn't running on that stack, the `loki_query` tool will just return errors; the other tools still work and the model will avoid the ANOMALIES section.

### File layout

```
infra-ai/
├── Dockerfile
├── requirements.txt
└── app/
    ├── main.py          # FastAPI routes, static mount
    ├── agent.py         # Provider resolution, tool-use loop, system prompt
    ├── tools.py         # 5 tools (prom_metric_search, prom_instant, prom_range, loki_query, beacon_get)
    └── static/
        ├── index.html   # Chat shell + provider/model dropdowns
        ├── style.css    # Dark theme, monospace
        └── app.js       # SSE consumer, history kept client-side
```

### Known v1 rough edges

- No request cancellation in the UI (a long round-trip can't be aborted).
- No tool-result caching — identical questions re-run all tool calls.
- Some Groq models (e.g. `llama-3.3-70b-versatile`) intermittently emit malformed tool calls; retry usually works. No auto-retry/fallback yet.
- Conversation history is held in the page only; reload wipes it.

See `docs/ai-assistant-design.md` for the longer design notes.
