from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 6


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


_TASK_BOX_RE = re.compile(r"^\s*[-*+]\s*\[\s*[xX✓✔]?\s*\]\s*")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+")


def filter_task_lines(value: Any) -> str:
    """只丢弃去掉 checkbox 后为空的“空任务行”，任何含正文的行（生活事实）都保留。

    真实 Looki 数据里没有 `- [ ]`（P0 探针确认），因此这里刻意保守：
    不做模板猜测、不按 `[x]` 判断完成度、不删除任何带文字的行。
    """
    kept = []
    for line in str(value or "").splitlines():
        if _TASK_BOX_RE.match(line) and not _TASK_BOX_RE.sub("", line).strip():
            continue
        kept.append(line)
    return "\n".join(kept)


def _strip_markers(value: Any) -> str:
    """去掉 Looki 文本里的 Markdown 任务框与标题符号，只保留正文。"""
    lines = []
    for line in str(value or "").splitlines():
        line = _TASK_BOX_RE.sub("", line)
        line = _HEADING_RE.sub("", line)
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


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
                raw_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                media_types TEXT NOT NULL DEFAULT '[]',
                capture_mode TEXT NOT NULL DEFAULT 'unknown',
                remote_updated_at TEXT, remote_deleted INTEGER NOT NULL DEFAULT 0,
                media_synced_at TEXT
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
        # schema v4：来源模式、远端状态与媒体同步状态。只加列，不重建表、不覆盖人工编辑。
        if "media_types" not in columns:
            self.db.execute("ALTER TABLE schedules ADD COLUMN media_types TEXT NOT NULL DEFAULT '[]'")
        if "capture_mode" not in columns:
            self.db.execute("ALTER TABLE schedules ADD COLUMN capture_mode TEXT NOT NULL DEFAULT 'unknown'")
        if "remote_updated_at" not in columns:
            self.db.execute("ALTER TABLE schedules ADD COLUMN remote_updated_at TEXT")
        if "remote_deleted" not in columns:
            self.db.execute("ALTER TABLE schedules ADD COLUMN remote_deleted INTEGER NOT NULL DEFAULT 0")
        if "media_synced_at" not in columns:
            self.db.execute("ALTER TABLE schedules ADD COLUMN media_synced_at TEXT")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS media_evidence ("
            "id TEXT PRIMARY KEY, schedule_id TEXT, looki_id TEXT NOT NULL,"
            "evidence_key TEXT UNIQUE NOT NULL, media_type TEXT NOT NULL,"
            "remote_file_id TEXT, mime_type TEXT NOT NULL DEFAULT '',"
            "width INTEGER, height INTEGER, duration_ms INTEGER,"
            "has_thumbnail INTEGER NOT NULL DEFAULT 0,"
            "availability TEXT NOT NULL DEFAULT 'available',"
            "text_excerpt TEXT NOT NULL DEFAULT '',"
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_media_looki ON media_evidence(looki_id)")
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.commit()

    def close(self):
        self.db.close()

    def _row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["date"] = item.pop("day")
        item["keywords"] = json.loads(item["keywords"])
        item["media_types"] = json.loads(item.get("media_types") or "[]")
        item["remote_deleted"] = bool(item.get("remote_deleted"))
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
                normalized["title"] = _strip_markers(filter_task_lines(normalized["title"]))
                if not normalized["title"]:
                    continue
                dedupe = _key(normalized)
                keywords = json.dumps(normalized.get("keywords", []), ensure_ascii=False)
                description = _strip_markers(filter_task_lines(normalized.get("description")))
                audio = _strip_markers(filter_task_lines(normalized.get("audio_summary")))
                media_types = json.dumps(normalized.get("media_types") or [], ensure_ascii=False)
                capture_mode = str(normalized.get("capture_mode") or "unknown")
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
                        self.db.execute("UPDATE schedules SET day=?,start_at=?,end_at=?,title=?,description=?,audio_summary=?,keywords=?,media_types=?,capture_mode=?,raw_json=?,updated_at=? WHERE id=?", (normalized["date"], normalized["start"], normalized["end"], normalized["title"], description, audio, keywords, media_types, capture_mode, raw if store_raw else existing["raw_json"], now, existing["id"]))
                    continue
                self.db.execute("INSERT INTO schedules (id,dedupe_key,looki_id,day,start_at,end_at,title,description,audio_summary,keywords,source,user_edited,deleted,raw_json,created_at,updated_at,media_types,capture_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?,?,?)", (str(uuid.uuid4()), dedupe, normalized.get("looki_id"), normalized["date"], normalized["start"], normalized["end"], normalized["title"], description, audio, keywords, "looki", raw, now, now, media_types, capture_mode))
                inserted += 1
            self.merge_content_duplicates()
            self.merge_superseded_looki_rows()
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

    def merge_superseded_looki_rows(self) -> int:
        """合并 Looki 重新切分同一段活动产生的重复记录。

        Looki 会先下发若干短片段，之后把同一段合并成更长的片段并以新 id 重新下发，
        于是同一天出现“同开始或同结束、时间相互重叠”的多条记录。
        这里按天分组，只要两条记录时间重叠且开始或结束时刻相同就归为一组，
        保留跨度最大的一条（跨度相同取创建更晚），其余纯 Looki、未编辑、未删除的记录软删除。
        """
        rows = []
        for row in self.db.execute("SELECT * FROM schedules WHERE day IS NOT NULL").fetchall():
            try:
                rows.append((row, datetime.fromisoformat(row["start_at"]), datetime.fromisoformat(row["end_at"])))
            except ValueError:
                continue
        by_day: dict[str, list[tuple[sqlite3.Row, datetime, datetime]]] = {}
        for item in rows:
            by_day.setdefault(item[0]["day"], []).append(item)
        now = datetime.now().astimezone().isoformat()
        removed = 0
        with self.db:
            for day_rows in by_day.values():
                candidates = [i for i, item in enumerate(day_rows) if item[0]["source"] == "looki" and item[0]["looki_id"] and not item[0]["deleted"] and not item[0]["user_edited"]]
                parent = {i: i for i in candidates}

                def find(i: int, parent: dict[int, int] = parent) -> int:
                    while parent[i] != i:
                        i = parent[i]
                    return i

                for x, i in enumerate(candidates):
                    for j in candidates[x + 1:]:
                        a, b = day_rows[i], day_rows[j]
                        if a[1] >= b[2] or b[1] >= a[2]:
                            continue
                        if a[1] == b[1] or a[2] == b[2]:
                            parent[find(i)] = find(j)
                clusters: dict[int, list[int]] = {}
                for i in candidates:
                    clusters.setdefault(find(i), []).append(i)
                for cluster in clusters.values():
                    if len(cluster) < 2:
                        continue
                    keep = max(cluster, key=lambda i: (day_rows[i][2] - day_rows[i][1], day_rows[i][0]["created_at"] or ""))
                    for i in cluster:
                        if i == keep:
                            continue
                        self.db.execute("UPDATE schedules SET deleted=1,updated_at=? WHERE id=?", (now, day_rows[i][0]["id"]))
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

    def pending_media_moments(self, start_day: str, end_day: str, limit: int = 6) -> list[dict[str, Any]]:
        """已同步事件里还没抓过媒体证据的 looki_id，按时间倒序取前 limit 个。"""
        rows = self.db.execute(
            "SELECT looki_id, audio_summary FROM schedules WHERE day BETWEEN ? AND ? AND deleted=0 "
            "AND looki_id IS NOT NULL AND looki_id != '' AND media_synced_at IS NULL "
            "ORDER BY start_at DESC LIMIT ?",
            (start_day, end_day, max(1, min(int(limit), 50))),
        ).fetchall()
        return [{"looki_id": r["looki_id"], "audio_summary": r["audio_summary"] or ""} for r in rows]

    def sync_media_evidence(self, looki_id: str, entries: list[dict[str, Any]], mark_synced: bool = True) -> int:
        """按远端文件 ID upsert 媒体证据；未知类型也保留（availability=unsupported），不再静默丢弃。"""
        now = datetime.now().astimezone().isoformat()
        schedule = self.db.execute("SELECT id FROM schedules WHERE looki_id=? ORDER BY deleted LIMIT 1", (looki_id,)).fetchone()
        schedule_id = schedule["id"] if schedule else None
        saved = 0
        with self.db:
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("evidence_key"):
                    continue
                self.db.execute(
                    "INSERT INTO media_evidence (id,schedule_id,looki_id,evidence_key,media_type,remote_file_id,mime_type,width,height,duration_ms,has_thumbnail,availability,text_excerpt,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(evidence_key) DO UPDATE SET schedule_id=excluded.schedule_id,media_type=excluded.media_type,"
                    "remote_file_id=excluded.remote_file_id,mime_type=excluded.mime_type,width=excluded.width,height=excluded.height,"
                    "duration_ms=excluded.duration_ms,has_thumbnail=excluded.has_thumbnail,availability=excluded.availability,"
                    "text_excerpt=excluded.text_excerpt,updated_at=excluded.updated_at",
                    (
                        str(uuid.uuid4()), schedule_id, looki_id, f"looki:{looki_id}:{entry['evidence_key']}",
                        str(entry.get("media_type") or "unknown"), str(entry.get("remote_file_id") or ""),
                        str(entry.get("mime_type") or ""), entry.get("width"), entry.get("height"),
                        entry.get("duration_ms"), 1 if entry.get("has_thumbnail") else 0,
                        str(entry.get("availability") or "available"), str(entry.get("text_excerpt") or "")[:300],
                        now, now,
                    ),
                )
                saved += 1
            if mark_synced:
                self.db.execute("UPDATE schedules SET media_synced_at=? WHERE looki_id=?", (now, looki_id))
        return saved

    def media_evidence(self, looki_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        if not looki_ids:
            return result
        placeholders = ",".join("?" for _ in looki_ids)
        rows = self.db.execute(
            f"SELECT looki_id,evidence_key,media_type,mime_type,width,height,duration_ms,has_thumbnail,availability,text_excerpt "
            f"FROM media_evidence WHERE looki_id IN ({placeholders}) ORDER BY created_at",
            tuple(looki_ids),
        ).fetchall()
        for row in rows:
            item = dict(row)
            looki_id = item.pop("looki_id")
            item.pop("evidence_key", None)
            item["has_thumbnail"] = bool(item["has_thumbnail"])
            result.setdefault(looki_id, []).append(item)
        return result

    def media_summary(self, start_day: str, end_day: str) -> dict[str, dict[str, int]]:
        """页面用：按事件汇总各媒体类型证据数量。"""
        rows = self.db.execute(
            "SELECT s.looki_id AS looki_id, m.media_type AS media_type, COUNT(*) AS c "
            "FROM media_evidence m JOIN schedules s ON s.looki_id=m.looki_id "
            "WHERE s.day BETWEEN ? AND ? AND s.deleted=0 GROUP BY s.looki_id, m.media_type",
            (start_day, end_day),
        ).fetchall()
        result: dict[str, dict[str, int]] = {}
        for row in rows:
            result.setdefault(row["looki_id"], {})[row["media_type"]] = row["c"]
        return result

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
