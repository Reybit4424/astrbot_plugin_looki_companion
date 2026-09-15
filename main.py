from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

try:
    from astrbot.core.agent.message import TextPart
except ImportError:  # pragma: no cover
    TextPart = None  # type: ignore


PLUGIN_NAME = "astrbot_plugin_looki_companion"
AUTHOR = "瑞贝特"


def _load_plugin_version() -> str:
    metadata_path = Path(__file__).with_name("metadata.yaml")
    try:
        for line in metadata_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip("\"'")
    except OSError:
        return "unknown"
    return "unknown"


PLUGIN_VERSION = _load_plugin_version()


class LookiError(Exception):
    code = "LOOKI_ERROR"
    retryable = False

    def __init__(self, message: str, *, status_code: int | None = None, retry_after_sec: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_sec = retry_after_sec


class LookiConfigError(LookiError):
    code = "CONFIG_ERROR"


class LookiAuthError(LookiError):
    code = "AUTH_ERROR"


class LookiRateLimitError(LookiError):
    code = "RATE_LIMITED"
    retryable = True


class LookiTemporaryUnavailableError(LookiError):
    code = "TEMPORARILY_UNAVAILABLE"
    retryable = True


class LookiInvalidParamsError(LookiError):
    code = "INVALID_PARAMS"


class LookiNetworkError(LookiError):
    code = "NETWORK_ERROR"
    retryable = True


@pydantic_dataclass
class LookiGetRecentExperienceTool(FunctionTool[AstrAgentContext]):
    plugin: Any = None
    name: str = "looki_get_recent_experience"
    description: str = "获取最近一段时间的共同经历摘要，只返回一段自然语言。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "minutes": {
                    "type": "integer",
                    "description": "回看最近多少分钟，默认 120，最大 720。",
                    "default": 120,
                    "minimum": 10,
                    "maximum": 720,
                },
                "focus": {
                    "type": "string",
                    "description": "可选关注点，例如书店、晚饭、咖啡店。",
                },
            },
            "required": [],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return await self.plugin.tool_get_recent_experience(context.context.event, kwargs)


@pydantic_dataclass
class LookiGetCurrentSceneTool(FunctionTool[AstrAgentContext]):
    plugin: Any = None
    name: str = "looki_get_current_scene"
    description: str = "获取当前或最近几分钟的场景描述，只返回一段自然语言。"
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}, "required": []})

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        del kwargs
        return await self.plugin.tool_get_current_scene(context.context.event)


@pydantic_dataclass
class LookiRememberExperienceTool(FunctionTool[AstrAgentContext]):
    plugin: Any = None
    name: str = "looki_remember_experience"
    description: str = "在最近 12 小时内做语义回忆搜索，只返回一段自然语言。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要回忆的对象、地点或经历。",
                }
            },
            "required": ["query"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return await self.plugin.tool_remember_experience(context.context.event, kwargs)


@pydantic_dataclass
class LookiGetDaySummaryTool(FunctionTool[AstrAgentContext]):
    plugin: Any = None
    name: str = "looki_get_day_summary"
    description: str = "获取今天仍处于 12 小时有效期内的共同经历概览。"
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}, "required": []})

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        del kwargs
        return await self.plugin.tool_get_day_summary(context.context.event)


@register(
    PLUGIN_NAME,
    AUTHOR,
    "把 Looki 的最近 moments 和实时事件整理成陪伴式共同经历摘要。",
    PLUGIN_VERSION,
)
class LookiCompanionPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._http_client: httpx.AsyncClient | None = None
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_session: ClientSession | None = None
        self._cache: dict[str, dict[str, Any]] = {}
        self.context.add_llm_tools(
            LookiGetRecentExperienceTool(plugin=self),
            LookiGetCurrentSceneTool(plugin=self),
            LookiRememberExperienceTool(plugin=self),
            LookiGetDaySummaryTool(plugin=self),
        )

    async def initialize(self):
        if self._mcp_enabled():
            try:
                self._mcp_stack = AsyncExitStack()
                await self._mcp_stack.__aenter__()
                streams = await self._mcp_stack.enter_async_context(
                    streamablehttp_client(
                        self._mcp_url(),
                        headers={"x-origin-secret": self._mcp_secret()},
                        timeout=self._request_timeout(),
                    )
                )
                self._mcp_session = await self._mcp_stack.enter_async_context(
                    ClientSession(streams[0], streams[1])
                )
                await self._mcp_session.initialize()
                tools = await self._mcp_session.list_tools()
                logger.info("[%s] MCP 已连接，工具数: %d", PLUGIN_NAME, len(tools.tools))
            except Exception as exc:
                logger.error("[%s] MCP 连接失败: %s", PLUGIN_NAME, exc)
                if self._mcp_stack is not None:
                    await self._mcp_stack.aclose()
                self._mcp_stack = None
                self._mcp_session = None
        logger.info("[%s] 插件已初始化", PLUGIN_NAME)

    async def terminate(self):
        if self._mcp_stack is not None:
            await self._mcp_stack.aclose()
            self._mcp_stack = None
            self._mcp_session = None
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if TextPart is None or not self._inject_routing_hint():
            return
        part = TextPart(text=self._build_tool_policy(event))
        if hasattr(part, "mark_as_temp"):
            part = part.mark_as_temp()
        req.extra_user_content_parts.append(part)

    @filter.command_group("looki")
    def looki(self):
        pass

    @looki.command("status")
    async def looki_status(self, event: AstrMessageEvent):
        denied = self._deny_debug_if_not_admin(event)
        if denied:
            yield event.plain_result(denied)
            return
        lines = [
            "Looki 插件状态：",
            f"- 版本: {PLUGIN_VERSION}",
            f"- Base URL: {self._base_url()}",
            f"- 已配置 API Key: {'是' if bool(self._api_key()) else '否'}",
            f"- 启用实时事件: {'是' if self._enable_realtime() else '否'}",
            f"- 群聊可用: {'是' if self._enable_in_group_chat() else '否'}",
            f"- 仅管理员可触发: {'是' if self._admin_only() else '否'}",
            f"- 图片转文字: {'开' if self._enable_image_captioning() else '关'}",
            f"- 最近经历默认窗口: {self._recent_experience_window_minutes()} 分钟",
            f"- 请求超时: {self._request_timeout()} 秒",
            f"- 调试日志: {'开' if self._debug_enabled() else '关'}",
        ]
        yield event.plain_result("\n".join(lines))

    @looki.command("me")
    async def looki_me(self, event: AstrMessageEvent):
        denied = self._deny_debug_if_not_admin(event)
        if denied:
            yield event.plain_result(denied)
            return
        yield event.plain_result(await self._with_looki_errors(self._do_get_user_info_text))

    @looki.command("scene")
    async def looki_scene(self, event: AstrMessageEvent):
        yield event.plain_result(await self.tool_get_current_scene(event))

    @looki.command("recent")
    async def looki_recent(self, event: AstrMessageEvent):
        payload = self._extract_subcommand_payload(event.message_str, "looki recent")
        minutes = self._recent_experience_window_minutes()
        if payload:
            try:
                minutes = int(payload)
            except ValueError:
                yield event.plain_result("用法：/looki recent 90")
                return
        yield event.plain_result(await self.tool_get_recent_experience(event, {"minutes": minutes}))

    @looki.command("remember")
    async def looki_remember(self, event: AstrMessageEvent):
        payload = self._extract_subcommand_payload(event.message_str, "looki remember")
        if not payload:
            yield event.plain_result("用法：/looki remember 刚才那家日料店")
            return
        yield event.plain_result(await self.tool_remember_experience(event, {"query": payload}))

    @looki.command("today")
    async def looki_today(self, event: AstrMessageEvent):
        yield event.plain_result(await self.tool_get_day_summary(event))

    async def tool_get_recent_experience(self, event: AstrMessageEvent, payload: dict[str, Any]) -> str:
        return await self._guarded_tool_call(event, self._do_get_recent_experience, payload)

    async def tool_get_current_scene(self, event: AstrMessageEvent) -> str:
        return await self._guarded_tool_call(event, self._do_get_current_scene)

    async def tool_remember_experience(self, event: AstrMessageEvent, payload: dict[str, Any]) -> str:
        return await self._guarded_tool_call(event, self._do_remember_experience, payload)

    async def tool_get_day_summary(self, event: AstrMessageEvent) -> str:
        return await self._guarded_tool_call(event, self._do_get_day_summary)

    async def _guarded_tool_call(self, event: AstrMessageEvent, func, *args) -> str:
        denied = self._tool_denied_text(event)
        if denied:
            return denied
        return await self._with_looki_errors(func, event, *args)

    async def _with_looki_errors(self, func, *args) -> str:
        try:
            return await func(*args)
        except LookiConfigError:
            return "共同经历这条线还没接好，我先不乱补现场细节。"
        except LookiAuthError:
            return "Looki 认证刚刚没接上，我先按普通聊天继续。"
        except LookiInvalidParamsError as exc:
            return f"这次回忆的条件不太对：{exc}"
        except LookiRateLimitError:
            return "我这会儿回看得有点频繁了，稍等一下我再帮你续上这段经历。"
        except (LookiTemporaryUnavailableError, LookiNetworkError):
            return "Looki 这会儿有点不稳定，我先不硬猜我们刚才看到了什么。"
        except Exception as exc:  # pragma: no cover
            logger.exception("[%s] 未预期异常: %s", PLUGIN_NAME, exc)
            return "这段共同经历我暂时没整理顺，就先不乱讲了。"

    async def _do_get_recent_experience(self, event: AstrMessageEvent, payload: dict[str, Any]) -> str:
        minutes = self._normalize_recent_minutes(payload.get("minutes"))
        focus = self._clip(payload.get("focus"), 40)
        now = self._now_local()
        start_at = max(now - timedelta(minutes=minutes), now - timedelta(hours=12))
        moments = await self._fetch_window_moments(start_at, now)
        if not moments:
            return "刚才这段时间里，我没抓到还能安全使用的共同经历片段。"
        await self._enrich_moments_with_captions(event, moments, max_count=3)
        return self._render_timeline_summary(
            moments,
            precise_allowed=not self._is_group_chat(event),
            intro=f"从 {self._format_clock(start_at)} 到 {self._format_clock(now)} 这段时间，",
            focus=focus,
        )

    async def _do_get_current_scene(self, event: AstrMessageEvent) -> str:
        precise_allowed = not self._is_group_chat(event)
        if self._enable_realtime():
            item = await self._fetch_latest_event()
            if item is not None:
                await self._enrich_event_with_caption(event, item)
                return self._render_current_scene(item, precise_allowed=precise_allowed)
        now = self._now_local()
        moments = await self._fetch_window_moments(now - timedelta(minutes=30), now)
        if moments:
            latest = moments[-1]
            await self._enrich_moments_with_captions(event, [latest], max_count=1)
            return self._render_fallback_scene(latest, precise_allowed=precise_allowed)
        return "这会儿我没抓到足够新的现场片段，就先不乱猜你现在在哪儿。"

    async def _do_remember_experience(self, event: AstrMessageEvent, payload: dict[str, Any]) -> str:
        query = str(payload.get("query", "")).strip()
        if not query:
            raise LookiInvalidParamsError("query 不能为空。")
        moments = await self._search_recent_moments(query)
        if not moments:
            return f"我在最近 12 小时里还没想起和“{query}”特别贴近的那段经历。"
        await self._enrich_moments_with_captions(event, moments, max_count=2)
        return self._render_memory_summary(query, moments, precise_allowed=not self._is_group_chat(event))

    async def _do_get_day_summary(self, event: AstrMessageEvent) -> str:
        now = self._now_local()
        start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_at = max(start_of_today, now - timedelta(hours=12))
        moments = await self._fetch_window_moments(start_at, now)
        if not moments:
            return "今天这段有效时间里，我还没抓到稳定可用的共同经历摘要。"
        await self._enrich_moments_with_captions(event, moments, max_count=3)
        return self._render_timeline_summary(
            moments,
            precise_allowed=not self._is_group_chat(event),
            intro="今天到现在，",
            focus=None,
        )

    async def _do_get_user_info_text(self) -> str:
        payload = await self._request_json("/me")
        raw = self._extract_data(payload)
        user = raw.get("user") if isinstance(raw, dict) and isinstance(raw.get("user"), dict) else raw
        if not isinstance(user, dict):
            return "Looki 这边暂时没有返回用户信息。"
        nickname = self._pick(user, "nickname", "name", "display_name", "first_name")
        timezone_text = self._pick(user, "timezone", "tz")
        user_id = self._pick(user, "id", "user_id", "uid")
        locale = self._pick(user, "locale", "language")
        return "\n".join(
            [
                "Looki 当前用户：",
                f"- 昵称: {nickname or '未提供'}",
                f"- 用户 ID: {user_id or '未提供'}",
                f"- 时区: {timezone_text or '未提供'}",
                f"- 语言: {locale or '未提供'}",
            ]
        )

    async def _fetch_latest_event(self) -> dict[str, Any] | None:
        cached = self._get_cache("latest_event")
        if isinstance(cached, dict):
            return cached
        payload = await self._request_json("/realtime/latest-event")
        raw = self._extract_data(payload)
        if not isinstance(raw, dict):
            return None
        item = self._normalize_event(raw)
        occurred_at = item.get("occurred_at_dt")
        if not isinstance(occurred_at, datetime):
            return None
        if not self._is_inside_looki_window(occurred_at, self._now_local()):
            return None
        self._set_cache("latest_event", item, self._realtime_cache_seconds())
        return item

    async def _fetch_window_moments(self, start_at: datetime, end_at: datetime) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for day in self._enumerate_dates(start_at, end_at):
            payload = await self._request_json("/moments", params={"on_date": day, "page_size": 50})
            for raw in self._extract_items(payload):
                if not isinstance(raw, dict):
                    continue
                item = self._normalize_moment(raw)
                if item is None:
                    continue
                occurred_at = item.get("occurred_at_dt")
                if not isinstance(occurred_at, datetime):
                    continue
                if not (start_at <= occurred_at <= end_at):
                    continue
                if not self._is_inside_looki_window(occurred_at, end_at):
                    continue
                items.append(item)
        deduped: dict[str, dict[str, Any]] = {}
        for item in items:
            key = str(item.get("moment_id") or item.get("occurred_at") or "")
            if key:
                deduped[key] = item
        result = list(deduped.values())
        result.sort(key=lambda item: item["occurred_at_dt"])
        return result[-8:]

    async def _search_recent_moments(self, query: str) -> list[dict[str, Any]]:
        payload = await self._request_json("/moments/search", params={"query": query, "page_size": 8})
        now = self._now_local()
        items: list[dict[str, Any]] = []
        for raw in self._extract_items(payload):
            if not isinstance(raw, dict):
                continue
            item = self._normalize_moment(raw)
            if item is None:
                continue
            occurred_at = item.get("occurred_at_dt")
            if not isinstance(occurred_at, datetime):
                continue
            if not self._is_inside_looki_window(occurred_at, now):
                continue
            items.append(item)
        items.sort(key=lambda item: (float(item.get("score") or 0.0), item["occurred_at_dt"].timestamp()), reverse=True)
        return items[:3]

    async def _enrich_moments_with_captions(self, event: AstrMessageEvent, moments: list[dict[str, Any]], *, max_count: int) -> None:
        if not self._enable_image_captioning():
            return
        captioned = 0
        for item in moments:
            if captioned >= max_count or item.get("visual_text"):
                continue
            image_url = await self._resolve_moment_image_url(item)
            if not image_url:
                continue
            caption = await self._caption_image(event, image_url)
            if caption:
                item["visual_text"] = caption
                captioned += 1

    async def _enrich_event_with_caption(self, event: AstrMessageEvent, item: dict[str, Any]) -> None:
        if not self._enable_image_captioning() or item.get("visual_text"):
            return
        image_url = item.get("image_url")
        if not isinstance(image_url, str) or not image_url:
            return
        caption = await self._caption_image(event, image_url)
        if caption:
            item["visual_text"] = caption

    async def _resolve_moment_image_url(self, item: dict[str, Any]) -> str | None:
        cover_url = item.get("cover_url")
        if isinstance(cover_url, str) and cover_url and self._is_url_fresh(item.get("cover_expires_at")):
            return cover_url
        moment_id = str(item.get("moment_id") or "").strip()
        if not moment_id:
            return None
        cache_key = f"moment_file:{moment_id}"
        cached = self._get_cache(cache_key)
        if isinstance(cached, str) and cached:
            return cached
        payload = await self._request_json(f"/moments/{moment_id}/files")
        for raw in self._extract_items(payload):
            if not isinstance(raw, dict):
                continue
            file_item = self._normalize_file(raw)
            if file_item is None:
                continue
            url = file_item.get("temporary_url")
            if isinstance(url, str) and url and self._is_url_fresh(file_item.get("expires_at")):
                self._set_cache(cache_key, url, 300)
                return url
        return None

    async def _caption_image(self, event: AstrMessageEvent, image_url: str) -> str | None:
        cache_key = f"caption:{image_url}"
        cached = self._get_cache(cache_key)
        if isinstance(cached, str):
            return cached
        if self._caption_model() and self._caption_api_base() and self._caption_api_key():
            text = await self._caption_with_custom_api(image_url)
        else:
            text = await self._caption_with_current_provider(event, image_url)
        if not text:
            return None
        normalized = self._sanitize_scene_text(text)
        if normalized:
            self._set_cache(cache_key, normalized, 1800)
        return normalized

    async def _caption_with_custom_api(self, image_url: str) -> str | None:
        client = self._get_http_client()
        url = f"{self._caption_api_base().rstrip('/')}/chat/completions"
        payload = {
            "model": self._caption_model(),
            "temperature": 0.2,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请用一句简短中文描述可见画面，不要提到照片，也不要猜测不可见的隐私信息。"},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
        }
        headers = {"Authorization": f"Bearer {self._caption_api_key()}", "Content-Type": "application/json"}
        try:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            if self._debug_enabled():
                logger.warning("[%s] 图片转文字失败: %s", PLUGIN_NAME, exc)
            return None
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            return None
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        return content.strip() if isinstance(content, str) and content.strip() else None

    async def _caption_with_current_provider(self, event: AstrMessageEvent, image_url: str) -> str | None:
        try:
            provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
            if not provider_id:
                return None
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt="请用一句简短中文描述可见画面，不要提到照片，也不要猜测不可见的隐私信息。",
                image_urls=[image_url],
            )
        except Exception as exc:
            if self._debug_enabled():
                logger.warning("[%s] 使用当前模型做图片转文字失败: %s", PLUGIN_NAME, exc)
            return None
        text = getattr(llm_resp, "completion_text", None)
        return text.strip() if isinstance(text, str) and text.strip() else None

    def _render_current_scene(self, item: dict[str, Any], *, precise_allowed: bool) -> str:
        scene_text = self._build_scene_detail(item)
        location = self._render_location(item.get("location_name"), precise_allowed=precise_allowed)
        occurred_at = item.get("occurred_at_dt")
        time_text = self._format_clock(occurred_at) if isinstance(occurred_at, datetime) else "刚刚"
        if scene_text:
            return f"{time_text} 这会儿我们像是在 {location}，还能注意到 {scene_text}。"
        return f"{time_text} 这会儿我们像是在 {location}。"

    def _render_fallback_scene(self, item: dict[str, Any], *, precise_allowed: bool) -> str:
        scene_text = self._build_scene_detail(item)
        location = self._render_location(item.get("location_name"), precise_allowed=precise_allowed)
        occurred_at = item.get("occurred_at_dt")
        time_text = self._format_clock(occurred_at) if isinstance(occurred_at, datetime) else "刚刚"
        if scene_text:
            return f"我没抓到实时事件，不过 {time_text} 前后我们像是在 {location}，还能注意到 {scene_text}。"
        return f"我没抓到实时事件，不过 {time_text} 前后我们像是在 {location}。"

    def _render_memory_summary(self, query: str, moments: list[dict[str, Any]], *, precise_allowed: bool) -> str:
        first = self._render_memory_snippet(moments[0], precise_allowed=precise_allowed)
        if len(moments) == 1:
            return f"如果你说的是“{query}”，我最先想到的是：{first}。"
        second = self._render_memory_snippet(moments[1], precise_allowed=precise_allowed)
        return f"如果你说的是“{query}”，我最先想到的是：{first}；另一段也有点像，是 {second}。"

    def _render_timeline_summary(self, moments: list[dict[str, Any]], *, precise_allowed: bool, intro: str, focus: str | None) -> str:
        snippets = [self._render_timeline_snippet(item, precise_allowed=precise_allowed, index=index) for index, item in enumerate(moments[:4])]
        body = "; ".join(text for text in snippets if text)
        if len(moments) > 4:
            body += "；后面还有几段零碎片段"
        if focus:
            return f"如果只抓和“{focus}”更贴近的部分，最清楚的主线是：{body}。"
        return f"{intro}{body}."

    def _render_timeline_snippet(self, item: dict[str, Any], *, precise_allowed: bool, index: int) -> str:
        occurred_at = item.get("occurred_at_dt")
        time_text = self._format_clock(occurred_at) if isinstance(occurred_at, datetime) else "then"
        location = self._render_location(item.get("location_name"), precise_allowed=precise_allowed)
        detail = self._build_scene_detail(item)
        prefix = f"{time_text} 我们先到了 {location}" if index == 0 else f"{time_text} 后来又到了 {location}"
        return f"{prefix}，还能注意到 {detail}" if detail else prefix

    def _render_memory_snippet(self, item: dict[str, Any], *, precise_allowed: bool) -> str:
        occurred_at = item.get("occurred_at_dt")
        time_text = self._format_clock(occurred_at) if isinstance(occurred_at, datetime) else "那会儿"
        location = self._render_location(item.get("location_name"), precise_allowed=precise_allowed)
        detail = self._build_scene_detail(item)
        return f"{time_text} 前后我们在 {location}，还能注意到 {detail}" if detail else f"{time_text} 前后我们在 {location}"

    def _build_scene_detail(self, item: dict[str, Any]) -> str | None:
        for key in ("visual_text", "summary", "title", "activity"):
            text = self._sanitize_scene_text(item.get(key))
            if text:
                return text
        return None

    def _build_tool_policy(self, event: AstrMessageEvent) -> str:
        if self._tool_denied_text(event):
            return (
                "<looki_tool_policy>\n"
                "当前会话禁止使用任何 looki_* 工具。\n"
                "不要调用 Looki 工具，也不要提及 Looki、权限限制或查询失败。\n"
                "</looki_tool_policy>"
            )
        group_note = (
            "当前是群聊。若需要调用 Looki，请保持模糊表达，不要暴露精确位置。"
            if self._is_group_chat(event)
            else "当前是私聊，可以自然使用“我们”的共同经历口吻。"
        )
        return (
            "<looki_tool_policy>\n"
            "Looki 只负责最近 12 小时内的实时场景和共同经历，不负责 journals，也不负责更久以前的旧事。\n"
            "当用户问我们刚才去了哪里、我们刚刚在做什么、今天我们去了哪些地方时，优先调用 looki_get_recent_experience、looki_get_current_scene、looki_get_day_summary。\n"
            "当用户在回忆最近 12 小时内的某个地方、物件或经历时，优先调用 looki_remember_experience。\n"
            "如果用户在问超过 12 小时的旧事，或者在问日记正文、笔记原文，不要使用 Looki，应交给记忆插件或日记插件。\n"
            "Looki 工具已经直接返回自然语言摘要。请直接融入回答，不要提及 API、moment、URL 或 JSON。\n"
            f"{group_note}\n"
            "</looki_tool_policy>"
        )

    async def _request_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        if not self._api_key():
            raise LookiConfigError("Looki API Key 尚未配置。")
        last_error: Exception | None = None
        for attempt in range(self._max_attempts()):
            try:
                return await self._request_json_once(path, params=params)
            except (LookiRateLimitError, LookiTemporaryUnavailableError, LookiNetworkError) as exc:
                last_error = exc
                if attempt + 1 >= self._max_attempts():
                    break
                await asyncio.sleep(self._retry_delay(attempt, exc.retry_after_sec))
            except Exception as exc:
                last_error = exc
                break
        if isinstance(last_error, Exception):
            raise last_error
        raise LookiTemporaryUnavailableError("Looki 请求失败。")

    async def _request_json_once(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        if self._mcp_enabled():
            return await self._mcp_request_json(path, params=params)
        client = self._get_http_client()
        url = f"{self._base_url().rstrip('/')}/{path.lstrip('/')}"
        headers = {"X-API-Key": self._api_key(), "Accept": "application/json", "User-Agent": f"{PLUGIN_NAME}/{PLUGIN_VERSION}"}
        if self._debug_enabled():
            logger.info("[%s] GET %s params=%s", PLUGIN_NAME, path, params or {})
        try:
            response = await client.get(url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise LookiNetworkError("Looki 请求超时。") from exc
        except httpx.HTTPError as exc:
            raise LookiNetworkError("Looki 网络连接失败。") from exc
        if response.status_code in {401, 403}:
            raise LookiAuthError("Looki API Key 无效或没有权限。", status_code=response.status_code)
        if response.status_code == 429:
            retry_after = self._parse_retry_after(response.headers.get("Retry-After"))
            raise LookiRateLimitError("Looki 请求过于频繁。", status_code=429, retry_after_sec=retry_after)
        if response.status_code in {502, 503, 504}:
            raise LookiTemporaryUnavailableError("Looki 服务暂时不可用。", status_code=response.status_code)
        if response.status_code >= 500:
            raise LookiTemporaryUnavailableError(f"Looki 上游服务异常，HTTP {response.status_code}。", status_code=response.status_code)
        if response.status_code in {400, 404}:
            raise LookiInvalidParamsError(self._extract_error_message(response) or f"请求参数无效，HTTP {response.status_code}。")
        if response.status_code >= 300:
            raise LookiTemporaryUnavailableError(self._extract_error_message(response) or f"Looki 请求失败，HTTP {response.status_code}。", status_code=response.status_code)
        try:
            return response.json()
        except Exception as exc:
            raise LookiTemporaryUnavailableError("Looki 返回了无法解析的 JSON。") from exc

    async def _mcp_request_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        if self._mcp_session is None:
            raise LookiTemporaryUnavailableError("Looki MCP 服务尚未连接。")
        params = params or {}
        if path == "/me":
            name, arguments = "get_profile", {}
        elif path == "/realtime/latest-event":
            name, arguments = "get_realtime_event", {}
        elif path == "/moments":
            name, arguments = "get_moments_by_date", {"date": params.get("on_date")}
        elif path == "/moments/search":
            name, arguments = "search_moments", {"query": params.get("query", ""), "days": 1, "max_results": params.get("page_size", 8)}
        elif path.startswith("/moments/") and path.endswith("/files"):
            name, arguments = "get_moment_files", {"moment_id": path.split("/")[2]}
        else:
            raise LookiInvalidParamsError(f"MCP 暂未映射接口: {path}")
        try:
            result = await self._mcp_session.call_tool(name, arguments)
        except Exception as exc:
            raise LookiNetworkError("Looki MCP 调用失败。") from exc
        if getattr(result, "is_error", getattr(result, "isError", False)):
            raise LookiTemporaryUnavailableError("Looki MCP 返回错误。")
        text = "\n".join(getattr(item, "text", "") for item in result.content)
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"code": 0, "detail": "OK", "data": text}

    def _extract_error_message(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except Exception:
            return ""
        if isinstance(payload, dict):
            for key in ("message", "error", "detail"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            timeout_seconds = self._request_timeout()
            timeout = httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))
            self._http_client = httpx.AsyncClient(timeout=timeout)
        return self._http_client

    def _normalize_event(self, raw: dict[str, Any]) -> dict[str, Any]:
        image_url, expires_at = self._extract_image_source(raw)
        return {
            "event_id": self._pick(raw, "id", "event_id"),
            "title": self._clip(self._pick(raw, "title", "name"), 80),
            "summary": self._clip(self._pick(raw, "summary", "description", "activity", "text"), self._summary_max_chars()),
            "activity": self._clip(self._pick(raw, "activity", "type", "event_type"), 80),
            "occurred_at": self._pick(raw, "start_time", "occurred_at", "created_at", "timestamp"),
            "occurred_at_dt": self._parse_datetime(self._pick(raw, "start_time", "occurred_at", "created_at", "timestamp")),
            "location_name": self._pick(raw, "location_name", "place_name", "venue", "location"),
            "image_url": image_url,
            "image_expires_at": expires_at,
        }

    def _normalize_moment(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        occurred_at = self._pick(raw, "start_time", "occurred_at", "created_at", "timestamp")
        occurred_at_dt = self._parse_datetime(occurred_at)
        if occurred_at_dt is None:
            return None
        cover_url, cover_expires_at = self._extract_image_source(raw)
        return {
            "moment_id": self._pick(raw, "id", "moment_id"),
            "title": self._clip(self._pick(raw, "title", "name"), 80),
            "summary": self._clip(self._pick(raw, "summary", "description", "caption", "text"), self._summary_max_chars()),
            "occurred_at": occurred_at,
            "occurred_at_dt": occurred_at_dt,
            "location_name": self._pick(raw, "location_name", "place_name", "venue"),
            "cover_url": cover_url,
            "cover_expires_at": cover_expires_at,
            "score": self._pick(raw, "score", "similarity"),
        }

    def _normalize_file(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        file_obj = raw.get("file") if isinstance(raw.get("file"), dict) else raw
        if not isinstance(file_obj, dict):
            return None
        url = self._pick(file_obj, "temporary_url", "url", "signed_url", "download_url")
        if not isinstance(url, str) or not url:
            return None
        return {"temporary_url": url, "expires_at": self._resolve_expires_at(file_obj)}

    def _extract_image_source(self, raw: dict[str, Any]) -> tuple[str | None, str | None]:
        cover_file = raw.get("cover_file")
        if isinstance(cover_file, dict):
            file_obj = cover_file.get("file") if isinstance(cover_file.get("file"), dict) else cover_file
            if isinstance(file_obj, dict):
                url = self._pick(file_obj, "temporary_url", "url", "signed_url", "download_url")
                return (url if isinstance(url, str) else None, self._resolve_expires_at(file_obj))
        for key in ("cover", "thumbnail", "image", "file"):
            value = raw.get(key)
            if isinstance(value, dict):
                url = self._pick(value, "temporary_url", "url", "signed_url", "download_url")
                return (url if isinstance(url, str) else None, self._resolve_expires_at(value))
        url = self._pick(raw, "cover_url", "thumbnail_url", "temporary_url", "image_url")
        return (url if isinstance(url, str) else None, self._resolve_expires_at(raw))

    def _extract_data(self, payload: Any) -> Any:
        if isinstance(payload, dict):
            for key in ("data", "item", "event", "moment", "user"):
                if key in payload:
                    return payload.get(key)
        return payload

    def _extract_items(self, payload: Any) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("items", "moments", "results", "days", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
                if isinstance(value, dict):
                    for nested_key in ("items", "moments", "results", "days"):
                        nested = value.get(nested_key)
                        if isinstance(nested, list):
                            return nested
        return []

    def _pick(self, raw: dict[str, Any], *keys: str, default: Any = None) -> Any:
        for key in keys:
            if key in raw and raw[key] not in (None, ""):
                return raw[key]
        return default

    def _clip(self, value: Any, limit: int) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "..."

    def _sanitize_scene_text(self, value: Any) -> str | None:
        text = self._clip(value, self._summary_max_chars())
        if not text:
            return None
        text = text.replace("photo", "scene").replace("Photo", "Scene").replace("moment", "scene")
        return " ".join(text.split()).strip(" .,;:") or None

    def _render_location(self, value: Any, *, precise_allowed: bool) -> str:
        name = self._clip(value, 30)
        if not name:
            return "外面"
        if precise_allowed:
            return name
        lowered = name.lower()
        if any(word in lowered for word in ("cafe", "coffee", "tea")):
            return "一家喝东西的地方"
        if any(word in lowered for word in ("book", "store", "shop")):
            return "室内某处"
        if any(word in lowered for word in ("mall", "plaza")):
            return "公共场所里"
        if any(word in lowered for word in ("station", "metro", "bus")):
            return "路上"
        return "外面"

    def _parse_datetime(self, value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value.astimezone()
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone()
        text = str(value).strip().replace("Z", "+00:00")
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self._now_local().tzinfo)
        return dt.astimezone()

    def _resolve_expires_at(self, raw: dict[str, Any]) -> str | None:
        value = self._pick(raw, "expires_at", "expire_at", "temporary_url_expires_at", "url_expires_at")
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone().isoformat(timespec="seconds")
        if self._pick(raw, "temporary_url", "url", "signed_url", "download_url"):
            return (self._now_local() + timedelta(hours=12)).isoformat(timespec="seconds")
        return None

    def _is_url_fresh(self, expires_at: Any) -> bool:
        dt = self._parse_datetime(expires_at)
        return True if dt is None else dt > self._now_local()

    def _is_inside_looki_window(self, occurred_at: datetime, reference_now: datetime) -> bool:
        return reference_now - timedelta(hours=12) <= occurred_at <= reference_now + timedelta(minutes=5)

    def _format_clock(self, dt: datetime) -> str:
        return dt.astimezone().strftime("%H:%M")

    def _enumerate_dates(self, start_at: datetime, end_at: datetime) -> list[str]:
        result: list[str] = []
        current = start_at.date()
        end_date = end_at.date()
        while current <= end_date:
            result.append(current.isoformat())
            current += timedelta(days=1)
        return result

    def _extract_subcommand_payload(self, message: str, command_text: str) -> str:
        normalized = (message or "").strip()
        for prefix in (f"/{command_text}", command_text):
            if normalized.startswith(prefix):
                return normalized[len(prefix) :].strip()
        return ""

    def _deny_debug_if_not_admin(self, event: AstrMessageEvent) -> str | None:
        return None if event.is_admin() else "这个调试命令当前只允许管理员使用。"

    def _tool_denied_text(self, event: AstrMessageEvent) -> str | None:
        if self._is_group_chat(event) and not self._enable_in_group_chat():
            return "请按普通聊天继续回答，不要提及 Looki、权限限制或查询失败。"
        if self._admin_only() and not event.is_admin():
            return "请按普通聊天继续回答，不要提及 Looki、权限限制或查询失败。"
        return None

    def _is_group_chat(self, event: AstrMessageEvent) -> bool:
        return not event.is_private_chat()

    def _set_cache(self, key: str, value: Any, ttl_seconds: int) -> None:
        self._cache[key] = {"expires_at": self._now_local().timestamp() + max(1, ttl_seconds), "value": value}

    def _get_cache(self, key: str) -> Any:
        cache = self._cache.get(key)
        if not cache:
            return None
        if float(cache.get("expires_at", 0)) <= self._now_local().timestamp():
            self._cache.pop(key, None)
            return None
        return cache.get("value")

    def _retry_delay(self, attempt: int, retry_after_sec: int | None) -> float:
        if retry_after_sec is not None and retry_after_sec > 0:
            return min(float(retry_after_sec), 10.0)
        base = 0.8 * (2**attempt)
        return min(base + random.uniform(0.0, 0.25), 5.0)

    def _parse_retry_after(self, value: str | None) -> int | None:
        if not value:
            return None
        try:
            return max(1, int(value))
        except ValueError:
            return None

    def _max_attempts(self) -> int:
        return 3

    def _base_url(self) -> str:
        return str(self.config.get("looki_base_url") or "https://open.looki.tech/api/v1").strip()

    def _mcp_enabled(self) -> bool:
        return bool(self.config.get("mcp_enabled", True))

    def _mcp_url(self) -> str:
        return str(self.config.get("mcp_url", "http://172.17.0.1:3456/mcp")).strip()

    def _mcp_secret(self) -> str:
        return str(self.config.get("mcp_origin_secret", "")).strip()

    def _api_key(self) -> str:
        return str(self.config.get("looki_api_key") or "").strip()

    def _enable_realtime(self) -> bool:
        return bool(self.config.get("enable_realtime", True))

    def _enable_in_group_chat(self) -> bool:
        return bool(self.config.get("enable_in_group_chat", False))

    def _admin_only(self) -> bool:
        return bool(self.config.get("admin_only", True))

    def _enable_image_captioning(self) -> bool:
        return bool(self.config.get("enable_image_captioning", False))

    def _caption_model(self) -> str:
        return str(self.config.get("caption_model") or "").strip()

    def _caption_api_base(self) -> str:
        return str(self.config.get("caption_api_base") or "https://api.siliconflow.cn/v1").strip()

    def _caption_api_key(self) -> str:
        return str(self.config.get("caption_api_key") or "").strip()

    def _request_timeout(self) -> int:
        try:
            return max(5, int(self.config.get("request_timeout", 30)))
        except (TypeError, ValueError):
            return 30

    def _recent_experience_window_minutes(self) -> int:
        try:
            return max(10, min(720, int(self.config.get("recent_experience_window_minutes", 120))))
        except (TypeError, ValueError):
            return 120

    def _normalize_recent_minutes(self, value: Any) -> int:
        try:
            minutes = int(value if value is not None else self._recent_experience_window_minutes())
        except (TypeError, ValueError):
            minutes = self._recent_experience_window_minutes()
        return max(10, min(720, minutes))

    def _summary_max_chars(self) -> int:
        try:
            return max(80, int(self.config.get("moment_summary_max_chars", 220)))
        except (TypeError, ValueError):
            return 220

    def _realtime_cache_seconds(self) -> int:
        try:
            return max(1, int(self.config.get("realtime_cache_seconds", 15)))
        except (TypeError, ValueError):
            return 15

    def _inject_routing_hint(self) -> bool:
        return bool(self.config.get("inject_routing_hint", True))

    def _debug_enabled(self) -> bool:
        return bool(self.config.get("enable_debug_logging", False))

    def _now_local(self) -> datetime:
        return datetime.now().astimezone()
