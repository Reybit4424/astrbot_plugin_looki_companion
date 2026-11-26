from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .life_search_service import LifeSearchService

# 指代词只用于承接上一轮已确认的活动，不做新的猜测。
REFERENTIAL_WORDS = ("这个", "那个", "这条", "那条", "它", "刚才那个", "刚刚那个", "上面那个", "就这个", "就那个", "这次", "那次")
CONTEXT_TTL_SECONDS = 1800


def _context_key(event: Any) -> str:
    if event is None:
        return ""
    try:
        return f"{event.get_group_id() or 'private'}:{event.get_sender_id() or ''}"
    except Exception:
        return ""


def activity_period(activity: dict[str, Any], timezone: str) -> tuple[datetime, datetime]:
    """把候选活动换算到插件配置时区，供找图使用。"""
    zone = ZoneInfo(timezone)
    start = datetime.fromisoformat(activity["start_at"]).astimezone(zone)
    end = datetime.fromisoformat(activity["end_at"]).astimezone(zone)
    return start, end


class LookiIntentService:
    """把一句自然语言变成确定的检索结果：唯一活动、澄清问题或明确的无结果。"""

    def __init__(self, store: Any, timezone: str = "Asia/Shanghai", window_days: int = 1095, task_expire_days: int = 3):
        self.timezone = timezone
        self.search = LifeSearchService(store, timezone, window_days, task_expire_days)
        self._recent: dict[str, dict[str, Any]] = {}

    def remember(self, event: Any, activity: dict[str, Any]) -> None:
        key = _context_key(event)
        if not key:
            return
        self._recent[key] = {"activity": activity, "at": time.monotonic()}

    def recent(self, event: Any) -> dict[str, Any] | None:
        entry = self._recent.get(_context_key(event))
        if not entry or time.monotonic() - entry["at"] > CONTEXT_TTL_SECONDS:
            return None
        return entry["activity"]

    def resolve(self, query: str, event: Any = None, now: datetime | None = None) -> dict[str, Any]:
        """返回 status=ok / clarify / not_found / manual / no_media / missing。"""
        query = str(query or "").strip()
        referential = not query or any(word in query for word in REFERENTIAL_WORDS)
        explicit_query = query
        for word in REFERENTIAL_WORDS:
            explicit_query = explicit_query.replace(word, "")
        has_explicit_conditions = bool(explicit_query.strip(" ，,。.！？!?：:；;"))
        if referential and not has_explicit_conditions:
            activity = self.recent(event)
            if activity:
                return {"status": "ok", "activity": activity, "from_context": True, "reply": "", "result": None}
            if not query:
                return {"status": "missing", "activity": None, "from_context": False, "reply": "想找哪一天、或者哪件事的照片？", "result": None}

        result = self.search.search(query, now)
        candidates = result["candidates"]
        if result["needs_clarification"]:
            return {
                "status": "clarify",
                "activity": None,
                "from_context": False,
                "reply": result["clarification"] or "找到多条可能的记录，请补充日期或更具体的关键词。",
                "result": result,
            }
        if not candidates:
            reply = f"没有找到与「{query}」相关的日程，请补充大概日期或其他关键词。"
            if result["journal_evidence"]:
                reply = f"只在 Looki 日志里看到与「{query}」相关的记录，但没有对应日程，无法确定是哪一次。"
            return {"status": "not_found", "activity": None, "from_context": False, "reply": reply, "result": result}
        top = candidates[0]
        if not top["looki_id"]:
            return {
                "status": "manual",
                "activity": top,
                "from_context": False,
                "reply": f"找到 {top['day']}「{top['title']}」，但这是人工日程，没有 Looki 照片。",
                "result": result,
            }
        if "photo" not in top["media_types"] and "video" not in top["media_types"]:
            return {
                "status": "no_media",
                "activity": top,
                "from_context": False,
                "reply": f"{top['day']}「{top['title']}」没有可发送的照片或视频。",
                "result": result,
            }
        self.remember(event, top)
        return {"status": "ok", "activity": top, "from_context": False, "reply": "", "result": result}
