import json
import os
from typing import Iterator, Optional
import httpx
from openai import OpenAI, BadRequestError

from .tools import TOOL_SCHEMAS, TOOL_IMPLS

DEFAULT_PROVIDER = os.getenv("LLM_PROVIDER", "lmstudio").lower()
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "8"))
TOOL_USE_RETRIES = int(os.getenv("TOOL_USE_RETRIES", "1"))

LMSTUDIO_URL = os.getenv("LMSTUDIO_URL", "http://host.docker.internal:1234/v1")
LMSTUDIO_API_KEY = os.getenv("LMSTUDIO_API_KEY", "lm-studio")
LMSTUDIO_MODEL = os.getenv("LMSTUDIO_MODEL", "qwen/qwen3-coder-30b")

GROQ_URL = os.getenv("GROQ_URL", "https://api.groq.com/openai/v1")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

CEREBRAS_URL = os.getenv("CEREBRAS_URL", "https://api.cerebras.ai/v1")
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "llama-3.3-70b")


SYSTEM_PROMPT = """You are an observability assistant for a single Ethereum node running on the Hoodi testnet.
Stack: Geth (execution), Prysm (consensus, called "beacon"), Prometheus, Loki, Pyroscope, Tempo, Grafana.

== RESPONSE FORMAT — strict ==
Every briefing starts with ONE plain-English sentence (max 2) summarising the node's overall state.
Then a blank line.
Then ONLY the labelled sections that you have real data for. NEVER write "unknown" or "N/A" — if you don't have it, omit the line entirely.
Labels (use only what applies):

HEAD          <slot, sync_distance, is_syncing, is_optimistic>
FINALITY      <finalized epoch, justified epoch>
PEERS         <connected, by direction or state>
EXECUTION     <geth head block vs header, sync state, peers, disk>
RESOURCE      <memory, CPU, goroutines — formatted human-readable>
ANOMALIES     <log errors, restarts, unusual metric changes — only if observed>
NEXT          <one concrete next step if something looks off>

== ABSOLUTE RULES ==
1. If you didn't gather data for a section, DELETE THE LINE ENTIRELY. Do NOT write any of: "unknown", "N/A", "not checked", "not gathered", "TBD", "?", "—". The line must simply not appear in your output.
2. NEVER claim absence of something you didn't check. If you didn't call `loki_query`, OMIT the ANOMALIES line — do NOT write "0 errors", "no anomalies", "no restarts".
3. NEVER report raw byte counts. Format bytes as KB/MB/GB/TB:
   - 1135362560 → 1.06 GB
   - 213161288 → 203 MB
   - 850000000000 → 792 GB
4. Be terse. One short line per section.
5. Cite real numbers from tool calls — never invent.

== EXAMPLE — WRONG vs RIGHT ==
WRONG (emits empty sections):
  The node is healthy.

  HEAD          slot 3075124, sync_distance 0
  FINALITY      not checked
  PEERS         not checked
  EXECUTION     not checked
  RESOURCE      not checked
  ANOMALIES     no errors

RIGHT (only emits checked sections):
  The node is healthy and at head.

  HEAD          slot 3075124, sync_distance 0, is_syncing false
  ANOMALIES     no errors in last 30m (loki_query)

== TOOL USAGE ==
- Job labels in Prometheus are: `beacon`, `geth`, `node`, `peer-geo`, `fork-choice`. Prysm job is `beacon` (never `prysm`).
- If a query returns 0 samples, do NOT report "unknown" — call `prom_metric_search` to find the real name and retry.
- For ANOMALIES, you MUST call `loki_query` (e.g., `{service_name="beacon"} |~ "(?i)error|warn|fatal"`). If the call returns 0 lines, you may state "no errors in last 30m"; if you didn't call it, omit the section.
- Prefer 2–5 well-chosen tool calls over many shotgun ones.

== KNOWN GOOD METRIC NAMES (verified) ==
Beacon (Prysm, job="beacon"):
  beacon_head_slot                              current head slot
  beacon_current_active_validators              active validator count
  p2p_peer_count{state="Connected"}             beacon peers (use state="Connected")
  process_resident_memory_bytes{job="beacon"}   beacon RSS (bytes — convert!)
  go_goroutines{job="beacon"}                   beacon goroutines

Geth (job="geth"):
  chain_head_block                              EL head block (0 while snap-syncing)
  chain_head_header                             EL header tip (catches up first)
  chain_head_finalized                          EL finalized block
  eth_db_chaindata_disk_size                    geth on-disk size (bytes — convert!)
  system_memory_used                            geth process RSS (bytes — convert!)
  system_cpu_procload                           geth CPU load
  p2p_peers{job="geth"}                         geth peers
  connected_libp2p_peers                        geth libp2p peers

Geth does NOT export process_resident_memory_bytes for itself — use system_memory_used.

Beacon REST endpoints:
  /eth/v1/node/syncing
  /eth/v1/node/peer_count
  /eth/v1/beacon/headers/head
  /eth/v1/beacon/states/head/finality_checkpoints

== EXAMPLES OF GOOD OPENING SENTENCES ==
"The node is healthy and caught up to head; geth is still snap-syncing (header at 2.84M, block at 0)."
"Beacon is fully synced; geth is mid-snap with 1 peer — peer count is the immediate concern."
"Both clients are at head and finality is current."
"""


# Ranked fallback list used when the active provider/model emits a malformed
# tool call. Order: different provider first (most likely to succeed), then
# alternative tool-capable models on the same provider, then local. A `None`
# model means "use whatever the provider's default_model resolves to".
_FALLBACK_ORDER = [
    ("cerebras", "gpt-oss-120b"),
    ("groq", "openai/gpt-oss-120b"),
    ("groq", "moonshotai/kimi-k2-instruct"),
    ("groq", "openai/gpt-oss-20b"),
    ("lmstudio", None),
]


def _is_tool_use_failed(e: Exception) -> bool:
    """True for Groq's `tool_use_failed` 400 (model emitted a malformed tool call)."""
    if not isinstance(e, BadRequestError):
        return False
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error") or {}
        if isinstance(err, dict) and err.get("code") == "tool_use_failed":
            return True
    s = str(e)
    return "tool_use_failed" in s or "tool call validation failed" in s


def _fallback_suggestions(current_provider: str, current_model: str,
                          max_n: int = 3) -> list[dict]:
    """Probe providers and return up to `max_n` ranked alternatives, skipping
    the current pick and anything unavailable."""
    probes = list_providers().get("providers", {})
    out: list[dict] = []
    for prov, model in _FALLBACK_ORDER:
        info = probes.get(prov) or {}
        if not info.get("available"):
            continue
        models = info.get("models") or []
        chosen = model if (model and model in models) else info.get("default_model")
        if not chosen:
            continue
        if prov == current_provider and chosen == current_model:
            continue
        if any(s["provider"] == prov and s["model"] == chosen for s in out):
            continue
        out.append({"provider": prov, "model": chosen})
        if len(out) >= max_n:
            break
    return out


def _resolve(provider: Optional[str], model: Optional[str]) -> tuple[str, str, str, str]:
    p = (provider or DEFAULT_PROVIDER).lower()
    if p == "groq":
        if not GROQ_API_KEY:
            raise RuntimeError("provider=groq but GROQ_API_KEY is empty")
        return p, GROQ_URL, GROQ_API_KEY, (model or GROQ_MODEL)
    if p == "cerebras":
        if not CEREBRAS_API_KEY:
            raise RuntimeError("provider=cerebras but CEREBRAS_API_KEY is empty")
        return p, CEREBRAS_URL, CEREBRAS_API_KEY, (model or CEREBRAS_MODEL)
    if p == "lmstudio":
        return p, LMSTUDIO_URL, LMSTUDIO_API_KEY, (model or LMSTUDIO_MODEL)
    raise RuntimeError(f"unknown provider: {p}")


def _client_for(base_url: str, api_key: str) -> OpenAI:
    return OpenAI(base_url=base_url, api_key=api_key)


def ask(user_question: str, history: list | None = None,
        provider: Optional[str] = None, model: Optional[str] = None) -> dict:
    p, url, key, m = _resolve(provider, model)
    client = _client_for(url, key)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_question})
    trace = []

    for _ in range(MAX_TOOL_ROUNDS):
        resp = None
        for attempt in range(TOOL_USE_RETRIES + 1):
            try:
                resp = client.chat.completions.create(
                    model=m, messages=messages, tools=TOOL_SCHEMAS,
                    tool_choice="auto", temperature=0.2,
                )
                break
            except BadRequestError as e:
                if _is_tool_use_failed(e) and attempt < TOOL_USE_RETRIES:
                    continue
                if _is_tool_use_failed(e):
                    return {
                        "error_type": "tool_use_failed",
                        "message": str(e),
                        "provider": p, "model": m,
                        "suggestions": _fallback_suggestions(p, m),
                        "trace": trace,
                    }
                raise
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            return {"answer": msg.content or "", "trace": trace, "provider": p, "model": m}

        for call in msg.tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            impl = TOOL_IMPLS.get(name)
            if not impl:
                result = {"error": f"unknown tool {name}"}
            else:
                try:
                    result = impl(**args)
                except Exception as e:
                    result = {"error": f"{type(e).__name__}: {e}"}
            trace.append({"tool": name, "args": args, "result_preview": _preview(result)})
            messages.append({
                "role": "tool", "tool_call_id": call.id,
                "content": json.dumps(result)[:3500],
            })

    return {"answer": "(stopped after max tool rounds)", "trace": trace, "provider": p, "model": m}


def ask_stream(user_question: str, history: list | None = None,
               provider: Optional[str] = None, model: Optional[str] = None) -> Iterator[dict]:
    try:
        p, url, key, m = _resolve(provider, model)
    except Exception as e:
        yield {"type": "error", "message": str(e)}
        return
    client = _client_for(url, key)
    yield {"type": "meta", "provider": p, "model": m}

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_question})

    for _ in range(MAX_TOOL_ROUNDS):
        resp = None
        for attempt in range(TOOL_USE_RETRIES + 1):
            try:
                resp = client.chat.completions.create(
                    model=m, messages=messages, tools=TOOL_SCHEMAS,
                    tool_choice="auto", temperature=0.2,
                )
                break
            except BadRequestError as e:
                if _is_tool_use_failed(e) and attempt < TOOL_USE_RETRIES:
                    yield {"type": "retry", "attempt": attempt + 1,
                           "max": TOOL_USE_RETRIES, "reason": "tool_use_failed"}
                    continue
                if _is_tool_use_failed(e):
                    yield {"type": "recoverable_error",
                           "error_type": "tool_use_failed",
                           "message": str(e),
                           "provider": p, "model": m,
                           "suggestions": _fallback_suggestions(p, m)}
                    return
                yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
                return
            except Exception as e:
                yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
                return
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            yield {"type": "final", "answer": msg.content or ""}
            return

        for call in msg.tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            yield {"type": "tool_start", "tool": name, "args": args}
            impl = TOOL_IMPLS.get(name)
            if not impl:
                result = {"error": f"unknown tool {name}"}
            else:
                try:
                    result = impl(**args)
                except Exception as e:
                    result = {"error": f"{type(e).__name__}: {e}"}
            yield {"type": "tool_end", "tool": name, "result_preview": _preview(result)}
            messages.append({
                "role": "tool", "tool_call_id": call.id,
                "content": json.dumps(result)[:3500],
            })

    yield {"type": "final", "answer": "(stopped after max tool rounds)"}


def list_providers() -> dict:
    """Probe each provider and return available models. Used by the frontend selector."""
    out = {"default_provider": DEFAULT_PROVIDER, "providers": {}}

    # lmstudio: no auth, may not be reachable
    try:
        r = httpx.get(f"{LMSTUDIO_URL}/models", timeout=4)
        if r.status_code == 200:
            models = sorted(m["id"] for m in r.json().get("data", []))
            out["providers"]["lmstudio"] = {
                "available": True,
                "default_model": LMSTUDIO_MODEL if LMSTUDIO_MODEL in models else (models[0] if models else None),
                "models": models,
            }
        else:
            out["providers"]["lmstudio"] = {"available": False, "error": f"status {r.status_code}"}
    except Exception as e:
        out["providers"]["lmstudio"] = {"available": False, "error": f"{type(e).__name__}: {e}"}

    # groq: needs key
    if GROQ_API_KEY:
        try:
            r = httpx.get(f"{GROQ_URL}/models",
                          headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=6)
            if r.status_code == 200:
                models = sorted(m["id"] for m in r.json().get("data", []))
                # whitelist of Groq models known to support tool-use (curated)
                tool_capable = {
                    "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
                    "qwen/qwen3-32b", "openai/gpt-oss-120b", "openai/gpt-oss-20b",
                    "moonshotai/kimi-k2-instruct", "meta-llama/llama-4-scout-17b-16e-instruct",
                    "groq/compound", "groq/compound-mini",
                }
                models = [m for m in models if m in tool_capable] or models
                out["providers"]["groq"] = {
                    "available": True,
                    "default_model": GROQ_MODEL if GROQ_MODEL in models else (models[0] if models else None),
                    "models": models,
                }
            else:
                out["providers"]["groq"] = {"available": False, "error": f"status {r.status_code}"}
        except Exception as e:
            out["providers"]["groq"] = {"available": False, "error": f"{type(e).__name__}: {e}"}
    else:
        out["providers"]["groq"] = {"available": False, "error": "GROQ_API_KEY not set"}

    # cerebras: needs key
    if CEREBRAS_API_KEY:
        try:
            r = httpx.get(f"{CEREBRAS_URL}/models",
                          headers={"Authorization": f"Bearer {CEREBRAS_API_KEY}"}, timeout=6)
            if r.status_code == 200:
                models = sorted(m["id"] for m in r.json().get("data", []))
                out["providers"]["cerebras"] = {
                    "available": True,
                    "default_model": CEREBRAS_MODEL if CEREBRAS_MODEL in models else (models[0] if models else None),
                    "models": models,
                }
            else:
                out["providers"]["cerebras"] = {"available": False, "error": f"status {r.status_code}"}
        except Exception as e:
            out["providers"]["cerebras"] = {"available": False, "error": f"{type(e).__name__}: {e}"}
    else:
        out["providers"]["cerebras"] = {"available": False, "error": "CEREBRAS_API_KEY not set"}

    return out


def _preview(obj) -> str:
    s = json.dumps(obj, default=str)
    return s if len(s) <= 400 else s[:400] + "..."
