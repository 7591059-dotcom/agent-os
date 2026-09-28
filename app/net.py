"""Capped public-HTTPS requests with DNS pinning, no proxies or redirects."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import ipaddress
import socket
import tempfile
from urllib.parse import urlsplit

import httpx


class NetworkError(ValueError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _url_host(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment or parsed.port not in (None, 443)
                or any(ord(c) <= 32 for c in url) or "\\" in url):
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii").rstrip(".")
        if "%" in host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError
        return host
    except (ValueError, UnicodeError):
        raise NetworkError("Разрешён только публичный HTTPS-адрес без логина, фрагмента и нестандартного порта.") from None


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.ipv4_mapped is not None:
                return _is_public(str(ip.ipv4_mapped))
            # Translation/tunnelling ranges can conceal a private IPv4 target.
            if ip.sixtofour is not None or ip.teredo is not None or ip in ipaddress.ip_network("64:ff9b::/96"):
                return False
        return ip.is_global and not ip.is_multicast and not ip.is_reserved
    except ValueError:
        return False


async def _resolve_public(url: str) -> tuple[str, str]:
    host = _url_host(url)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        try:
            answers = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM), 8
            )
        except (OSError, TimeoutError):
            raise NetworkError("Не удалось разрешить публичное DNS-имя сервиса.") from None
        addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses or not all(_is_public(address) for address in addresses):
        raise NetworkError("Доступ к локальным, служебным и непубличным IP-адресам запрещён.")
    # Prefer IPv4 for common VPS installations without routed IPv6.
    addresses.sort(key=lambda address: ":" in address)
    return host, addresses[0]


async def validate_public_url(url: str) -> None:
    await _resolve_public(url)


class PublicHTTPTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host, address = await _resolve_public(str(request.url))
        headers = request.headers.copy()
        headers["host"] = f"[{host}]" if ":" in host else host
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = host
        pinned = httpx.Request(
            request.method, request.url.copy_with(host=address), headers=headers,
            stream=request.stream, extensions=extensions,
        )
        # The TCP target is the already-checked literal IP. TLS still verifies
        # the original hostname, preventing DNS rebinding between checks.
        return await super().handle_async_request(pinned)


def _client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=PublicHTTPTransport(retries=0), trust_env=False,
        follow_redirects=False, timeout=httpx.Timeout(timeout, connect=10),
        headers={"Accept-Encoding": "identity", "User-Agent": "AgentOS/1.0"},
    )


def _check_response(response: httpx.Response) -> None:
    if not 200 <= response.status_code < 300:
        code = response.status_code
        hints = {400: "Проверьте параметры запроса.", 401: "Проверьте токен.",
                 403: "Проверьте разрешения приложения и аккаунта.",
                 404: "Проверьте идентификатор ресурса и версию API.",
                 409: "Конфликт состояния сервиса.", 429: "Лимит запросов; повторите позднее."}
        hint = "Перенаправления запрещены." if 300 <= code < 400 else hints.get(code, "Сервис отклонил запрос.")
        raise NetworkError(f"Внешний сервис: HTTP {code}. {hint}", code)
    if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
        # We request identity encoding. Reject unsolicited compression before
        # decompression can allocate an arbitrarily large response in memory.
        raise NetworkError("Сервис проигнорировал Accept-Encoding: identity; сжатый ответ не принимается.")


async def request(method: str, url: str, *, timeout: float = 45,
                  max_bytes: int = 2_000_000, **kwargs) -> httpx.Response:
    _url_host(url)
    # This wrapper does not accept caller overrides for security properties.
    for forbidden in ("follow_redirects", "auth", "extensions"):
        if forbidden in kwargs:
            raise NetworkError("Недопустимая настройка внешнего запроса.")
    try:
        async with asyncio.timeout(timeout):
            async with _client(timeout) as client:
                async with client.stream(method, url, **kwargs) as response:
                    _check_response(response)
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        if len(chunks) + len(chunk) > max_bytes:
                            raise NetworkError("Ответ внешнего сервиса превышает допустимый размер.")
                        chunks.extend(chunk)
                    # Do not retain a request URL containing tokens in returned objects.
                    return httpx.Response(response.status_code, headers=response.headers, content=bytes(chunks))
    except (TimeoutError, httpx.TimeoutException):
        raise NetworkError("Истекло время ожидания внешнего сервиса; результат запроса не подтверждён.") from None
    except (httpx.HTTPError, httpx.InvalidURL):
        raise NetworkError("Ошибка защищённого соединения с внешним сервисом; результат не подтверждён.") from None


async def request_json(method: str, url: str, *, timeout: float = 45,
                       max_bytes: int = 2_000_000, **kwargs) -> dict:
    response = await request(method, url, timeout=timeout, max_bytes=max_bytes, **kwargs)
    try:
        data = response.json()
    except (ValueError, UnicodeError):
        raise NetworkError("Внешний сервис вернул некорректный JSON.") from None
    if not isinstance(data, dict):
        raise NetworkError("Внешний сервис вернул неожиданный формат данных.")
    return data


@asynccontextmanager
async def download_media(url: str, *, max_bytes: int = 200 * 1024 * 1024,
                         timeout: float = 120):
    """Yield (temporary binary file, byte length, MIME type); always delete it."""
    _url_host(url)
    with tempfile.TemporaryFile(mode="w+b") as output:
        try:
            async with asyncio.timeout(timeout):
                async with _client(timeout) as client:
                    async with client.stream("GET", url) as response:
                        _check_response(response)
                        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        if not mime.startswith("video/"):
                            raise NetworkError("URL видео должен возвращать Content-Type video/*.")
                        length = response.headers.get("content-length")
                        if length and (not length.isdigit() or int(length) > max_bytes):
                            raise NetworkError("Видео превышает установленный лимит размера.")
                        size = 0
                        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                            size += len(chunk)
                            if size > max_bytes:
                                raise NetworkError("Видео превышает установленный лимит размера.")
                            output.write(chunk)
                        if not size:
                            raise NetworkError("URL вернул пустое видео.")
            output.seek(0)
        except (TimeoutError, httpx.TimeoutException):
            raise NetworkError("Истекло время загрузки видео.") from None
        except (httpx.HTTPError, httpx.InvalidURL):
            raise NetworkError("Не удалось загрузить видео по защищённому соединению.") from None
        yield output, size, mime
