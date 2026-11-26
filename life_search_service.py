from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

_CJK = "\u4e00-\u9fff"
_SPLIT_RE = re.compile(rf"[^0-9a-zA-Z{_CJK}]+|[的了是和与给在]")
_PIECE_RE = re.compile(rf"[0-9a-z]+|[{_CJK}]+")

# 长词在前，避免“上上周”被“上周”截断。
_REMOVAL_WORDS = (
    "帮我找一下", "帮我找找", "帮我看看", "帮我找", "帮忙", "麻烦你", "麻烦",
    "请帮我", "请", "给我发一下", "发给我", "给我发", "发我", "给我看看",
    "看一下", "看一眼", "看看", "找一下", "找找", "查一下", "查查", "翻一下",
    "回忆一下", "回忆", "当时", "那时候", "那一次", "那次", "上次", "之前", "以前",
    "最近一次", "最近", "那天", "当天", "大前天", "前天", "昨天", "昨日", "今天", "今日",
    "上上周", "上周", "上个星期", "上星期", "本周", "这周", "这个星期", "本星期",
    "上个月", "上月", "本月", "这个月", "照片", "相片", "图片", "视频", "录像", "影像",
)
_MEDIA_WORDS = ("照片", "相片", "图片", "视频", "录像", "影像")
_DATE_PATTERNS = (
    re.compile(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日号]?"),
    re.compile(r"\d{1,2}月\d{1,2}[日号]?"),
    re.compile(r"\d{1,3}\s*天前"),
)
_HOLIDAYS = {
    "圣诞节": (12, 25),
    "平安夜": (12, 24),
    "元旦": (1, 1),
    "情人节": (2, 14),
    "万圣节": (10, 31),
}

TITLE_WEIGHT = 3.0
CONFIDENCE_FLOOR = 0.35


def normalize(value: Any) -> str:
    """检索用归一化：只保留字母与数字并转小写，中文按原字保留。"""
    return "".join(c for c in str(value or "").casefold() if c.isalnum())


def _day_text(day: str) -> str:
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        return day
    return f"{parsed.isoformat()} 周{'一二三四五六日'[parsed.weekday()]}"


@dataclass(frozen=True)
class DateHint:
    start: date
    end: date
    strict: bool
    label: str


def parse_date_hint(text: str, today: date) -> DateHint | None:
    """从自然语言里解析日期范围。strict=True 表示范围由用户明确指定，必须严格过滤。"""
    value = str(text or "")

    match = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?", value)
    if match:
        try:
            day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
        return DateHint(day, day, True, day.isoformat())

    match = re.search(r"(\d{1,2})月(\d{1,2})[日号]?", value)
    if match:
        month, day_number = int(match.group(1)), int(match.group(2))
        try:
            day = date(today.year, month, day_number)
            if day > today:
                day = date(today.year - 1, month, day_number)
        except ValueError:
            return None
        return DateHint(day, day, True, day.isoformat())

    for word, delta in (("大前天", 3), ("前天", 2), ("昨天", 1), ("昨日", 1), ("今天", 0), ("今日", 0)):
        if word in value:
            day = today - timedelta(days=delta)
            return DateHint(day, day, True, word)

    match = re.search(r"(\d{1,3})\s*天前", value)
    if match:
        day = today - timedelta(days=int(match.group(1)))
        return DateHint(day, day, True, f"{int(match.group(1))}天前")

    monday = today - timedelta(days=today.weekday())
    if "上上周" in value:
        start = monday - timedelta(days=14)
        return DateHint(start, start + timedelta(days=6), True, "上上周")
    if any(word in value for word in ("上周", "上个星期", "上星期")):
        return DateHint(monday - timedelta(days=7), monday - timedelta(days=1), True, "上周")
    if any(word in value for word in ("本周", "这周", "这个星期", "本星期")):
        return DateHint(monday, today, True, "本周")

    if any(word in value for word in ("上个月", "上月")):
        first = date(today.year, today.month, 1)
        end = first - timedelta(days=1)
        return DateHint(date(end.year, end.month, 1), end, True, "上个月")
    if any(word in value for word in ("本月", "这个月")):
        return DateHint(date(today.year, today.month, 1), today, True, "本月")

    for word, (month, day_number) in _HOLIDAYS.items():
        if word in value:
            day = date(today.year, month, day_number)
            if day > today:
                day = date(today.year - 1, month, day_number)
            # 节日字本身也是检索词，日期只作偏好，不强制过滤。
            return DateHint(day, day, False, word)
    return None


def extract_terms(query: str) -> list[str]:
    """把查询拆成可子串匹配的检索词。

    中文不分词：短片段整体作检索词，长片段拆成相邻二字组合，
    这样“上次圣诞节学校送的糖”能同时命中“圣诞节”“学校”“糖”。
    """
    cleaned = str(query or "")
    for word in _REMOVAL_WORDS:
        cleaned = cleaned.replace(word, " ")
    for pattern in _DATE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    terms: list[str] = []
    for chunk in _SPLIT_RE.split(cleaned):
        if not chunk:
            continue
        for piece in _PIECE_RE.findall(chunk.casefold()):
            if piece.isascii():
                if len(piece) >= 2:
                    terms.append(piece)
                continue
            if len(piece) <= 3:
                terms.append(piece)
            else:
                terms.extend(piece[index:index + 2] for index in range(len(piece) - 1))
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        if term not in seen:
            seen.add(term)
            result.append(term)
    return result


def _score(terms: list[str], fields: list[tuple[str, float, str]]) -> tuple[float, list[str]]:
    """按字段权重累加命中分数，并记录命中的字段名。"""
    total = 0.0
    matched: list[str] = []
    for term in terms:
        weights = sorted((weight for _, weight, text in fields if term in text), reverse=True)
        if not weights:
            continue
        total += weights[0] + min(1.0, 0.5 * (len(weights) - 1))
        for name, _, text in fields:
            if term in text and name not in matched:
                matched.append(name)
    return total, matched


def _journal_fields(journal: dict[str, Any], factor: float) -> list[tuple[str, float, str]]:
    tasks = journal.get("tasks") or []
    return [
        ("journal_title", 2.0 * factor, normalize(journal.get("title"))),
        ("journal_description", 1.5 * factor, normalize(journal.get("description"))),
        ("journal_content", 1.5 * factor, normalize(journal.get("content"))),
        ("journal_tasks", 1.5 * factor, normalize(" ".join(str(task.get("body") or "") for task in tasks))),
    ]


def _snippet(journal: dict[str, Any], terms: list[str]) -> str:
    for value in (journal.get("content"), journal.get("description"), journal.get("title")):
        text = str(value or "")
        lowered = text.casefold()
        for term in terms:
            index = lowered.find(term)
            if index >= 0:
                start = max(0, index - 20)
                return text[start:index + 40].replace("\n", " ").strip()
    return ""


def _clarify(candidates: list[dict[str, Any]], prefix: str = "找到多个可能的记录") -> str:
    parts = [f"{_day_text(item['day'])}「{item['title']}」" for item in candidates[:3]]
    return f"{prefix}：" + "、".join(parts) + "。你指的是哪一个？"


class LifeSearchService:
    """跨日程、日志、媒体与任务的统一只读检索层；只返回证据与候选，不发送任何消息。"""

    def __init__(self, store: Any, timezone: str = "Asia/Shanghai", window_days: int = 1095, task_expire_days: int = 3):
        self.store = store
        self.timezone = timezone
        self.window_days = max(30, int(window_days))
        self.task_expire_days = max(0, int(task_expire_days))

    def search(
        self,
        query: str,
        now: datetime | None = None,
        start_day: str | None = None,
        end_day: str | None = None,
    ) -> dict[str, Any]:
        """统一检索入口。传入 start_day/end_day 时按明确日期范围严格过滤。"""
        query = str(query or "").strip()
        if len(query) > 200:
            raise ValueError("查询词无效")
        today = (now or datetime.now(ZoneInfo(self.timezone))).date()
        if start_day and end_day:
            start = date.fromisoformat(start_day)
            end = date.fromisoformat(end_day)
            if end < start:
                raise ValueError("结束日期早于开始日期")
            label = start.isoformat() if start == end else f"{start.isoformat()}~{end.isoformat()}"
            hint = DateHint(start, end, True, label)
        else:
            hint = parse_date_hint(query, today)
        media_intent = "photo" if any(word in query for word in _MEDIA_WORDS) else None
        terms = extract_terms(query)
        result: dict[str, Any] = {
            "query": query,
            "terms": terms,
            "media_intent": media_intent,
            "date_hint": None,
            "candidates": [],
            "journal_evidence": [],
            "needs_clarification": False,
            "clarification": None,
        }
        if hint:
            result["date_hint"] = {
                "start": hint.start.isoformat(),
                "end": hint.end.isoformat(),
                "strict": hint.strict,
                "label": hint.label,
            }
        if not terms and hint is None:
            result["needs_clarification"] = True
            result["clarification"] = "想找哪一天、或者哪件事的记录？"
            return result

        start_day = hint.start if hint and hint.strict else today - timedelta(days=self.window_days)
        end_day = hint.end if hint and hint.strict else today
        schedules = self.store.life_schedules(start_day.isoformat(), end_day.isoformat())
        journals: list[dict[str, Any]] = []
        cursor = start_day
        # 存储层单次日志查询上限 366 天，这里按年切块，避免放宽既有接口约束。
        while cursor <= end_day:
            chunk_end = min(cursor + timedelta(days=365), end_day)
            journals.extend(self.store.journals(
                cursor.isoformat(), chunk_end.isoformat(),
                today=today, expire_days=self.task_expire_days,
            ))
            cursor = chunk_end + timedelta(days=1)

        by_schedule: dict[str, list[dict[str, Any]]] = {}
        by_day: dict[str, list[dict[str, Any]]] = {}
        matched_journals: list[dict[str, Any]] = []
        for journal in journals:
            fields = _journal_fields(journal, 1.0)
            score, matched = _score(terms, fields) if terms else (0.0, [])
            if score > 0:
                matched_journals.append({**journal, "_score": score, "_fields": matched})
            if journal.get("schedule_id"):
                by_schedule.setdefault(journal["schedule_id"], []).append(journal)
            else:
                by_day.setdefault(journal["day"], []).append(journal)

        candidates: list[dict[str, Any]] = []
        for item in schedules:
            fields = [
                ("title", TITLE_WEIGHT, normalize(item.get("title"))),
                ("keywords", 2.5, normalize(" ".join(item.get("keywords") or []))),
                ("description", 2.0, normalize(item.get("description"))),
                ("audio_summary", 2.0, normalize(item.get("audio_summary"))),
                ("corrections", 2.5, normalize(" ".join(str(c.get("note") or "") for c in item.get("corrections") or []))),
            ]
            for journal in by_schedule.get(item["id"], []):
                fields.extend(_journal_fields(journal, 1.0))
            for journal in by_day.get(item["date"], []):
                fields.extend(_journal_fields(journal, 0.6))
            if terms:
                score, matched_fields = _score(terms, fields)
                if score <= 0:
                    continue
                coverage = score / (TITLE_WEIGHT * len(terms))
            else:
                score, matched_fields, coverage = 1.0, [], 0.0
            if hint and not hint.strict and hint.start <= date.fromisoformat(item["date"]) <= hint.end:
                score *= 1.2
                coverage = min(1.0, coverage * 1.2)
            candidates.append({
                "schedule_id": item["id"],
                "looki_id": item.get("looki_id") or "",
                "day": item["date"],
                "start_at": item.get("start_at") or "",
                "end_at": item.get("end_at") or "",
                "title": item.get("title") or "",
                "source": item.get("source") or "",
                "score": round(score, 2),
                "coverage": round(coverage, 2),
                "matched_fields": matched_fields,
                "media_types": [],
                "has_photos": False,
                "is_looki": bool(item.get("looki_id")),
                "_media_types": list(item.get("media_types") or []),
                "_journal_ids": [journal["id"] for journal in by_schedule.get(item["id"], []) + by_day.get(item["date"], [])],
            })

        candidates.sort(key=lambda entry: (entry["coverage"], entry["score"], entry["day"], entry["start_at"]), reverse=True)
        candidates = candidates[:10]
        for entry in candidates:
            types = entry.pop("_media_types")
            entry["journal_ids"] = entry.pop("_journal_ids")
            entry["media_types"] = types
            entry["has_photos"] = "photo" in types

        result["journal_evidence"] = [
            {
                "journal_id": journal["id"],
                "day": journal["day"],
                "type": journal.get("type") or "",
                "title": journal.get("title") or "",
                "snippet": _snippet(journal, terms),
                "schedule_id": journal.get("schedule_id") or "",
                "matched_fields": journal["_fields"],
            }
            for journal in sorted(matched_journals, key=lambda row: (row["_score"], row["day"]), reverse=True)[:10]
        ]
        result["candidates"] = candidates

        if not candidates:
            return result
        if len(candidates) == 1:
            only = candidates[0]
            if terms and only["coverage"] < CONFIDENCE_FLOOR:
                result["needs_clarification"] = True
                result["clarification"] = f"我找到一条可能相关的记录：{_day_text(only['day'])}「{only['title']}」，是这一次吗？"
            return result
        top, second = candidates[0], candidates[1]
        if top["coverage"] >= CONFIDENCE_FLOOR and second["coverage"] < top["coverage"] * 0.75:
            return result
        result["needs_clarification"] = True
        result["clarification"] = _clarify(candidates)
        return result
