"""One place to call the language model: the main provider first, then the extra providers from /ai_add."""
import asyncio
import json
import time

import httpx

from .config import settings


def _providers() -> list[dict]:
    items = []
    if settings.llm_api_key:
        items.append({"name": "main", "base_url": settings.llm_base_url, "model": settings.llm_model,
                      "api_key": settings.llm_api_key})
    try:
        from .ai_providers import get_providers
        items.extend(dict(p) for p in get_providers())
    except Exception:
        pass
    return items


def available() -> bool:
    return bool(_providers())


def parse_json(content: str):
    content = (content or "").strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
    try:
        return json.loads(content)
    except ValueError:
        start, end = content.find("{"), content.rfind("}")
        if start >= 0 and end > start:
            return json.loads(content[start:end + 1])
        raise


def chat(messages: list[dict], *, json_mode: bool = False, temperature: float = 0.3,
         max_tokens: int | None = None, timeout: float = 90) -> str:
    """Blocking. Retries temporary errors, falls through to the next provider."""
    providers = _providers()
    if not providers:
        raise RuntimeError("ИИ не настроен: задай LLM_API_KEY или добавь провайдера через /ai_add")
    last = None
    for provider in providers:
        url = provider["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {provider['api_key']}", "Content-Type": "application/json"}
        use_json = json_mode
        for delay in (0, 4, 12):
            if delay:
                time.sleep(delay)
            body = {"model": provider["model"], "temperature": temperature, "messages": messages}
            if use_json:
                body["response_format"] = {"type": "json_object"}
            if max_tokens:
                body["max_tokens"] = max_tokens
            try:
                response = httpx.post(url, headers=headers, json=body, timeout=timeout)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                continue
            if response.status_code in (429, 500, 502, 503, 504):
                last = RuntimeError(f"HTTP {response.status_code}")
                continue
            if response.status_code == 400 and use_json:
                use_json = False  # some providers do not support response_format
                continue
            if response.status_code >= 400:
                last = RuntimeError(f"HTTP {response.status_code}")
                break  # try the next provider
            try:
                return response.json()["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, ValueError) as exc:
                last = exc
                break
    raise RuntimeError(f"ИИ недоступен: {type(last).__name__ if last else '?'} {str(last)[:120]}")


async def achat(messages, **kw) -> str:
    return await asyncio.to_thread(lambda: chat(messages, **kw))


async def achat_json(messages, **kw):
    text = await asyncio.to_thread(lambda: chat(messages, json_mode=True, **kw))
    return parse_json(text)
