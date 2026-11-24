"""OpenRouter 调用封装（OpenAI 兼容接口）+ token / 费用统计 + 费用上限。"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from contextvars import ContextVar

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash-1017"
BASE_URL = "https://openrouter.ai/api/v1"


class BudgetExceeded(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


_lock = threading.Lock()
_process_cost = 0.0  # 本进程累计费用（美元）
_client: OpenAI | None = None

# 每次请求（一条需求）独立统计
_usage: ContextVar[dict | None] = ContextVar("usage", default=None)


def model_name() -> str:
    return os.getenv("OPENROUTER_MODEL") or DEFAULT_MODEL


def max_cost() -> float:
    return float(os.getenv("MAX_COST_USD", "0.5"))


def client() -> OpenAI:
    global _client
    if _client is None:
        key = os.getenv("OPENROUTER_API_KEY")
        if not key:
            raise LLMError("未设置环境变量 OPENROUTER_API_KEY")
        _client = OpenAI(base_url=BASE_URL, api_key=key, timeout=90, max_retries=2)
    return _client


def start_usage() -> dict:
    u = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "by_agent": {}}
    _usage.set(u)
    return u


def process_cost() -> float:
    return _process_cost


def _extract_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        return json.loads(m.group(0))
    raise LLMError(f"模型未返回 JSON：{text[:200]}")


def chat_json(agent: str, system: str, user: str, max_tokens: int = 2000, reasoning: bool = False) -> dict:
    """调用模型并解析 JSON 输出，记录 token 与费用。"""
    global _process_cost
    if _process_cost >= max_cost():
        raise BudgetExceeded(f"已达到费用上限 ${max_cost():.2f}（MAX_COST_USD）")
    last_err: Exception | None = None
    for attempt in range(3):
        t0 = time.time()
        resp = client().chat.completions.create(
            model=model_name(),
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=max_tokens * (attempt + 1),
            extra_body={"usage": {"include": True},
                        "reasoning": {"effort": "low"} if reasoning else {"enabled": False}},
        )
        usage = resp.usage
        cost = float(getattr(usage, "cost", 0.0) or (usage.model_extra or {}).get("cost", 0.0) or 0.0) if usage else 0.0
        with _lock:
            _process_cost += cost
        u = _usage.get()
        if u is not None and usage is not None:
            u["calls"] += 1
            u["prompt_tokens"] += usage.prompt_tokens or 0
            u["completion_tokens"] += usage.completion_tokens or 0
            u["cost_usd"] += cost
            a = u["by_agent"].setdefault(agent, {"calls": 0, "cost_usd": 0.0, "seconds": 0.0})
            a["calls"] += 1
            a["cost_usd"] += cost
            a["seconds"] += time.time() - t0
        content = (resp.choices[0].message.content or "") if resp.choices else ""
        if not content.strip():  # 推理 token 用完导致空内容：关掉推理、加大上限重试
            reasoning = False
        try:
            return _extract_json(content)
        except (LLMError, json.JSONDecodeError) as e:
            last_err = e
    raise LLMError(f"{agent} 输出解析失败：{last_err}")
