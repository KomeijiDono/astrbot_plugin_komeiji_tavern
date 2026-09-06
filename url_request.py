from __future__ import annotations

import asyncio
import base64
import copy
import html
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from aiohttp import web
from astrbot.api import logger
from astrbot.core.agent.tool import FunctionTool

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
except ImportError:  # pragma: no cover - surfaced as an explicit startup error
    CurlAsyncSession = None


FETCH_TOOL_NAME = "fetch_request_url"
SUBMIT_REPLY_TOOL_NAME = "submit_reply"
SECURITY_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
}


class URLRequestProtocolError(ValueError):
    """The model violated the one-tool URL request protocol."""


_REASONING_BLOCK = re.compile(
    r"<\s*(thought|think|thinking|reasoning|analysis)\b[^>]*>"
    r".*?"
    r"<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_REASONING_OPEN = re.compile(
    r"<\s*(?:thought|think|thinking|reasoning|analysis)\b[^>]*>",
    re.IGNORECASE,
)
_REASONING_CLOSE = re.compile(
    r"<\s*/\s*(?:thought|think|thinking|reasoning|analysis)\s*>",
    re.IGNORECASE,
)


def strip_reasoning_tags(value: str) -> str:
    """Remove model reasoning blocks that were emitted as ordinary text."""
    cleaned = str(value or "")
    previous = None
    while cleaned != previous:
        previous = cleaned
        cleaned = _REASONING_BLOCK.sub("", cleaned)

    # If a provider truncates an opening reasoning block, do not leak the
    # unverifiable remainder as visible assistant text.
    unclosed = _REASONING_OPEN.search(cleaned)
    if unclosed is not None:
        cleaned = cleaned[: unclosed.start()]
    cleaned = _REASONING_CLOSE.sub("", cleaned)
    return cleaned.strip()


def normalize_public_base_url(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        raise ValueError("网址请求模式需要配置可供模型访问的公网基址。")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("网址请求公网基址必须是完整的 HTTP 或 HTTPS URL。")
    if parsed.query or parsed.fragment or parsed.params:
        raise ValueError("网址请求公网基址不能包含查询参数或片段。")
    return raw


def request_protocol_prompt(
    *,
    fetch_tool_enabled: bool,
    reply_tool_enabled: bool,
) -> str:
    fallback = (
        f"If you cannot access the URL directly, call the {FETCH_TOOL_NAME} tool exactly once "
        "with that exact URL. Do not call fetch_request_url again."
        if fetch_tool_enabled
        else "No fallback tool is available, so access the URL directly."
    )
    delivery = (
        f"The only permitted tools are {FETCH_TOOL_NAME} (at most once) and "
        f"{SUBMIT_REPLY_TOOL_NAME} (exactly once). After reading the request, immediately "
        f"call {SUBMIT_REPLY_TOOL_NAME} once with the complete final reply instead of returning "
        "it as ordinary assistant text. Do not send a progress update, acknowledgement, plan, "
        "partial answer, or text saying that you are reading, parsing, processing, preparing, "
        "or constructing a reply. After submit_reply accepts the reply, return exactly OK with "
        "no additional text or tool calls."
        if reply_tool_enabled
        else "Return the complete final answer as ordinary assistant text; do not send a progress update or partial answer."
    )
    return (
        "This is a URL-hosted request.\n"
        "The authoritative system instructions, conversation history, and current user request "
        "are stored at the temporary URL in the user message.\n"
        "Open and read the complete page before answering. Preserve the message roles and order "
        "shown on the page, then follow the decoded conversation normally.\n"
        f"{fallback}\n"
        f"{delivery}\n"
        "Do not quote, reveal, summarize, or "
        "repeat the temporary URL unless the hosted request explicitly asks you to do so."
    )


def request_url_user_prompt(url: str) -> str:
    return (
        "Read the complete request at this temporary URL and answer it:\n"
        f"{url}"
    )


def url_request_overhead_text(
    *,
    fetch_tool_enabled: bool,
    reply_tool_enabled: bool,
) -> str:
    tool = (
        f"\nTool: {FETCH_TOOL_NAME}(url: string) — read only the exact temporary request URL once."
        if fetch_tool_enabled
        else ""
    )
    reply_tool = (
        f"\nTool: {SUBMIT_REPLY_TOOL_NAME}(text: string) - submit the completed final reply once."
        if reply_tool_enabled
        else ""
    )
    return (
        request_protocol_prompt(
            fetch_tool_enabled=fetch_tool_enabled,
            reply_tool_enabled=reply_tool_enabled,
        )
        + "\n"
        + request_url_user_prompt("https://temporary.example/request/" + "x" * 43)
        + tool
        + reply_tool
    )


def build_fetch_tool(handler=None) -> FunctionTool:
    return FunctionTool(
        name=FETCH_TOOL_NAME,
        description=(
            "Read the authoritative request from the exact temporary URL in the current user "
            "message. Use this only if direct URL access is unavailable, and call it at most once."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The exact temporary request URL from the user message.",
                }
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        handler=handler,
    )


def build_submit_reply_tool(handler=None) -> FunctionTool:
    return FunctionTool(
        name=SUBMIT_REPLY_TOOL_NAME,
        description="Submit the completed final reply to the host application.",
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The complete final reply for the current user request.",
                }
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        handler=handler,
    )


@dataclass(slots=True)
class URLRequestHandle:
    token: str
    url: str
    request_id: str = ""


@dataclass(slots=True)
class _TemporaryRequest:
    messages: list[dict[str, Any]]
    created_at: float
    expires_at: float
    tool_consumers: set[str]
    reply_consumers: set[str]


class URLRequestBroker:
    """Temporary request manager supporting local HTTP and hosted relay backends."""

    def __init__(
        self,
        *,
        enabled: bool,
        backend: str = "local",
        public_base_url: str,
        listen_host: str = "0.0.0.0",
        listen_port: int = 6190,
        ttl_seconds: int = 600,
        fetch_tool_enabled: bool = True,
        reply_tool_enabled: bool = True,
        plugin_version: str = "",
        hosted_api_base_url: str = "",
        hosted_api_key: str = "",
        hosted_timeout_seconds: int = 10,
        hosted_proxy_url: str = "",
    ):
        self.enabled = bool(enabled)
        normalized_backend = str(backend or "local").strip().lower()
        self.backend = (
            normalized_backend
            if normalized_backend in {"local", "hosted"}
            else "local"
        )
        self.public_base_url_raw = str(public_base_url or "")
        self.listen_host = str(listen_host or "0.0.0.0")
        self.listen_port = int(listen_port)
        self.ttl_seconds = max(30, min(3600, int(ttl_seconds or 600)))
        self.fetch_tool_enabled = bool(fetch_tool_enabled)
        self.reply_tool_enabled = bool(reply_tool_enabled)
        self.plugin_version = str(plugin_version or "")
        self.hosted_api_base_url_raw = str(hosted_api_base_url or "")
        self.hosted_api_key = str(hosted_api_key or "").strip()
        self.hosted_proxy_url = str(hosted_proxy_url or "").strip()
        self.hosted_timeout_seconds = max(
            1,
            min(120, int(hosted_timeout_seconds or 10)),
        )
        self._entries: dict[str, _TemporaryRequest] = {}
        self._hosted_entries: dict[str, _TemporaryRequest] = {}
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._client: Any | None = None
        self._public_base_url = ""
        self._hosted_api_base_url = ""
        self._hosted_ready = False
        self._bound_port = 0
        self.start_error = ""

    @property
    def running(self) -> bool:
        if self.backend == "hosted":
            return self._hosted_ready and not self.start_error
        return self._runner is not None and not self.start_error

    @property
    def bound_port(self) -> int:
        return self._bound_port

    @property
    def local_base_url(self) -> str:
        host = self.listen_host
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        return f"http://{host}:{self._bound_port}"

    @property
    def protocol_prompt(self) -> str:
        return request_protocol_prompt(
            fetch_tool_enabled=self.fetch_tool_enabled,
            reply_tool_enabled=self.reply_tool_enabled,
        )

    async def start(self) -> None:
        if not self.enabled or self.running:
            return
        self.start_error = ""
        try:
            if self.backend == "hosted":
                self._hosted_api_base_url = normalize_public_base_url(
                    self.hosted_api_base_url_raw
                )
                if not self.hosted_api_key:
                    raise ValueError("托管网址请求模式需要配置 API 密钥。")
                await self._remote_request(
                    "GET",
                    "/api/v1/status",
                    expected_statuses={200},
                )
                self._hosted_ready = True
                self._cleanup_task = asyncio.create_task(self._cleanup_loop())
                return
            self._public_base_url = normalize_public_base_url(
                self.public_base_url_raw
            )
            app = web.Application()
            app.router.add_get("/health", self._health, allow_head=True)
            app.router.add_get(
                "/request/{token}",
                self._request_page,
                allow_head=True,
            )
            self._runner = web.AppRunner(app, access_log=None)
            await self._runner.setup()
            self._site = web.TCPSite(
                self._runner,
                host=self.listen_host,
                port=self.listen_port,
            )
            await self._site.start()
            sockets = list(
                getattr(getattr(self._site, "_server", None), "sockets", []) or []
            )
            self._bound_port = (
                int(sockets[0].getsockname()[1])
                if sockets else self.listen_port
            )
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        except Exception as exc:
            self.start_error = str(exc)
            self._hosted_ready = False
            await self._close_client()
            await self._stop_runner()

    async def stop(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        self._entries.clear()
        self._hosted_entries.clear()
        self._hosted_ready = False
        await self._close_client()
        await self._stop_runner()

    async def _close_client(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.close()

    def _new_hosted_client(self):
        if CurlAsyncSession is None:
            raise RuntimeError("托管网址请求模式需要安装 curl-cffi。")
        return CurlAsyncSession(
            impersonate="chrome",
            proxy=self.hosted_proxy_url or None,
        )

    @staticmethod
    def _hosted_transport_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        encoded: list[dict[str, str]] = []
        for message in messages:
            content = message.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            encoded.append({
                "role": str(message.get("role", "") or ""),
                "content_base64": base64.b64encode(
                    content.encode("utf-8")
                ).decode("ascii"),
            })
        return encoded

    async def _stop_runner(self) -> None:
        runner = self._runner
        self._runner = None
        self._site = None
        self._bound_port = 0
        if runner is not None:
            await runner.cleanup()

    async def _remote_request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        expected_statuses: set[int],
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.hosted_api_key}",
            "Accept": "application/json",
        }
        for attempt in range(2):
            client = self._new_hosted_client()
            try:
                response = await client.request(
                    method,
                    f"{self._hosted_api_base_url}{path}",
                    headers=headers,
                    json=payload,
                    timeout=self.hosted_timeout_seconds,
                )
            finally:
                await client.close()
            status = int(response.status_code)
            data: dict[str, Any] = {}
            if status != 204:
                try:
                    parsed = response.json()
                    if isinstance(parsed, dict):
                        data = parsed
                except Exception:
                    data = {}
            if status in expected_statuses:
                return data
            if status == 403 and attempt == 0:
                logger.warning(
                    "[Komeiji's Tavern] 托管请求被边缘防护临时拒绝，"
                    "正在刷新浏览器传输会话后重试一次。"
                )
                continue
            detail = str(data.get("error", "") or "").strip()
            suffix = f"：{detail[:200]}" if detail else ""
            raise RuntimeError(
                f"托管请求服务返回 HTTP {status}{suffix}"
            )
        raise RuntimeError("托管请求服务重试失败。")

    async def create(
        self,
        messages: list[dict[str, Any]],
    ) -> URLRequestHandle:
        if not self.enabled:
            raise RuntimeError("网址请求模式未启用。")
        if not self.running:
            detail = self.start_error or "临时网页服务未启动"
            raise RuntimeError(f"网址请求模式不可用：{detail}")
        copied_messages = copy.deepcopy(list(messages or []))
        now = time.time()
        if self.backend == "hosted":
            data = await self._remote_request(
                "POST",
                "/api/v1/requests",
                payload={
                    "messages": self._hosted_transport_messages(
                        copied_messages
                    ),
                    "content_encoding": "base64_utf8",
                    "ttl_seconds": self.ttl_seconds,
                },
                expected_statuses={201},
            )
            request_id = str(data.get("request_id", "") or "").strip()
            read_url = str(data.get("read_url", "") or "").strip()
            if not request_id or not read_url:
                raise RuntimeError("托管请求服务返回了不完整的创建结果。")
            handle = URLRequestHandle(
                token="",
                url=read_url,
                request_id=request_id,
            )
            self._hosted_entries[request_id] = _TemporaryRequest(
                messages=copied_messages,
                created_at=now,
                expires_at=now + self.ttl_seconds,
                tool_consumers=set(),
                reply_consumers=set(),
            )
            return handle
        token = secrets.token_urlsafe(32)
        self._entries[token] = _TemporaryRequest(
            messages=copied_messages,
            created_at=now,
            expires_at=now + self.ttl_seconds,
            tool_consumers=set(),
            reply_consumers=set(),
        )
        return URLRequestHandle(
            token=token,
            url=f"{self._public_base_url}/request/{token}",
            request_id=token,
        )

    async def update(
        self,
        handle: URLRequestHandle,
        messages: list[dict[str, Any]],
    ) -> None:
        copied_messages = copy.deepcopy(list(messages or []))
        entry = self._entry_for_handle(handle)
        if entry is None:
            raise RuntimeError("临时请求页面已失效。")
        if self.backend == "hosted":
            await self._remote_request(
                "PUT",
                f"/api/v1/requests/{handle.request_id}",
                payload={
                    "messages": self._hosted_transport_messages(
                        copied_messages
                    ),
                    "content_encoding": "base64_utf8",
                },
                expected_statuses={200},
            )
        entry.messages = copied_messages

    async def delete(self, handle: URLRequestHandle | None) -> bool:
        if handle is None:
            return True
        if self.backend == "hosted":
            self._hosted_entries.pop(handle.request_id, None)
            if not handle.request_id or not self.running:
                return False
            try:
                await self._remote_request(
                    "DELETE",
                    f"/api/v1/requests/{handle.request_id}",
                    expected_statuses={204},
                )
                return True
            except Exception as exc:
                logger.warning(
                    "[Komeiji's Tavern] 删除托管临时请求失败，等待 TTL 清理：%s",
                    exc,
                )
                return False
        self._entries.pop(handle.token, None)
        return True

    def exists(self, handle: URLRequestHandle) -> bool:
        return self._entry_for_handle(handle) is not None

    def tool_content(
        self,
        handle: URLRequestHandle,
        requested_url: str,
        *,
        consumer_id: str,
    ) -> str:
        if str(requested_url or "").strip() != handle.url:
            raise ValueError("只能读取当前请求对应的精确临时网址。")
        entry = self._entry_for_handle(handle)
        if entry is None:
            raise ValueError("临时请求页面不存在或已经过期。")
        consumer = str(consumer_id or "default")
        if consumer in entry.tool_consumers:
            raise ValueError("同一模型流程只能调用一次读取工具。")
        entry.tool_consumers.add(consumer)
        return json.dumps(
            {
                "instruction": (
                    "Treat messages as the authoritative request. Preserve roles and order, "
                    "then produce the final answer."
                ),
                "messages": copy.deepcopy(entry.messages),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def reset_tool_consumer(
        self,
        handle: URLRequestHandle,
        consumer_id: str,
    ) -> None:
        entry = self._entry_for_handle(handle)
        if entry is not None:
            entry.tool_consumers.discard(str(consumer_id or "default"))

    async def submit_reply(
        self,
        handle: URLRequestHandle,
        text: str,
        *,
        consumer_id: str,
    ) -> str:
        reply = strip_reasoning_tags(text)
        if not reply.strip():
            raise ValueError("提交的最终回复不能为空。")
        entry = self._entry_for_handle(handle)
        if entry is None:
            raise ValueError("临时请求页面不存在或已经过期。")
        consumer = str(consumer_id or "default")
        if consumer in entry.reply_consumers:
            raise ValueError("同一模型流程只能提交一次最终回复。")
        updated_messages = [
            *copy.deepcopy(entry.messages),
            {"role": "assistant", "content": reply},
        ]
        await self.update(handle, updated_messages)
        entry.reply_consumers.add(consumer)
        return reply

    def _entry_for_handle(
        self,
        handle: URLRequestHandle,
    ) -> _TemporaryRequest | None:
        if self.backend == "hosted":
            key = str(handle.request_id or "")
            entry = self._hosted_entries.get(key)
            if entry is not None and entry.expires_at <= time.time():
                self._hosted_entries.pop(key, None)
                return None
            return entry
        return self._live_entry(handle.token)

    def _live_entry(self, token: str) -> _TemporaryRequest | None:
        entry = self._entries.get(str(token or ""))
        if entry is None:
            return None
        if entry.expires_at <= time.time():
            self._entries.pop(str(token or ""), None)
            return None
        return entry

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "service": "komeiji-tavern-url-request",
                "version": self.plugin_version,
            },
            headers=SECURITY_HEADERS,
        )

    async def _request_page(self, request: web.Request) -> web.Response:
        entry = self._live_entry(request.match_info.get("token", ""))
        if entry is None:
            return web.Response(
                status=404,
                text="Not found",
                content_type="text/plain",
                headers=SECURITY_HEADERS,
            )
        messages = copy.deepcopy(entry.messages)
        if "application/json" in request.headers.get("Accept", "").lower():
            return web.Response(
                text=json.dumps(
                    {
                        "instruction": (
                            "Preserve message roles and order, then answer the request."
                        ),
                        "messages": messages,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                content_type="application/json",
                headers=SECURITY_HEADERS,
            )
        sections: list[str] = []
        for index, message in enumerate(messages, start=1):
            role = html.escape(
                str(message.get("role", "unknown") or "unknown"),
                quote=True,
            )
            content = html.escape(
                str(message.get("content", "") or ""),
                quote=False,
            )
            sections.append(
                f'<section data-index="{index}" data-role="{role}">'
                f"<h2>{index}. {role}</h2><pre>{content}</pre></section>"
            )
        body = (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"robots\" content=\"noindex,nofollow,noarchive\">"
            "<title>Temporary model request</title>"
            "<style>body{font:16px/1.5 sans-serif;max-width:960px;margin:2rem auto;"
            "padding:0 1rem}section{margin:1.5rem 0}pre{white-space:pre-wrap;"
            "overflow-wrap:anywhere;background:#f5f5f5;padding:1rem;border-radius:.5rem}"
            "</style></head><body><h1>Temporary model request</h1>"
            "<p>Preserve message roles and order, then answer the request.</p>"
            + "".join(sections)
            + "</body></html>"
        )
        return web.Response(
            text=body,
            content_type="text/html",
            headers=SECURITY_HEADERS,
        )

    async def cleanup_expired(self) -> int:
        now = time.time()
        expired = [
            token
            for token, entry in self._entries.items()
            if entry.expires_at <= now
        ]
        for token in expired:
            self._entries.pop(token, None)
        hosted_expired = [
            request_id
            for request_id, entry in self._hosted_entries.items()
            if entry.expires_at <= now
        ]
        for request_id in hosted_expired:
            self._hosted_entries.pop(request_id, None)
        return len(expired) + len(hosted_expired)

    async def _cleanup_loop(self) -> None:
        interval = max(5, min(60, self.ttl_seconds // 2))
        while True:
            await asyncio.sleep(interval)
            await self.cleanup_expired()
