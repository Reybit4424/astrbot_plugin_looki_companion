from __future__ import annotations

import asyncio
import base64
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from PIL import Image as PILImage
from PIL import ImageFilter, ImageStat

from astrbot.api import logger
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.media_utils import get_media_duration

DEFAULT_ANIME_PROMPT = "日系二次元插画风格，赛璐璐上色，线稿干净，配色柔和，画面干净通透，避免写实摄影质感与过度磨皮。"
IMAGE_RETENTION_SECONDS = 3 * 24 * 60 * 60
VIDEO_MAX_BYTES = 50 * 1024 * 1024
CANDIDATE_CONCURRENCY = 2


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

    async def send_activity_image(self, event: Any, query: str, preference: str = "", style_adjustment: str = "") -> str:
        item = await asyncio.to_thread(self.plugin.store.find_latest_looki_activity, query)
        if not item:
            return f"未在已保存日程中找到与「{query}」相关的活动，请补充其他关键词或大概日期。"
        zone = ZoneInfo(str(self.plugin.config.get("timezone") or "Asia/Shanghai"))
        start = datetime.fromisoformat(item["start_at"]).astimezone(zone)
        end = datetime.fromisoformat(item["end_at"]).astimezone(zone)
        result = await self.send_period_image(
            event,
            start.date().isoformat(),
            start.strftime("%H:%M:%S"),
            end.strftime("%H:%M:%S"),
            preference,
            style_adjustment,
            end.date().isoformat(),
            query=query,
        )
        if start.date() == end.date():
            period = f"{start.date().isoformat()} {start.strftime('%H:%M:%S')}–{end.strftime('%H:%M:%S')}"
        else:
            period = f"{start.date().isoformat()} {start.strftime('%H:%M:%S')}–{end.date().isoformat()} {end.strftime('%H:%M:%S')}"
        return f"已按活动「{query}」定位到最近一次日程：{period}「{item['title']}」。{result}"

    async def send_period_image(
        self,
        event: Any,
        day: str,
        start_time: str,
        end_time: str,
        preference: str = "",
        style_adjustment: str = "",
        end_date: str | None = None,
        query: str = "",
    ) -> str:
        await asyncio.to_thread(self._cleanup_expired)
        if len(preference) > 500 or len(style_adjustment) > 1000:
            raise ValueError("选图偏好或风格调整过长")
        timezone = str(self.plugin.config.get("timezone") or "Asia/Shanghai")
        zone = ZoneInfo(timezone)
        end_day = end_date or day
        start = datetime.fromisoformat(f"{day}T{start_time}").replace(tzinfo=zone)
        end = datetime.fromisoformat(f"{end_day}T{end_time}").replace(tzinfo=zone)
        if end <= start:
            raise ValueError("结束时间必须晚于开始时间")
        if self.plugin.schedule_provider is None:
            raise ValueError("未配置 Looki API Key，无法获取图片")

        items = await asyncio.to_thread(self.plugin.store.query, day, end_day, "", 100)
        matches = []
        nearby = []
        for item in items:
            if not item.get("looki_id"):
                continue
            item_start = datetime.fromisoformat(item["start_at"])
            item_end = datetime.fromisoformat(item["end_at"])
            if item_start < end and item_end > start:
                matches.append(item)
            else:
                distance = (start - item_end).total_seconds() if item_end <= start else (item_start - end).total_seconds()
                nearby.append((max(0.0, distance), item))
        nearby = [item for _, item in sorted(nearby, key=lambda entry: (entry[0], entry[1]["start_at"]))]
        if not matches and not nearby:
            return "该日期范围内没有可查询图片的 Looki 日程。"

        started = time.monotonic()
        work = Path(tempfile.mkdtemp(prefix="period-", dir=self.temp_root))
        count = max(1, min(int(self.plugin.config.get("image_candidate_count", 6)), 9))
        sent = False
        try:
            candidate_started = time.monotonic()
            candidates = await self._download_candidates(matches, count, work) if matches else []
            expanded = False
            if len(candidates) < count and nearby:
                additional = await self._download_candidates(nearby, count - len(candidates), work)
                expanded = bool(additional)
                candidates.extend(additional)
            logger.info("[Looki] 候选准备完成：%d 张，耗时 %.1f 秒", len(candidates), time.monotonic() - candidate_started)
            if not candidates:
                suffix = "；已尝试同日相邻活动，但仍无可用媒体" if nearby else ""
                return f"该时段没有可用图片或可抽帧视频{suffix}。"
            selection_started = time.monotonic()
            selected_index = await self._select_candidate(candidates, preference, event, query=query)
            logger.info("[Looki] 多模态挑图完成：第 %d 张，耗时 %.1f 秒", selected_index + 1, time.monotonic() - selection_started)
            selected = candidates[selected_index]
            if self.plugin.config.get("image_send_original", False):
                output = selected["path"]
                mode = "原图"
            else:
                generation_started = time.monotonic()
                output = await self._generate_image(selected["path"], work, style_adjustment, event)
                logger.info("[Looki] 图生图完成，耗时 %.1f 秒", time.monotonic() - generation_started)
                mode = "图生图结果"
            await event.send(MessageChain().file_image(str(output)))
            sent = True
            logger.info("[Looki] 图片工具总耗时 %.1f 秒", time.monotonic() - started)
            source_start = str(selected.get("start_at") or "").replace("T", " ")
            source_end = str(selected.get("end_at") or "").replace("T", " ")
            source_period = f"{source_start}–{source_end}" if source_end else source_start
            scope = "候选不足时已纳入同日相邻活动" if expanded else "仅使用原时段候选"
            return (
                f"已从 {len(candidates)} 张候选图中选定第 {selected_index + 1} 张并发送{mode}；"
                f"照片来源：{source_period}「{selected.get('title') or '未命名活动'}」；{scope}；"
                "只发送了最终 1 张图片，请勿重复发图。"
            )
        finally:
            if not sent:
                shutil.rmtree(work, ignore_errors=True)

    async def _download_candidates(self, matches: list[dict[str, Any]], limit: int, work: Path) -> list[dict[str, Any]]:
        provider = self.plugin.schedule_provider
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        groups: list[tuple[dict[str, Any], list[tuple[str, str]]]] = []
        target_index = 0
        for path in work.glob("candidate_*"):
            match = re.match(r"candidate_(\d+)", path.name)
            if match:
                target_index = max(target_index, int(match.group(1)))

        async def download(entry: tuple[dict[str, Any], str, str, Path]) -> list[dict[str, Any]]:
            item, media_type, url, target = entry
            if media_type == "video":
                frames = await self._download_video_frames(client, url, target)
            else:
                frames = [(await self._download_image(client, url, target), None)]
            quality_rows = []
            for path, position in frames:
                try:
                    acceptable, score = await asyncio.to_thread(self._image_quality, path)
                except Exception as exc:
                    logger.info("[Looki] 跳过无法解码的候选图片: %s", type(exc).__name__)
                    continue
                quality_rows.append((path, position, acceptable, score))
            accepted = [row for row in quality_rows if row[2]]
            if not accepted and media_type == "video" and quality_rows:
                accepted = [max(quality_rows, key=lambda row: row[3])]
            return [
                {
                    "path": path,
                    "title": item.get("title") or "",
                    "start_at": item.get("start_at") or "",
                    "end_at": item.get("end_at") or "",
                    "frame_position": position,
                }
                for path, position, _, _ in accepted
            ]

        async with httpx.AsyncClient(timeout=provider.timeout, follow_redirects=True) as client:
            for item in matches:
                try:
                    response = await client.get(
                        f"{provider.base_url}/moments/{item['looki_id']}/files",
                        headers={"x-api-key": provider.api_key},
                    )
                    response.raise_for_status()
                except Exception as exc:
                    logger.info("[Looki] 跳过无法读取文件列表的活动: %s", type(exc).__name__)
                    continue
                sources = []
                for raw in _extract_items(response.json()):
                    media_type, url = self._file_media_url(raw)
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    sources.append((media_type, url))
                if sources:
                    groups.append((item, sources))

            queue: list[tuple[dict[str, Any], str, str, Path]] = []
            per_group = max(1, (limit + len(groups) - 1) // len(groups)) if groups else 1
            ordered_groups = []
            for item, sources in groups:
                selected_count = min(per_group, len(sources))
                if selected_count == 1:
                    indices = {0}
                else:
                    indices = {round(i * (len(sources) - 1) / (selected_count - 1)) for i in range(selected_count)}
                ordered = [source for i, source in enumerate(sources) if i in indices]
                ordered.extend(source for i, source in enumerate(sources) if i not in indices)
                ordered_groups.append((item, ordered))
            for row in range(max((len(sources) for _, sources in ordered_groups), default=0)):
                for item, sources in ordered_groups:
                    if row >= len(sources):
                        continue
                    media_type, url = sources[row]
                    target_index += 1
                    queue.append((item, media_type, url, work / f"candidate_{target_index}"))

            for offset in range(0, len(queue), CANDIDATE_CONCURRENCY):
                batch = queue[offset:offset + CANDIDATE_CONCURRENCY]
                results = await asyncio.gather(*(download(entry) for entry in batch), return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        logger.info("[Looki] 跳过不可用候选媒体: %s", type(result).__name__)
                    else:
                        candidates.extend(result)
                if len(candidates) >= limit:
                    return candidates[:limit]
        return candidates

    @staticmethod
    def _image_quality(path: Path) -> tuple[bool, float]:
        with PILImage.open(path) as source:
            gray = source.convert("L")
            gray.thumbnail((96, 96))
            stats = ImageStat.Stat(gray)
            mean = stats.mean[0]
            contrast = stats.stddev[0]
            edges = gray.filter(ImageFilter.FIND_EDGES)
            if edges.width > 4 and edges.height > 4:
                edges = edges.crop((2, 2, edges.width - 2, edges.height - 2))
            detail = ImageStat.Stat(edges).mean[0]
        acceptable = 6 <= mean <= 249 and contrast >= 3 and detail >= 2
        return acceptable, detail + contrast * 0.1

    @staticmethod
    def _file_media_url(raw: Any) -> tuple[str, str]:
        if not isinstance(raw, dict):
            return "", ""
        file_obj = raw.get("file") if isinstance(raw.get("file"), dict) else raw
        media_type = "image"
        for obj in (file_obj, raw):
            kind = str(obj.get("media_type") or obj.get("mime_type") or obj.get("content_type") or obj.get("file_type") or "").lower()
            if not kind or kind.startswith("image"):
                continue
            if kind.startswith("video"):
                media_type = "video"
            else:
                return "", ""
        for key in ("temporary_url", "url", "signed_url", "download_url"):
            url = file_obj.get(key)
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return media_type, url
        return "", ""

    @staticmethod
    async def _download_image(client: httpx.AsyncClient, url: str, target_without_suffix: Path) -> Path:
        download_path = target_without_suffix.with_suffix(".download")
        downloaded = 0
        try:
            with download_path.open("wb") as output:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        downloaded += len(chunk)
                        if downloaded > 20 * 1024 * 1024:
                            raise ValueError("图片超过 20 MB")
                        output.write(chunk)
            with download_path.open("rb") as source:
                header = source.read(12)
            suffix = _image_suffix(header)
            if not suffix:
                raise ValueError("文件不是支持的 JPG、PNG 或 WebP 图片")
            path = target_without_suffix.with_suffix(suffix)
            download_path.replace(path)
            return path
        except BaseException:
            download_path.unlink(missing_ok=True)
            for suffix in (".jpg", ".png", ".webp"):
                target_without_suffix.with_suffix(suffix).unlink(missing_ok=True)
            raise

    async def _download_video_frames(self, client: httpx.AsyncClient, url: str, target_without_suffix: Path) -> list[tuple[Path, float]]:
        video_path = target_without_suffix.with_suffix(".mp4")
        try:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > VIDEO_MAX_BYTES:
                    raise ValueError("视频超过 50 MiB")
                downloaded = 0
                with video_path.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        downloaded += len(chunk)
                        if downloaded > VIDEO_MAX_BYTES:
                            raise ValueError("视频超过 50 MiB")
                        output.write(chunk)
        except Exception:
            video_path.unlink(missing_ok=True)
            raise

        duration_ms = await get_media_duration(str(video_path))
        positions = [duration_ms * ratio / 1000 for ratio in (0.2, 0.5, 0.8)] if duration_ms and duration_ms > 0 else [0.0]
        frames = []
        last_error = ""
        for index, position in enumerate(positions, 1):
            frame_path = target_without_suffix.with_name(f"{target_without_suffix.name}_{index}.jpg")
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{position:.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(frame_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await process.communicate()
            except asyncio.CancelledError:
                process.kill()
                await process.wait()
                frame_path.unlink(missing_ok=True)
                for previous_frame, _ in frames:
                    previous_frame.unlink(missing_ok=True)
                video_path.unlink(missing_ok=True)
                raise
            if process.returncode != 0:
                frame_path.unlink(missing_ok=True)
                last_error = stderr.decode(errors="replace").strip()[-500:] or "ffmpeg 执行失败"
                continue
            if not frame_path.exists() or frame_path.stat().st_size == 0:
                frame_path.unlink(missing_ok=True)
                last_error = "视频抽帧没有生成图片"
                continue
            frames.append((frame_path, position))
        if not frames:
            raise ValueError(f"视频抽帧失败: {last_error or '没有可用帧'}")
        return frames

    def _current_provider_id(self, event: Any) -> str:
        provider = self.plugin.context.get_using_provider(getattr(event, "unified_msg_origin", None))
        if not provider:
            return ""
        return str(provider.provider_config.get("id") or "")

    def _selection_provider_id(self, event: Any) -> str:
        return str(self.plugin.config.get("image_selection_provider_id") or "").strip() or self._current_provider_id(event)

    async def _select_candidate(self, candidates: list[dict[str, Any]], preference: str, event: Any, query: str = "") -> int:
        provider_id = self._selection_provider_id(event)
        if not provider_id:
            raise ValueError("未配置挑图多模态模型，且当前没有可用的对话模型")
        source_rows = []
        for index, item in enumerate(candidates, 1):
            row = f"{index}. 来源活动：{item['start_at']} {item['title']}"
            if item.get("frame_position") is not None:
                row += f"；视频帧位置：{item['frame_position']:.1f} 秒"
            source_rows.append(row)
        prompt = (
            f"下面按顺序传入 {len(candidates)} 张候选图片，编号从 1 开始。\n" + "\n".join(source_rows) + "\n"
            "先根据画面内容匹配目标活动、场景和用户偏好，再比较清晰度、构图和信息量，选出最适合发送的 1 张。"
            "活动标题和时间只是辅助线索，不得替代画面内容。不要猜测画面外的事实。只回复选中的阿拉伯数字编号，不要解释。"
        )
        if query:
            prompt += f"\n目标活动关键词：{query}"
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
                payload = {
                    "model": model,
                    "prompt": prompt,
                    "image": f"data:{mime};base64," + base64.b64encode(data).decode("ascii"),
                    "watermark": False,
                }
                if "seedream" in model.lower():
                    payload["size"] = "1K"
                response = await client.post(base, headers=headers, json=payload)
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
