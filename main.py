from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .image_service import DEFAULT_ANIME_PROMPT, LookiImageService
from .media_service import fetch_moment_evidence, summary_entry
from .schedule_store import ScheduleStore

PLUGIN_NAME = "astrbot_plugin_looki_companion"
AUTHOR = "瑞贝特"
VERSION = "0.8.0"
DEFAULT_LOOKI_BASE_URL = "https://open.looki.tech/api/v1"
INITIAL_IMPORT_META_KEY = "initial_import_choice"
CLUE_SCOPE_ADMIN = "仅对管理成员开放"
CLUE_SCOPES = (CLUE_SCOPE_ADMIN, "关闭")


def _config_ids(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = re.split(r"[\s,，;；]+", str(value or ""))
    return {str(item).strip() for item in values if str(item).strip()}


def _has_schedule_access(plugin: Any, event: Any) -> bool:
    if event is None or event.is_private_chat():
        return True
    sender_id = str(event.get_sender_id() or "")
    group_id = str(event.get_group_id() or "")
    if sender_id and sender_id in _config_ids(plugin.config.get("whitelist_user_ids")):
        return True
    if group_id and group_id in _config_ids(plugin.config.get("whitelist_group_ids")):
        return True
    return bool(plugin.config.get("allow_group_query", False))


def _localize_event(payload: dict, timezone: str) -> dict:
    """把页面提交的起止时间统一换算到插件配置时区。

    页面用 toISOString() 提交 UTC，若原样入库，ORDER BY start_at 会因偏移不同而乱序，
    day 也可能与实际日期错位。
    """
    zone = ZoneInfo(timezone)
    for field in ("start", "end"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                payload[field] = parsed.astimezone(zone).isoformat()
    return payload


def _get_plugin(context: Context) -> Any:
    metadata = context.get_registered_star(PLUGIN_NAME)
    return metadata.star_cls if metadata else None


_MEDIA_TYPE_ALIASES = {
    "image": "photo",
    "photo": "photo",
    "picture": "photo",
    "video": "video",
    "movie": "video",
    "audio": "audio",
    "voice": "audio",
    "sound": "audio",
    "transcript": "transcript",
    "text": "transcript",
}


def _normalize_media_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    for alias, normalized in _MEDIA_TYPE_ALIASES.items():
        if text.startswith(alias):
            return normalized
    return "unknown"


def moment_media_types(moment: Any) -> list[str]:
    """汇总 moment 自带的媒体类型与封面类型，未知类型统一记为 unknown。"""
    if not isinstance(moment, dict):
        return []
    raw_types: list[Any] = []
    values = moment.get("media_types")
    if isinstance(values, list):
        raw_types.extend(values)
    elif isinstance(values, str) and values.strip():
        raw_types.append(values)
    for key in ("cover_file", "file"):
        obj = moment.get(key)
        if isinstance(obj, dict):
            inner = obj.get("file") if isinstance(obj.get("file"), dict) else obj
            raw_types.append(inner.get("media_type"))
    result: list[str] = []
    for value in raw_types:
        if not value:
            continue
        normalized = _normalize_media_type(value)
        if normalized not in result:
            result.append(normalized)
    return result


def capture_mode_of(moment: Any, media_types: list[str] | None = None) -> str:
    """优先使用 Looki 明示的采集模式；缺失时由媒体类型保守推断，未知即 unknown。"""
    declared = ""
    if isinstance(moment, dict):
        for key in ("capture_mode", "recording_mode", "media_mode", "mode"):
            value = moment.get(key)
            if isinstance(value, str) and value.strip():
                declared = value.strip().lower()
                break
    if declared:
        for known in ("photo", "video", "audio", "auto"):
            if declared.startswith(known):
                return known
        return "unknown"
    types = media_types if media_types is not None else moment_media_types(moment)
    known = [item for item in types if item in ("photo", "video", "audio")]
    if len(known) == 1:
        return known[0]
    if len(known) > 1:
        return "mixed"
    return "unknown"


def extract_moment_rows(payload: Any) -> list[dict[str, Any]]:
    """兼容 data(list) / data.moments / data.items / 嵌套 data.data 四种响应形态。"""
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    for _ in range(3):
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            for key in ("moments", "items"):
                rows = data.get(key)
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)]
            data = data.get("data")
            continue
        break
    raise ValueError(f"Looki moments 响应格式无效：顶层类型 {type(data).__name__}")


def parse_moment(moment: Any, zone: ZoneInfo) -> tuple[dict[str, Any] | None, str]:
    """解析单个 moment。成功返回 (记录, "")，失败返回 (None, 可诊断原因)。"""
    if not isinstance(moment, dict) or not moment.get("id"):
        return None, "缺少 id"
    start_raw = moment.get("start_time")
    end_raw = moment.get("end_time")
    if not start_raw or not end_raw:
        return None, "缺少起止时间"
    try:
        start = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None, "起止时间格式无效"
    if start.tzinfo is None:
        start = start.replace(tzinfo=zone)
    if end.tzinfo is None:
        end = end.replace(tzinfo=zone)
    # 统一换算到插件配置时区：否则 UTC 偏移会让 day 归属和“22 点后活动”判断出错
    start = start.astimezone(zone)
    end = end.astimezone(zone)
    try:
        day = date.fromisoformat(str(moment.get("date") or "").strip()).isoformat()
    except ValueError:
        day = start.date().isoformat()
    media_types = moment_media_types(moment)
    return {
        "looki_id": str(moment["id"]),
        "date": day,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "title": str(moment.get("title") or "").strip(),
        "description": str(moment.get("description") or "").strip(),
        "audio_summary": str(moment.get("audio_summary") or "").strip(),
        "keywords": [k.strip() for k in str(moment.get("search_context") or "").split(",") if k.strip()][:30],
        "media_types": media_types,
        "capture_mode": capture_mode_of(moment, media_types),
        "raw_json": moment,
    }, ""


class LookiScheduleProvider:
    def __init__(self, base_url: str, api_key: str, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    async def _fetch_day(self, client: httpx.AsyncClient, day: date) -> list[dict[str, Any]]:
        response = None
        for attempt in range(2):
            try:
                response = await client.get(
                    f"{self.base_url}/moments",
                    params={"on_date": day.isoformat()},
                    headers={"x-api-key": self.api_key},
                )
                response.raise_for_status()
                break
            except (httpx.ConnectError, httpx.TimeoutException):
                if attempt:
                    raise
                await asyncio.sleep(2)
        if response is None:
            raise ValueError("Looki moments 请求未返回响应")
        return extract_moment_rows(response.json())

    async def fetch(self, start_day: date, end_day: date, timezone: str) -> list[dict[str, Any]]:
        zone = ZoneInfo(timezone)
        result = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            current = start_day
            while current <= end_day:
                try:
                    rows = await self._fetch_day(client, current)
                except Exception as exc:
                    # 单日失败只记录日志并跳过，不中断整个同步窗口（P1-4）。
                    logger.warning("[Looki] %s 同步失败，已跳过该日期：%s", current.isoformat(), exc)
                    current += timedelta(days=1)
                    continue
                skipped = 0
                for moment in rows:
                    parsed, reason = parse_moment(moment, zone)
                    if parsed is None:
                        skipped += 1
                        logger.info("[Looki] 跳过无法解析的活动 %s：%s", str(moment.get("id") or "?")[:16], reason)
                        continue
                    result.append(parsed)
                logger.info("[Looki] %s 拉取 %d 条，解析成功 %d 条，跳过 %d 条", current.isoformat(), len(rows), len(rows) - skipped, skipped)
                current += timedelta(days=1)
        return result


@dataclass
class QueryScheduleTool(FunctionTool[AstrAgentContext]):
    name: str = "query_looki_schedule"
    description: str = ("按日期查阅本地日程事实，返回记录 id、时间段、标题、关键词、Looki 原始描述、音频摘要、媒体类型和对话校正。日期按插件配置时区理解。用户询问细节、具体日期或活动时用本工具。若 corrections 与原始描述冲突，以最新的对话校正为准，原始描述仅作线索。audio_summary 是 Looki 生成的音频摘要文本（可能来自录音或自动场景理解），不是原始录音；media_evidence 中 summary 类型的 text_excerpt 同理。关键词可能不同于用户用词：若返回 keyword_matched=false，说明关键词未直接命中，items 只是候选记录，须如实说明。结果是事实材料，不是回复台词。")
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "start_date": {"type": "string", "description": "起始日期 YYYY-MM-DD"},
            "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD；单日查询时与起始日期相同"},
            "query": {"type": "string", "description": "可选关键词或提示，同时匹配标题和关键词"},
            "limit": {"type": "integer", "description": "最多返回条数，1-50", "default": 20},
        },
        "required": ["start_date", "end_date"],
    })

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        plugin = _get_plugin(context.context.context)
        if not plugin:
            return "没有日程记录。"
        event = context.context.event
        # 私聊维持开放；群聊由原有总开关或新增成员/群白名单独立放行。
        if not _has_schedule_access(plugin, event):
            return "当前群聊未开放日程查询，请私聊查询，或让管理员开启群聊查询、配置白名单。"
        keyword = str(kwargs.get("query") or "").strip()
        try:
            items, matched = await asyncio.to_thread(plugin.store.search, kwargs["start_date"], kwargs["end_date"], keyword, kwargs.get("limit", 20))
        except (ValueError, TypeError, KeyError):
            return "查询日期或参数无效。"
        if not items:
            return "该日期范围内没有记录。"
        looki_ids = [item["looki_id"] for item in items if item.get("looki_id")]
        evidence = await asyncio.to_thread(plugin.store.media_evidence, looki_ids)
        payload = {
            "keyword_matched": matched,
            "items": [
                {
                    **{k: item[k] for k in ("id", "date", "start_at", "end_at", "title", "keywords", "description", "audio_summary", "corrections", "source")},
                    "media_types": item.get("media_types") or [],
                    "capture_mode": item.get("capture_mode") or "unknown",
                    "media_evidence": [
                        {k: row[k] for k in ("media_type", "mime_type", "duration_ms", "has_thumbnail", "availability", "text_excerpt")}
                        for row in evidence.get(item.get("looki_id"), [])[:5]
                    ],
                }
                for item in items
            ],
        }
        if keyword and not matched:
            payload["notice"] = f"关键词「{keyword}」未直接匹配任何记录的标题或关键词；以上为该日期范围内的全部候选记录，不得当作确定事实，需要时请向用户确认。"
        return json.dumps(payload, ensure_ascii=False)


@dataclass
class SendLookiImageTool(FunctionTool[AstrAgentContext]):
    name: str = "send_looki_image"
    description: str = ("查询并发送 Looki 已记录的历史照片，不是让 Bot 现场拍摄；用户索要 Looki 照片时，不得以不在现场或无法亲自拍摄为由拒绝。由多模态模型挑选 1 张后发送原图或图生图结果。支持两种调用方式：用户提供明确日期和起止时间时，传 date、start_time、end_time，优先查询该时段，候选不足时可补充同一日期范围内的相邻活动；用户只说“之前一起玩 Switch 的照片”或“上午在教室学习的照片”这类活动、场景回忆而没有准确起止时间时，只传 query 关键词，由插件从本地日程定位最近一次匹配，不得编造日期或起止时间。候选图只供模型内部挑选，最终只发送 1 张图。style_adjustment 仅用于本次风格调整。")
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "明确时段模式的图片日期 YYYY-MM-DD，须与起止时间一起提供"},
            "start_time": {"type": "string", "description": "明确时段模式的开始时间 HH:MM 或 HH:MM:SS"},
            "end_time": {"type": "string", "description": "明确时段模式的结束时间 HH:MM 或 HH:MM:SS，必须晚于开始时间"},
            "query": {"type": "string", "description": "活动回忆模式的关键词，例如 Switch；未提供明确日期和起止时间时使用"},
            "preference": {"type": "string", "description": "可选选图偏好，例如偏好人像、食物或清晰全景"},
            "style_adjustment": {"type": "string", "description": "可选的本次图生图风格调整；原图模式下不生效"},
        },
    })

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        plugin = _get_plugin(context.context.context)
        if not plugin:
            return "日程插件当前不可用。"
        event = context.context.event
        if event is None:
            return "当前对话无法发送图片。"
        if not _has_schedule_access(plugin, event):
            return "当前群聊未开放日程图片查询，请私聊使用，或让管理员配置白名单。"
        day = str(kwargs.get("date") or "").strip()
        start_time = str(kwargs.get("start_time") or "").strip()
        end_time = str(kwargs.get("end_time") or "").strip()
        query = str(kwargs.get("query") or "").strip()
        preference = str(kwargs.get("preference") or "").strip()
        style_adjustment = str(kwargs.get("style_adjustment") or "").strip()
        time_fields = (day, start_time, end_time)
        try:
            if all(time_fields):
                return await plugin.image_service.send_period_image(event, day, start_time, end_time, preference, style_adjustment)
            if any(time_fields):
                return "明确时段找图需要同时提供 date、start_time 和 end_time；不要猜测缺失时间。"
            if not query:
                return "请提供完整日期和起止时间，或提供 query 活动关键词。"
            return await plugin.image_service.send_activity_image(event, query, preference, style_adjustment)
        except (ValueError, TypeError, KeyError) as exc:
            return f"图片查询或生成参数无效：{exc}"
        except Exception as exc:
            logger.exception("[%s] 时段图片处理失败", PLUGIN_NAME)
            return f"图片获取、挑选或生成失败：{str(exc)[:300]}"


@dataclass
class AddScheduleCorrectionTool(FunctionTool[AstrAgentContext]):
    name: str = "add_looki_correction"
    description: str = ("为一条日程追加对话校正。当对话表明 Looki 原始描述中的地点、活动或其他事实有误，或出现了可以补充确认的新事实时，可根据对话自行判断并记录，不必等待用户明确说“纠正”。先用 query_looki_schedule 找到对应日程并取得 id；无法确定唯一记录时不要调用。不得修改原始 Looki 内容，不得凭空猜测。note 用简洁中文记录校正后的事实及必要反差，例如“实际地点是教室，不是宿舍”。校正会保留历史，后续查询以最新校正为准。")
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "schedule_id": {"type": "string", "description": "query_looki_schedule 返回的日程 id"},
            "note": {"type": "string", "description": "本次对话校正注记，最多 2000 字"},
        },
        "required": ["schedule_id", "note"],
    })

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = context.context.event
        if event is None or not event.is_admin():
            return "只有 AstrBot 管理员可以添加对话校正。"
        plugin = _get_plugin(context.context.context)
        if not plugin:
            return "日程插件当前不可用。"
        try:
            correction = await asyncio.to_thread(plugin.store.add_correction, str(kwargs.get("schedule_id") or "").strip(), str(kwargs.get("note") or ""))
        except (ValueError, TypeError) as exc:
            return str(exc)
        return json.dumps({"saved": True, "correction": correction}, ensure_ascii=False)


@register(PLUGIN_NAME, AUTHOR, "Looki 日程同步、日历编辑、对话校正、白名单查阅，以及历史照片挑选和图生图。", VERSION)
class LookiCompanionPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.config = config or {}
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        self.store = ScheduleStore(data_dir / "schedules.sqlite3")
        self._sync_lock = asyncio.Lock()
        self._sync_task: asyncio.Task | None = None
        self._stopping = False
        base_url = str(self.config.get("looki_base_url") or DEFAULT_LOOKI_BASE_URL).strip()
        api_key = str(self.config.get("looki_api_key") or "").strip()
        self.schedule_provider = LookiScheduleProvider(base_url, api_key, int(self.config.get("request_timeout_seconds", 20))) if api_key else None
        self.image_service = LookiImageService(self, data_dir)
        self.context.add_llm_tools(QueryScheduleTool(), SendLookiImageTool(), AddScheduleCorrectionTool())
        routes = [
            ("/" + PLUGIN_NAME + "/events", self.api_events, ["GET"]),
            ("/" + PLUGIN_NAME + "/density", self.api_density, ["GET"]),
            ("/" + PLUGIN_NAME + "/events", self.api_create, ["POST"]),
            ("/" + PLUGIN_NAME + "/events/<item_id>/update", self.api_update, ["POST"]),
            ("/" + PLUGIN_NAME + "/events/<item_id>/delete", self.api_delete, ["POST"]),
            ("/" + PLUGIN_NAME + "/sync", self.api_sync, ["POST"]),
            ("/" + PLUGIN_NAME + "/initial-import", self.api_initial_import, ["POST"]),
            ("/" + PLUGIN_NAME + "/status", self.api_status, ["GET"]),
            ("/" + PLUGIN_NAME + "/config", self.api_get_config, ["GET"]),
            ("/" + PLUGIN_NAME + "/config", self.api_set_config, ["POST"]),
            ("/" + PLUGIN_NAME + "/media", self.api_media, ["GET"]),
            ("/" + PLUGIN_NAME + "/media-summary", self.api_media_summary, ["GET"]),
        ]
        for route, handler, methods in routes:
            self.context.register_web_api(route, handler, methods, "Looki schedule management")

    async def initialize(self):
        if self.config.get("auto_sync", True):
            self._stopping = False
            self._sync_task = asyncio.create_task(self._sync_loop(), name="looki_sync_loop")
            self._sync_task.add_done_callback(self._on_sync_task_done)

    def _on_sync_task_done(self, task: asyncio.Task):
        """同步任务结束时记录原因，便于定位定时同步停摆。"""
        if task.cancelled():
            logger.warning("[%s] 同步任务被取消，自动同步已停止", PLUGIN_NAME)
            return
        exc = task.exception()
        if exc is not None:
            logger.error("[%s] 同步任务意外退出: %s", PLUGIN_NAME, str(exc) or type(exc).__name__)
            return
        logger.warning("[%s] 同步任务已正常结束，stopping=%s", PLUGIN_NAME, self._stopping)

    async def terminate(self):
        self._stopping = True
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(self.store.close)

    async def _sync_media_evidence(self, provider: Any, today: date, days: int) -> None:
        """抓取最近窗口内尚未抓过的少量事件的媒体证据；任何失败都不影响主同步。"""
        if not bool(self.config.get("enable_media_evidence", True)):
            return
        try:
            limit = max(1, min(int(self.config.get("media_evidence_max_moments", 6)), 30))
            start = (today - timedelta(days=days - 1)).isoformat()
            pending = await asyncio.to_thread(self.store.pending_media_moments, start, today.isoformat(), limit)
            for row in pending:
                looki_id = row["looki_id"]
                entries = await fetch_moment_evidence(provider, looki_id, 2)
                if entries is None:
                    continue
                summary = summary_entry(looki_id, row.get("audio_summary", ""))
                if summary:
                    entries = entries + [summary]
                saved = await asyncio.to_thread(self.store.sync_media_evidence, looki_id, entries)
                logger.info("[Looki] 媒体证据 %s：%d 条", looki_id[:16], saved)
        except Exception as exc:
            logger.warning("[%s] 媒体证据同步失败（不影响日程同步）：%s", PLUGIN_NAME, str(exc) or type(exc).__name__)

    async def _sync_loop(self):
        while not self._stopping:
            try:
                await self.sync_once(automatic=True)
                interval = max(1, int(self.config.get("sync_interval_minutes", 5))) * 60
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 任何意外异常都不能让同步任务退出，60 秒后重试
                logger.warning("[%s] 同步循环异常: %s", PLUGIN_NAME, str(exc) or type(exc).__name__)
                await asyncio.sleep(60)

    def _initial_import_required(self, meta: dict[str, str]) -> bool:
        has_key = bool(str(self.config.get("looki_api_key") or "").strip())
        return has_key and not meta.get("last_success") and not meta.get(INITIAL_IMPORT_META_KEY)

    async def _initial_import_state(self):
        meta = await asyncio.to_thread(self.store.meta)
        return {
            "initial_import_required": self._initial_import_required(meta),
            "initial_import_choice": meta.get(INITIAL_IMPORT_META_KEY, ""),
        }

    async def sync_once(self, automatic: bool = False):
        if self._sync_lock.locked():
            return {"skipped": True}
        async with self._sync_lock:
            if automatic:
                meta = await asyncio.to_thread(self.store.meta)
                if self._initial_import_required(meta):
                    return {"skipped": True, "initial_import_required": True}
            now = datetime.now().astimezone().isoformat()
            await asyncio.to_thread(self.store.set_meta, "last_attempt", now)
            provider = getattr(self, "schedule_provider", None)
            if provider is None:
                message = "未配置 Looki API Key，未发起远端请求。"
                await asyncio.to_thread(self.store.set_meta, "last_error", message)
                return {"error": message}
            try:
                days = max(1, min(int(self.config.get("backfill_days", 7)), 366))
                timezone = str(self.config.get("timezone", "Asia/Shanghai"))
                today = datetime.now(ZoneInfo(timezone)).date()
                items = await provider.fetch(today - timedelta(days=days - 1), today, timezone)
                inserted = await asyncio.to_thread(self.store.upsert_remote, items, bool(self.config.get("store_raw_json", False)))
                await self._sync_media_evidence(provider, today, days)
                synced = datetime.now().astimezone().isoformat()
                await asyncio.to_thread(self.store.set_meta, "last_success", synced)
                await asyncio.to_thread(self.store.set_meta, "last_error", "")
                meta = await asyncio.to_thread(self.store.meta)
                if not meta.get(INITIAL_IMPORT_META_KEY):
                    await asyncio.to_thread(self.store.set_meta, INITIAL_IMPORT_META_KEY, "imported")
                if items:
                    newest = max(str(item.get("start", "")) for item in items)
                    await asyncio.to_thread(self.store.set_meta, "latest_event", newest)
                return {"inserted": inserted}
            except Exception as exc:
                detail = str(exc) or type(exc).__name__
                logger.warning("[%s] 日程同步失败: %s", PLUGIN_NAME, detail)
                await asyncio.to_thread(self.store.set_meta, "last_error", detail[:500])
                return {"error": detail}

    @filter.on_llm_request()
    async def add_schedule_clues(self, event: AstrMessageEvent, req: ProviderRequest):
        if not event.is_admin():
            return
        try:
            from astrbot.core.agent.message import TextPart
        except ImportError:
            return
        guidance = (
            "[Looki 图片工具规则]\n"
            "用户索要 Looki 已记录的照片，或描述活动、场景、时段来要图时，应调用 send_looki_image 查询并发送历史照片，"
            "不要以不在现场或无法亲自拍摄为由拒绝。"
            "只有活动或场景描述而没有准确起止时间时，只传 query；有明确日期和起止时间时才传 date、start_time、end_time，不得猜测。"
        )
        part = TextPart(text=guidance)
        if hasattr(part, "mark_as_temp"):
            part = part.mark_as_temp()
        req.extra_user_content_parts.append(part)
        if str(self.config.get("clue_scope") or CLUE_SCOPE_ADMIN).strip() != CLUE_SCOPE_ADMIN:
            return
        try:
            timezone = str(self.config.get("timezone", "Asia/Shanghai"))
            clues = await asyncio.to_thread(self.store.clues, int(self.config.get("clue_days", 7)), int(self.config.get("max_clues", 3)), timezone)
        except Exception:
            logger.exception("[%s] 近期线索读取失败", PLUGIN_NAME)
            return
        if clues:
            today = datetime.now(ZoneInfo(timezone)).date().isoformat()
            text = f"[近期生活线索（今天 {today}，时区 {timezone}），仅作背景参考，不要逐条复述]\n" + "\n".join(clues) + "\n具体日期和活动请在需要时查询日程。"
            part = TextPart(text=text)
            if hasattr(part, "mark_as_temp"):
                part = part.mark_as_temp()
            req.extra_user_content_parts.append(part)

    async def api_events(self):
        try:
            start, end = request.query.get("start"), request.query.get("end")
            items = await asyncio.to_thread(self.store.query, start, end, "", 100)
            return json_response(items)
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)

    async def api_density(self):
        try:
            year, month = int(request.query.get("year", 0)), int(request.query.get("month", 0))
            if not 1 <= month <= 12 or not 1970 <= year <= 2999:
                raise ValueError("年月无效")
            return json_response(await asyncio.to_thread(self.store.density, year, month))
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)

    async def api_create(self):
        payload = await request.json(default={})
        try:
            if not isinstance(payload, dict):
                raise TypeError("日程数据格式无效")
            _localize_event(payload, str(self.config.get("timezone", "Asia/Shanghai")))
            item_id = await asyncio.to_thread(self.store.save, payload)
            return json_response({"id": item_id})
        except (ValueError, KeyError, TypeError) as exc:
            return error_response(str(exc), status_code=400)

    async def api_update(self, item_id: str):
        payload = await request.json(default={})
        try:
            if not isinstance(payload, dict):
                raise TypeError("日程数据格式无效")
            _localize_event(payload, str(self.config.get("timezone", "Asia/Shanghai")))
            saved_id = await asyncio.to_thread(self.store.save, payload, item_id)
            return json_response({"id": saved_id})
        except (ValueError, KeyError, TypeError) as exc:
            return error_response(str(exc), status_code=400)

    async def api_delete(self, item_id: str):
        try:
            await asyncio.to_thread(self.store.delete, item_id)
            return json_response({"deleted": True})
        except ValueError as exc:
            return error_response(str(exc), status_code=404)

    async def api_media(self):
        """页面用：查事件媒体证据。只返回类型/尺寸/时长/摘要，不返回任何签名 URL。"""
        try:
            looki_ids = [part for part in str(request.query.get("looki_ids") or "").split(",") if part.strip()][:50]
            start, end = request.query.get("start"), request.query.get("end")
            if not looki_ids:
                if not start or not end:
                    raise ValueError("需要 looki_ids 或 start/end")
                items = await asyncio.to_thread(self.store.query, start, end, "", 100)
                looki_ids = [item["looki_id"] for item in items if item.get("looki_id")]
            return json_response(await asyncio.to_thread(self.store.media_evidence, looki_ids))
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)

    async def api_media_summary(self):
        try:
            start, end = request.query.get("start"), request.query.get("end")
            if not start or not end:
                raise ValueError("需要 start 与 end")
            return json_response(await asyncio.to_thread(self.store.media_summary, start, end))
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)

    async def api_sync(self):
        return json_response(await self.sync_once())

    async def api_initial_import(self):
        payload = await request.json(default={})
        choice = str(payload.get("choice") or "") if isinstance(payload, dict) else ""
        if choice not in ("import", "skip"):
            return error_response("首次导入选择无效", status_code=400)
        state = await self._initial_import_state()
        if not state["initial_import_required"]:
            return json_response({**state, "completed": True})
        if choice == "skip":
            # 与正在进行的同步串行，避免“暂不导入”和确认导入同时写入不同选择。
            async with self._sync_lock:
                state = await self._initial_import_state()
                if state["initial_import_required"]:
                    await asyncio.to_thread(self.store.set_meta, INITIAL_IMPORT_META_KEY, "skipped")
                    state = await self._initial_import_state()
            return json_response({**state, "saved": True})
        result = await self.sync_once()
        state = await self._initial_import_state()
        return json_response({**result, **state})

    async def api_status(self):
        meta = await asyncio.to_thread(self.store.meta)
        return json_response({
            **meta,
            "automatic": bool(self.config.get("auto_sync", True)),
            "interval_minutes": int(self.config.get("sync_interval_minutes", 5)),
            "backfill_days": max(1, min(int(self.config.get("backfill_days", 7)), 366)),
            "timezone": str(self.config.get("timezone", "Asia/Shanghai")),
            "initial_import_required": self._initial_import_required(meta),
            "initial_import_choice": meta.get(INITIAL_IMPORT_META_KEY, ""),
            "delay_notice": "同步频率只代表插件请求 Looki 的频率；Looki 尚未生成的数据仍不可用。",
        })

    _MASK = "••••••"

    @staticmethod
    def _mask_key(key: str) -> str:
        return f"{key[:5]}••••{key[-4:]}" if len(key) > 12 else "••••••"

    def _image_provider_ids(self) -> list[str]:
        provider_ids = []
        for provider in self.context.get_all_providers():
            config = getattr(provider, "provider_config", {}) or {}
            provider_id = str(config.get("id") or "").strip()
            modalities = config.get("modalities")
            if isinstance(modalities, (list, tuple, set)) and modalities and "image" not in modalities:
                continue
            if provider_id:
                provider_ids.append(provider_id)
        return provider_ids

    async def api_get_config(self):
        def str_field(name, default=""):
            return str(self.config.get(name, default) or default)
        key = str_field("looki_api_key")
        image_key = str_field("image_api_key")
        reveal = request.query.get("reveal") == "1"
        return json_response({
            "looki_api_key": key if reveal else self._mask_key(key),
            "looki_api_key_masked": self._mask_key(key),
            "has_key": bool(key),
            "looki_base_url": str_field("looki_base_url", DEFAULT_LOOKI_BASE_URL),
            "auto_sync": bool(self.config.get("auto_sync", True)),
            "sync_interval_minutes": int(self.config.get("sync_interval_minutes", 5)),
            "backfill_days": int(self.config.get("backfill_days", 7)),
            "timezone": str_field("timezone", "Asia/Shanghai"),
            "request_timeout_seconds": int(self.config.get("request_timeout_seconds", 20)),
            "clue_scope": str(self.config.get("clue_scope") or CLUE_SCOPE_ADMIN),
            "allow_group_query": bool(self.config.get("allow_group_query", False)),
            "store_raw_json": bool(self.config.get("store_raw_json", False)),
            "clue_days": int(self.config.get("clue_days", 7)),
            "max_clues": int(self.config.get("max_clues", 3)),
            "whitelist_user_ids": str_field("whitelist_user_ids"),
            "whitelist_group_ids": str_field("whitelist_group_ids"),
            "image_candidate_count": int(self.config.get("image_candidate_count", 6)),
            "image_send_original": bool(self.config.get("image_send_original", False)),
            "image_style_prompt": str_field("image_style_prompt", DEFAULT_ANIME_PROMPT),
            "image_selection_provider_id": str_field("image_selection_provider_id"),
            "image_generation_mode": str_field("image_generation_mode", "multimodal"),
            "image_generation_provider_id": str_field("image_generation_provider_id"),
            "image_api_base_url": str_field("image_api_base_url"),
            "image_api_model": str_field("image_api_model"),
            "image_api_key": image_key if reveal else self._mask_key(image_key),
            "image_api_key_masked": self._mask_key(image_key),
            "has_image_api_key": bool(image_key),
            "image_provider_ids": self._image_provider_ids(),
            **(await self._initial_import_state()),
        })

    async def api_set_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("配置格式无效", status_code=400)
        updates: dict[str, Any] = {}
        try:
            if "looki_api_key" in payload:
                key = str(payload["looki_api_key"]).strip()
                if "•" not in key:
                    updates["looki_api_key"] = key
            if "image_api_key" in payload:
                key = str(payload["image_api_key"]).strip()
                if "•" not in key:
                    updates["image_api_key"] = key
            if "looki_base_url" in payload:
                url = str(payload["looki_base_url"]).strip().rstrip("/")
                if url and not url.startswith("https://") and not url.startswith("http://"):
                    return error_response("接口地址必须以 http(s):// 开头", status_code=400)
                updates["looki_base_url"] = url
            if "image_api_base_url" in payload:
                url = str(payload["image_api_base_url"]).strip().rstrip("/")
                if url and not url.startswith(("https://", "http://")):
                    return error_response("图生图接口地址必须以 http(s):// 开头", status_code=400)
                updates["image_api_base_url"] = url
            if "timezone" in payload:
                ZoneInfo(str(payload["timezone"]).strip())
                updates["timezone"] = str(payload["timezone"]).strip()
            for name, low, high in (("sync_interval_minutes", 1, 1440), ("backfill_days", 1, 366), ("request_timeout_seconds", 5, 120), ("clue_days", 1, 31), ("max_clues", 0, 10), ("image_candidate_count", 1, 9)):
                if name in payload:
                    value = int(payload[name])
                    if not low <= value <= high:
                        raise ValueError(f"{name} 需在 {low}-{high} 之间")
                    updates[name] = value
            if "clue_scope" in payload:
                scope = str(payload["clue_scope"]).strip()
                if scope not in CLUE_SCOPES:
                    raise ValueError(f"clue_scope 只能是 {' / '.join(CLUE_SCOPES)}")
                updates["clue_scope"] = scope
            if "image_generation_mode" in payload:
                mode = str(payload["image_generation_mode"]).strip()
                if mode not in ("multimodal", "openai_compatible"):
                    raise ValueError("image_generation_mode 配置无效")
                updates["image_generation_mode"] = mode
            for name in ("image_selection_provider_id", "image_generation_provider_id", "image_api_model", "image_style_prompt"):
                if name in payload:
                    updates[name] = str(payload[name]).strip()
            for name in ("whitelist_user_ids", "whitelist_group_ids"):
                if name in payload:
                    updates[name] = "\n".join(sorted(_config_ids(payload[name])))
            for name in ("auto_sync", "allow_group_query", "store_raw_json", "image_send_original"):
                if name in payload:
                    if not isinstance(payload[name], bool):
                        raise ValueError(f"{name} 必须是布尔值")
                    updates[name] = payload[name]
            merged = dict(self.config)
            merged.update(updates)
            provider_ids = self._image_provider_ids()
            selection_provider = str(merged.get("image_selection_provider_id") or "").strip()
            if selection_provider and selection_provider not in provider_ids:
                raise ValueError("挑图模型未加载或不支持图像输入")
            if not bool(merged.get("image_send_original", False)):
                mode = str(merged.get("image_generation_mode") or "multimodal")
                if mode == "multimodal":
                    generation_provider = str(merged.get("image_generation_provider_id") or "").strip()
                    if generation_provider and generation_provider not in provider_ids:
                        raise ValueError("图生图模型未加载或不支持图像输入")
                elif not str(merged.get("image_api_base_url") or "").strip() or not str(merged.get("image_api_model") or "").strip():
                    raise ValueError("兼容图生图接口必须填写接口地址和模型名称")
        except (ValueError, TypeError, KeyError) as exc:
            return error_response(f"配置校验失败: {exc}", status_code=400)
        if not updates:
            return error_response("没有可保存的配置项", status_code=400)
        previous_interval = int(self.config.get("sync_interval_minutes", 5))
        previous_auto_sync = bool(self.config.get("auto_sync", True))
        self.config.update(updates)
        save_config = getattr(self.config, "save_config", None)
        if save_config:
            await asyncio.to_thread(save_config)
        base_url = str(self.config.get("looki_base_url") or DEFAULT_LOOKI_BASE_URL).strip()
        api_key = str(self.config.get("looki_api_key") or "").strip()
        timeout = int(self.config.get("request_timeout_seconds", 20))
        self.schedule_provider = LookiScheduleProvider(base_url, api_key, timeout) if api_key else None
        want_sync = bool(self.config.get("auto_sync", True))
        interval = int(self.config.get("sync_interval_minutes", 5))
        restart_sync = interval != previous_interval or want_sync != previous_auto_sync
        if self._sync_task and not self._sync_task.done() and (not want_sync or restart_sync):
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
            self._sync_task = None
        if want_sync and (self._sync_task is None or self._sync_task.done()):
            self._stopping = False
            self._sync_task = asyncio.create_task(self._sync_loop())
        elif not want_sync:
            self._sync_task = None
        return json_response({
            "saved": sorted(updates),
            "note": "配置已保存并即时生效。",
            **(await self._initial_import_state()),
        })
