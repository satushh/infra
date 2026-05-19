import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from openai import RateLimitError, BadRequestError, APIConnectionError, APIStatusError

from .agent import ask, ask_stream, list_providers, DEFAULT_PROVIDER

app = FastAPI(title="infra-ai")

STATIC_DIR = Path(__file__).parent / "static"


class AskBody(BaseModel):
    q: str
    history: list | None = None
    provider: Optional[str] = None
    model: Optional[str] = None


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
def healthz():
    return {"ok": True, "default_provider": DEFAULT_PROVIDER}


@app.get("/providers")
def providers():
    return list_providers()


@app.post("/ask")
def ask_endpoint(body: AskBody):
    try:
        return ask(body.q, body.history, body.provider, body.model)
    except RateLimitError as e:
        return JSONResponse(status_code=429, content={"error_type": "rate_limit", "message": str(e)})
    except BadRequestError as e:
        return JSONResponse(status_code=400, content={"error_type": "bad_request", "message": str(e)})
    except APIConnectionError as e:
        return JSONResponse(status_code=502, content={"error_type": "connection", "message": str(e)})
    except APIStatusError as e:
        return JSONResponse(status_code=502, content={"error_type": "api", "message": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error_type": type(e).__name__, "message": str(e)})


@app.post("/ask/stream")
def ask_stream_endpoint(body: AskBody):
    def gen():
        for event in ask_stream(body.q, body.history, body.provider, body.model):
            yield f"data: {json.dumps(event)}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
