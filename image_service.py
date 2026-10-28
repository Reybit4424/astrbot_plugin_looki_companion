from __future__ import annotations

import asyncio
import base64
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from astrbot.api import logger
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageChain

DEFAULT_ANIME_PROMPT = "日系二次元插画风格，赛璐璐上色，线稿干净，配色柔和，画面干净通透，避免写实摄影质感与过度磨皮。"
IMAGE_RETENTION_SECONDS = 3 * 24 * 60 * 60


def _extract_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "files", "moments", "results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = _extract_items(value)
                if nested:
                    return nested
    return []


def _image_suffix(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return None


class LookiImageService:
    def __init__(self, plugin: Any, data_dir: Path):
        self.plugin = plugin
        self.temp_root = data_dir / "image_tmp"
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self._cleanup_expired()

    def _cleanup_expired(self) -> None:
        cutoff = datetime.now().timestamp() - IMAGE_RETENTION_SECONDS
        for path in self.temp_root.iterdir():
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except FileNotFoundError:
                pass

    async def send_period_image(
        self,
        event: Any,
        day: str,
        start_time: str,
        end_time: str,
        preference: str = "",
        style_adjustment: str = "",
    ) -> str:
        await asyncio.to_thread(self._cleanup_expired)
        if len(preference) > 500 or len(style_adjustment) > 1000:
            raise ValueError("选图偏好或风格调整过长")
        timezone = str(self.plugin.config.get("timezone") or "Asia/Shanghai")
        zone = ZoneInfo(timezone)
        start = datetime.fromisoformat(f"{day}T{start_time}").replace(tzinfo=zone)
        end = datetime.fromisoformat(f"{day}T{end_time}").replace(tzinfo=zone)
        if end <= start:
            raise ValueError("结束时间必须晚于开始时间")
        if self.plugin.schedule_provider is None:
            raise ValueError("未配置 Looki API Key，无法获取图片")

        items = await asyncio.to_thread(self.plugin.store.query, day, day, "", 100)
        matches = []
        for item in items:
            if not item.get("looki_id"):
                continue
            item_start = datetime.fromisoformat(item["start_at"])
            item_end = datetime.fromisoformat(item["end_at"])
            if item_start < end and item_end > start:
                matches.append(item)
        if not matches:
            return "该时段没有可查询图片的 Looki 日程，未扩展到其他时段。"

        work = Path(tempfile.mkdtemp(prefix="period-", dir=self.temp_root))
        count = max(1, min(int(self.plugin.config.get("image_candidate_count", 3)), 9))
        candidates = await self._download_candidates(matches, count, work)
        if not candidates:
            return "该时段没有可用图片，未扩展到其他时段。"
        selected_index = await self._select_candidate(candidates, preference, event)
        selected = candidates[selected_index]
        if self.plugin.config.get("image_send_original", False):
            output = selected["path"]
            mode = "原图"
        else:
            output = await self._generate_image(selected["path"], work, style_adjustment, event)
            mode = "图生图结果"
        await event.send(MessageChain().file_image(str(output)))
        return f"已从 {len(candidates)} 张候选图中选定第 {selected_index + 1} 张并发送{mode}；只发送了最终 1 张图片，请勿重复发图。"

    async def _download_candidates(self, matches: list[dict[str, Any]], limit: int, work: Path) -> list[dict[str, Any]]:
        provider = self.plugin.schedule_provider
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(timeout=provider.timeout, follow_redirects=True) as client:
            for item in matches:
                response = await client.get(
                    f"{provider.base_url}/moments/{item['looki_id']}/files",
                    headers={"x-api-key": provider.api_key},
                )
                response.raise_for_status()
                for raw in _extract_items(response.json()):
                    url = self._file_image_url(raw)
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    try:
                        path = await self._download_image(client, url, work / f"candidate_{len(candidates) + 1}")
                    except (httpx.HTTPError, OSError, ValueError) as exc:
                        logger.info("[Looki] 跳过不可用候选图: %s", exc)
                        continue
                    candidates.append({"path": path, "title": item.get("title") or "", "start_at": item.get("start_at") or ""})
                    if len(candidates) >= limit:
                        return candidates
        return candidates

    @staticmethod
    def _file_image_url(raw: Any) -> str:
        if not isinstance(raw, dict):
            return ""
        file_obj = raw.get("file") if isinstance(raw.get("file"), dict) else raw
        for obj in (file_obj, raw):
            kind = str(obj.get("media_type") or obj.get("mime_type") or obj.get("content_type") or obj.get("file_type") or "").lower()
            if ("/" in kind and not kind.startswith("image/")) or kind.startswith(("audio", "video")):
                return ""
        for key in ("temporary_url", "url", "signed_url", "download_url"):
            url = file_obj.get(key)
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url
        return ""

    @staticmethod
    async def _download_image(client: httpx.AsyncClient, url: str, target_without_suffix: Path) -> Path:
        response = await client.get(url)
        response.raise_for_status()
        data = response.content
        if len(data) > 20 * 1024 * 1024:
            raise ValueError("图片超过 20 MB")
        suffix = _image_suffix(data)
        if not suffix:
            raise ValueError("文件不是支持的 JPG、PNG 或 WebP 图片")
        path = target_without_suffix.with_suffix(suffix)
        await asyncio.to_thread(path.write_bytes, data)
        return path

    def _current_provider_id(self, event: Any) -> str:
        provider = self.plugin.context.get_using_provider(getattr(event, "unified_msg_origin", None))
        if not provider:
            return ""
        return str(provider.provider_config.get("id") or "")

    def _selection_provider_id(self, event: Any) -> str:
        return str(self.plugin.config.get("image_selection_provider_id") or "").strip() or self._current_provider_id(event)

    async def _select_candidate(self, candidates: list[dict[str, Any]], preference: str, event: Any) -> int:
        provider_id = self._selection_provider_id(event)
        if not provider_id:
            raise ValueError("未配置挑图多模态模型，且当前没有可用的对话模型")
        sources = "\n".join(f"{i}. 来源：{item['start_at']} {item['title']}" for i, item in enumerate(candidates, 1))
        prompt = (
            f"下面按顺序传入 {len(candidates)} 张候选图片，编号从 1 开始。\n{sources}\n"
            "请根据画面清晰度、信息量、构图和与用户偏好的匹配度选出最适合发送的 1 张。"
            "不要猜测画面外的事实。只回复选中的阿拉伯数字编号，不要解释。"
        )
        if preference:
            prompt += f"\n用户本次选图偏好：{preference}"
        response = await self.plugin.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            image_urls=[str(item["path"]) for item in candidates],
        )
        match = re.search(r"(?<!\d)([1-9]\d*)(?!\d)", str(response.completion_text or ""))
        if not match or not 1 <= int(match.group(1)) <= len(candidates):
            raise ValueError("挑图模型没有返回有效的候选编号")
        return int(match.group(1)) - 1

    async def _generate_image(self, reference: Path, work: Path, style_adjustment: str, event: Any) -> Path:
        style = str(self.plugin.config.get("image_style_prompt") or DEFAULT_ANIME_PROMPT).strip()
        prompt = (
            "以传入的参考图为唯一视觉依据进行图生图，保留主要人物、物体、构图和场景关系。"
            f"转换风格：{style}。"
        )
        if style_adjustment:
            prompt += f"本次额外调整：{style_adjustment}。"
        prompt += "只输出 1 张图片，不要输出文字。"
        mode = str(self.plugin.config.get("image_generation_mode") or "multimodal")
        if mode == "openai_compatible":
            return await self._generate_with_api(reference, work, prompt)
        if mode != "multimodal":
            raise ValueError("图生图方式配置无效")
        provider_id = str(self.plugin.config.get("image_generation_provider_id") or "").strip() or self._selection_provider_id(event)
        if not provider_id:
            raise ValueError("未配置图生图多模态模型")
        response = await self.plugin.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            image_urls=[str(reference)],
        )
        chain = getattr(response, "result_chain", None)
        for component in getattr(chain, "chain", []):
            if isinstance(component, Image):
                source = Path(await component.convert_to_file_path())
                suffix = source.suffix.lower() if source.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} else ".png"
                output = work / f"generated{suffix}"
                await asyncio.to_thread(shutil.copyfile, source, output)
                return output
        raise ValueError("图生图模型没有返回图片；不会退化为纯文生图")

    async def _generate_with_api(self, reference: Path, work: Path, prompt: str) -> Path:
        base = str(self.plugin.config.get("image_api_base_url") or "").strip().rstrip("/")
        model = str(self.plugin.config.get("image_api_model") or "").strip()
        key = str(self.plugin.config.get("image_api_key") or "").strip()
        if not base or not model:
            raise ValueError("请填写兼容图生图接口地址和模型名称")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        data = await asyncio.to_thread(reference.read_bytes)
        mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(reference.suffix.lower(), "image/png")
        async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
            if base.endswith("/images/generations"):
                response = await client.post(
                    base,
                    headers=headers,
                    json={
                        "model": model,
                        "prompt": prompt,
                        "image": f"data:{mime};base64," + base64.b64encode(data).decode("ascii"),
                        "watermark": False,
                    },
                )
            else:
                endpoint = base if base.endswith("/images/edits") else f"{base}/images/edits"
                response = await client.post(
                    endpoint,
                    headers=headers,
                    data={"model": model, "prompt": prompt},
                    files={"image": (reference.name, data, mime)},
                )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("data") if isinstance(payload, dict) else None
            first = rows[0] if isinstance(rows, list) and rows else None
            if not isinstance(first, dict):
                raise ValueError("图生图接口没有返回图片数据")
            if first.get("b64_json"):
                image_data = base64.b64decode(first["b64_json"])
            elif first.get("url"):
                image_response = await client.get(str(first["url"]))
                image_response.raise_for_status()
                image_data = image_response.content
            else:
                raise ValueError("图生图接口没有返回 url 或 b64_json")
        suffix = _image_suffix(image_data)
        if not suffix:
            raise ValueError("图生图接口返回的内容不是支持的图片")
        output = work / f"generated{suffix}"
        await asyncio.to_thread(output.write_bytes, image_data)
        return output
