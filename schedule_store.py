from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 3


def _iso(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed.isoformat()


def _key(item: dict[str, Any]) -> str:
    raw_id = item.get("looki_id")
    if raw_id:
        return f"looki:{raw_id}"
    return "composite:" + "|".join(str(item.get(k) or "").strip().casefold() for k in ("date", "start", "end", "title"))


class ScheduleStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY, dedupe_key TEXT UNIQUE NOT NULL,
                looki_id TEXT, day TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
                title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                audio_summary TEXT NOT NULL DEFAULT '',
                keywords TEXT NOT NULL, source TEXT NOT NULL,
                user_edited INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS schedule_corrections (
                id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL,
                note TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_schedules_day ON schedules(day);
            CREATE INDEX IF NOT EXISTS idx_schedules_looki ON schedules(looki_id);
            CREATE INDEX IF NOT EXISTS idx_corrections_schedule ON schedule_corrections(schedule_id, created_at);
        """)
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(schedules)")}
        if "description" not in columns:
            self.db.execute("ALTER TABLE schedules ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        if "audio_summary" not in columns:
            self.db.execute("ALTER TABLE schedules ADD COLUMN audio_summary TEXT NOT NULL DEFAULT ''")
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.commit()

    def close(self):
        self.db.close()

    def _row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["date"] = item.pop("day")
        item["keywords"] = json.loads(item["keywords"])
        item["user_edited"] = bool(item["user_edited"])
        item["deleted"] = bool(item["deleted"])
        item.pop("dedupe_key", None)
        item.pop("raw_json", None)
        return item

    def query(self, start_day: str, end_day: str, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        start = date.fromisoformat(start_day)
        end = date.fromisoformat(end_day)
        if end < start or (end - start).days > 366:
            raise ValueError("日期范围无效或超过 366 天")
        limit = max(1, min(int(limit), 100))
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("查询词无效")
        rows = self.db.execute("SELECT * FROM schedules WHERE day BETWEEN ? AND ? AND deleted=0 ORDER BY start_at LIMIT 500", (start_day, end_day)).fetchall()
        normalize = lambda value: "".join(c.casefold() for c in value if c.isalnum())
        terms = normalize(query)
        if terms:
            rows = [r for r in rows if terms in normalize(r["title"] + " " + " ".join(json.loads(r["keywords"]))) ]
        items = [self._row(r) for r in rows[:limit]]
        for item in items:
            item["corrections"] = []
        if items:
            by_id = {item["id"]: item for item in items}
            placeholders = ",".join("?" for _ in by_id)
            corrections = self.db.execute(f"SELECT id,schedule_id,note,created_at FROM schedule_corrections WHERE schedule_id IN ({placeholders}) ORDER BY created_at", tuple(by_id)).fetchall()
            for correction in corrections:
                by_id[correction["schedule_id"]]["corrections"].append({"id": correction["id"], "note": correction["note"], "created_at": correction["created_at"]})
        return items

    def search(self, start_day: str, end_day: str, query: str = "", limit: int = 50) -> tuple[list[dict[str, Any]], bool]:
        """按关键词检索；未命中时退化为该范围内的全部记录，第二个返回值表示关键词是否直接命中。"""
        keyword = (query or "").strip()
        items = self.query(start_day, end_day, keyword, limit)
        if items or not keyword:
            return items, bool(keyword)
        return self.query(start_day, end_day, "", limit), False

    def density(self, year: int, month: int) -> dict[str, int]:
        if not 1 <= int(month) <= 12:
            raise ValueError("月份无效")
        start = date(int(year), int(month), 1)
        end = date(start.year + (start.month == 12), start.month % 12 + 1, 1) - timedelta(days=1)
        rows = self.db.execute("SELECT day, COUNT(*) AS c FROM schedules WHERE day BETWEEN ? AND ? AND deleted=0 GROUP BY day", (start.isoformat(), end.isoformat())).fetchall()
        return {r["day"]: r["c"] for r in rows}

    def upsert_remote(self, items: list[dict[str, Any]], store_raw: bool = False) -> int:
        now = datetime.now().astimezone().isoformat()
        inserted = 0
        with self.db:
            for item in items:
                normalized = {**item, "start": _iso(item["start"]), "end": _iso(item["end"])}
                if datetime.fromisoformat(normalized["end"]) <= datetime.fromisoformat(normalized["start"]):
                    raise ValueError("结束时间必须晚于开始时间")
                normalized["date"] = date.fromisoformat(normalized["date"]).isoformat()
                if not isinstance(normalized.get("title"), str) or not normalized["title"].strip():
                    raise ValueError("远端日程缺少标题")
                if not isinstance(normalized.get("keywords", []), list):
                    raise ValueError("远端关键词格式无效")
                dedupe = _key(normalized)
                existing = self.db.execute("SELECT * FROM schedules WHERE dedupe_key=?", (dedupe,)).fetchone()
                keywords = json.dumps(normalized.get("keywords", []), ensure_ascii=False)
                description = str(normalized.get("description") or "").strip()
                audio = str(normalized.get("audio_summary") or "").strip()
                raw = json.dumps(item, ensure_ascii=False) if store_raw else None
                if existing:
                    if not existing["user_edited"] and existing["source"] == "looki" and not existing["deleted"]:
                        self.db.execute("UPDATE schedules SET day=?,start_at=?,end_at=?,title=?,description=?,audio_summary=?,keywords=?,raw_json=?,updated_at=? WHERE id=?", (normalized["date"], normalized["start"], normalized["end"], normalized["title"], description, audio, keywords, raw, now, existing["id"]))
                    continue
                self.db.execute("INSERT INTO schedules (id,dedupe_key,looki_id,day,start_at,end_at,title,description,audio_summary,keywords,source,user_edited,deleted,raw_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?)", (str(uuid.uuid4()), dedupe, normalized.get("looki_id"), normalized["date"], normalized["start"], normalized["end"], normalized["title"], description, audio, keywords, "looki", raw, now, now))
                inserted += 1
        return inserted

    def save(self, item: dict[str, Any], item_id: str | None = None) -> str:
        day = date.fromisoformat(item["date"]).isoformat()
        start, end = _iso(item["start"]), _iso(item["end"])
        if datetime.fromisoformat(end) <= datetime.fromisoformat(start):
            raise ValueError("结束时间必须晚于开始时间")
        title = str(item["title"]).strip()
        if not title or len(title) > 200:
            raise ValueError("标题不能为空且不能超过 200 字")
        keywords = item.get("keywords", [])
        if not isinstance(keywords, list) or len(keywords) > 30 or any(not isinstance(k, str) or len(k) > 60 for k in keywords):
            raise ValueError("关键词格式无效")
        description = str(item.get("description") or "").strip()
        if len(description) > 20000:
            raise ValueError("描述过长")
        now = datetime.now().astimezone().isoformat()
        with self.db:
            if item_id:
                cur = self.db.execute("UPDATE schedules SET day=?,start_at=?,end_at=?,title=?,description=?,keywords=?,user_edited=1,deleted=0,updated_at=? WHERE id=?", (day, start, end, title, description, json.dumps(keywords, ensure_ascii=False), now, item_id))
                if not cur.rowcount:
                    raise ValueError("记录不存在")
                return item_id
            new_id = str(uuid.uuid4())
            self.db.execute("INSERT INTO schedules (id,dedupe_key,looki_id,day,start_at,end_at,title,description,keywords,source,user_edited,deleted,raw_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,0,NULL,?,?)", (new_id, f"manual:{new_id}", None, day, start, end, title, description, json.dumps(keywords, ensure_ascii=False), "manual", now, now))
            return new_id

    def add_correction(self, item_id: str, note: str) -> dict[str, str]:
        note = str(note).strip()
        if not note or len(note) > 2000:
            raise ValueError("校正注记不能为空且不能超过 2000 字")
        now = datetime.now().astimezone().isoformat()
        correction_id = str(uuid.uuid4())
        with self.db:
            schedule = self.db.execute("SELECT title,day FROM schedules WHERE id=? AND deleted=0", (item_id,)).fetchone()
            if not schedule:
                raise ValueError("记录不存在或已删除")
            self.db.execute("INSERT INTO schedule_corrections (id,schedule_id,note,created_at) VALUES (?,?,?,?)", (correction_id, item_id, note, now))
        return {"id": correction_id, "schedule_id": item_id, "note": note, "created_at": now, "title": schedule["title"], "date": schedule["day"]}

    def delete(self, item_id: str):
        with self.db:
            cur = self.db.execute("UPDATE schedules SET deleted=1,user_edited=1,updated_at=? WHERE id=? AND deleted=0", (datetime.now().astimezone().isoformat(), item_id))
            if not cur.rowcount:
                raise ValueError("记录不存在或已删除")

    def set_meta(self, key: str, value: str):
        with self.db:
            self.db.execute("INSERT INTO sync_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def meta(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT key,value FROM sync_meta"))

    def clues(self, days: int = 7, max_items: int = 3, timezone: str = "Asia/Shanghai") -> list[str]:
        end = datetime.now(ZoneInfo(timezone)).date()
        start = end - timedelta(days=max(1, min(days, 31)) - 1)
        rows = self.db.execute("SELECT day,title,keywords,end_at FROM schedules WHERE day BETWEEN ? AND ? AND deleted=0 ORDER BY day DESC,start_at DESC", (start.isoformat(), end.isoformat())).fetchall()
        late_days = {r["day"] for r in rows if datetime.fromisoformat(r["end_at"]).hour >= 22}
        limit = max(0, min(max_items, 10))
        result = []
        correction_rows = self.db.execute("SELECT s.day,s.title,c.note FROM schedule_corrections c JOIN schedules s ON s.id=c.schedule_id WHERE s.day BETWEEN ? AND ? AND s.deleted=0 ORDER BY c.created_at DESC LIMIT ?", (start.isoformat(), end.isoformat(), limit)).fetchall()
        result.extend(f"{r['day']}《{r['title']}》对话校正：{r['note']}" for r in correction_rows)
        if len(late_days) >= 3:
            result.append(f"回看的日程中，有{len(late_days)}天记录到结束时间在22点或之后的活动；这只说明晚间仍有活动记录，不代表睡眠情况。")
        counts: dict[str, int] = {}
        for row in rows:
            for word in json.loads(row["keywords"]):
                counts[word] = counts.get(word, 0) + 1
        result.extend(f"近期日程关键词多次出现：{word}（{count}次）。" for word, count in sorted(counts.items(), key=lambda x: (-x[1], x[0])) if count >= 2)
        return result[:limit]
