"""Owner-approved social publishing and a private, allowlisted Telegram controller.

These functions are deliberately absent from the LLM engine's tool surface.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

from app.db import db, now
from app.net import NetworkError, download_media, request, request_json, validate_public_url
from app.security import decrypt


class IntegrationError(ValueError):
    def __init__(self, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


def _secrets(connector: dict) -> dict:
    try:
        value = json.loads(decrypt(connector.get("secret_encrypted", "")) or "{}")
        if not isinstance(value, dict):
            raise ValueError
        return value
    except Exception:
        raise IntegrationError("Не удалось прочитать секреты подключения; сохраните их заново.") from None


def _required(values: dict, name: str) -> str:
    value = values.get(name)
    if value is None or not str(value).strip():
        raise IntegrationError(f"Не заполнено поле {name}.")
    return str(value).strip()


def _id(value, label="ID") -> str:
    value = str(value)
    if not re.fullmatch(r"[0-9]{1,32}", value):
        raise IntegrationError(f"Некорректный {label}: нужен числовой идентификатор.")
    return value


def _telegram_token(secret: dict) -> str:
    value = _required(secret, "bot_token")
    if not re.fullmatch(r"[0-9]{4,20}:[A-Za-z0-9_-]{20,100}", value):
        raise IntegrationError("Некорректный формат bot_token.")
    return value


def _code(value) -> str:
    # Never include arbitrary error messages from upstream (may echo credentials).
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else "не указан"


async def _telegram(token: str, method: str, payload: dict | None = None,
                    *, timeout: float = 35, effect: bool = False):
    call = _effect if effect else request_json
    data = await call("POST", f"https://api.telegram.org/bot{token}/{method}",
                      json=payload or {}, timeout=timeout)
    if data.get("ok") is not True:
        if data.get("ok") is not False:
            raise IntegrationError("Telegram не подтвердил результат запроса.", uncertain=effect)
        raise IntegrationError(f"Telegram отклонил запрос (код {_code(data.get('error_code'))}). Проверьте токен, права бота и состояние webhook.")
    if "result" not in data:
        raise IntegrationError("Telegram не вернул результат запроса.", uncertain=effect)
    return data["result"]


async def _effect(method: str, url: str, **kwargs) -> dict:
    try:
        return await request_json(method, url, **kwargs)
    except NetworkError as exc:
        # Even a gateway 5xx can follow a committed write. Do not auto-repeat it.
        uncertain = exc.status_code is None or exc.status_code >= 500
        raise IntegrationError(str(exc), uncertain=uncertain) from None


def _instagram_base(config: dict) -> str:
    version = _required(config, "api_version")
    if not re.fullmatch(r"v[0-9]{1,3}\.0", version):
        raise IntegrationError("Instagram api_version должна иметь формат v23.0; укажите версию из Meta App Dashboard.")
    return f"https://graph.instagram.com/{version}"


def _vk_params(config: dict, secret: dict) -> dict:
    version = str(config.get("api_version") or "5.199")
    if not re.fullmatch(r"5\.[0-9]{1,3}", version):
        raise IntegrationError("Некорректная версия VK API.")
    owner = _required(config, "owner_id")
    if not re.fullmatch(r"-?[0-9]{1,20}", owner) or int(owner) == 0:
        raise IntegrationError("VK owner_id: ID пользователя или отрицательный ID сообщества.")
    return {"owner_id": int(owner), "v": version, "access_token": _required(secret, "access_token")}


def _vk_result(data: dict, *, effect=False):
    if "error" in data:
        error = data.get("error")
        code = error.get("error_code") if isinstance(error, dict) else None
        raise IntegrationError(f"VK отклонил запрос (код {_code(code)}). Проверьте токен пользователя, право wall и доступ к стене.")
    if "response" not in data:
        raise IntegrationError("VK не вернул результат запроса.", uncertain=effect)
    return data["response"]


def _graph_result(data: dict) -> dict:
    if "error" in data:
        error = data.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        raise IntegrationError(f"Instagram отклонил запрос (код {_code(code)}). Проверьте токен, разрешения и параметры медиа.")
    return data


async def _youtube_token(config: dict, secret: dict) -> str:
    data = await request_json("POST", "https://oauth2.googleapis.com/token", data={
        "client_id": _required(config, "client_id"),
        "client_secret": _required(secret, "client_secret"),
        "refresh_token": _required(secret, "refresh_token"),
        "grant_type": "refresh_token",
    })
    if not isinstance(data.get("access_token"), str) or not data["access_token"]:
        raise IntegrationError("Google не выдал access_token. Повторите OAuth с правом youtube.upload и offline access.")
    return data["access_token"]


async def test_connector(connector: dict) -> dict:
    """Credential/read check. Never publish or POST a webhook test event."""
    config, secret = connector.get("config") or {}, _secrets(connector)
    kind = connector.get("kind")
    if kind == "telegram":
        me = await _telegram(_telegram_token(secret), "getMe")
        if not isinstance(me, dict) or not me.get("id"):
            raise IntegrationError("Telegram не подтвердил данные бота.")
        return {"ok": True, "message": "Токен бота проверен; отправка сообщений не выполнялась.", "bot_id": me["id"], "username": me.get("username", "")}
    if kind == "vk":
        data = await request_json("POST", "https://api.vk.com/method/wall.get", data={**_vk_params(config, secret), "count": 1})
        _vk_result(data)
        return {"ok": True, "message": "Доступ к чтению стены проверен. Право публикации проверяется при отправке."}
    if kind == "instagram":
        account = _id(_required(config, "account_id"), "Instagram account_id")
        data = _graph_result(await request_json("GET", f"{_instagram_base(config)}/{account}",
            params={"fields": "id,username"}, headers={"Authorization": f"Bearer {_required(secret, 'access_token')}"}))
        if not data.get("id"):
            raise IntegrationError("Instagram не подтвердил аккаунт.")
        return {"ok": True, "message": "Чтение профиля проверено; разрешение публикации и формат медиа ещё не проверены.", "account_id": str(data["id"]), "username": data.get("username", "")}
    if kind == "youtube":
        await _youtube_token(config, secret)
        return {"ok": True, "message": "Обновление OAuth-токена прошло. Видео не загружалось; право youtube.upload проверяется при загрузке."}
    if kind == "webhook":
        await validate_public_url(_required(config, "url"))
        _required(secret, "bearer_token")
        return {"ok": True, "message": "HTTPS-адрес и публичный DNS проверены. Webhook не вызывался; доступность обработчика не подтверждена."}
    raise IntegrationError("Неизвестный тип подключения.")


async def publish(publication: dict, connector: dict) -> dict:
    if publication.get("status") not in {"approved", "publishing"} or not publication.get("approved_at"):
        raise IntegrationError("Публикацию должен явно одобрить владелец в панели.")
    if not connector.get("enabled", True):
        raise IntegrationError("Подключение отключено.")
    if publication.get("connector_id") != connector.get("id"):
        raise IntegrationError("Одобренное подключение не совпадает с адресатом.")
    config, secret = connector.get("config") or {}, _secrets(connector)
    kind = connector.get("kind")
    text = str(publication.get("text") or "")
    media = publication.get("media_url")
    if kind == "telegram":
        if media:
            raise IntegrationError("Telegram-адаптер этой версии публикует текст. Медиа не поддерживаются.")
        if not text.strip() or len(text.encode("utf-16-le")) // 2 > 4096:
            raise IntegrationError("Telegram: нужен текст длиной до 4096 UTF-16 символов.")
        chat = _required(config, "chat_id")
        if not re.fullmatch(r"(?:-?[0-9]{1,20}|@[A-Za-z][A-Za-z0-9_]{3,31})", chat):
            raise IntegrationError("Telegram chat_id должен быть числом или @именем канала.")
        result = await _telegram(_telegram_token(secret), "sendMessage", {
            "chat_id": chat, "text": text, "link_preview_options": {"is_disabled": True}}, effect=True)
        if not isinstance(result, dict) or not result.get("message_id"):
            raise IntegrationError("Telegram не подтвердил ID сообщения.", uncertain=True)
        return {"external_id": f"{chat}:{result['message_id']}"}
    if kind == "vk":
        if media:
            raise IntegrationError("VK-адаптер этой версии публикует текст; загрузка фото и видео не поддерживается.")
        if not text.strip() or len(text) > 15000:
            raise IntegrationError("VK: нужен текст длиной до 15000 символов.")
        params = _vk_params(config, secret)
        data = await _effect("POST", "https://api.vk.com/method/wall.post", data={
            **params, "message": text, "from_group": int(params["owner_id"] < 0), "guid": publication["id"]})
        result = _vk_result(data, effect=True)
        if not isinstance(result, dict) or not result.get("post_id"):
            raise IntegrationError("VK не подтвердил ID публикации.", uncertain=True)
        external_id = f"{params['owner_id']}_{result['post_id']}"
        return {"external_id": external_id, "url": f"https://vk.com/wall{external_id}"}
    if kind == "instagram":
        return await _publish_instagram(publication, config, secret)
    if kind == "youtube":
        return await _publish_youtube(publication, config, secret)
    if kind == "webhook":
        url = _required(config, "url")
        if media:
            await validate_public_url(media)
        # The remote automation receives only this reviewed payload, never agent/provider config.
        data = await _effect("POST", url, json={
            "event": "publication.approved", "publication_id": publication["id"],
            "task_id": publication.get("task_id"), "text": text,
            "title": publication.get("title"), "media_url": media,
        }, headers={"Authorization": f"Bearer {_required(secret, 'bearer_token')}",
                    "Idempotency-Key": publication["id"]})
        if data.get("ok") is not True or not isinstance(data.get("id"), (str, int)) or not str(data["id"]):
            raise IntegrationError("Webhook должен подтвердить выполнение JSON-ответом {ok:true,id:...}; результат не подтверждён.", uncertain=True)
        return {"external_id": str(data["id"])[:200]}
    raise IntegrationError("Неизвестный тип подключения.")


async def _publish_instagram(publication: dict, config: dict, secret: dict) -> dict:
    media = _required(publication, "media_url")
    await validate_public_url(media)
    caption = str(publication.get("text") or "")
    if len(caption) > 2200:
        raise IntegrationError("Instagram: подпись должна быть не длиннее 2200 символов.")
    kind = config.get("media_type", "IMAGE")
    if kind not in {"IMAGE", "REELS"}:
        raise IntegrationError("Instagram media_type: IMAGE или REELS.")
    base = _instagram_base(config)
    account = _id(_required(config, "account_id"), "Instagram account_id")
    headers = {"Authorization": f"Bearer {_required(secret, 'access_token')}"}
    payload = {"caption": caption}
    if kind == "REELS":
        payload.update({"media_type": "REELS", "video_url": media, "share_to_feed": "true"})
    else:
        payload["image_url"] = media
    # A container is not public. A timeout here cannot create a duplicate post.
    container = _graph_result(await request_json("POST", f"{base}/{account}/media", data=payload, headers=headers))
    container_id = _id(container.get("id", ""), "ID контейнера Instagram")
    db.update("publication", publication["id"], {"external_container_id": container_id})
    deadline, ready = time.monotonic() + 180, False
    while time.monotonic() < deadline:
        state = _graph_result(await request_json("GET", f"{base}/{container_id}",
            params={"fields": "status_code"}, headers=headers, timeout=max(.01, min(30, deadline - time.monotonic()))))
        status = state.get("status_code")
        if status == "FINISHED":
            ready = True
            break
        if status in {"ERROR", "EXPIRED"}:
            raise IntegrationError("Instagram не смог подготовить медиа. Проверьте публичный URL и требования к формату.")
        if status == "PUBLISHED":
            raise IntegrationError("Instagram сообщает, что контейнер уже опубликован; проверьте аккаунт.", uncertain=True)
        if status != "IN_PROGRESS":
            raise IntegrationError("Instagram вернул неизвестный статус контейнера; публикация не запускалась.")
        await asyncio.sleep(min(5, max(0, deadline - time.monotonic())))
    if not ready:
        raise IntegrationError("Instagram не подготовил медиа за 180 секунд. Контейнер создан, публикация не запускалась.")
    data = _graph_result(await _effect("POST", f"{base}/{account}/media_publish", data={"creation_id": container_id}, headers=headers))
    if not re.fullmatch(r"[0-9]{1,32}", str(data.get("id", ""))):
        raise IntegrationError("Instagram не подтвердил ID опубликованного медиа.", uncertain=True)
    return {"external_id": str(data["id"])}


async def _publish_youtube(publication: dict, config: dict, secret: dict) -> dict:
    media = _required(publication, "media_url")
    title = _required(publication, "title")
    description = str(publication.get("text") or "")
    if len(title) > 100 or len(description.encode("utf-8")) > 5000 or "<" in title or ">" in title:
        raise IntegrationError("YouTube: заголовок до 100 символов без <>, описание до 5000 UTF-8 байт.")
    privacy = config.get("privacy_status", "private")
    if privacy not in {"private", "unlisted", "public"}:
        raise IntegrationError("YouTube privacy_status: private, unlisted или public.")
    category_id = _id(str(config.get("category_id") or "22"), "YouTube category_id")
    token = await _youtube_token(config, secret)
    async with download_media(media) as (file, size, mime):
        headers = {"Authorization": f"Bearer {token}", "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": mime}
        response = await request("POST", "https://www.googleapis.com/upload/youtube/v3/videos", params={"uploadType": "resumable", "part": "snippet,status"},
            headers=headers, json={"snippet": {"title": title, "description": description, "categoryId": category_id},
                                  "status": {"privacyStatus": privacy}})
        upload_url = response.headers.get("location", "")
        # Google returns a signed session URL. Never send OAuth credentials to arbitrary hosts.
        if urlsplit(upload_url).hostname not in {"www.googleapis.com", "youtube.googleapis.com"}:
            raise IntegrationError("YouTube вернул недопустимый адрес сессии загрузки.")
        await validate_public_url(upload_url)
        async def content():
            while chunk := file.read(256 * 1024):
                yield chunk
        data = await _effect("PUT", upload_url, headers={"Authorization": f"Bearer {token}", "Content-Type": mime, "Content-Length": str(size)}, content=content(), timeout=180)
        external_id = str(data.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", external_id):
            raise IntegrationError("YouTube не подтвердил ID загруженного видео.", uncertain=True)
        return {"external_id": external_id, "url": f"https://www.youtube.com/watch?v={external_id}", "privacy_status": privacy}


HELP = ("АГЕНТНАЯ · управление\n/tasks — последние задачи\n/task <описание> — создать задачу\n"
        "/plan <id> — составить план\n/run <id> — запустить\n/pause <id> — пауза\n"
        "/resume <id> — продолжить\n/cancel <id> — отменить\n/result <id> — результат\n"
        "ID можно сократить до первых 8 символов. Публикации одобряются и отправляются через веб-панель.")


def _authorized_message(update: dict, config: dict) -> dict | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat, sender = message.get("chat") or {}, message.get("from") or {}
    allowed = config.get("allowed_user_ids") or []
    user_id = sender.get("id")
    if (chat.get("type") != "private" or type(user_id) is not int
            or user_id <= 0 or type(chat.get("id")) is not int or chat["id"] != user_id
            or sender.get("is_bot") is not False or "forward_origin" in message
            or not isinstance(allowed, list)
            or user_id not in [value for value in allowed if type(value) is int and value > 0]):
        return None
    date = message.get("date")
    if type(date) is not int or not 0 <= time.time() - date < 300:
        return None
    if not isinstance(message.get("text"), str) or not message["text"].startswith("/"):
        return None
    return message


def _resolve_task(prefix: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{8,32}", prefix):
        raise IntegrationError("Укажите ID задачи (минимум первые 8 символов).")
    matches = [task for task in db.list("task") if task["id"].startswith(prefix)]
    if len(matches) != 1:
        raise IntegrationError("Задача не найдена или сокращённый ID неоднозначен.")
    return matches[0]


async def handle_telegram_update(update: dict, connector: dict, engine) -> str | None:
    """Return reply text only for an authenticated fresh command; no automatic publication."""
    message = _authorized_message(update, connector.get("config") or {})
    if message is None:
        return None
    parts = message["text"].strip().split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""
    if command in {"/start", "/help"}:
        return HELP
    if command == "/tasks":
        tasks = db.list("task")[:12]
        return "\n".join(f"{task['id'][:8]} · {task.get('status')} · {str(task.get('title',''))[:100]}" for task in tasks) or "Задач пока нет. /task <описание>"
    if command == "/task":
        if not argument or len(argument) > 12000:
            raise IntegrationError("После /task нужно описание задачи, до 12000 символов.")
        providers = [provider for provider in db.list("provider") if provider.get("enabled", True) and provider.get("kind") != "mock"]
        if not providers:
            raise IntegrationError("Сначала подключите реальный ИИ API в веб-панели.")
        task = engine.create_task({"title": argument[:100], "brief": argument, "planner_provider_id": providers[0]["id"], "auto_run": False})
        return f"Создана задача {task['id'][:8]}. Для плана: /plan {task['id'][:8]}"
    if command in {"/plan", "/run", "/pause", "/resume", "/cancel", "/result"}:
        task = _resolve_task(argument)
        if command == "/result":
            result = str(task.get("result") or "Результат пока не готов.")
            return f"{task['title']} · {task['status']}\n\n{result[:5500]}" + ("\n\nПолный результат в веб-панели." if len(result) > 5500 else "")
        changed = await engine.action(task["id"], command[1:])
        return f"{changed['id'][:8]} · {changed['status']}"
    return HELP


async def _delay(stop: asyncio.Event, seconds: float):
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except TimeoutError:
        pass


async def _telegram_worker(connector: dict, stop: asyncio.Event, engine):
    token = _telegram_token(_secrets(connector))
    offset_key = "telegram_offset_" + connector["id"] + "_" + hashlib.sha256(token.encode()).hexdigest()[:12]
    offset = db.setting(offset_key)
    if offset is None:
        # Start at the end of the queue when first connected. Do not execute an old command.
        updates = await _telegram(token, "getUpdates", {"offset": -1, "limit": 1, "timeout": 0, "allowed_updates": ["message"]})
        offset = max([item.get("update_id", -1) + 1 for item in updates], default=0)
        db.set_setting(offset_key, offset)
    last_command: dict[int, float] = {}
    while not stop.is_set():
        try:
            updates = await _telegram(token, "getUpdates", {"offset": offset, "timeout": 25, "limit": 30, "allowed_updates": ["message"]}, timeout=35)
            if not isinstance(updates, list):
                raise IntegrationError("Telegram вернул некорректную очередь команд.")
            db.update("connector", connector["id"], {"last_error": "", "last_poll_at": now()})
            for update in updates:
                update_id = update.get("update_id")
                if type(update_id) is not int or update_id < offset:
                    continue
                offset = update_id + 1
                # Persist before action: at most once across restarts, no paid-run replay.
                db.set_setting(offset_key, offset)
                message = _authorized_message(update, connector.get("config") or {})
                if not message:
                    continue
                sender_id = message["from"]["id"]
                if time.monotonic() - last_command.get(sender_id, -100) < 1:
                    continue
                last_command[sender_id] = time.monotonic()
                try:
                    reply = await handle_telegram_update(update, connector, engine)
                except ValueError as exc:
                    reply = str(exc)[:1000]
                except Exception:
                    reply = "Не удалось выполнить команду. Проверьте задачу и журнал в веб-панели."
                if reply:
                    # 2000 Unicode scalars fit Telegram's 4096 UTF-16-unit cap.
                    for start in range(0, min(len(reply), 6000), 2000):
                        await _telegram(token, "sendMessage", {"chat_id": sender_id, "text": reply[start:start + 2000],
                            "protect_content": True, "link_preview_options": {"is_disabled": True}}, effect=True)
        except (IntegrationError, NetworkError) as exc:
            db.update("connector", connector["id"], {"last_error": str(exc)[:500]})
            await _delay(stop, 10)


async def telegram_loop(stop_event: asyncio.Event, engine, workflow=None):
    # Keep legacy helpers above for compatibility; application uses the new controller.
    from app.telegram import telegram_loop as controller
    await controller(stop_event, engine, workflow)
