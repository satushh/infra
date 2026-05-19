# Node Observability AI Assistant — Design Doc

**Author:** sdas
**Date:** 2026-05-18 (revised same day)
**Status:** v1 built and running

## 1. Problem

Running a Prysm + EL node generates more telemetry than a human wants to watch:
Prometheus has thousands of series, Loki ingests megabytes of logs an hour,
Pyroscope holds continuous profiles, Tempo has traces. A core-dev investigating
issues like the state-diff bug, per-epoch disk usage, or heap regressions ends
up writing the same PromQL/LogQL queries over and over.

We want an assistant that answers questions like:
- "How is my node doing?"
- "Did finality break in the last hour?"
- "Why did the heap grow 200 MB since 9am?"
- "Anything weird in the logs since the last restart?"

and produces a tight briefing pulled from the live observability stack.

## 2. Goals / non-goals

**Goals**
- Briefings on demand: ask in natural language, get a 5–20 line answer with the supporting numbers.
- Use real data: pull live from Prometheus, Loki, beacon REST. No synthetic summaries.
- Run locally beside the node, talking to a **local** LLM (Qwen3-coder-30b via LM Studio). No data leaves the host.
- Pluggable for future scheduled briefings, not v1 scope.

**Non-goals (v1)**
- Auto-remediation. The assistant reports; it does not restart containers or change configs.
- Alerting. Alertmanager already does this; the assistant is for human-driven investigation.
- Multi-node fleets. Single node only.

## 3. Architecture

```
       Browser (http://localhost:7777)
              │
              ▼
   ┌──────────────────────────────────┐
   │ infra-ai container               │
   │   FastAPI (Python)               │
   │   /         — static chat UI     │
   │   /ask      — non-streaming      │
   │   /ask/stream — SSE              │
   │   /healthz                       │
   │                                  │
   │   agent.py  — tool-use loop      │
   │   tools.py  — Prom/Loki/beacon   │
   └────────┬─────────────────────────┘
            │ OpenAI-compatible HTTP
            ▼
   ┌──────────────────────────────────┐
   │ LM Studio on host                │
   │   http://host.docker.internal:1234 │
   │   model: qwen/qwen3-coder-30b    │
   └──────────────────────────────────┘
            │
   ┌────────┼────────────────────────┐
   ▼        ▼        ▼               ▼
 Prometheus Loki   beacon REST    (future: Pyroscope, docker)
   :9090   :3100    :3500
```

One container, one process, talks to:
- LM Studio on the host for inference (OpenAI-compatible API).
- Prometheus, Loki, beacon REST inside the docker network for data.

## 4. LLM choice — Qwen3-coder-30b via LM Studio

Switched from Anthropic to **local Qwen3-coder-30b served by LM Studio**.

- Why local: zero data egress; no API key management; no per-token cost.
- Why Qwen3-coder-30b: strong tool-use, fits in LM Studio on a workstation, OpenAI tool-call schema works as-is.
- Wire: the `openai` Python SDK pointed at `http://host.docker.internal:1234/v1`. Standard `tools=[...]` + `tool_choice="auto"` loop.
- Latency observed at runtime: ~100s for a single tool-using turn on the user's hardware. Fine for human-driven briefings; not fine for chat-style back-and-forth.

Swappable: the only LM-specific code is in `agent.py` (~30 lines). Switch to Anthropic, OpenAI cloud, or another local model by changing `LMSTUDIO_URL` and `LMSTUDIO_MODEL` env vars.

## 5. Tools given to the LLM

Narrow surface so the model picks correctly. All implemented in `app/tools.py`:

| Tool | Wraps | Used for |
|------|-------|----------|
| `prom_instant(query)` | `/api/v1/query` | Current value of a metric |
| `prom_range(query, minutes_back, step)` | `/api/v1/query_range` | Trend over a window |
| `loki_query(logql, minutes_back, limit)` | `/loki/api/v1/query_range` | Log search |
| `beacon_get(path)` | `http://beacon:3500{path}` | Sync, peers, finality, fork-choice |

Results are clipped before the model sees them:
- Prom range: up to 10 series, summarized to first/last/min/max per series.
- Loki: lines clipped to 500 chars, capped at the requested limit.
- Beacon: full body up to 2000 chars on error paths.

Tool results round-tripped to the model are JSON-stringified and capped at 6000 chars so context doesn't blow up.

**v2 candidates** (deliberately out of scope for v1):
- `pyroscope_diff(profile, t1, t2)` — heap/CPU profile diff
- `docker_ps()` — container states + restart counts
- `tempo_trace(trace_id)` — span tree

## 6. Briefing format

The system prompt asks for a fixed-section briefing. Sections are skipped when empty (the model is told *not* to pad — early runs showed it filling with `N/A` lines, which is a prompt-tuning fix in v1.1).

```
HEAD          slot 3074561, sync_distance 0, is_syncing=false, is_optimistic=true
FINALITY      finalized epoch 96073, no missed checkpoints in last 4h
PEERS         47 connected (target 70); inbound 12 / outbound 35
EXECUTION     geth snap-sync 38%, 8 peers
RESOURCE      heap 1.2 GB (↑ 180 MB since 10:00)
ANOMALIES     14 "context deadline exceeded" in beacon log, all from sync/initial
NEXT          wait for snap sync to reach state heal
```

Frontend detects this format and renders it monospace.

## 7. Frontend

Single-page chat UI served by FastAPI from `app/static/`:

- `index.html` — chat shell
- `style.css` — dark theme, monospace
- `app.js` — vanilla JS, no framework. Talks to `/ask/stream` via SSE.

Three event types over SSE:
- `tool_start` — show a spinner + tool name + args
- `tool_end` — replace spinner with result preview
- `final` — render the assistant's text answer

Conversation history is held in the page (`history` array) and re-sent with each request — no server-side session storage in v1. Cleared on reload.

This is intentionally minimal — vanilla JS, one HTML file, no build step. Total frontend code is ~150 lines. If we ever need richer UI (embedded graphs, multi-tab sessions), swap in SvelteKit later; the API shape stays the same.

## 8. Data hygiene

- Never send raw private keys, JWT secrets, or `.env` contents to the LLM. (None of the tools expose them.)
- Log lines from beacon/geth are fine — public via gossip.
- All tool responses are clipped before reaching the model.
- Local inference means no third-party sees prompts, logs, or metric data.

## 9. Where it runs

Added as a service in `docker-compose.hoodi-2026-05-18.yaml`:

```yaml
infra-ai:
  container_name: infra-ai
  build: ./infra-ai
  restart: unless-stopped
  extra_hosts:
    - "host.docker.internal:host-gateway"
  environment:
    - LMSTUDIO_URL=http://host.docker.internal:1234/v1
    - LMSTUDIO_MODEL=qwen/qwen3-coder-30b
    - PROM_URL=http://prometheus:9090
    - LOKI_URL=http://loki:3100
    - BEACON_URL=http://beacon:3500
  ports:
    - "127.0.0.1:7777:7777"
```

Browser → `http://127.0.0.1:7777/`.
Headless → `curl -s -X POST http://127.0.0.1:7777/ask -H 'Content-Type: application/json' -d '{"q":"..."}'`.

## 10. Status & next steps

**Built and running (2026-05-18):**
- Tool wrappers, tool-use loop, FastAPI server, frontend, docker-compose integration.
- End-to-end test: asked "what is the current beacon sync distance?", model called `beacon_get`, answered correctly from live data.

**Known v1 rough edges to fix in v1.1:**
- System prompt currently makes the model emit `N/A` in empty briefing sections — should *omit* them entirely.
- No request cancellation in the UI yet (a long round-trip can't be aborted).
- No caching — identical questions re-run all tool calls.

**Next features (in order):**
1. Tighten the system prompt (skip empty sections; force concrete numbers).
2. Add `pyroscope_diff` tool for the heap-regression use case.
3. 30-second response cache per (tool, args).
4. Request cancellation button in the frontend.
5. Optional: scheduled briefings (cron → write to local sqlite + display history page).
6. Optional: Grafana panel plugin that calls `/ask` and renders the briefing.

## 11. Open questions

- Should the assistant be able to write *back* to Grafana annotations on charts when it identifies an event? Probably yes, but punt past v1.
- Scheduled briefings — useful, but needs a target (Slack? local log file?). Decide after using on-demand for a week.
- Conversation persistence — server-side history with sqlite would let us audit past briefings and resume sessions. Cheap; defer to v1.2.
