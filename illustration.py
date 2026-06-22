from __future__ import annotations

import asyncio
import base64
import binascii
import uuid
from os.path import abspath, exists
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image
from astrbot.api.provider import LLMResponse

from .constants import PLUGIN_ID, PLUGIN_TAG


class OmniDrawBridge:
    """软依赖 omnidraw 的自动配图桥接。

    在 LLM 回复后异步调用 omnidraw 的 generate_images_for_plugin，
    生图完成后用 event.send 单独补发一张图片，不阻塞文本回复。
    """

    def __init__(self, context: Any, config: dict[str, Any]):
        self.context = context
        self.config = config
        self._tasks: set[asyncio.Task] = set()
        self._semaphore: asyncio.Semaphore | None = None

    def update_config(self, config: dict[str, Any]) -> None:
        self.config = config
        self._semaphore = None

    def _cfg(self, key: str, default: Any) -> Any:
        return self.config.get(key, default)

    def _get_omnidraw(self) -> Any | None:
        plugin_id = str(self._cfg("illustration_plugin_id", "astrbot_plugin_omnidraw") or "").strip()
        if not plugin_id:
            return None
        try:
            meta = self.context.get_registered_star(plugin_id)
        except Exception:
            return None
        if meta is None:
            return None
        inst = getattr(meta, "star_cls", None)
        if inst is None:
            return None
        if not hasattr(inst, "generate_images_for_plugin"):
            return None
        return inst

    async def maybe_illustrate(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        """钩子入口：检查开关与文本长度后，派发后台生图任务。"""
        await self.maybe_illustrate_text(event, str(response.completion_text or ""))

    async def maybe_illustrate_text(self, event: AstrMessageEvent, text: str) -> None:
        """在正文发送完成后，根据回复文本派发后台生图任务。"""
        if not bool(self._cfg("illustration_enabled", False)):
            return
        text = str(text or "").strip()
        if len(text) < 20:
            return
        omnidraw = self._get_omnidraw()
        if omnidraw is None:
            logger.debug("%s 未找到可用生图插件实例，跳过配图。", PLUGIN_TAG)
            return
        prompt = self._build_prompt(text)
        if not prompt:
            return
        consume = bool(self._cfg("illustration_consume_quota", False))
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(max(1, int(self._cfg("illustration_max_concurrency", 2))))
        task = asyncio.create_task(self._run(event, omnidraw, prompt, consume, self._semaphore))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _build_prompt(self, text: str) -> str:
        prefix = str(self._cfg("illustration_prompt_prefix", "") or "").strip()
        max_chars = max(20, int(self._cfg("illustration_max_text_chars", 600) or 600))
        trimmed = text[:max_chars].strip()
        if not trimmed:
            return ""
        return f"{prefix} {trimmed}".strip() if prefix else trimmed

    async def _run(
        self,
        event: AstrMessageEvent,
        omnidraw: Any,
        prompt: str,
        consume: bool,
        semaphore: asyncio.Semaphore | None,
    ) -> None:
        """后台生图 runner：调用 omnidraw 并补发图片，失败静默。"""
        try:
            async def _core() -> None:
                size = str(self._cfg("illustration_size", "") or "").strip()
                mode = str(self._cfg("illustration_mode", "text2img") or "text2img").strip().lower()
                kwargs: dict[str, Any] = {
                    "prompt": prompt,
                    "count": 1,
                    "mode": mode,
                    "event": event if consume else None,
                    "record_usage": consume,
                }
                if size:
                    kwargs["size"] = size
                result = await omnidraw.generate_images_for_plugin(**kwargs)
                if not isinstance(result, dict) or not result.get("success"):
                    message = result.get("message") if isinstance(result, dict) else "无返回"
                    logger.debug("%s 配图生成未成功: %s", PLUGIN_TAG, message)
                    return
                images = result.get("images") or []
                if not images:
                    return
                component = self._image_component(images[0])
                if component is None:
                    logger.debug("%s 配图结果无可发送的图片组件。", PLUGIN_TAG)
                    return
                await event.send(event.chain_result([component]))

            if semaphore is None:
                await _core()
            else:
                async with semaphore:
                    await _core()
        except Exception as exc:
            logger.warning("%s 配图任务异常: %s", PLUGIN_TAG, exc)

    def _image_component(self, image: dict[str, Any]) -> Any | None:
        """从 omnidraw 返回的 image 字典构造可发送的 Image 组件。"""
        file_path = str(image.get("file_path") or "").strip()
        url = str(image.get("url") or image.get("image_url") or "").strip()
        data_url = str(image.get("data_url") or "").strip()
        if file_path and exists(file_path):
            try:
                return Image.fromFileSystem(abspath(file_path))
            except Exception as exc:
                logger.warning("%s 本地配图加载失败: %s", PLUGIN_TAG, exc)
        if url.startswith("http"):
            try:
                return Image.fromURL(url)
            except Exception as exc:
                logger.warning("%s 配图 URL 构造失败: %s", PLUGIN_TAG, exc)
        if data_url.startswith("data:image"):
            saved = self._save_data_url(data_url)
            if saved:
                try:
                    return Image.fromFileSystem(saved)
                except Exception as exc:
                    logger.warning("%s 配图 data_url 落盘后加载失败: %s", PLUGIN_TAG, exc)
        return None

    def _save_data_url(self, data_url: str) -> str | None:
        """把 data:image/...;base64,... 落盘到插件数据目录并返回路径。"""
        try:
            header, encoded = data_url.split(",", 1)
        except ValueError:
            return None
        mime = "image/png"
        if header.lower().startswith("data:image/") and ";base64" in header.lower():
            mime_part = header.split(":", 1)[-1].split(";base64", 1)[0]
            if mime_part:
                mime = mime_part
        ext_map = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "image/gif": ".gif",
            "image/bmp": ".bmp",
            "image/avif": ".avif",
        }
        ext = ext_map.get(mime, ".png")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return None
        data_dir = Path.home() / ".astrbot" / "data" / PLUGIN_ID / "illustrations"
        data_dir.mkdir(parents=True, exist_ok=True)
        file_path = data_dir / f"{uuid.uuid4().hex}{ext}"
        file_path.write_bytes(data)
        return str(file_path)

    async def terminate(self) -> None:
        """插件卸载时取消所有后台生图任务。"""
        if not self._tasks:
            return
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
