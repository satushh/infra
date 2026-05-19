import os
import time
import httpx

PROM_URL = os.getenv("PROM_URL", "http://prometheus:9090")
LOKI_URL = os.getenv("LOKI_URL", "http://loki:3100")
BEACON_URL = os.getenv("BEACON_URL", "http://beacon:3500")

_client = httpx.Client(timeout=20.0)


def _clip(s: str, n: int = 8000) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"\n... [truncated, original {len(s)} chars]"


def prom_metric_search(substring: str, limit: int = 60) -> dict:
    """Find Prometheus metric names containing a substring. Use to discover what's available before querying."""
    r = _client.get(f"{PROM_URL}/api/v1/label/__name__/values")
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        return {"error": data}
    s = substring.lower()
    matches = [n for n in data["data"] if s in n.lower()]
    return {"total_match": len(matches), "names": matches[:limit], "truncated": len(matches) > limit}


def prom_instant(query: str) -> dict:
    r = _client.get(f"{PROM_URL}/api/v1/query", params={"query": query})
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        return {"error": data}
    results = data["data"]["result"]
    return {
        "resultType": data["data"]["resultType"],
        "count": len(results),
        "samples": results[:50],
        "truncated": len(results) > 50,
    }


def prom_range(query: str, minutes_back: int = 60, step: str = "60s") -> dict:
    end = int(time.time())
    start = end - minutes_back * 60
    r = _client.get(
        f"{PROM_URL}/api/v1/query_range",
        params={"query": query, "start": start, "end": end, "step": step},
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        return {"error": data}
    results = data["data"]["result"]
    trimmed = []
    for series in results[:10]:
        vals = series.get("values", [])
        trimmed.append({
            "metric": series.get("metric"),
            "sample_count": len(vals),
            "first": vals[0] if vals else None,
            "last": vals[-1] if vals else None,
            "min": min((float(v[1]) for v in vals), default=None),
            "max": max((float(v[1]) for v in vals), default=None),
        })
    return {"series_count": len(results), "series": trimmed, "truncated_series": len(results) > 10}


def loki_query(logql: str, minutes_back: int = 30, limit: int = 30) -> dict:
    limit = min(limit, 50)
    end_ns = int(time.time() * 1e9)
    start_ns = end_ns - minutes_back * 60 * int(1e9)
    r = _client.get(
        f"{LOKI_URL}/loki/api/v1/query_range",
        params={"query": logql, "start": start_ns, "end": end_ns, "limit": limit, "direction": "backward"},
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        return {"error": data}
    streams = data["data"]["result"]
    flat = []
    for stream in streams:
        labels = stream.get("stream", {})
        for ts, line in stream.get("values", []):
            flat.append({"labels": labels, "ts": ts, "line": _clip(line, 220)})
    flat.sort(key=lambda x: x["ts"], reverse=True)
    return {"line_count": len(flat), "lines": flat[:limit]}


def beacon_get(path: str) -> dict:
    if not path.startswith("/"):
        path = "/" + path
    r = _client.get(f"{BEACON_URL}{path}")
    if r.status_code >= 400:
        return {"status_code": r.status_code, "body": _clip(r.text, 2000)}
    try:
        return r.json()
    except Exception:
        return {"text": _clip(r.text, 2000)}


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "prom_metric_search",
            "description": "Find Prometheus metric names containing a substring. Use FIRST when unsure what metric to query, e.g. 'memory', 'peers', 'sync', 'head'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "substring": {"type": "string", "description": "Substring to match (case-insensitive)."},
                    "limit": {"type": "integer", "default": 60},
                },
                "required": ["substring"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prom_instant",
            "description": "Run a Prometheus instant query (PromQL). Returns the current sample(s) for the expression. Use for 'what is the current value of X' questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "A PromQL expression, e.g. 'up{job=\"geth\"}' or 'rate(beacon_head_slot[5m])'."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prom_range",
            "description": "Run a Prometheus range query and return summarized series (first/last/min/max per series). Use for trend questions like 'how has memory grown in the last hour'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "minutes_back": {"type": "integer", "default": 60, "description": "How far back to query, in minutes."},
                    "step": {"type": "string", "default": "60s", "description": "Step duration like '15s', '60s', '5m'."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "loki_query",
            "description": "Search logs in Loki using LogQL. Returns recent matching log lines. Useful for 'any errors in the beacon logs', 'what did geth log around 11:42'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "logql": {"type": "string", "description": "A LogQL expression, e.g. '{service_name=\"beacon\"} |= \"error\"' or '{service_name=\"geth\"}'."},
                    "minutes_back": {"type": "integer", "default": 30},
                    "limit": {"type": "integer", "default": 30, "description": "Max lines to return (hard cap 50)."},
                },
                "required": ["logql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "beacon_get",
            "description": "GET a path on the beacon REST API (http://beacon:3500). Use for sync status, peers, finality, fork-choice. Examples: '/eth/v1/node/syncing', '/eth/v1/node/peer_count', '/eth/v1/beacon/headers/head'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path starting with /."}
                },
                "required": ["path"],
            },
        },
    },
]


TOOL_IMPLS = {
    "prom_metric_search": prom_metric_search,
    "prom_instant": prom_instant,
    "prom_range": prom_range,
    "loki_query": loki_query,
    "beacon_get": beacon_get,
}
