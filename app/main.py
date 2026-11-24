"""FastAPI 服务：网页 + /api/quote（一次性返回）+ /api/quote/stream（实时进度，SSE）。"""
from __future__ import annotations

import asyncio
import json
import threading
from contextvars import copy_context
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .graph import run
from .llm import BudgetExceeded, model_name

STATIC = Path(__file__).resolve().parent.parent / "static"
app = FastAPI(title="水果礼盒 / 大单报价助手")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class QuoteIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "model": model_name()}


@app.post("/api/quote")
async def quote(body: QuoteIn):
    try:
        return await run_in_threadpool(run, body.text.strip())
    except BudgetExceeded as e:
        raise HTTPException(status_code=429, detail=str(e))


@app.post("/api/quote/stream")
async def quote_stream(body: QuoteIn):
    """每完成一个节点推送一条 progress 事件，最后推送 result 事件。"""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def emit(event: str, data):
        loop.call_soon_threadsafe(queue.put_nowait, (event, data))

    def worker():
        try:
            res = run(body.text.strip(), on_progress=lambda step, node: emit("progress", {"step": step, "node": node}))
            emit("result", res)
        except BudgetExceeded as e:
            emit("result", {"status": "error", "error": str(e), "elapsed_s": 0, "usage": {"calls": 0, "cost_usd": 0}})
        except Exception as e:  # noqa: BLE001
            emit("result", {"status": "error", "error": f"服务出错：{e}", "elapsed_s": 0, "usage": {"calls": 0, "cost_usd": 0}})
        emit("end", None)

    threading.Thread(target=copy_context().run, args=(worker,), daemon=True).start()

    async def gen():
        while True:
            event, data = await queue.get()
            if event == "end":
                break
            yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
