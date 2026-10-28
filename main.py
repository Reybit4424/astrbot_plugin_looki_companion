from __future__ import annotations

import asyncio
import json
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

from .schedule_store import ScheduleStore

PLUGIN_NAME = "astrbot_plugin_looki_companion"
AUTHOR = "瑞贝特"
VERSION = "0.5.1"
DEFAULT_LOOKI_BASE_URL = "https://open.looki.tech/api/v1"


class LookiScheduleProvider:
    def __init__(self, base_url: str, api_key: str, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    async def fetch(self, start_day: date, end_day: date, timezone: str) -> list[dict[str, Any]]:
        zone = ZoneInfo(timezone)
        result = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            current = start_day
            while current <= end_day:
                response = await client.get(
                    f"{self.base_url}/moments",
                    params={"on_date": current.isoformat()},
                    headers={"x-api-key": self.api_key},
                )
                response.raise_for_status()
                payload = response.json()
                data = payload.get("data", payload) if isinstance(payload, dict) else payload
                rows = data.get("moments", data.get("items", [])) if isinstance(data, dict) else data
                if not isinstance(rows, list):
                    raise ValueError("Looki moments 响应格式无效")
                for moment in rows:
                    if not isinstance(moment, dict) or not moment.get("id"):
                        continue
                    start_raw = moment.get("start_time")
                    end_raw = moment.get("end_time")
                    if not start_raw or not end_raw:
                        continue
                    start = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
                    end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
                    if start.tzinfo is None:
                        start = start.replace(tzinfo=zone)
                    if end.tzinfo is None:
                        end = end.replace(tzinfo=zone)
                    result.append({
                        "looki_id": str(moment["id"]),
                        "date": str(moment.get("date") or current.isoformat()),
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "title": str(moment.get("title") or "").strip(),
                        "description": str(moment.get("description") or "").strip(),
                        "audio_summary": str(moment.get("audio_summary") or "").strip(),
                        "keywords": [k.strip() for k in str(moment.get("search_context") or "").split(",") if k.strip()][:30],
                        "raw_json": moment,
                    })
                current += timedelta(days=1)
        return result


@dataclass
class QueryScheduleTool(FunctionTool[AstrAgentContext]):
    name: str = "query_looki_schedule"
    description: str = ("按日期查阅本地日程事实，返回时间段、标题、关键词，以及 Looki 自动生成的画面描述。日期按插件配置时区理解。近期背景线索只含标题关键词；用户询问细节、具体日期或活动时用本工具。关键词可能不同于用户用词：若返回 keyword_matched=false，说明关键词未直接命中，items 只是该日期范围内的候选记录，须如实告知用户并不得当作确定事实。结果是事实材料，不是回复台词。")
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
        plugin = context.context.context.get_registered_star(PLUGIN_NAME)
        if not plugin:
            return "没有日程记录。"
        event = context.context.event
        # 查询权限不再做 owner 限制：私聊开放，群聊由 allow_group_query 开关控制，
        # 谁能触发由 AstrBot 管理面板「扩展 → 组件」中本工具的权限（管理员/成员）决定。
        if event is not None and not event.is_private_chat() and not plugin.config.get("allow_group_query", False):
            return "群聊中未开启日程查询，请私聊查询，或让管理员在插件设置中开启「在群聊中开放查询」。"
        keyword = str(kwargs.get("query") or "").strip()
        try:
            items, matched = await asyncio.to_thread(plugin.store.search, kwargs["start_date"], kwargs["end_date"], keyword, kwargs.get("limit", 20))
        except (ValueError, TypeError, KeyError):
            return "查询日期或参数无效。"
        if not items:
            return "该日期范围内没有记录。"
        payload = {
            "keyword_matched": matched,
            "items": [{k: item[k] for k in ("date", "start_at", "end_at", "title", "keywords", "description", "source")} for item in items],
        }
        if keyword and not matched:
            payload["notice"] = f"关键词「{keyword}」未直接匹配任何记录的标题或关键词；以上为该日期范围内的全部候选记录，不得当作确定事实，需要时请向用户确认。"
        return json.dumps(payload, ensure_ascii=False)


@register(PLUGIN_NAME, AUTHOR, "Looki 日程同步、管理与 Bot 查阅，支持本地持久化、日历编辑和近期生活线索。", VERSION)
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
        self.context.add_llm_tools(QueryScheduleTool())
        routes = [
            ("/" + PLUGIN_NAME + "/events", self.api_events, ["GET"]),
            ("/" + PLUGIN_NAME + "/density", self.api_density, ["GET"]),
            ("/" + PLUGIN_NAME + "/events", self.api_create, ["POST"]),
            ("/" + PLUGIN_NAME + "/events/<item_id>/update", self.api_update, ["POST"]),
            ("/" + PLUGIN_NAME + "/events/<item_id>/delete", self.api_delete, ["POST"]),
            ("/" + PLUGIN_NAME + "/sync", self.api_sync, ["POST"]),
            ("/" + PLUGIN_NAME + "/status", self.api_status, ["GET"]),
            ("/" + PLUGIN_NAME + "/config", self.api_get_config, ["GET"]),
            ("/" + PLUGIN_NAME + "/config", self.api_set_config, ["POST"]),
        ]
        for route, handler, methods in routes:
            self.context.register_web_api(route, handler, methods, "Looki schedule management")

    async def initialize(self):
        if self.config.get("auto_sync", True):
            self._sync_task = asyncio.create_task(self._sync_loop())

    async def terminate(self):
        self._stopping = True
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(self.store.close)

    async def _sync_loop(self):
        await self.sync_once()
        while not self._stopping:
            try:
                interval = max(60, int(self.config.get("sync_interval_minutes", 5))) * 60
                await asyncio.sleep(interval)
                await self.sync_once()
            except asyncio.CancelledError:
                raise

    async def sync_once(self):
        if self._sync_lock.locked():
            return {"skipped": True}
        async with self._sync_lock:
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
                await asyncio.to_thread(self.store.upsert_remote, items, bool(self.config.get("store_raw_json", False)))
                synced = datetime.now().astimezone().isoformat()
                await asyncio.to_thread(self.store.set_meta, "last_success", synced)
                await asyncio.to_thread(self.store.set_meta, "last_error", "")
                if items:
                    newest = max(str(item.get("start", "")) for item in items)
                    await asyncio.to_thread(self.store.set_meta, "latest_event", newest)
                return {"inserted": len(items)}
            except Exception as exc:
                logger.warning("[%s] 日程同步失败: %s", PLUGIN_NAME, exc)
                await asyncio.to_thread(self.store.set_meta, "last_error", str(exc)[:500])
                return {"error": str(exc)}

    def _is_owner(self, sender_id: Any) -> bool:
        owner_id = str(self.config.get("owner_id", "")).strip()
        return bool(owner_id) and str(sender_id) == owner_id

    @filter.on_llm_request()
    async def add_schedule_clues(self, event: AstrMessageEvent, req: ProviderRequest):
        if not event.is_private_chat() or not self._is_owner(event.get_sender_id()) or not self.config.get("inject_clues", True):
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
            try:
                from astrbot.core.agent.message import TextPart
                part = TextPart(text=text)
                if hasattr(part, "mark_as_temp"):
                    part = part.mark_as_temp()
                req.extra_user_content_parts.append(part)
            except ImportError:
                return

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
            item_id = await asyncio.to_thread(self.store.save, payload)
            return json_response({"id": item_id})
        except (ValueError, KeyError, TypeError) as exc:
            return error_response(str(exc), status_code=400)

    async def api_update(self, item_id: str):
        payload = await request.json(default={})
        try:
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

    async def api_sync(self):
        return json_response(await self.sync_once())

    async def api_status(self):
        meta = await asyncio.to_thread(self.store.meta)
        return json_response({**meta, "automatic": bool(self.config.get("auto_sync", True)), "interval_minutes": int(self.config.get("sync_interval_minutes", 5)), "delay_notice": "同步频率只代表插件请求 Looki 的频率；Looki 尚未生成的数据仍不可用。"})

    _MASK = "••••••"

    @staticmethod
    def _mask_key(key: str) -> str:
        return f"{key[:5]}••••{key[-4:]}" if len(key) > 12 else "••••••"

    async def api_get_config(self):
        def str_field(name, default=""):
            return str(self.config.get(name, default) or default)
        key = str_field("looki_api_key")
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
            "inject_clues": bool(self.config.get("inject_clues", True)),
            "allow_group_query": bool(self.config.get("allow_group_query", False)),
            "store_raw_json": bool(self.config.get("store_raw_json", False)),
            "clue_days": int(self.config.get("clue_days", 7)),
            "max_clues": int(self.config.get("max_clues", 3)),
            "owner_id": str_field("owner_id"),
        })

    async def api_set_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("配置格式无效", status_code=400)
        updates: dict[str, Any] = {}
        try:
            if "looki_api_key" in payload:
                key = str(payload["looki_api_key"]).strip()
                if key and "•" not in key:
                    updates["looki_api_key"] = key
            if "looki_base_url" in payload:
                url = str(payload["looki_base_url"]).strip().rstrip("/")
                if url and not url.startswith("https://") and not url.startswith("http://"):
                    return error_response("接口地址必须以 http(s):// 开头", status_code=400)
                updates["looki_base_url"] = url
            if "timezone" in payload:
                ZoneInfo(str(payload["timezone"]).strip())
                updates["timezone"] = str(payload["timezone"]).strip()
            for name, low, high in (("sync_interval_minutes", 1, 1440), ("backfill_days", 1, 366), ("request_timeout_seconds", 5, 120), ("clue_days", 1, 31), ("max_clues", 0, 10)):
                if name in payload:
                    value = int(payload[name])
                    if not low <= value <= high:
                        raise ValueError(f"{name} 需在 {low}-{high} 之间")
                    updates[name] = value
            if "owner_id" in payload:
                updates["owner_id"] = str(payload["owner_id"]).strip()
            for name in ("auto_sync", "inject_clues", "allow_group_query", "store_raw_json"):
                if name in payload:
                    updates[name] = bool(payload[name])
        except (ValueError, TypeError, KeyError) as exc:
            return error_response(f"配置校验失败: {exc}", status_code=400)
        if not updates:
            return error_response("没有可保存的配置项", status_code=400)
        self.config.update(updates)
        save_config = getattr(self.config, "save_config", None)
        if save_config:
            await asyncio.to_thread(save_config)
        base_url = str(self.config.get("looki_base_url") or DEFAULT_LOOKI_BASE_URL).strip()
        api_key = str(self.config.get("looki_api_key") or "").strip()
        timeout = int(self.config.get("request_timeout_seconds", 20))
        self.schedule_provider = LookiScheduleProvider(base_url, api_key, timeout) if api_key else None
        want_sync = bool(self.config.get("auto_sync", True))
        if want_sync and (self._sync_task is None or self._sync_task.done()):
            self._stopping = False
            self._sync_task = asyncio.create_task(self._sync_loop())
        elif not want_sync and self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
            self._sync_task = None
        return json_response({"saved": sorted(updates), "note": "配置已保存并即时生效。"})
