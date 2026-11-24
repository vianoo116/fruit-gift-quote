"""FastAPI 服务：提供网页和 /api/quote 接口。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
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
