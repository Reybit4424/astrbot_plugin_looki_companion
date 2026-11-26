from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

# 只有按时间生成的音频摘要与日记能确定归属；昨日回顾、漫画页、视频日志等没有可靠时间锚点，
# 一律视为未匹配，放到当天时间轴底部，不猜测。
MATCHABLE_TYPES = ("AUDIO_SUMMARY", "DIARY")

# 支持常见 Markdown checkbox 变体：`- [ ]`、`* [ ]`、`+ [ ]`，以及 `[x]`/`[✓]` 已完成状态。
_PENDING_CHECKBOX_RE = re.compile(r"^\s*[-*+]\s+\[([ xX✓✔]?)\]\s*(.*)$")
_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.*)$")
_TASK_BOX_RE = re.compile(r"^\s*[-*+]\s*\[([xX✓✔]?)\]\s*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_RE = re.compile(r"(\*\*|__|`)")


def parse_journal(journal: Any, zone: ZoneInfo) -> dict[str, Any] | None:
    """把 Looki journal 响应行规整为插件内部结构；无法定位日期时返回 None。"""
    if not isinstance(journal, dict) or not journal.get("id"):
        return None
    recorded_at = ""
    recorded = None
    raw_recorded = journal.get("recorded_at")
    if isinstance(raw_recorded, str) and raw_recorded.strip():
        try:
            recorded = datetime.fromisoformat(raw_recorded.strip().replace("Z", "+00:00"))
            if recorded.tzinfo is None:
                recorded = recorded.replace(tzinfo=zone)
            recorded = recorded.astimezone(zone)
            recorded_at = recorded.isoformat()
        except ValueError:
            recorded = None
    day = ""
    for key in ("date", "start_date"):
        try:
            day = date.fromisoformat(str(journal.get(key) or "").strip()).isoformat()
            break
        except ValueError:
            continue
    if not day and recorded is not None:
        day = recorded.date().isoformat()
    if not day:
        return None
    media_items = journal.get("media_items")
    # 日记、漫画页、视频日志没有正文，只有一句话简介，正文为空时退化为简介。
    raw_content = str(journal.get("content") or "").strip() or str(journal.get("description") or "")
    content_lines = raw_content.splitlines()
    while content_lines and not content_lines[0].strip():
        content_lines.pop(0)
    # 正文里的 Markdown 标题由页面渲染并按标题分块，这里不再剥离开头的一级标题。
    return {
        "id": str(journal["id"]),
        "day": day,
        "type": str(journal.get("type") or "").strip().upper(),
        "title": str(journal.get("title") or "").strip(),
        "description": str(journal.get("description") or "").strip(),
        "content": "\n".join(content_lines).strip(),
        "recorded_at": recorded_at,
        "media_count": len(media_items) if isinstance(media_items, list) else 0,
    }


def match_schedule(recorded_at: str, items: list[dict[str, Any]]) -> str | None:
    """按录音时间戳找到包含它的最具体时间段；没有任何时间段包含时返回 None。"""
    if not recorded_at:
        return None
    try:
        moment = datetime.fromisoformat(recorded_at)
    except ValueError:
        return None
    best: tuple[float, str] | None = None
    for item in items:
        try:
            start = datetime.fromisoformat(item["start_at"])
            end = datetime.fromisoformat(item["end_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if start > moment or end < moment:
            continue
        span = (end - start).total_seconds()
        if best is None or span < best[0]:
            best = (span, item["id"])
    return best[1] if best else None


def _clean_task_text(value: str) -> str:
    text = _LINK_RE.sub(r"\1", value)
    text = _BOLD_RE.sub("", text)
    return text.strip().strip("-").strip()


def _body_key(value: str) -> str:
    return "".join(c.casefold() for c in value if c.isalnum())


def extract_journal_tasks(content: Any) -> list[dict[str, Any]]:
    """纯规则提取任务：识别 `- [ ]`、`* [ ]`、`+ [ ]` 等 checkbox 及其完成状态。

    编号列表与普通列表仍视为日志正文，不进入任务。
    """
    tasks: list[dict[str, Any]] = []
    section = ""
    for raw in str(content or "").splitlines():
        line = raw.rstrip()
        heading = _HEADING_RE.match(line)
        if heading:
            section = heading.group(2).strip()
            continue
        checkbox = _PENDING_CHECKBOX_RE.match(line)
        if not checkbox:
            continue
        body = _clean_task_text(checkbox.group(2))
        if body:
            tasks.append({
                "body": body,
                "section": section,
                "source_line": line.strip(),
                "done": bool(checkbox.group(1).strip()),
            })
    # 同一篇日志内按正文去重，避免重复提醒同一条任务
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for task in tasks:
        key = _body_key(task["body"])
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(task)
    return unique


def task_key(journal_id: str, body: str) -> str:
    return f"{journal_id}:{_body_key(body)}"
