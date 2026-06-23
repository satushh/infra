# Ethereum infrastructure

Docker Compose stack to run an Ethereum node (execution + consensus) alongside an observability stack.

## Services

### `jwt-init`
One-shot bootstrap container. Generates the shared Engine API JWT secret at `./data/${NETWORK}/ipc/jwt-secret` if it does not already exist, then exits. The execution client and `beacon` both `depends_on` it (`service_completed_successfully`), so the secret is guaranteed to be present before they start. This is client-agnostic: both Nethermind and Geth would create the secret themselves if missing, but generating it up front guarantees the file is present before either starts.

### `nethermind` / `geth` (execution client)
Execution layer client. Processes transactions, executes the EVM, and exposes the JSON-RPC and Engine API used by the consensus client. The stack ships **two** alternative execution clients — Nethermind and Geth — exactly one runs at a time, selected via a Compose profile (see [Choosing the execution client](#choosing-the-execution-client)). Whichever is active is reachable under the shared network alias `execution`, so the rest of the stack does not care which one runs.

### `beacon`
Consensus layer client (Prysm). Drives consensus, follows the beacon chain, and instructs the execution client what to build/validate via the Engine API (`http://execution:8551`). Authenticates with a shared JWT secret mounted from `./data/${NETWORK}/ipc`.

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
| `NETWORK` | Ethereum network name (e.g. `mainnet`, `hoodi`). |
| `COMPOSE_PROFILES` | Selects the execution client: `nethermind` or `geth`. Only the matching service is started. |
| `NETHERMIND_IMAGE` | Docker image (with tag) for the `nethermind` service, e.g. `nethermind/nethermind:1.37.2`. Pinned here so upgrades are explicit. Used only when `COMPOSE_PROFILES=nethermind`. |
| `GETH_IMAGE` | Docker image (with tag) for the `geth` service, e.g. `ethereum/client-go:v1.16.1`. Pinned here so upgrades are explicit. Used only when `COMPOSE_PROFILES=geth`. |
| `BEACON_IMAGE` | Docker image (with tag) to use for the `beacon` service, e.g. `gcr.io/prysmaticlabs/prysm/beacon-chain:v6.0.4`. |
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
- Some Groq models (e.g. `llama-3.3-70b-versatile`) intermittently emit malformed tool calls (text-encoded `<function=...>` instead of a structured tool call) and Groq's server rejects the request with `tool_use_failed`. The agent auto-retries once (configurable via `TOOL_USE_RETRIES`); on exhaustion the UI shows an inline panel offering ranked fallback provider/model picks — clicking one switches the dropdowns (sticky) and re-runs the same question.
- Conversation history is held in the page only; reload wipes it.

### Adding a new LLM provider

Any OpenAI-compatible chat-completions endpoint can be plugged in with a small patch in three places:

1. **`.env`** — add `MYPROVIDER_API_KEY=...` and `MYPROVIDER_MODEL=...` (plus an optional `MYPROVIDER_URL` if it isn't the default).
2. **`docker-compose.hoodi-2026-05-18.yaml`** — forward both env vars into the `infra-ai` service's `environment:` block, with `${VAR:-default}` syntax so the container starts even when unset.
3. **`infra-ai/app/agent.py`** — read the env vars near the top of the file, add a branch to `_resolve()` that returns the matching `(provider, base_url, api_key, model)` tuple, and add a probe block to `list_providers()` that calls `<base_url>/models` so the frontend dropdown can populate.

Rebuild with `docker compose -f docker-compose.hoodi-2026-05-18.yaml up -d --build infra-ai`. The new provider then shows up in the UI dropdown automatically.

The Groq, Cerebras, and LM Studio integrations in `agent.py` are each ~20 lines and serve as templates. If the new provider doesn't return a useful `/models` list (some don't), the frontend dropdown falls back gracefully — you can still pass `model` explicitly per-request.

### Where the model gets its "known good" metric names

The system prompt in `infra-ai/app/agent.py` includes a curated cheat sheet of real metric names verified against this stack (e.g. `beacon_head_slot`, `chain_head_block`, `eth_db_chaindata_disk_size`, `system_memory_used`, `p2p_peer_count{state="Connected"}`). When extending the assistant to a stack with different metric labels (different EL client, additional exporters), update that block — otherwise the model will keep querying names that don't exist and recovering via `prom_metric_search` round-trips, which is slow.

See `docs/ai-assistant-design.md` for the longer design notes.

## Running ePBS devnets with Kurtosis (and pointing `infra-ai` at them)

The repo contains `kurtosis-epbs-devnet-4.yaml` — a Kurtosis ethereum-package args file
for the **glamsterdam-devnet-4 / gloas (ePBS)** devnet. Prysm + Lighthouse paired with
ethrex, 6s slots, `gloas_fork_epoch: 1`, with `dora`, `assertoor`, `spamoor`,
`checkpointz` add-ons and a builder-lifecycle assertoor playbook.

### Why not just put this in `docker-compose.yaml`?

Kurtosis is a separate orchestrator on top of Docker, with its own engine and
Starlark configs. Trying to wrap it inside `docker-compose` re-implements what
Kurtosis already does. The clean pattern is: **run Kurtosis directly**, then point
`infra-ai` at whatever the enclave brings up.

### Running the devnet

```sh
kurtosis run --enclave epbs-devnet-4 \
  github.com/ethpandaops/ethereum-package \
  --args-file kurtosis-epbs-devnet-4.yaml
```

Kurtosis prints a service/port table at the end. Before running, sanity-check that
the image tags still exist on the registry — `prysm-beacon-chain:glamsterdam-devnet-4-tmp`
and `eserilev/lighthouse:glamsterdam-devnet-4` are temporary/personal tags that
get rotated.

### Wiring `infra-ai` to the devnet — three layered options

Choose one. All three keep `infra-ai` itself unchanged from how it already runs
against the Hoodi stack; what changes is *where* its `PROM_URL` / `BEACON_URL`
point.

**Option 1 (recommended): add `prometheus_grafana` to `additional_services`**
in `kurtosis-epbs-devnet-4.yaml`. The ethereum-package will then spin up a
Prometheus that scrapes every EL, CL, and validator client in the enclave, plus
a Grafana with pre-built dashboards. `infra-ai` gets a single `PROM_URL` to
target and the model can query per-client metrics directly. Cleanest unlock —
one config line, no glue code.

**Option 2: a small `kurtosis-env.sh` helper** that runs
`kurtosis enclave inspect epbs-devnet-4 --format json`, picks the Prometheus +
a CL REST port out of it, writes a `.env.kurtosis`, then `docker compose up
infra-ai`. Useful if you'll restart the enclave often (ports change every run).

**Option 3: native `kurtosis_inspect` tool inside `infra-ai`** so the LLM can
ask "which services are up in the enclave?" itself. Most flexible, most code —
only worth it if you're poking many enclaves with very different layouts.

### Automatic regular briefings (the "babysitter" pattern)

Once `infra-ai` is pointed at the devnet, you can have a tiny scheduler sidecar
hit `/ask` every N minutes and persist the answers. Sketch (would go into a
new `docker-compose.kurtosis.yaml`, not the Hoodi one):

```yaml
infra-ai-scheduler:
  image: alpine
  depends_on: [infra-ai]
  command: >
    sh -c 'apk add --no-cache curl && while true; do
      curl -s -X POST http://infra-ai:7777/ask
        -H "Content-Type: application/json"
        -d "{\"q\":\"How is the devnet doing? Anomalies in the last 15 min?\",\"provider\":\"groq\"}"
        >> /var/log/briefings/$(date +%Y%m%d-%H%M).json;
      sleep 900;
    done'
  volumes:
    - ./data/briefings:/var/log/briefings
```

That gives you "kurtosis run → automatic AI briefings every 15 min, persisted
to `./data/briefings/`", with no Kurtosis-specific code in `infra-ai`.

### Status (as of 2026-05-19) — what to do next

Nothing in this section is implemented yet — it's the plan agreed at the end of
the previous session. To resume:

1. Edit `kurtosis-epbs-devnet-4.yaml`: add `prometheus_grafana` to
   `additional_services`. Re-verify image tags first.
2. `kurtosis run` the enclave (see command above) and note the published
   Prometheus port from the output.
3. Create `docker-compose.kurtosis.yaml` with just two services: `infra-ai`
   (env vars pointing at the enclave's Prometheus + a CL beacon REST port) and
   `infra-ai-scheduler` (the sidecar above).
4. `docker compose -f docker-compose.kurtosis.yaml up -d` and watch
   `./data/briefings/` fill up.

If port discovery turns out to be annoying enough to warrant Option 2, the
`kurtosis enclave inspect ... --format json` output structure is documented at
https://docs.kurtosis.com/cli/inspect.

## Repository state — handoff notes (2026-06-23)

For anyone (or any Claude session) picking this up cold:

- **Working tree**: branch `infra-ai-hoodi` in this repo, pushed to
  `origin = https://github.com/satushh/infra.git` (the user's fork). Upstream
  is `https://github.com/nalepae/infra.git` as the `upstream` remote. The
  branch was last merged with `upstream/master` on 2026-06-23.
- **What is running locally**: the Hoodi stack from
  `docker-compose.hoodi-2026-05-18.yaml` — Geth + Prysm v7.1.3 + the
  observability stack + `infra-ai`. To check: `docker compose -f
  docker-compose.hoodi-2026-05-18.yaml ps`. To pause without losing data: see
  the "Pausing and resuming the stack" section above (`docker compose stop`,
  *not* `down -v`).
- **What's in `.env`** (gitignored — never commit): three LLM provider API
  keys (`GROQ_API_KEY`, `CEREBRAS_API_KEY`, plus LM Studio URL pointing at
  `host.docker.internal:1234`), plus the Hoodi node config (`NETWORK`,
  `GETH_IMAGE`, `BEACON_IMAGE`, `CHECKPOINT_SYNC_URL`, `P2P_HOST_IP`).
- **User's local source clones**: `go-ethereum/` and `prysm/` are checked out
  in the repo root for reference but are gitignored. Do *not* commit them.
- **Open thread from last session**: this Kurtosis integration. Recommended
  path is Option 1 + the scheduler sidecar above.
- **PR direction not yet decided**: the branch lives on the user's fork. No
  PR has been opened. Whether to PR upstream to `nalepae/infra` or keep this
  as a long-lived fork-only branch is an open call — confirm before opening
  any PR.

If you change any of the above (e.g. land Option 1, or move work back to
`master`), update this section so the next pickup is just as quick.
