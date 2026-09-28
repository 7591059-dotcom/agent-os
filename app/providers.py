"""Small, text-only provider adapters. Credentials never enter model messages.

Reference protocols (2026-09-16):
https://developers.openai.com/api/reference/resources/chat
https://docs.anthropic.com/en/api/messages
https://ai.google.dev/api/generate-content
All custom endpoints use the same pinned-public-HTTPS transport as integrations.
"""
from __future__ import annotations

import asyncio
import json
import os
import re

from app.net import NetworkError, request_json
from app.security import decrypt


class ProviderError(ValueError):
    """An intentionally safe error suitable for user-visible event storage."""


def demo_enabled() -> bool:
    return os.getenv("DEMO_MODE", "false").lower() in {"1", "true", "yes"}


def _safe_int(value, default=0):
    if isinstance(value, bool):
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _mock(messages, max_tokens):
    if not demo_enabled():
        raise ProviderError("Демо-провайдер отключён: требуется DEMO_MODE=true.")
    if messages and "AGENT_OS_INTAKE" in messages[0]["content"]:
        context = json.loads(messages[-1]["content"])
        body = ("ДЕМО · Предложение тестового приёмного агента, без настоящего ИИ.\n\n"
                "Идея: " + str(context.get("idea", ""))[:1000] + "\n\n"
                "Предлагаю подготовить текстовое решение: аналитик уточняет требования, "
                "редактор готовит материал, сборщик объединяет результат.\n"
                "После одобрения оркестратор создаст агентов и запустит этапы. "
                "Программы и внешние сервисы этот демо-процесс не запускает.\n"
                "Правки владельца: " + "; ".join(context.get("feedback", []))[:1500] + "\n\n"
                "Публикация требует отдельного согласования. Одобрить и выполнить или внести правки?")
    elif messages and "AGENT_OS_PLANNER" in messages[0]["content"]:
        context = json.loads(messages[-1]["content"])
        provider = next((p["id"] for p in context["providers"] if p["kind"] == "mock"), None)
        if not provider:
            raise ProviderError("Выберите включённый демо-провайдер.")
        count = min(3, context["max_steps"])
        names = ["Аналитик", "Редактор", "Сборщик результата"][:count]
        agents = [{"id": f"a{i}", "name": name, "role": name,
                   "instructions": "Демонстрационный текстовый агент. Не заявляй о выполненных внешних действиях.",
                   "provider_id": provider, "max_tokens": 1024} for i, name in enumerate(names)]
        if context.get("auto_create_agents") is False:
            existing=context.get("existing_agents",[])
            if not existing: raise ProviderError("В ручном проекте сначала назначьте агентов.")
            agents=[];count=min(count,len(existing));names=[a['name'] for a in existing[:count]]
        steps = [{"id": f"s{i}", "name": name, "agent_id": f"a{i}" if context.get("auto_create_agents",True) else existing[i]["id"],
                  "depends_on": [f"s{j}" for j in range(i)] if i == count - 1 else [],
                  "input": "Подготовь свой раздел по исходному заданию."} for i, name in enumerate(names)]
        body = json.dumps({"agents": agents, "steps": steps}, ensure_ascii=False)
    else:
        body = ("ДЕМОНСТРАЦИЯ · Ответ создан локальным тестовым провайдером, без обращения к ИИ.\n\n"
                "Этап обработан. В рабочем режиме здесь будет ответ выбранной модели на задание "
                "с учётом результатов предыдущих этапов. Интернет-поиск, запуск программ и публикация не выполнялись.")
    return {"text": body, "usage": {"input_tokens": 0, "output_tokens": 0,
                                     "source": "demo_no_billable_tokens"}, "model": "demo"}


async def complete(provider, messages, *, model=None, temperature=.3, max_tokens=2048):
    if not isinstance(provider, dict) or not provider.get("enabled", True):
        raise ProviderError("Провайдер отсутствует или отключён.")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 1 <= max_tokens <= 32768:
        raise ProviderError("Лимит ответа должен быть от 1 до 32768 токенов.")
    if not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2:
        raise ProviderError("Температура должна быть от 0 до 2.")
    if not isinstance(messages, list) or not messages or any(
        not isinstance(m, dict) or m.get("role") not in {"system", "user", "assistant"}
        or not isinstance(m.get("content"), str) for m in messages
    ):
        raise ProviderError("Неверный формат текстового запроса.")
    if sum(len(m["content"]) for m in messages) > 500_000:
        raise ProviderError("Контекст превышает локальный лимит 500 000 символов. Сократите задание или зависимости.")
    kind = provider.get("kind")
    if kind == "mock":
        await asyncio.sleep(.05)
        return _mock(messages, max_tokens)
    selected_model = model or provider.get("model")
    if not isinstance(selected_model, str) or not re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,160}", selected_model):
        raise ProviderError("Укажите действительный идентификатор текстовой модели у провайдера.")
    try:
        key = decrypt(provider.get("api_key_encrypted", ""))
    except Exception:
        raise ProviderError("Не удалось расшифровать API-ключ. Проверьте MASTER_KEY и сохраните ключ заново.") from None
    if not key:
        raise ProviderError("У провайдера не сохранён API-ключ.")
    headers = {"Content-Type": "application/json"}
    base = (provider.get("base_url") or "").rstrip("/")
    if kind in {"openai", "openai_compatible"}:
        if kind == "openai":
            if base and base != "https://api.openai.com/v1":
                raise ProviderError("Для своего URL выберите тип OpenAI-compatible.")
            base = "https://api.openai.com/v1"
        if not base:
            raise ProviderError("Для OpenAI-compatible задайте полный базовый HTTPS URL, включая /v1 при необходимости.")
        url = base + "/chat/completions"
        headers["Authorization"] = "Bearer " + key
        body = {"model": selected_model, "messages": messages}
        if kind == "openai":
            body.update({"max_completion_tokens": max_tokens, "store": False})
            # Reasoning model families do not uniformly accept temperature.
            # Omit it for native OpenAI; explicit compatible adapters retain it.
        else:
            body.update({"max_tokens": max_tokens, "temperature": temperature})
    elif kind == "anthropic":
        if base and base != "https://api.anthropic.com/v1":
            raise ProviderError("Anthropic использует https://api.anthropic.com/v1.")
        url = "https://api.anthropic.com/v1/messages"
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        body = {"model": selected_model, "max_tokens": max_tokens,
                "temperature": min(temperature, 1),
                "messages": [m for m in messages if m["role"] != "system"]}
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        if system:
            body["system"] = system
    elif kind == "gemini":
        if base and base != "https://generativelanguage.googleapis.com/v1beta":
            raise ProviderError("Gemini использует https://generativelanguage.googleapis.com/v1beta.")
        short_model = selected_model.removeprefix("models/")
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,160}", short_model):
            raise ProviderError("Укажите имя Gemini-модели без пути и параметров URL.")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{short_model}:generateContent"
        headers["x-goog-api-key"] = key
        body = {"contents": [{"role": "model" if m["role"] == "assistant" else "user",
                              "parts": [{"text": m["content"]}]} for m in messages if m["role"] != "system"],
                "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
    else:
        raise ProviderError("Неизвестный тип ИИ-провайдера.")
    payload = None
    for attempt in range(3):
        try:
            payload = await request_json("POST", url, headers=headers, json=body, timeout=120,
                                         max_bytes=4_000_000)
            break
        except NetworkError as exc:
            status = getattr(exc, "status_code", None)
            if attempt < 2 and (status == 429 or isinstance(status, int) and 500 <= status <= 599):
                await asyncio.sleep(.5 * (2 ** attempt))
                continue
            if status in {401, 403}:
                reason = "API отклонил доступ. Проверьте ключ, права и доступность модели."
            elif status == 429:
                reason = "Лимит API: проверьте квоту, баланс и частоту запросов."
            elif status in {400, 404, 422}:
                reason = "API отклонил параметры. Проверьте модель, URL и поддерживаемый лимит токенов."
            elif status:
                reason = f"Ошибка API (HTTP {status}). Ответ сервера скрыт, чтобы не раскрывать секреты."
            else:
                reason = "Не удалось безопасно подключиться к API. Проверьте публичный HTTPS URL, DNS и сеть."
            raise ProviderError(reason) from None
        except Exception:
            raise ProviderError("Ошибка транспорта ИИ-провайдера. Подробности скрыты для защиты секретов.") from None
    try:
        if kind in {"openai", "openai_compatible"}:
            text = payload["choices"][0]["message"].get("content") or ""
            if not isinstance(text, str):
                raise TypeError
            usage = payload.get("usage") or {}
            it, ot = usage.get("prompt_tokens"), usage.get("completion_tokens")
            truncated = payload["choices"][0].get("finish_reason") == "length"
        elif kind == "anthropic":
            text = "\n".join(p["text"] for p in payload["content"] if p.get("type") == "text")
            usage = payload.get("usage") or {}
            it, ot = usage.get("input_tokens"), usage.get("output_tokens")
            truncated = payload.get("stop_reason") == "max_tokens"
        else:
            text = "\n".join(p["text"] for p in payload["candidates"][0]["content"]["parts"]
                             if "text" in p and not p.get("thought"))
            usage = payload.get("usageMetadata") or {}
            it = usage.get("promptTokenCount")
            # Gemini reasoning tokens are output consumption too.
            ot = (int(usage["candidatesTokenCount"]) + _safe_int(usage.get("thoughtsTokenCount"))) if "candidatesTokenCount" in usage else None
            truncated = payload["candidates"][0].get("finishReason") == "MAX_TOKENS"
        known = isinstance(it, int) and not isinstance(it, bool) and it >= 0 and isinstance(ot, int) and not isinstance(ot, bool) and ot >= 0
        normalized = {"input_tokens": _safe_int(it), "output_tokens": _safe_int(ot) if known else max_tokens,
                      "source": "provider" if known else "reserved_upper_bound"}
        # Even a misbehaving upstream must not echo its credential into persisted results.
        text = text.replace(key, "[REDACTED]")
        result = {"text": text, "usage": normalized, "model": selected_model}
        if not text.strip():
            result["error"] = "Модель не вернула текст. Возможны блокировка ответа или недостаточный лимит токенов."
        elif truncated:
            result["error"] = "Ответ обрезан лимитом токенов. Частичный текст сохранён; увеличьте лимит агента или сократите задание и повторите этап."
        return result
    except (KeyError, IndexError, TypeError, ValueError):
        raise ProviderError("API вернул неподдерживаемый формат ответа. Проверьте совместимость текстовой модели.") from None


async def test_provider(provider):
    response = await complete(provider, [{"role": "user", "content": "Reply with the single word OK."}], max_tokens=256)
    if response.get("error"):
        raise ProviderError(response["error"])
    return {"ok": True, "model": response["model"], "usage": response["usage"],
            "demo": provider.get("kind") == "mock",
            "message": "Демо-провайдер доступен; внешний API не вызывался." if provider.get("kind") == "mock" else "Модель ответила на тестовый API-запрос."}
