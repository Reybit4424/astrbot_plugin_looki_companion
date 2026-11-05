from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 3


def _iso(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed.isoformat()


def _instant(value: str) -> str:
    """绝对时刻（UTC），用于跨时区偏移比较同一时间点。"""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed.astimezone(timezone.utc).isoformat()


def _title_key(title: Any) -> str:
    return "".join(c.casefold() for c in str(title or "") if c.isalnum())


def _content_key(day: str, start: str, title: Any) -> str:
    """内容签名：Looki 换 id 重新下发同一活动时，用它识别“同一条日程”。"""
    return "content:" + "|".join((date.fromisoformat(day).isoformat(), _instant(start), _title_key(title)))


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
        terms = "".join(c.casefold() for c in query if c.isalnum())
        sql = "SELECT * FROM schedules WHERE day BETWEEN ? AND ? AND deleted=0 ORDER BY start_at"
        if not terms:
            rows = self.db.execute(sql + " LIMIT 500", (start_day, end_day)).fetchall()
        else:
            # 关键词过滤在 Python 侧完成，用游标流式读取并在凑够 limit 时提前结束，避免整段加载进内存
            normalize = lambda value: "".join(c.casefold() for c in value if c.isalnum())
            rows = []
            for row in self.db.execute(sql, (start_day, end_day)):
                if terms in normalize(row["title"] + " " + " ".join(json.loads(row["keywords"]))):
                    rows.append(row)
                    if len(rows) >= limit:
                        break
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

    def find_latest_looki_activity(self, query: str) -> dict[str, Any] | None:
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("查询词无效")
        normalize = lambda value: "".join(c.casefold() for c in value if c.isalnum())
        terms = normalize(query)
        if not terms:
            raise ValueError("查询词无效")
        rows = self.db.execute("SELECT * FROM schedules WHERE deleted=0 AND looki_id IS NOT NULL AND looki_id != '' ORDER BY start_at DESC").fetchall()
        correction_rows = self.db.execute("SELECT schedule_id,note FROM schedule_corrections ORDER BY created_at").fetchall()
        corrections: dict[str, list[str]] = {}
        for correction in correction_rows:
            corrections.setdefault(correction["schedule_id"], []).append(correction["note"])
        for row in rows:
            fields = [row["title"], row["description"], row["audio_summary"], *json.loads(row["keywords"]), *corrections.get(row["id"], [])]
            if terms in normalize(" ".join(fields)):
                return self._row(row)
        return None

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
                if not isinstance(item, dict):
                    continue
                try:
                    normalized = {**item, "start": _iso(item["start"]), "end": _iso(item["end"])}
                    normalized["date"] = date.fromisoformat(normalized["date"]).isoformat()
                    start_at = datetime.fromisoformat(normalized["start"])
                    end_at = datetime.fromisoformat(normalized["end"])
                except (KeyError, TypeError, ValueError):
                    continue
                if end_at <= start_at:
                    # Looki 会返回 start==end 的单点记录：补 1 秒保留；真正倒置的记录跳过，不拖垮整批同步
                    if end_at == start_at:
                        normalized["end"] = (start_at + timedelta(seconds=1)).isoformat()
                    else:
                        continue
                if not isinstance(normalized.get("title"), str) or not normalized["title"].strip():
                    continue
                if not isinstance(normalized.get("keywords", []), list):
                    continue
                dedupe = _key(normalized)
                keywords = json.dumps(normalized.get("keywords", []), ensure_ascii=False)
                description = str(normalized.get("description") or "").strip()
                audio = str(normalized.get("audio_summary") or "").strip()
                raw = json.dumps(item, ensure_ascii=False) if store_raw else None
                existing = self.db.execute("SELECT * FROM schedules WHERE dedupe_key=?", (dedupe,)).fetchone()
                if existing is None:
                    # Looki 可能对同一活动换 id 重新下发：按内容签名兜底匹配，避免重复插入
                    existing = self._content_match(normalized["date"], normalized["start"], normalized["title"])
                    if existing is not None:
                        # 认同一条日程后迁移远端标识，保证下次按 looki_id 直接命中
                        self.db.execute("UPDATE schedules SET looki_id=?,dedupe_key=?,updated_at=? WHERE id=?", (normalized.get("looki_id"), dedupe, now, existing["id"]))
                        existing = self.db.execute("SELECT * FROM schedules WHERE id=?", (existing["id"],)).fetchone()
                if existing is not None:
                    if not existing["user_edited"] and existing["source"] == "looki" and not existing["deleted"]:
                        self.db.execute("UPDATE schedules SET day=?,start_at=?,end_at=?,title=?,description=?,audio_summary=?,keywords=?,raw_json=?,updated_at=? WHERE id=?", (normalized["date"], normalized["start"], normalized["end"], normalized["title"], description, audio, keywords, raw if store_raw else existing["raw_json"], now, existing["id"]))
                    continue
                self.db.execute("INSERT INTO schedules (id,dedupe_key,looki_id,day,start_at,end_at,title,description,audio_summary,keywords,source,user_edited,deleted,raw_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?)", (str(uuid.uuid4()), dedupe, normalized.get("looki_id"), normalized["date"], normalized["start"], normalized["end"], normalized["title"], description, audio, keywords, "looki", raw, now, now))
                inserted += 1
            self.merge_content_duplicates()
        return inserted

    def _content_match(self, day: str, start: str, title: str) -> sqlite3.Row | None:
        """在同一天内按“开始时刻 + 标题”识别已存在的同一条活动。

        精确匹配不限制来源（人工录入内容与远端一致时也应合并）；标题被人工改过时，
        退化为只比对开始时刻，且仅限 Looki 来源记录，避免误并人工日程。
        """
        exact, relaxed = [], []
        for row in self.db.execute("SELECT * FROM schedules WHERE day=?", (day,)).fetchall():
            try:
                row_start = _instant(row["start_at"])
            except ValueError:
                continue
            if row_start != _instant(start):
                continue
            if _title_key(row["title"]) == _title_key(title):
                exact.append(row)
            elif row["source"] == "looki" and row["looki_id"]:
                relaxed.append(row)
        for bucket in (exact, relaxed):
            if bucket:
                # 优先保留：未删除 > 人工编辑 > 创建更早
                return min(bucket, key=lambda r: (r["deleted"], 0 if r["user_edited"] else 1, r["created_at"]))
        return None

    def merge_content_duplicates(self) -> int:
        """合并历史上因 Looki 更换 id 产生的重复记录。

        每条内容相同的记录组只保留“未删除、人工编辑优先、创建更早”的一条，
        其余纯 Looki 未编辑记录标记为已删除；人工记录与人工编辑过的记录永不删除。
        """
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in self.db.execute("SELECT * FROM schedules ORDER BY created_at").fetchall():
            try:
                key = _content_key(row["day"], row["start_at"], row["title"])
            except ValueError:
                continue
            groups.setdefault(key, []).append(row)
        now = datetime.now().astimezone().isoformat()
        removed = 0
        with self.db:
            for rows in groups.values():
                if len(rows) < 2:
                    continue
                keep = min(rows, key=lambda r: (r["deleted"], 0 if r["user_edited"] else 1, r["created_at"]))
                for row in rows:
                    if row["id"] == keep["id"] or row["deleted"] or row["user_edited"] or row["source"] != "looki":
                        continue
                    self.db.execute("UPDATE schedules SET deleted=1,updated_at=? WHERE id=?", (now, row["id"]))
                    removed += 1
        return removed

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
        has_audio_summary = "audio_summary" in item
        audio_summary = str(item.get("audio_summary") or "").strip()
        if len(audio_summary) > 20000:
            raise ValueError("音频摘要过长")
        now = datetime.now().astimezone().isoformat()
        with self.db:
            if item_id:
                if has_audio_summary:
                    cur = self.db.execute("UPDATE schedules SET day=?,start_at=?,end_at=?,title=?,description=?,audio_summary=?,keywords=?,user_edited=1,deleted=0,updated_at=? WHERE id=?", (day, start, end, title, description, audio_summary, json.dumps(keywords, ensure_ascii=False), now, item_id))
                else:
                    cur = self.db.execute("UPDATE schedules SET day=?,start_at=?,end_at=?,title=?,description=?,keywords=?,user_edited=1,deleted=0,updated_at=? WHERE id=?", (day, start, end, title, description, json.dumps(keywords, ensure_ascii=False), now, item_id))
                if not cur.rowcount:
                    raise ValueError("记录不存在")
                return item_id
            new_id = str(uuid.uuid4())
            self.db.execute("INSERT INTO schedules (id,dedupe_key,looki_id,day,start_at,end_at,title,description,audio_summary,keywords,source,user_edited,deleted,raw_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,0,NULL,?,?)", (new_id, f"manual:{new_id}", None, day, start, end, title, description, audio_summary, json.dumps(keywords, ensure_ascii=False), "manual", now, now))
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
