"""LLM 客戶端：本機（llama.cpp，OpenAI 相容）與雲端（Anthropic）。

輸出一律要通過 pydantic schema 驗證；每次呼叫都寫入 analyses 表以便重現。
雲端沒有金鑰或當日花費超過預算時，自動改用本機模型。
"""
import json
import logging
import re
import threading
import time

import requests
from pydantic import BaseModel, ValidationError

from .config import cfg, secret
from .db import dumps
from .http import request
from .timeutil import iso, today_tw

log = logging.getLogger(__name__)
_db_lock = threading.Lock()


class LLMError(Exception):
    pass


def extract_json(text: str):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise LLMError("輸出中找不到 JSON")
    obj, _ = json.JSONDecoder(strict=False).raw_decode(text[min(starts):])
    return obj


def provider_for(conn, role: str) -> str:
    c = cfg()["llm"]
    if c["roles"].get(role, "local") != "cloud" or not secret("ANTHROPIC_API_KEY"):
        return "local"
    with _db_lock:
        spent = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM analyses WHERE provider='cloud' AND created_at>=?",
                             (f"{today_tw()}T00:00:00",)).fetchone()[0]
    return "local" if spent >= c["cloud"]["daily_budget_usd"] else "cloud"


def _call_local(messages, thinking: bool, max_tokens: int, temperature: float):
    c = cfg()["llm"]["local"]
    body = {"model": c["model"], "messages": messages, "max_tokens": max_tokens, "temperature": temperature,
            "chat_template_kwargs": {"enable_thinking": thinking}}
    r = request("POST", f"{c['base_url']}/chat/completions", json=body, timeout=900, retries=2).json()
    usage = r.get("usage", {})
    return (r["choices"][0]["message"].get("content") or "", usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0), c["model"])


def _call_cloud(messages, max_tokens: int, temperature: float):
    c = cfg()["llm"]["cloud"]
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    body = {"model": c["model"], "max_tokens": max_tokens, "system": system, "temperature": temperature,
            "messages": [m for m in messages if m["role"] != "system"]}
    base = c.get("base_url", "https://api.anthropic.com").rstrip("/")
    key = secret("ANTHROPIC_API_KEY")
    # 官方 API 用 x-api-key；OpenRouter 等相容閘道用 Bearer（OpenRouter 會把 x-api-key 當成轉發給 Anthropic 的金鑰）
    auth = {"x-api-key": key} if "api.anthropic.com" in base else {"Authorization": f"Bearer {key}"}
    r = request("POST", f"{base}/v1/messages", json=body, timeout=600, retries=2,
                headers={**auth, "anthropic-version": "2023-06-01"}).json()
    text = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
    return text, r["usage"]["input_tokens"], r["usage"]["output_tokens"], c["model"]


def _log(conn, **row) -> int:
    with _db_lock:
        cur = conn.execute(
            "INSERT INTO analyses (created_at, role, provider, model, prompt_version, input, output, ok, error, "
            "tokens_in, tokens_out, cost_usd, latency_s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iso(), row["role"], row["provider"], row.get("model"), row["prompt_version"], row["input"],
             row.get("output"), row["ok"], row.get("error"), row.get("tokens_in"), row.get("tokens_out"),
             row.get("cost_usd"), row.get("latency_s")))
        conn.commit()
        return cur.lastrowid


def ask(conn, role: str, system: str, user: str, schema: type[BaseModel], prompt_version: str,
        thinking: bool = False, max_tokens: int = 1500, temperature: float = 0.2):
    """回傳 (驗證後的物件或 None, analysis_id)。驗證失敗會把錯誤回饋給模型重試一次。"""
    provider = provider_for(conn, role)
    thinking = cfg()["llm"].get("thinking", {}).get(role, thinking)
    schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    messages = [{"role": "system", "content": f"{system}\n\n只輸出一個符合以下 JSON schema 的 JSON 物件，不要任何其他文字。\n{schema_text}"},
                {"role": "user", "content": user}]
    price = cfg()["llm"]["cloud"]["price_per_mtok"]
    analysis_id = None
    for attempt in range(2):
        t0, text, tin, tout, model = time.time(), "", 0, 0, None
        try:
            if provider == "cloud":
                text, tin, tout, model = _call_cloud(messages, max_tokens, temperature)
            else:
                text, tin, tout, model = _call_local(messages, thinking, max_tokens if not thinking else max_tokens + 6000,
                                                     temperature)
            obj = schema.model_validate(extract_json(text))
            error = None
        except (LLMError, ValidationError, json.JSONDecodeError, requests.RequestException, KeyError) as e:
            obj, error = None, f"{type(e).__name__}: {e}"[:1000]
        cost = (tin * price["input"] + tout * price["output"]) / 1e6 if provider == "cloud" else 0.0
        analysis_id = _log(conn, role=role, provider=provider, model=model, prompt_version=prompt_version,
                           input=dumps(messages), output=text, ok=int(obj is not None), error=error,
                           tokens_in=tin, tokens_out=tout, cost_usd=cost, latency_s=round(time.time() - t0, 2))
        if obj is not None:
            return obj, analysis_id
        log.warning("LLM %s 第 %d 次失敗：%s", role, attempt + 1, error)
        if not text:
            if provider == "local" and thinking:
                thinking = False  # 推理過程用光 token 而沒有輸出答案時，改用非推理模式重試
                continue
            break
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": f"上面的輸出無法通過驗證（{error}）。請只輸出符合 schema 的 JSON。"}]
    return None, analysis_id
