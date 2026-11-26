from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from .journal_service import task_key
except ImportError:  # 测试可能以顶层模块方式导入
    from journal_service import task_key

SCHEMA_VERSION = 10


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


def _titles_compatible(a: Any, b: Any) -> bool:
    """判断两个标题是否像同一次活动的不同表述（相等或互相包含）。"""
    left, right = _title_key(a), _title_key(b)
    if not left or not right:
        return False
    if left == right:
        return True
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    return len(short) >= 2 and short in long


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


def _clue_excerpt(value: Any, limit: int = 80) -> str:
    """把描述压成单行短摘要，避免把全文或换行带进上下文。"""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def is_task_expired(day: str, today: date, expire_days: int) -> bool:
    """任务来源日期距今天超过 expire_days 天即逾期；等于该天数仍有效。"""
    return (today - date.fromisoformat(str(day))).days > max(0, int(expire_days))


# 页面与提醒一律排除未匹配日志下的任务；日志行缺失的历史任务保持可见。
_MATCHED_TASK_SCOPE = (
    "NOT EXISTS (SELECT 1 FROM looki_journals j"
    " WHERE j.id={alias}.journal_id AND j.match_state='unmatched')"
)


class ScheduleStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = str(path)
        self._db_local = threading.local()
        self._db_connections: list[sqlite3.Connection] = []
        self._db_connections_lock = threading.Lock()
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
                remote_updated_at TEXT, remote_deleted INTEGER NOT NULL DEFAULT 0
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
        # schema v4：来源模式与远端状态。只加列，不重建表、不覆盖人工编辑。
        if "media_types" not in columns:
            self.db.execute("ALTER TABLE schedules ADD COLUMN media_types TEXT NOT NULL DEFAULT '[]'")
        if "capture_mode" not in columns:
            self.db.execute("ALTER TABLE schedules ADD COLUMN capture_mode TEXT NOT NULL DEFAULT 'unknown'")
        if "remote_updated_at" not in columns:
            self.db.execute("ALTER TABLE schedules ADD COLUMN remote_updated_at TEXT")
        if "remote_deleted" not in columns:
            self.db.execute("ALTER TABLE schedules ADD COLUMN remote_deleted INTEGER NOT NULL DEFAULT 0")
        # schema v7：Looki AI 日志与由日志提取的任务。只加表，不改动既有日程数据。
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS looki_journals ("
            "id TEXT PRIMARY KEY, day TEXT NOT NULL, type TEXT NOT NULL DEFAULT '',"
            "title TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',"
            "content TEXT NOT NULL DEFAULT '', recorded_at TEXT NOT NULL DEFAULT '',"
            "match_state TEXT NOT NULL DEFAULT 'unmatched', schedule_id TEXT,"
            "media_count INTEGER NOT NULL DEFAULT 0, user_edited INTEGER NOT NULL DEFAULT 0,"
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS looki_tasks ("
            "id TEXT PRIMARY KEY, task_key TEXT UNIQUE NOT NULL, journal_id TEXT NOT NULL,"
            "day TEXT NOT NULL, schedule_id TEXT, body TEXT NOT NULL,"
            "section TEXT NOT NULL DEFAULT '', source_line TEXT NOT NULL DEFAULT '',"
            "done INTEGER NOT NULL DEFAULT 0, done_day TEXT, reminded_at TEXT,"
            "deleted INTEGER NOT NULL DEFAULT 0, sort_order INTEGER NOT NULL DEFAULT 0,"
            "user_edited INTEGER NOT NULL DEFAULT 0,"
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS task_reminders ("
            "task_id TEXT NOT NULL, user TEXT NOT NULL, reminded_at TEXT NOT NULL,"
            "message TEXT NOT NULL DEFAULT '',"
            "PRIMARY KEY (task_id, user))"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_journals_day ON looki_journals(day)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_journals_schedule ON looki_journals(schedule_id)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_day ON looki_tasks(day)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_journal ON looki_tasks(journal_id)")
        journal_columns = {r["name"] for r in self.db.execute("PRAGMA table_info(looki_journals)")}
        if "user_edited" not in journal_columns:
            self.db.execute("ALTER TABLE looki_journals ADD COLUMN user_edited INTEGER NOT NULL DEFAULT 0")
        task_columns = {r["name"] for r in self.db.execute("PRAGMA table_info(looki_tasks)")}
        for name, ddl in (
            ("deleted", "INTEGER NOT NULL DEFAULT 0"),
            ("sort_order", "INTEGER NOT NULL DEFAULT 0"),
            ("user_edited", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in task_columns:
                self.db.execute(f"ALTER TABLE looki_tasks ADD COLUMN {name} {ddl}")
        # schema v10：保存实际发送的提醒原文，供后续私聊还原任务上下文。
        reminder_columns = {r["name"] for r in self.db.execute("PRAGMA table_info(task_reminders)")}
        if "message" not in reminder_columns:
            self.db.execute("ALTER TABLE task_reminders ADD COLUMN message TEXT NOT NULL DEFAULT ''")
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.commit()

    @property
    def db(self) -> sqlite3.Connection:
        """每个工作线程使用独立连接，避免事务与游标在线程间互相干扰。"""
        connection = getattr(self._db_local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self._db_path, check_same_thread=False, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
            with self._db_connections_lock:
                self._db_connections.append(connection)
            self._db_local.connection = connection
        return connection

    def close(self):
        with self._db_connections_lock:
            connections = list(self._db_connections)
            self._db_connections.clear()
        self._db_local.connection = None
        for connection in connections:
            connection.close()

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
        return self._with_corrections([self._row(r) for r in rows[:limit]])

    def _with_corrections(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """给日程补上对话校正注记，保持既有返回值结构。"""
        for item in items:
            item["corrections"] = []
        if items:
            by_id = {item["id"]: item for item in items}
            placeholders = ",".join("?" for _ in by_id)
            corrections = self.db.execute(f"SELECT id,schedule_id,note,created_at FROM schedule_corrections WHERE schedule_id IN ({placeholders}) ORDER BY created_at", tuple(by_id)).fetchall()
            for correction in corrections:
                by_id[correction["schedule_id"]]["corrections"].append({"id": correction["id"], "note": correction["note"], "created_at": correction["created_at"]})
        return items

    def life_schedules(self, start_day: str, end_day: str) -> list[dict[str, Any]]:
        """检索层只读查询：范围内全部未删除日程（不限条数），含人工与 Looki 来源及校正注记。"""
        start = date.fromisoformat(start_day)
        end = date.fromisoformat(end_day)
        if end < start or (end - start).days > 3660:
            raise ValueError("日期范围无效或超过 3660 天")
        rows = self.db.execute(
            "SELECT * FROM schedules WHERE day BETWEEN ? AND ? AND deleted=0 ORDER BY start_at",
            (start_day, end_day),
        ).fetchall()
        return self._with_corrections([self._row(row) for row in rows])

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
        if exact:
            # 优先保留：未删除 > 人工编辑 > 创建更早
            return min(exact, key=lambda r: (r["deleted"], 0 if r["user_edited"] else 1, r["created_at"]))
        if len(relaxed) == 1:
            return relaxed[0]
        # 同一起点存在多条标题不同的 Looki 记录时无法判断归属，宁可新增也不猜
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
        这里按天分组，开始时刻相同即可归为一组；仅结束时刻相同时还要求标题互相兼容，
        避免把首尾相接、内容不同的两段活动误并成一条。
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
                        same_start = a[1] == b[1]
                        same_end = a[2] == b[2] and _titles_compatible(a[0]["title"], b[0]["title"])
                        if same_start or same_end:
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

    def update_correction(self, correction_id: str, note: str) -> dict[str, str]:
        note = str(note).strip()
        if not note or len(note) > 2000:
            raise ValueError("校正注记不能为空且不能超过 2000 字")
        with self.db:
            cur = self.db.execute("UPDATE schedule_corrections SET note=? WHERE id=?", (note, correction_id))
            if not cur.rowcount:
                raise ValueError("校正记录不存在")
        return {"id": correction_id, "note": note}

    def delete_correction(self, correction_id: str) -> None:
        with self.db:
            cur = self.db.execute("DELETE FROM schedule_corrections WHERE id=?", (correction_id,))
            if not cur.rowcount:
                raise ValueError("校正记录不存在")

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

    # ---- Looki AI 日志与任务（schema v7） ----
    def upsert_journals(self, items: list[dict[str, Any]]) -> int:
        """写入日志并同步其任务；按远端日志 id 幂等，重复同步不会重复建任务。"""
        now = datetime.now().astimezone().isoformat()
        saved = 0
        with self.db:
            for item in items:
                if not isinstance(item, dict):
                    continue
                journal_id = str(item.get("id") or "").strip()
                if not journal_id:
                    continue
                try:
                    day = date.fromisoformat(str(item.get("day") or "")).isoformat()
                except ValueError:
                    continue
                schedule_id = str(item.get("schedule_id") or "").strip() or None
                match_state = "matched" if schedule_id else "unmatched"
                values = (
                    day, str(item.get("type") or ""), str(item.get("title") or ""),
                    str(item.get("description") or ""), str(item.get("content") or ""),
                    str(item.get("recorded_at") or ""), match_state, schedule_id,
                    int(item.get("media_count") or 0), now,
                )
                existing = self.db.execute("SELECT id,user_edited FROM looki_journals WHERE id=?", (journal_id,)).fetchone()
                if existing is None:
                    self.db.execute(
                        "INSERT INTO looki_journals (day,type,title,description,content,recorded_at,match_state,"
                        "schedule_id,media_count,updated_at,id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        values + (journal_id, now),
                    )
                elif existing["user_edited"]:
                    # 本地编辑过的日志：同步只更新归属与远端元信息，不覆盖正文，也不重建任务
                    self.db.execute(
                        "UPDATE looki_journals SET day=?,type=?,recorded_at=?,match_state=?,schedule_id=?,"
                        "media_count=?,updated_at=? WHERE id=?",
                        (
                            day, str(item.get("type") or ""), str(item.get("recorded_at") or ""),
                            match_state, schedule_id, int(item.get("media_count") or 0), now, journal_id,
                        ),
                    )
                    saved += 1
                    continue
                else:
                    self.db.execute(
                        "UPDATE looki_journals SET day=?,type=?,title=?,description=?,content=?,recorded_at=?,"
                        "match_state=?,schedule_id=?,media_count=?,updated_at=? WHERE id=?",
                        values + (journal_id,),
                    )
                saved += 1
                if isinstance(item.get("tasks"), list):
                    self._sync_journal_tasks(journal_id, day, schedule_id, item["tasks"])
        return saved

    def update_journal(self, journal_id: str, payload: dict[str, Any], tasks: list[Any] | None = None) -> None:
        """本地编辑日志正文/标题/描述；标记 user_edited 后远端同步不再覆盖。

        传入 tasks 时按新正文重建任务清单，源 Markdown 中已删除的任务会被物理删除。
        """
        fields: dict[str, str] = {}
        for name, limit in (("title", 200), ("description", 20000), ("content", 50000)):
            if name in payload:
                value = str(payload[name] or "").strip()
                if len(value) > limit:
                    raise ValueError(f"{name} 过长")
                fields[name] = value
        if not fields:
            raise ValueError("没有可保存的内容")
        now = datetime.now().astimezone().isoformat()
        with self.db:
            journal = self.db.execute("SELECT day,schedule_id FROM looki_journals WHERE id=?", (journal_id,)).fetchone()
            if journal is None:
                raise ValueError("日志不存在")
            assignments = ",".join(f"{name}=?" for name in fields)
            self.db.execute(
                f"UPDATE looki_journals SET {assignments},user_edited=1,updated_at=? WHERE id=?",
                (*fields.values(), now, journal_id),
            )
            if tasks is not None:
                self._sync_journal_tasks(journal_id, journal["day"], journal["schedule_id"], tasks)

    def _sync_journal_tasks(self, journal_id: str, day: str, schedule_id: str | None, tasks: list[Any]) -> None:
        now = datetime.now().astimezone().isoformat()
        keys: set[str] = set()
        for sort_order, task in enumerate(tasks):
            if not isinstance(task, dict):
                continue
            body = str(task.get("body") or "").strip()
            if not body:
                continue
            key = task_key(journal_id, body)
            if key in keys:
                continue
            keys.add(key)
            existing = self.db.execute("SELECT id,user_edited FROM looki_tasks WHERE task_key=?", (key,)).fetchone()
            if existing is not None:
                # 保留本地的完成状态、删除标记、排序与提醒记录；正文被人工改过时也不再覆盖
                if existing["user_edited"]:
                    self.db.execute(
                        "UPDATE looki_tasks SET section=?,source_line=?,day=?,schedule_id=?,updated_at=? WHERE task_key=?",
                        (
                            str(task.get("section") or ""), str(task.get("source_line") or ""),
                            day, schedule_id, now, key,
                        ),
                    )
                else:
                    self.db.execute(
                        "UPDATE looki_tasks SET body=?,section=?,source_line=?,day=?,schedule_id=?,updated_at=? WHERE task_key=?",
                        (
                            body, str(task.get("section") or ""), str(task.get("source_line") or ""),
                            day, schedule_id, now, key,
                        ),
                    )
                continue
            done = 1 if task.get("done") else 0
            self.db.execute(
                "INSERT INTO looki_tasks (id,task_key,journal_id,day,schedule_id,body,section,source_line,"
                "done,done_day,reminded_at,sort_order,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)",
                (
                    str(uuid.uuid4()), key, journal_id, day, schedule_id, body,
                    str(task.get("section") or ""), str(task.get("source_line") or ""),
                    done, day if done else None, sort_order, now, now,
                ),
            )
        for row in self.db.execute("SELECT id,task_key,source_line FROM looki_tasks WHERE journal_id=?", (journal_id,)).fetchall():
            if row["task_key"] not in keys:
                # 旧版本可能遗留编号列表等非 checkbox 任务，解析规则收紧时不得误删其完成状态。
                if not _TASK_BOX_RE.match(row["source_line"]):
                    continue
                # 源 Markdown 已删除该任务标记：物理删除任务与提醒记录
                self.db.execute("DELETE FROM looki_tasks WHERE id=?", (row["id"],))
                self.db.execute("DELETE FROM task_reminders WHERE task_id=?", (row["id"],))

    def set_journal_match(self, journal_id: str, schedule_id: str | None) -> None:
        now = datetime.now().astimezone().isoformat()
        with self.db:
            self.db.execute(
                "UPDATE looki_journals SET schedule_id=?,match_state=?,updated_at=? WHERE id=?",
                (schedule_id, "matched" if schedule_id else "unmatched", now, journal_id),
            )
            self.db.execute("UPDATE looki_tasks SET schedule_id=?,updated_at=? WHERE journal_id=?", (schedule_id, now, journal_id))

    def journal_days(self, start_day: str, end_day: str) -> set[str]:
        rows = self.db.execute("SELECT DISTINCT day FROM looki_journals WHERE day BETWEEN ? AND ?", (start_day, end_day)).fetchall()
        return {r["day"] for r in rows}

    def journals(
        self,
        start_day: str,
        end_day: str,
        only_unmatched: bool = False,
        today: date | None = None,
        expire_days: int = 3,
        include_deleted_tasks: bool = False,
    ) -> list[dict[str, Any]]:
        """默认只返回已匹配日志；`only_unmatched=True` 仅供内部重匹配使用。

        传入 `today` 时（模型上下文/检索方向），剔除已逾期未完成的任务及其原文行；
        页面不传 `today`，仍可看到过期任务。
        """
        start = date.fromisoformat(start_day)
        end = date.fromisoformat(end_day)
        if end < start or (end - start).days > 366:
            raise ValueError("日期范围无效或超过 366 天")
        sql = "SELECT * FROM looki_journals WHERE day BETWEEN ? AND ?"
        sql += " AND match_state='unmatched'" if only_unmatched else " AND match_state='matched'"
        sql += " ORDER BY day, recorded_at, created_at"
        rows = self.db.execute(sql, (start_day, end_day)).fetchall()
        items = [self._journal_row(r) for r in rows]
        if items:
            by_id = {item["id"]: item for item in items}
            placeholders = ",".join("?" for _ in by_id)
            task_rows = self.db.execute(
                f"SELECT id,journal_id,day,schedule_id,body,section,source_line,done,done_day,deleted,sort_order "
                f"FROM looki_tasks WHERE journal_id IN ({placeholders})"
                + ("" if include_deleted_tasks else " AND deleted=0")
                + " ORDER BY sort_order, created_at, id",
                tuple(by_id),
            ).fetchall()
            expired_lines: set[str] = set()
            for task in task_rows:
                journal = by_id.get(task["journal_id"])
                if journal is None:
                    continue
                if today is not None and not task["done"] and is_task_expired(task["day"], today, expire_days):
                    expired_lines.add(str(task["source_line"] or "").strip())
                    continue
                journal["tasks"].append({
                    "id": task["id"], "journal_id": task["journal_id"], "day": task["day"],
                    "schedule_id": task["schedule_id"], "body": task["body"], "section": task["section"],
                    "done": bool(task["done"]), "done_day": task["done_day"],
                    "deleted": bool(task["deleted"]),
                })
            if expired_lines:
                expired_lines.discard("")
                for journal in items:
                    content = str(journal.get("content") or "")
                    if content:
                        journal["content"] = "\n".join(
                            line for line in content.splitlines() if line.strip() not in expired_lines
                        )
        return items

    @staticmethod
    def _journal_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["media_count"] = int(item.get("media_count") or 0)
        item["user_edited"] = bool(item.get("user_edited"))
        item["tasks"] = []
        return item

    def tasks(
        self,
        start_day: str | None = None,
        end_day: str | None = None,
        only_pending: bool = False,
        limit: int = 500,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        """页面任务查询：包含过期任务，但排除未匹配日志下的任务与软删除任务。"""
        sql = (
            "SELECT id,journal_id,day,schedule_id,body,section,done,done_day,reminded_at,"
            "deleted,sort_order,user_edited FROM looki_tasks"
        )
        clauses: list[str] = [_MATCHED_TASK_SCOPE.format(alias="looki_tasks")]
        params: list[Any] = []
        if start_day and end_day:
            date.fromisoformat(start_day)
            date.fromisoformat(end_day)
            clauses.append("day BETWEEN ? AND ?")
            params.extend((start_day, end_day))
        if only_pending:
            clauses.append("done=0")
        if not include_deleted:
            clauses.append("deleted=0")
        sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY deleted, done, sort_order, day DESC, created_at, id LIMIT ?"
        params.append(max(1, min(int(limit), 2000)))
        return [self._task_row(r) for r in self.db.execute(sql, tuple(params)).fetchall()]

    def tasks_today_pending(self, day: str) -> list[dict[str, Any]]:
        """左侧任务面板：只返回当天来源且未完成、未删除的任务。"""
        date.fromisoformat(day)
        rows = self.db.execute(
            "SELECT id,journal_id,day,schedule_id,body,section,done,done_day,reminded_at,"
            "deleted,sort_order,user_edited FROM looki_tasks "
            "WHERE day=? AND done=0 AND deleted=0 AND " + _MATCHED_TASK_SCOPE.format(alias="looki_tasks")
            + " ORDER BY sort_order, created_at, id",
            (day,),
        ).fetchall()
        return [self._task_row(r) for r in rows]

    def tasks_done_on(self, day: str) -> list[dict[str, Any]]:
        date.fromisoformat(day)
        rows = self.db.execute(
            "SELECT id,journal_id,day,schedule_id,body,section,done,done_day,reminded_at,"
            "deleted,sort_order,user_edited FROM looki_tasks "
            "WHERE done=1 AND done_day=? AND deleted=0 AND " + _MATCHED_TASK_SCOPE.format(alias="looki_tasks")
            + " ORDER BY updated_at DESC",
            (day,),
        ).fetchall()
        return [self._task_row(r) for r in rows]

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["done"] = bool(item["done"])
        item["deleted"] = bool(item.get("deleted"))
        item["user_edited"] = bool(item.get("user_edited"))
        item.pop("sort_order", None)
        return item

    def set_task_done(self, task_id: str, done: bool, day: str) -> None:
        now = datetime.now().astimezone().isoformat()
        with self.db:
            cur = self.db.execute(
                "UPDATE looki_tasks SET done=?,done_day=?,updated_at=? WHERE id=?",
                (1 if done else 0, date.fromisoformat(day).isoformat() if done else None, now, task_id),
            )
            if not cur.rowcount:
                raise ValueError("任务不存在")

    def update_task(self, task_id: str, body: str) -> None:
        body = str(body or "").strip()
        if not body or len(body) > 500:
            raise ValueError("任务内容不能为空且不能超过 500 字")
        with self.db:
            cur = self.db.execute(
                "UPDATE looki_tasks SET body=?,user_edited=1,updated_at=? WHERE id=?",
                (body, datetime.now().astimezone().isoformat(), task_id),
            )
            if not cur.rowcount:
                raise ValueError("任务不存在")

    def delete_task(self, task_id: str) -> None:
        with self.db:
            cur = self.db.execute(
                "UPDATE looki_tasks SET deleted=1,updated_at=? WHERE id=?",
                (datetime.now().astimezone().isoformat(), task_id),
            )
            if not cur.rowcount:
                raise ValueError("任务不存在")

    def restore_task(self, task_id: str) -> None:
        with self.db:
            cur = self.db.execute(
                "UPDATE looki_tasks SET deleted=0,updated_at=? WHERE id=?",
                (datetime.now().astimezone().isoformat(), task_id),
            )
            if not cur.rowcount:
                raise ValueError("任务不存在")

    def move_task(self, task_id: str, direction: str) -> None:
        """在同一天、同一状态分组（未完成/已完成/已删除）内上移或下移一格。"""
        if direction not in ("up", "down"):
            raise ValueError("方向无效")
        row = self.db.execute("SELECT done,deleted,day FROM looki_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("任务不存在")
        rows = self.db.execute(
            "SELECT id FROM looki_tasks WHERE done=? AND deleted=? AND day=? ORDER BY sort_order, created_at, id",
            (row["done"], row["deleted"], row["day"]),
        ).fetchall()
        ids = [r["id"] for r in rows]
        index = ids.index(task_id)
        target = index - 1 if direction == "up" else index + 1
        if target < 0 or target >= len(ids):
            return
        ids[index], ids[target] = ids[target], ids[index]
        with self.db:
            for order, tid in enumerate(ids):
                self.db.execute("UPDATE looki_tasks SET sort_order=? WHERE id=?", (order, tid))

    def pending_task_reminders(
        self,
        limit: int = 50,
        today: date | None = None,
        expire_days: int = 3,
    ) -> list[dict[str, Any]]:
        clauses = ["done=0", "deleted=0", "reminded_at IS NULL", _MATCHED_TASK_SCOPE.format(alias="looki_tasks")]
        params: list[Any] = []
        if today is not None:
            clauses.append("day >= ?")
            params.append((today - timedelta(days=max(0, int(expire_days)))).isoformat())
        params.append(max(1, min(int(limit), 200)))
        rows = self.db.execute(
            "SELECT id,journal_id,day,schedule_id,body,section,done,done_day,reminded_at FROM looki_tasks "
            "WHERE " + " AND ".join(clauses) + " ORDER BY day, created_at LIMIT ?",
            tuple(params),
        ).fetchall()
        return [self._task_row(r) for r in rows]

    def mark_tasks_reminded(self, task_ids: list[str]) -> None:
        if not task_ids:
            return
        now = datetime.now().astimezone().isoformat()
        placeholders = ",".join("?" for _ in task_ids)
        with self.db:
            self.db.execute(f"UPDATE looki_tasks SET reminded_at=?,updated_at=? WHERE id IN ({placeholders})", (now, now, *task_ids))

    def pending_task_reminders_for(
        self,
        user: str,
        limit: int = 50,
        today: date | None = None,
        expire_days: int = 3,
    ) -> list[dict[str, Any]]:
        """按接收人查询尚未提醒的任务：每个接收人各自维护提醒状态。"""
        clauses = [
            "t.done=0",
            "t.deleted=0",
            "NOT EXISTS (SELECT 1 FROM task_reminders r WHERE r.task_id=t.id AND r.user=?)",
            _MATCHED_TASK_SCOPE.format(alias="t"),
        ]
        params: list[Any] = [user]
        if today is not None:
            clauses.append("t.day >= ?")
            params.append((today - timedelta(days=max(0, int(expire_days)))).isoformat())
        params.append(max(1, min(int(limit), 200)))
        rows = self.db.execute(
            "SELECT t.id,t.journal_id,t.day,t.schedule_id,t.body,t.section,t.done,t.done_day,t.reminded_at,"
            "j.content AS journal_content FROM looki_tasks t LEFT JOIN looki_journals j ON j.id=t.journal_id WHERE "
            + " AND ".join(clauses) + " ORDER BY t.day, t.created_at LIMIT ?",
            tuple(params),
        ).fetchall()
        return [self._task_row(r) for r in rows]

    def mark_tasks_reminded_for(self, task_ids: list[str], user: str, message: str = "") -> None:
        if not task_ids:
            return
        now = datetime.now().astimezone().isoformat()
        with self.db:
            for task_id in task_ids:
                self.db.execute(
                    "INSERT OR REPLACE INTO task_reminders (task_id,user,reminded_at,message) VALUES (?,?,?,?)",
                    (task_id, user, now, str(message)),
                )

    def latest_task_reminder_for(self, user: str) -> dict[str, Any] | None:
        """返回该接收人最近一条仍有上下文的未完成任务提醒。"""
        row = self.db.execute(
            "SELECT t.id AS task_id,t.day,t.body,t.section,r.reminded_at,r.message,"
            "j.content AS journal_content FROM task_reminders r "
            "JOIN looki_tasks t ON t.id=r.task_id "
            "JOIN looki_journals j ON j.id=t.journal_id "
            "WHERE r.user=? AND r.message<>'' AND t.done=0 AND t.deleted=0 "
            "ORDER BY r.reminded_at DESC LIMIT 1",
            (str(user),),
        ).fetchone()
        return dict(row) if row else None

    def last_task_reminder_at_for(self, user: str) -> str | None:
        row = self.db.execute(
            "SELECT MAX(reminded_at) FROM task_reminders WHERE user=?", (user,)
        ).fetchone()
        return str(row[0]) if row and row[0] else None

    def delete_unreminded_task(self, task_id: str) -> bool:
        """低价值任务可软删除，但不能误删已经提醒过其他接收人的任务。"""
        cursor = self.db.execute(
            "UPDATE looki_tasks SET deleted=1,updated_at=? "
            "WHERE id=? AND deleted=0 AND NOT EXISTS "
            "(SELECT 1 FROM task_reminders WHERE task_id=?)",
            (datetime.now().astimezone().isoformat(), task_id, task_id),
        )
        self.db.commit()
        return cursor.rowcount > 0

    def complete_latest_reminded_task(self, user: str, done_day: str | None = None) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT t.id FROM looki_tasks t JOIN task_reminders r ON r.task_id=t.id "
            "WHERE r.user=? AND t.done=0 AND t.deleted=0 "
            "ORDER BY r.reminded_at DESC,r.rowid DESC LIMIT 1",
            (user,),
        ).fetchone()
        if row is None:
            return None
        self.set_task_done(row["id"], True, done_day or date.today().isoformat())
        completed = self.db.execute("SELECT * FROM looki_tasks WHERE id=?", (row["id"],)).fetchone()
        return self._task_row(completed) if completed else None

    def clues(self, days: int = 7, max_items: int = 3, timezone: str = "Asia/Shanghai") -> list[str]:
        """生成近期生活线索：已结束的真实活动优先，再用剩余名额放校正、晚间与关键词统计。"""
        zone = ZoneInfo(timezone)
        now = datetime.now(zone)
        end = now.date()
        start = end - timedelta(days=max(1, min(days, 31)) - 1)
        limit = max(0, min(max_items, 10))
        if limit == 0:
            return []
        rows = self.db.execute(
            "SELECT id,day,start_at,end_at,title,description,keywords,source FROM schedules "
            "WHERE day BETWEEN ? AND ? AND deleted=0",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        correction_rows = self.db.execute(
            "SELECT s.id AS schedule_id,s.day,s.title,c.note FROM schedule_corrections c "
            "JOIN schedules s ON s.id=c.schedule_id WHERE s.day BETWEEN ? AND ? AND s.deleted=0 "
            "ORDER BY c.created_at DESC",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        latest_correction: dict[str, str] = {}
        for row in correction_rows:
            latest_correction.setdefault(row["schedule_id"], row["note"])

        # 只把已经结束的记录当作“发生过”，并按换算后的本地开始时间从近到远排序。
        ended: list[tuple[datetime, datetime, sqlite3.Row]] = []
        for row in rows:
            try:
                local_start = datetime.fromisoformat(row["start_at"]).astimezone(zone)
                local_end = datetime.fromisoformat(row["end_at"]).astimezone(zone)
            except ValueError:
                continue
            if local_end <= now:
                ended.append((local_start, local_end, row))
        ended.sort(key=lambda entry: entry[0], reverse=True)

        result: list[str] = []
        shown_corrections: set[str] = set()
        for local_start, local_end, row in ended[:limit]:
            source = "Looki 记录" if row["source"] == "looki" else "手工补录"
            line = f"{local_start:%Y-%m-%d} {local_start:%H:%M}-{local_end:%H:%M}《{row['title']}》[{source}]"
            excerpt = _clue_excerpt(row["description"])
            if excerpt:
                line += f"：{excerpt}"
            note = latest_correction.get(row["id"])
            if note:
                line += f"（对话校正：{_clue_excerpt(note, 60)}）"
                shown_corrections.add(row["id"])
            result.append(line)
        for row in correction_rows:
            if len(result) >= limit:
                break
            if row["schedule_id"] in shown_corrections:
                continue
            if row["schedule_id"] not in {entry[2]["id"] for entry in ended}:
                continue
            result.append(f"{row['day']}《{row['title']}》对话校正：{row['note']}")
        ended_rows = [entry[2] for entry in ended]
        late_days = {row["day"] for row in ended_rows if datetime.fromisoformat(row["end_at"]).astimezone(zone).hour >= 22}
        if len(result) < limit and len(late_days) >= 3:
            result.append(f"回看的日程中，有{len(late_days)}天记录到结束时间在22点或之后的活动；这只说明晚间仍有活动记录，不代表睡眠情况。")
        counts: dict[str, int] = {}
        for row in ended_rows:
            for word in json.loads(row["keywords"]):
                counts[word] = counts.get(word, 0) + 1
        if len(result) < limit:
            result.extend(f"近期日程关键词多次出现：{word}（{count}次）。" for word, count in sorted(counts.items(), key=lambda x: (-x[1], x[0])) if count >= 2)
        return result[:limit]
