import asyncio
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx

from schedule_store import SCHEMA_VERSION, ScheduleStore


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeClient:
    instances = []

    def __init__(self, **kwargs):
        self.get = AsyncMock(side_effect=[FakeResponse({"data": {"moments": [
            {"id": "remote-1", "date": "2026-10-25", "start_time": "2026-10-25T09:00:00", "end_time": "2026-10-25T10:00:00", "title": "测试活动", "description": "不应进入日程事实", "audio_summary": "课堂录音摘要"},
            {"id": "no-time", "title": "跳过缺少时间"},
        ]}})])
        self.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class LookiProviderTests(unittest.TestCase):
    def test_fetch_uses_auth_and_maps_only_structured_fields(self):
        from astrbot_plugin_looki_companion.main import LookiScheduleProvider

        provider = LookiScheduleProvider("https://open.looki.tech/api/v1/", "test-key")
        with patch("httpx.AsyncClient", FakeClient) as client_class:
            items = asyncio.run(provider.fetch(date(2026, 10, 25), date(2026, 10, 25), "Asia/Shanghai"))

        client = FakeClient.instances[-1]
        call = client.get.await_args
        self.assertEqual(call.args[0], "https://open.looki.tech/api/v1/moments")
        self.assertEqual(call.kwargs["params"], {"on_date": "2026-10-25"})
        self.assertEqual(call.kwargs["headers"], {"x-api-key": "test-key"})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["start"], "2026-10-25T09:00:00+08:00")
        # description / audio_summary 是独立字段（不合并入 title/keywords），0.5.1 起加入 fetch 结果
        self.assertEqual(set(items[0]), {"looki_id", "date", "start", "end", "title", "keywords", "description", "audio_summary", "raw_json", "media_types", "capture_mode"})
        self.assertEqual(items[0]["description"], "不应进入日程事实")  # description 原样保留，不混入 title
        self.assertEqual(items[0]["audio_summary"], "课堂录音摘要")
        self.assertNotIn("不应进入日程事实", items[0]["title"])
        with tempfile.TemporaryDirectory() as temp:
            store = ScheduleStore(Path(temp) / "schedules.sqlite3")
            self.assertEqual(store.upsert_remote(items), 1)
            row = store.query("2026-10-25", "2026-10-25")[0]
            self.assertEqual(row["title"], "测试活动")
            # description / audio_summary 是独立列，不混入 title
            self.assertEqual(row["description"], "不应进入日程事实")
            self.assertEqual(row["audio_summary"], "课堂录音摘要")
            store.close()

    def test_fetch_converts_utc_times_to_configured_timezone(self):
        """远端返回 UTC 时必须换算到配置时区，否则 day 归属和晚间判断都会错位"""
        from astrbot_plugin_looki_companion.main import LookiScheduleProvider

        class UtcClient:
            def __init__(self, **kwargs):
                self.get = AsyncMock(return_value=FakeResponse({"data": {"moments": [{
                    "id": "utc-1",
                    "date": "2026-10-25",
                    "start_time": "2026-10-25T01:00:00Z",
                    "end_time": "2026-10-25T02:00:00Z",
                    "title": "UTC 活动",
                }]}}))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        provider = LookiScheduleProvider("https://open.looki.tech/api/v1/", "test-key")
        with patch("httpx.AsyncClient", UtcClient):
            items = asyncio.run(provider.fetch(date(2026, 10, 25), date(2026, 10, 25), "Asia/Shanghai"))

        self.assertEqual(items[0]["start"], "2026-10-25T09:00:00+08:00")
        self.assertEqual(items[0]["end"], "2026-10-25T10:00:00+08:00")
        self.assertEqual(items[0]["date"], "2026-10-25")

    def test_fetch_raises_when_every_day_fails(self):
        """全部日期失败必须抛错，避免上层把空结果记成同步成功"""
        from astrbot_plugin_looki_companion.main import LookiScheduleProvider

        class FailClient:
            def __init__(self, **kwargs):
                self.get = AsyncMock(side_effect=ValueError("boom"))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        provider = LookiScheduleProvider("https://open.looki.tech/api/v1/", "test-key")
        with patch("httpx.AsyncClient", FailClient):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(provider.fetch(date(2026, 10, 25), date(2026, 10, 27), "Asia/Shanghai"))
        self.assertIn("全部日期同步失败", str(ctx.exception))

    def test_fetch_returns_remaining_days_when_one_day_fails(self):
        """单日失败只跳过该日，其余日期的数据仍要返回"""
        from astrbot_plugin_looki_companion.main import LookiScheduleProvider

        class PartClient:
            def __init__(self, **kwargs):
                self.get = AsyncMock(side_effect=[
                    ValueError("boom"),
                    FakeResponse({"data": {"moments": [{
                        "id": "ok-1",
                        "date": "2026-10-26",
                        "start_time": "2026-10-26T09:00:00",
                        "end_time": "2026-10-26T10:00:00",
                        "title": "第二天活动",
                    }]}}),
                ])

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        provider = LookiScheduleProvider("https://open.looki.tech/api/v1/", "test-key")
        with patch("httpx.AsyncClient", PartClient):
            items = asyncio.run(provider.fetch(date(2026, 10, 25), date(2026, 10, 26), "Asia/Shanghai"))
        self.assertEqual([item["looki_id"] for item in items], ["ok-1"])

    def test_fetch_journals_raises_when_every_day_fails(self):
        from astrbot_plugin_looki_companion.main import LookiScheduleProvider

        class FailClient:
            def __init__(self, **kwargs):
                self.get = AsyncMock(side_effect=ValueError("boom"))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        provider = LookiScheduleProvider("https://open.looki.tech/api/v1/", "test-key")
        with patch("httpx.AsyncClient", FailClient):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(provider.fetch_journals(date(2026, 10, 25), date(2026, 10, 25), "Asia/Shanghai"))
        self.assertIn("全部日期同步失败", str(ctx.exception))


class ScheduleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ScheduleStore(Path(self.temp.name) / "schedules.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def item(self, **changes):
        value = {
            "looki_id": "remote-1",
            "date": "2026-10-25",
            "start": "2026-10-25T21:00:00+08:00",
            "end": "2026-10-25T22:30:00+08:00",
            "title": "阅读",
            "keywords": ["书籍"],
        }
        return {**value, **changes}

    def test_remote_upsert_is_idempotent_and_user_edit_is_preserved(self):
        self.assertEqual(self.store.upsert_remote([self.item()]), 1)
        self.assertEqual(self.store.upsert_remote([self.item()]), 0)
        record = self.store.query("2026-10-25", "2026-10-25")[0]
        edited = {"date": record["date"], "start": record["start_at"], "end": record["end_at"], "title": "手动修改", "keywords": record["keywords"]}
        self.store.save(edited, record["id"])
        self.store.upsert_remote([self.item(title="远端更新")])
        self.assertEqual(self.store.query("2026-10-25", "2026-10-25")[0]["title"], "手动修改")

    def test_manual_record_survives_remote_sync(self):
        self.store.save({"date": "2026-10-25", "start": "2026-10-25T09:00:00+08:00", "end": "2026-10-25T10:00:00+08:00", "title": "手动", "keywords": []})
        self.store.upsert_remote([self.item()])
        self.assertEqual(len(self.store.query("2026-10-25", "2026-10-25")), 2)

    def test_timezone_and_date_range_validation(self):
        self.store.upsert_remote([self.item()])
        self.assertEqual(len(self.store.query("2026-10-25", "2026-10-25")), 1)
        with self.assertRaises(ValueError):
            self.store.save({"date": "2026-10-25", "start": "2026-10-25T09:00:00", "end": "2026-10-25T10:00:00+08:00", "title": "无时区", "keywords": []})

    def test_keyword_fallback_and_empty_results(self):
        self.store.upsert_remote([self.item()])
        self.assertEqual(self.store.query("2026-10-25", "2026-10-25", "不存在", 10), [])
        self.assertEqual(self.store.query("2026-10-26", "2026-10-26"), [])

    def test_clues_do_not_infer_sleep(self):
        today = datetime.now().astimezone().date()
        for offset in range(1, 4):
            day = today - timedelta(days=offset)
            self.store.upsert_remote([self.item(looki_id=f"id-{offset}", date=day.isoformat(), start=f"{day.isoformat()}T21:00:00+08:00", end=f"{day.isoformat()}T22:30:00+08:00")])
        clues = self.store.clues(7, 6)
        self.assertTrue(any("晚间仍有活动记录" in clue for clue in clues))
        self.assertFalse(any("熬夜" in clue or "睡眠不足" in clue for clue in clues))

    def test_update_manual_delete_persists(self):
        item_id = self.store.save({"date": "2026-10-25", "start": "2026-10-25T09:00:00+08:00", "end": "2026-10-25T10:00:00+08:00", "title": "待办", "keywords": []})
        self.store.save({"date": "2026-10-25", "start": "2026-10-25T09:00:00+08:00", "end": "2026-10-25T10:00:00+08:00", "title": "修改", "keywords": ["工作"]}, item_id)
        self.assertEqual(self.store.query("2026-10-25", "2026-10-25")[0]["title"], "修改")
        self.store.delete(item_id)
        self.assertEqual(self.store.query("2026-10-25", "2026-10-25"), [])

    def test_remote_bad_record_does_not_block_batch(self):
        """单点记录（start==end）补 1 秒保留；倒置或缺标题的记录跳过，不影响同批其他记录"""
        batch = [
            self.item(looki_id="instant", start="2026-10-25T07:06:11+08:00", end="2026-10-25T07:06:11+08:00"),
            self.item(looki_id="reversed", start="2026-10-25T10:00:00+08:00", end="2026-10-25T09:00:00+08:00"),
            self.item(looki_id="blank-title", title="   "),
            self.item(looki_id="normal", title="正常活动", start="2026-10-25T20:00:00+08:00", end="2026-10-25T21:00:00+08:00"),
        ]
        self.assertEqual(self.store.upsert_remote(batch), 2)
        rows = self.store.query("2026-10-25", "2026-10-25")
        self.assertEqual({row["looki_id"] for row in rows}, {"instant", "normal"})
        instant = next(row for row in rows if row["looki_id"] == "instant")
        duration = datetime.fromisoformat(instant["end_at"]) - datetime.fromisoformat(instant["start_at"])
        self.assertEqual(duration.total_seconds(), 1)

    def test_search_fallback_when_keyword_not_matched(self):
        """search() 关键词无命中时退化为全量，returned matched=False"""
        self.store.upsert_remote([self.item()])
        items, matched = self.store.search("2026-10-25", "2026-10-25", "不存在关键词")
        self.assertFalse(matched)
        self.assertEqual(len(items), 1)  # 退化返回全量

    def test_search_matched_when_keyword_hits(self):
        """search() 关键词命中时 matched=True"""
        self.store.upsert_remote([self.item(keywords=["书籍", "专注"])])
        items, matched = self.store.search("2026-10-25", "2026-10-25", "书籍")
        self.assertTrue(matched)
        self.assertEqual(len(items), 1)

    def test_keyword_search_checks_records_after_first_500(self):
        batch = [
            self.item(
                looki_id=f"bulk-{index}",
                start=f"2026-10-25T{index // 60:02d}:{index % 60:02d}:00+08:00",
                end=f"2026-10-25T{index // 60:02d}:{index % 60:02d}:30+08:00",
                title="普通活动" if index < 500 else "目标活动",
            )
            for index in range(501)
        ]
        self.store.upsert_remote(batch)

        items, matched = self.store.search("2026-10-25", "2026-10-25", "目标活动")

        self.assertTrue(matched)
        self.assertEqual([item["title"] for item in items], ["目标活动"])

    def test_search_no_keyword_returns_all(self):
        """search() 无关键词时返回全量，matched=False"""
        self.store.upsert_remote([self.item()])
        items, matched = self.store.search("2026-10-25", "2026-10-25", "")
        self.assertFalse(matched)
        self.assertEqual(len(items), 1)

    def test_activity_lookup_matches_description_audio_and_corrections(self):
        for index, field in enumerate(("description", "audio_summary", "correction")):
            with self.subTest(field=field):
                # 每个子用例使用不同时段：删除过的记录不会因新 id 的同内容活动而复活
                hour = 20 + index
                item = self.item(
                    looki_id=f"activity-{field}",
                    title="普通活动",
                    start=f"2026-10-25T{hour:02d}:00:00+08:00",
                    end=f"2026-10-25T{hour:02d}:30:00+08:00",
                    description="",
                    audio_summary="",
                )
                self.store.upsert_remote([item])
                record = self.store.query("2026-10-25", "2026-10-25")[0]
                if field == "correction":
                    self.store.add_correction(record["id"], "当时和朋友一起玩 Switch")
                else:
                    item[field] = "当时和朋友一起玩 Switch"
                    self.store.upsert_remote([item])
                found = self.store.find_latest_looki_activity("sWiTcH")
                self.assertEqual(found["looki_id"], f"activity-{field}")
                self.store.delete(record["id"])

    def test_activity_lookup_uses_latest_looki_record_only(self):
        def activity(looki_id, day):
            return self.item(
                looki_id=looki_id,
                date=day,
                start=f"{day}T20:00:00+08:00",
                end=f"{day}T21:00:00+08:00",
                title="和朋友玩 Switch",
                keywords=[],
            )

        self.store.upsert_remote([
            activity("older", "2026-10-23"),
            activity("latest", "2026-10-25"),
            activity("deleted", "2026-10-26"),
        ])
        deleted_id = self.store.query("2026-10-26", "2026-10-26")[0]["id"]
        self.store.delete(deleted_id)
        self.store.save({
            "date": "2026-10-27",
            "start": "2026-10-27T20:00:00+08:00",
            "end": "2026-10-27T21:00:00+08:00",
            "title": "手工记录 Switch",
            "keywords": [],
        })

        found = self.store.find_latest_looki_activity(" Switch ")
        self.assertEqual(found["looki_id"], "latest")
        self.assertIsNone(self.store.find_latest_looki_activity("不存在的活动"))

    def test_upsert_remote_store_raw_false_does_not_write_raw_json(self):
        """store_raw=False（默认）时 raw_json 列为 NULL"""
        self.store.upsert_remote([self.item()], store_raw=False)
        row = self.store.db.execute("SELECT raw_json FROM schedules WHERE looki_id='remote-1'").fetchone()
        self.assertIsNone(row["raw_json"])

    def test_upsert_remote_store_raw_true_writes_raw_json(self):
        """store_raw=True 时 raw_json 列有内容"""
        self.store.upsert_remote([self.item()], store_raw=True)
        row = self.store.db.execute("SELECT raw_json FROM schedules WHERE looki_id='remote-1'").fetchone()
        self.assertIsNotNone(row["raw_json"])
        data = json.loads(row["raw_json"])
        self.assertEqual(data["title"], "阅读")

    def test_audio_summary_survives_manual_edit(self):
        """手动编辑日程不覆盖已同步的录音摘要"""
        self.store.upsert_remote([self.item(audio_summary="课堂录音摘要")])
        record = self.store.query("2026-10-25", "2026-10-25")[0]
        self.store.save({"date": record["date"], "start": record["start_at"], "end": record["end_at"], "title": "手动修改", "keywords": []}, record["id"])
        row = self.store.query("2026-10-25", "2026-10-25")[0]
        self.assertEqual(row["title"], "手动修改")
        self.assertEqual(row["audio_summary"], "课堂录音摘要")

    def test_audio_summary_can_be_changed_and_cleared(self):
        self.store.upsert_remote([self.item(audio_summary="课堂录音摘要")])
        record = self.store.query("2026-10-25", "2026-10-25")[0]
        base = {"date": record["date"], "start": record["start_at"], "end": record["end_at"], "title": record["title"], "keywords": []}

        self.store.save({**base, "audio_summary": "人工修订后的摘要"}, record["id"])
        self.assertEqual(self.store.query("2026-10-25", "2026-10-25")[0]["audio_summary"], "人工修订后的摘要")

        self.store.upsert_remote([self.item(audio_summary="远端再次捕捉的摘要")])
        self.assertEqual(self.store.query("2026-10-25", "2026-10-25")[0]["audio_summary"], "人工修订后的摘要")

        self.store.save({**base, "audio_summary": ""}, record["id"])
        self.assertEqual(self.store.query("2026-10-25", "2026-10-25")[0]["audio_summary"], "")
        self.store.upsert_remote([self.item(audio_summary="不应覆盖人工清空")])
        self.assertEqual(self.store.query("2026-10-25", "2026-10-25")[0]["audio_summary"], "")

    def test_manual_record_can_save_audio_summary(self):
        item_id = self.store.save({
            "date": "2026-10-25",
            "start": "2026-10-25T09:00:00+08:00",
            "end": "2026-10-25T10:00:00+08:00",
            "title": "手工日程",
            "keywords": [],
            "audio_summary": "手工补录的音频摘要",
        })
        row = self.store.query("2026-10-25", "2026-10-25")[0]
        self.assertEqual(row["id"], item_id)
        self.assertEqual(row["audio_summary"], "手工补录的音频摘要")

    def test_audio_summary_length_validation_does_not_partially_update(self):
        self.store.upsert_remote([self.item(audio_summary="原始摘要")])
        record = self.store.query("2026-10-25", "2026-10-25")[0]
        item = {
            "date": record["date"],
            "start": record["start_at"],
            "end": record["end_at"],
            "title": "不应保存",
            "keywords": [],
            "audio_summary": "x" * 20001,
        }
        with self.assertRaises(ValueError):
            self.store.save(item, record["id"])
        row = self.store.query("2026-10-25", "2026-10-25")[0]
        self.assertEqual(row["title"], "阅读")
        self.assertEqual(row["audio_summary"], "原始摘要")

    def test_db_user_version_is_schema_version(self):
        """DB user_version 应等于 SCHEMA_VERSION（版本管理）"""
        from schedule_store import SCHEMA_VERSION
        ver = self.store.db.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(ver, SCHEMA_VERSION)

    def test_v1_database_migrates_in_place_without_data_loss(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        legacy = sqlite3.connect(path)
        legacy.executescript("""
            CREATE TABLE schedules (
                id TEXT PRIMARY KEY, dedupe_key TEXT UNIQUE NOT NULL,
                looki_id TEXT, day TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
                title TEXT NOT NULL, keywords TEXT NOT NULL, source TEXT NOT NULL,
                user_edited INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE sync_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            PRAGMA user_version=1;
            INSERT INTO schedules VALUES (
                'old-id','looki:old-remote','old-remote','2026-10-25',
                '2026-10-25T09:00:00+08:00','2026-10-25T10:00:00+08:00',
                '用户改过的标题','[]','looki',1,0,NULL,'2026-10-25T01:00:00+00:00','2026-10-25T02:00:00+00:00'
            );
        """)
        legacy.commit()
        legacy.close()

        migrated = ScheduleStore(path)
        try:
            row = migrated.query("2026-10-25", "2026-10-25")[0]
            self.assertEqual(row["id"], "old-id")
            self.assertEqual(row["title"], "用户改过的标题")
            self.assertTrue(row["user_edited"])
            self.assertEqual(migrated.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        finally:
            migrated.close()

    def test_v7_database_preserves_evidence_and_business_data(self):
        """旧库升级保留媒体历史与业务数据，并补充提醒原文列"""
        path = Path(self.temp.name) / "v7.sqlite3"
        legacy = sqlite3.connect(path)
        legacy.executescript("""
            CREATE TABLE schedules (
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
                media_synced_at TEXT, media_partial INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE sync_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE schedule_corrections (
                id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL,
                note TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE media_evidence (
                id TEXT PRIMARY KEY, schedule_id TEXT, looki_id TEXT NOT NULL,
                evidence_key TEXT UNIQUE NOT NULL, media_type TEXT NOT NULL,
                remote_file_id TEXT, mime_type TEXT NOT NULL DEFAULT '',
                width INTEGER, height INTEGER, duration_ms INTEGER,
                has_thumbnail INTEGER NOT NULL DEFAULT 0,
                availability TEXT NOT NULL DEFAULT 'available',
                text_excerpt TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE looki_journals (
                id TEXT PRIMARY KEY, day TEXT NOT NULL, type TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '', recorded_at TEXT NOT NULL DEFAULT '',
                match_state TEXT NOT NULL DEFAULT 'unmatched', schedule_id TEXT,
                media_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE looki_tasks (
                id TEXT PRIMARY KEY, task_key TEXT UNIQUE NOT NULL, journal_id TEXT NOT NULL,
                day TEXT NOT NULL, schedule_id TEXT, body TEXT NOT NULL,
                section TEXT NOT NULL DEFAULT '', source_line TEXT NOT NULL DEFAULT '',
                done INTEGER NOT NULL DEFAULT 0, done_day TEXT, reminded_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE task_reminders (
                task_id TEXT NOT NULL, user TEXT NOT NULL, reminded_at TEXT NOT NULL,
                PRIMARY KEY (task_id, user)
            );
            PRAGMA user_version=7;
            INSERT INTO schedules VALUES (
                'keep-id','looki:keep','keep','2026-10-25',
                '2026-10-25T09:00:00+08:00','2026-10-25T10:00:00+08:00',
                '保留的活动','描述','摘要','["关键词"]','looki',0,0,NULL,
                '2026-10-25T01:00:00+00:00','2026-10-25T02:00:00+00:00',
                '["photo"]','photo',NULL,0,'2026-10-25T03:00:00+00:00',1
            );
            INSERT INTO media_evidence (
                id,schedule_id,looki_id,evidence_key,media_type,created_at,updated_at
            ) VALUES (
                'media-1','keep-id','keep','keep-photo','photo',
                '2026-10-25T03:00:00+00:00','2026-10-25T03:00:00+00:00'
            );
            INSERT INTO looki_tasks VALUES (
                'task-1','key-1','journal-1','2026-10-25','keep-id','买牛奶',
                '今日建议','- [ ] 买牛奶',0,NULL,NULL,
                '2026-10-25T03:00:00+00:00','2026-10-25T03:00:00+00:00'
            );
            INSERT INTO task_reminders VALUES ('legacy-task','legacy-user','2026-10-25T04:00:00+00:00');
        """)
        legacy.commit()
        legacy.close()

        migrated = ScheduleStore(path)
        try:
            row = migrated.query("2026-10-25", "2026-10-25")[0]
            self.assertEqual(row["title"], "保留的活动")
            raw = migrated.db.execute(
                "SELECT media_partial,media_synced_at FROM schedules WHERE id='keep-id'"
            ).fetchone()
            self.assertEqual(raw["media_partial"], 1)
            self.assertEqual(raw["media_synced_at"], "2026-10-25T03:00:00+00:00")
            evidence = migrated.db.execute(
                "SELECT id,schedule_id FROM media_evidence WHERE id='media-1'"
            ).fetchone()
            self.assertEqual(evidence["schedule_id"], "keep-id")
            self.assertEqual([task["body"] for task in migrated.tasks("2026-10-25", "2026-10-25")], ["买牛奶"])
            reminder_columns = {row["name"] for row in migrated.db.execute("PRAGMA table_info(task_reminders)")}
            self.assertIn("message", reminder_columns)
            legacy_reminder = migrated.db.execute(
                "SELECT reminded_at,message FROM task_reminders WHERE task_id='legacy-task'"
            ).fetchone()
            self.assertEqual(legacy_reminder["reminded_at"], "2026-10-25T04:00:00+00:00")
            self.assertEqual(legacy_reminder["message"], "")
            migrated.mark_tasks_reminded_for(["task-1"], "1001")
            self.assertEqual(migrated.pending_task_reminders_for("1001", 10), [])
            self.assertEqual(len(migrated.pending_task_reminders_for("1002", 10)), 1)
            self.assertEqual(migrated.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        finally:
            migrated.close()

    def test_reopen_preserves_edits_deletions_corrections_and_sync_state(self):
        path = Path(self.temp.name) / "reopen.sqlite3"
        store = ScheduleStore(path)
        store.upsert_remote([
            self.item(looki_id="keep", title="人工标题"),
            self.item(looki_id="deleted", title="删除记录", start="2026-10-25T12:00:00+08:00", end="2026-10-25T13:00:00+08:00"),
        ])
        rows = store.query("2026-10-25", "2026-10-25")
        kept = next(row for row in rows if row["looki_id"] == "keep")
        deleted = next(row for row in rows if row["looki_id"] == "deleted")
        store.save({
            "date": kept["date"], "start": kept["start_at"], "end": kept["end_at"],
            "title": "用户修改后的标题", "keywords": kept["keywords"],
        }, kept["id"])
        store.add_correction(kept["id"], "保留这条对话校正")
        store.delete(deleted["id"])
        store.set_meta("last_success", "2026-10-25T12:00:00+08:00")
        store.set_meta("initial_import_choice", "skipped")
        store.db.execute("PRAGMA user_version=5")
        store.db.commit()
        store.close()

        self.store.close()
        self.store = ScheduleStore(path)
        row = self.store.query("2026-10-25", "2026-10-25")[0]
        self.assertEqual(row["title"], "用户修改后的标题")
        self.assertTrue(row["user_edited"])
        self.assertEqual([item["note"] for item in row["corrections"]], ["保留这条对话校正"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM schedules WHERE deleted=1 AND looki_id='deleted'").fetchone()[0], 1)
        self.assertEqual(self.store.meta()["initial_import_choice"], "skipped")
        self.assertEqual(self.store.meta()["last_success"], "2026-10-25T12:00:00+08:00")
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_clues_fallback_ignores_unfinished_activities(self):
        old_day = (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=20)).isoformat()
        self.store.upsert_remote([
            self.item(looki_id="old-evening", date=old_day, start=f"{old_day}T20:00:00+08:00", end=f"{old_day}T21:00:00+08:00", title="旧晚餐"),
            self.item(looki_id="old-task", date=old_day, start=f"{old_day}T21:00:00+08:00", end=f"{old_day}T22:00:00+08:00", title="补作业"),
        ])
        future_day = (datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)).isoformat()
        future = self.store.save({
            "date": future_day,
            "start": f"{future_day}T23:50:00+08:00",
            "end": f"{future_day}T23:59:00+08:00",
            "title": "尚未开始的补录",
            "description": "手工补录内容",
            "keywords": ["旧晚餐"],
        })
        self.store.add_correction(future, "未来活动尚未发生")

        lines = self.store.clues(30, 6, "Asia/Shanghai")

        self.assertFalse(any("未来活动尚未发生" in line for line in lines))
        self.assertFalse(any("晚间回顾" in line for line in lines))
        self.assertFalse(any("记录里常见的事" in line for line in lines))

    def test_manual_and_correction_clues_do_not_claim_hardware_origin(self):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        day = now.date().isoformat()
        item_id = self.store.save({
            "date": day,
            "start": (now - timedelta(hours=2)).isoformat(),
            "end": (now - timedelta(hours=1)).isoformat(),
            "title": "手工补录散步",
            "description": "沿河边走了一圈",
            "keywords": ["散步"],
        })
        self.store.add_correction(item_id, "同行的是同学")

        lines = self.store.clues(7, 6, "Asia/Shanghai")
        text = "\n".join(lines)

        self.assertIn("[手工补录]", text)
        self.assertIn("对话校正", text)
        self.assertNotIn("[Looki 记录]", text)

    def test_clues_timezone_param_used(self):
        """clues() 使用传入 timezone 而非服务器本地时区"""
        from datetime import date, timedelta
        # 直接构造今天日期（Asia/Shanghai）写入数据，验证 clues 能读到
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        for i in range(1, 4):
            day = today - timedelta(days=i)
            self.store.upsert_remote([self.item(
                looki_id=f"tz-{i}",
                date=day.isoformat(),
                start=f"{day.isoformat()}T21:00:00+08:00",
                end=f"{day.isoformat()}T22:30:00+08:00",
            )])
        clues = self.store.clues(7, 6, "Asia/Shanghai")
        self.assertIsInstance(clues, list)
        self.assertTrue(any("晚间仍有活动记录" in c for c in clues))

    def test_corrections_preserve_history_and_original_fields(self):
        self.store.upsert_remote([self.item(description="用户在宿舍", audio_summary="原始音频摘要")])
        record = self.store.query("2026-10-25", "2026-10-25")[0]
        self.store.add_correction(record["id"], "实际地点是教室，不是宿舍")
        self.store.add_correction(record["id"], "当天上的是高等数学课")
        row = self.store.query("2026-10-25", "2026-10-25")[0]
        self.assertEqual(row["description"], "用户在宿舍")
        self.assertEqual(row["audio_summary"], "原始音频摘要")
        self.assertEqual([c["note"] for c in row["corrections"]], ["实际地点是教室，不是宿舍", "当天上的是高等数学课"])
        self.store.upsert_remote([self.item(description="远端描述", audio_summary="原始音频摘要")])
        row = self.store.query("2026-10-25", "2026-10-25")[0]
        self.assertEqual(len(row["corrections"]), 2)
        self.assertEqual(row["description"], "远端描述")

    def test_correction_validation_and_deleted_record(self):
        self.store.upsert_remote([self.item()])
        item_id = self.store.query("2026-10-25", "2026-10-25")[0]["id"]
        for note in ("", "   ", "x" * 2001):
            with self.subTest(note_length=len(note)), self.assertRaises(ValueError):
                self.store.add_correction(item_id, note)
        with self.assertRaises(ValueError):
            self.store.add_correction("missing", "不存在的记录")
        self.store.delete(item_id)
        with self.assertRaises(ValueError):
            self.store.add_correction(item_id, "已删除记录")

    def test_clues_include_corrections(self):
        from zoneinfo import ZoneInfo
        # 固定使用已结束的昨天，避免凌晨运行时把今天 09 点的活动误判成未来记录。
        day = (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)).isoformat()
        self.store.upsert_remote([self.item(date=day, start=f"{day}T09:00:00+08:00", end=f"{day}T10:00:00+08:00")])
        item_id = self.store.query(day, day)[0]["id"]
        self.store.add_correction(item_id, "实际地点是教室，不是宿舍")
        clues = self.store.clues(7, 3, "Asia/Shanghai")
        self.assertTrue(any("对话校正：实际地点是教室，不是宿舍" in clue for clue in clues))

    def test_clues_prioritize_recent_ended_activities(self):
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        recent_start, recent_end = now - timedelta(hours=2), now - timedelta(hours=1)
        older_start, older_end = now - timedelta(days=1, hours=1), now - timedelta(days=1)
        self.store.upsert_remote([self.item(
            looki_id="older", date=older_start.date().isoformat(), start=older_start.isoformat(), end=older_end.isoformat(),
            title="早餐", description="在厨房做饭",
        )])
        self.store.save({
            "date": recent_start.date().isoformat(), "start": recent_start.isoformat(), "end": recent_end.isoformat(),
            "title": "晨跑", "keywords": [], "description": "沿河跑步",
        })
        clues = self.store.clues(7, 3, "Asia/Shanghai")
        self.assertEqual(len(clues), 2)
        self.assertIn("《晨跑》[手工补录]：沿河跑步", clues[0])
        self.assertIn("《早餐》[Looki 记录]：在厨房做饭", clues[1])

    def test_clues_exclude_future_or_unfinished_activities(self):
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        self.store.save({
            "date": now.date().isoformat(), "start": (now - timedelta(minutes=10)).isoformat(),
            "end": (now + timedelta(minutes=50)).isoformat(), "title": "进行中的活动", "keywords": [],
        })
        self.assertEqual(self.store.clues(7, 3, "Asia/Shanghai"), [])

    def test_clues_show_latest_correction_inline(self):
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        start, end = now - timedelta(hours=2), now - timedelta(hours=1)
        self.store.upsert_remote([self.item(date=start.date().isoformat(), start=start.isoformat(), end=end.isoformat())])
        item_id = self.store.query(start.date().isoformat(), start.date().isoformat())[0]["id"]
        self.store.add_correction(item_id, "第一次校正")
        self.store.add_correction(item_id, "第二次校正")
        activity = next(clue for clue in self.store.clues(7, 3, "Asia/Shanghai") if "《阅读》" in clue)
        self.assertIn("第二次校正", activity)
        self.assertNotIn("第一次校正", activity)

    def test_clues_respect_limit(self):
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        for offset in range(4):
            start, end = now - timedelta(hours=offset + 2), now - timedelta(hours=offset + 1)
            self.store.upsert_remote([self.item(looki_id=f"limit-{offset}", date=start.date().isoformat(), start=start.isoformat(), end=end.isoformat())])
        self.assertEqual(len(self.store.clues(7, 2, "Asia/Shanghai")), 2)

    def test_correction_tool_permissions_and_query_payload(self):
        from types import SimpleNamespace
        from astrbot.core.agent.run_context import ContextWrapper
        from astrbot_plugin_looki_companion.main import AddScheduleCorrectionTool, QueryScheduleTool

        correction_tool = AddScheduleCorrectionTool()
        self.assertIn("中性", correction_tool.description)
        self.assertNotIn("第一人称口吻概括", correction_tool.description)
        self.assertIn("不逐字照抄", correction_tool.description)
        self.store.upsert_remote([self.item(description="用户在宿舍")])
        plugin = SimpleNamespace(store=self.store, config={"allow_group_query": False})
        registry = SimpleNamespace(
            get_registered_star=lambda _: SimpleNamespace(star_cls=plugin)
        )

        def context(admin):
            event = SimpleNamespace(is_admin=lambda: admin, is_private_chat=lambda: True)
            return ContextWrapper(context=SimpleNamespace(context=registry, event=event))

        denied = asyncio.run(AddScheduleCorrectionTool().call(context(False), schedule_id="anything", note="不应写入"))
        self.assertIn("只有 AstrBot 管理员", denied)
        record = self.store.query("2026-10-25", "2026-10-25")[0]
        saved = asyncio.run(AddScheduleCorrectionTool().call(context(True), schedule_id=record["id"], note="实际地点是教室，不是宿舍"))
        self.assertTrue(json.loads(saved)["saved"])
        result = asyncio.run(QueryScheduleTool().call(context(False), start_date="2026-10-25", end_date="2026-10-25"))
        item = json.loads(result)["items"][0]
        self.assertEqual(item["id"], record["id"])
        self.assertEqual(item["description"], "用户在宿舍")
        self.assertEqual([c["note"] for c in item["corrections"]], ["实际地点是教室，不是宿舍"])

    def test_date_range_keyword_uses_full_life_search(self):
        """带明确日期时，关键词也要命中描述等生活记录，而不是只查标题"""
        from types import SimpleNamespace
        from astrbot.core.agent.run_context import ContextWrapper
        from astrbot_plugin_looki_companion.intent_service import LookiIntentService
        from astrbot_plugin_looki_companion.main import QueryScheduleTool

        self.store.upsert_remote([self.item(description="学校发了圣诞礼物", title="班级活动", keywords=["班级"])])
        plugin = SimpleNamespace(
            store=self.store,
            config={"allow_group_query": False},
            intent=LookiIntentService(self.store, "Asia/Shanghai"),
        )
        registry = SimpleNamespace(get_registered_star=lambda _: SimpleNamespace(star_cls=plugin))
        event = SimpleNamespace(is_private_chat=lambda: True)
        context = ContextWrapper(context=SimpleNamespace(context=registry, event=event))

        payload = json.loads(asyncio.run(QueryScheduleTool().call(
            context, query="礼物", start_date="2026-10-25", end_date="2026-10-25"
        )))

        self.assertTrue(payload["keyword_matched"])
        self.assertEqual(payload["items"][0]["title"], "班级活动")

    def _insert_raw(self, looki_id, created_at, *, user_edited=0, source="looki", title="阅读", duration=(21, 0, 22, 30), deleted=0):
        """绕过内容去重直接写库，模拟历史版本已经产生的重复记录。"""
        day = "2026-10-25"
        start = f"{day}T{duration[0]:02d}:{duration[1]:02d}:00+08:00"
        end = f"{day}T{duration[2]:02d}:{duration[3]:02d}:00+08:00"
        self.store.db.execute(
            "INSERT INTO schedules (id,dedupe_key,looki_id,day,start_at,end_at,title,description,audio_summary,keywords,source,user_edited,deleted,raw_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"id-{looki_id}", f"looki:{looki_id}", looki_id, day, start, end, title, "", "", "[]", source, user_edited, deleted, None, created_at, created_at),
        )
        self.store.db.commit()

    def test_same_activity_with_new_looki_id_is_not_duplicated(self):
        """Looki 对同一活动更换 moment id 时，不得重复入库"""
        self.store.upsert_remote([self.item()])
        self.assertEqual(self.store.upsert_remote([self.item(looki_id="remote-2")]), 0)
        rows = self.store.query("2026-10-25", "2026-10-25")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["looki_id"], "remote-2")

    def test_new_looki_id_keeps_manual_edits(self):
        """换 id 后仍要保留人工修改过的摘要和其他字段"""
        self.store.upsert_remote([self.item(audio_summary="原始摘要")])
        record = self.store.query("2026-10-25", "2026-10-25")[0]
        self.store.save({
            "date": record["date"],
            "start": record["start_at"],
            "end": record["end_at"],
            "title": record["title"],
            "keywords": [],
            "audio_summary": "人工修订",
        }, record["id"])

        self.store.upsert_remote([self.item(looki_id="remote-2", audio_summary="远端新摘要")])
        rows = self.store.query("2026-10-25", "2026-10-25")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["audio_summary"], "人工修订")
        self.assertEqual(rows[0]["looki_id"], "remote-2")

    def test_deleted_record_is_not_revived_by_new_looki_id(self):
        """人工删除过的记录不会因远端换了 id 而复活"""
        self.store.upsert_remote([self.item()])
        record = self.store.query("2026-10-25", "2026-10-25")[0]
        self.store.delete(record["id"])
        self.store.upsert_remote([self.item(looki_id="remote-2")])
        self.assertEqual(self.store.query("2026-10-25", "2026-10-25"), [])
        total = self.store.db.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]
        self.assertEqual(total, 1)

    def test_legacy_duplicate_rows_are_merged(self):
        """存量重复：保留最早一条，其余纯 Looki 记录软删除"""
        self._insert_raw("a", "2026-10-27T17:02:05+08:00")
        self._insert_raw("b", "2026-10-27T19:14:51+08:00")
        self.assertEqual(self.store.merge_content_duplicates(), 1)
        rows = self.store.query("2026-10-25", "2026-10-25")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["looki_id"], "a")

    def test_merge_keeps_user_edited_row(self):
        """存量重复中有人工编辑记录时，保留人工那条"""
        self._insert_raw("a", "2026-10-27T17:02:05+08:00")
        self._insert_raw("b", "2026-10-27T19:14:51+08:00", user_edited=1)
        self.assertEqual(self.store.merge_content_duplicates(), 1)
        rows = self.store.query("2026-10-25", "2026-10-25")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["looki_id"], "b")

    def test_merge_never_deletes_manual_rows(self):
        """人工新建记录即使在同内容组内也不会被合掉"""
        self._insert_raw("a", "2026-10-27T17:02:05+08:00")
        self._insert_raw("m", "2026-10-27T19:14:51+08:00", source="manual", user_edited=1)
        self.assertEqual(self.store.merge_content_duplicates(), 1)
        rows = self.store.query("2026-10-25", "2026-10-25")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "manual")

    def test_upsert_strips_task_box_and_heading_markers(self):
        """Looki 文本里的 Markdown 任务框与标题符号不得进入本地日程"""
        self.store.upsert_remote([self.item(title="# 阅读", description="- [ ] 打开课本", audio_summary="## 摘要\n- 记录")])
        row = self.store.query("2026-10-25", "2026-10-25")[0]
        self.assertEqual(row["title"], "阅读")
        self.assertEqual(row["description"], "打开课本")
        self.assertEqual(row["audio_summary"], "摘要\n- 记录")

    def test_resplit_looki_segments_are_merged(self):
        """Looki 先下发短片段、再合并成长片段时，只保留跨度最大的一条"""
        self._insert_raw("d", "2026-10-25T08:44:00+08:00", title="晨间自习与同桌讨论", duration=(7, 14, 7, 42))
        self._insert_raw("a", "2026-10-25T10:00:00+08:00", title="早晨抵达教室", duration=(7, 8, 7, 12))
        self._insert_raw("b", "2026-10-25T23:13:00+08:00", title="清晨教室里的漫长自习", duration=(7, 8, 11, 59))
        self._insert_raw("c", "2026-10-25T14:15:00+08:00", title="教室里的漫长自习", duration=(7, 14, 11, 59))
        self.assertEqual(self.store.merge_superseded_looki_rows(), 3)
        rows = self.store.query("2026-10-25", "2026-10-25")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["looki_id"], "b")

    def test_adjacent_activities_are_not_merged(self):
        """首尾相接但没有重叠的活动不得合并"""
        self._insert_raw("a", "2026-10-25T10:00:00+08:00", duration=(9, 0, 10, 0))
        self._insert_raw("b", "2026-10-25T10:10:00+08:00", title="另一件事", duration=(10, 0, 11, 0))
        self.assertEqual(self.store.merge_superseded_looki_rows(), 0)
        self.assertEqual(len(self.store.query("2026-10-25", "2026-10-25")), 2)

    def test_resplit_merge_skips_edited_and_manual_rows(self):
        """人工编辑过或手工新建的记录不参与重切分合并"""
        self._insert_raw("a", "2026-10-25T10:00:00+08:00", duration=(7, 8, 7, 12), user_edited=1)
        self._insert_raw("b", "2026-10-25T10:30:00+08:00", duration=(7, 8, 11, 59))
        self._insert_raw("m", "2026-10-25T10:40:00+08:00", duration=(7, 14, 11, 59), source="manual", user_edited=1)
        self.assertEqual(self.store.merge_superseded_looki_rows(), 0)
        self.assertEqual(len(self.store.query("2026-10-25", "2026-10-25")), 3)

    def test_resplit_merge_keeps_same_end_different_titles(self):
        """结束时刻相同但标题无关的两段活动不得当成重切分片段合并"""
        self._insert_raw("a", "2026-10-25T09:00:00+08:00", title="数学课", duration=(9, 0, 10, 0))
        self._insert_raw("b", "2026-10-25T09:30:00+08:00", title="课间休息", duration=(9, 30, 10, 0))
        self.assertEqual(self.store.merge_superseded_looki_rows(), 0)
        self.assertEqual(len(self.store.query("2026-10-25", "2026-10-25")), 2)

    def test_ambiguous_same_start_rows_are_not_guessed(self):
        """同一起点已有两条不同标题的 Looki 记录时，内容匹配必须放弃而不是挑一条"""
        self._insert_raw("a", "2026-10-25T09:00:00+08:00", title="数学课", duration=(9, 0, 10, 0))
        self._insert_raw("b", "2026-10-25T09:00:00+08:00", title="课间休息", duration=(9, 0, 9, 30))
        self.assertIsNone(self.store._content_match("2026-10-25", "2026-10-25T09:00:00+08:00", "英语课"))


    def test_raw_json_survives_sync_with_store_raw_disabled(self):
        """关闭原始 JSON 存储后再次同步，不应抹掉已保存的 raw_json"""
        self.store.upsert_remote([self.item()], store_raw=True)
        self.store.upsert_remote([self.item(keywords=["专注"])], store_raw=False)
        row = self.store.db.execute("SELECT raw_json FROM schedules WHERE looki_id='remote-1'").fetchone()
        self.assertIsNotNone(row["raw_json"])

    def test_content_match_ignores_timezone_offset(self):
        """人工记录用 UTC 提交、远端用 +08:00 时，仍识别为同一活动"""
        item_id = self.store.save({
            "date": "2026-10-25",
            "start": "2026-10-25T13:00:00+00:00",
            "end": "2026-10-25T14:30:00+00:00",
            "title": "阅读",
            "keywords": [],
        })
        self.store.upsert_remote([self.item()])
        rows = self.store.query("2026-10-25", "2026-10-25")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], item_id)
        self.assertEqual(rows[0]["looki_id"], "remote-1")
    def test_task_can_be_completed_and_cancelled_persistently(self):
        self.store.upsert_journals([{
            "id": "journal-task-toggle",
            "day": "2026-10-25",
            "type": "DIARY",
            "content": "今日建议\n- [ ] 整理资料",
            "schedule_id": "looki:task-toggle",
            "tasks": [{"body": "整理资料", "section": "今日建议", "source_line": "- [ ] 整理资料"}],
        }])
        task = self.store.tasks(only_pending=True)[0]

        self.store.set_task_done(task["id"], True, "2026-10-25")
        completed = self.store.tasks_done_on("2026-10-25")
        self.assertEqual(len(completed), 1)
        self.assertTrue(completed[0]["done"])
        self.assertEqual(completed[0]["done_day"], "2026-10-25")

        self.store.set_task_done(task["id"], False, "2026-10-25")
        pending = self.store.tasks(only_pending=True)
        self.assertEqual([row["id"] for row in pending], [task["id"]])
        self.assertFalse(pending[0]["done"])
        self.assertIsNone(pending[0]["done_day"])
        self.assertEqual(self.store.tasks_done_on("2026-10-25"), [])

        self.store.close()
        reopened = ScheduleStore(Path(self.temp.name) / "schedules.sqlite3")
        try:
            persisted = reopened.tasks()[0]
            self.assertFalse(persisted["done"])
            self.assertIsNone(persisted["done_day"])
        finally:
            reopened.close()


class TaskApiTests(unittest.TestCase):
    def setUp(self):
        from astrbot_plugin_looki_companion.main import LookiCompanionPlugin

        self.temp = tempfile.TemporaryDirectory()
        self.plugin = LookiCompanionPlugin.__new__(LookiCompanionPlugin)
        self.plugin.config = {"timezone": "Asia/Shanghai"}
        self.plugin.store = ScheduleStore(Path(self.temp.name) / "schedules.sqlite3")
        self.plugin.store.upsert_journals([{
            "id": "journal-api-toggle",
            "day": "2026-10-25",
            "type": "DIARY",
            "content": "今日建议\n- [ ] 整理资料",
            "schedule_id": "looki:task-toggle",
            "tasks": [{"body": "整理资料", "section": "今日建议", "source_line": "- [ ] 整理资料"}],
        }])
        self.task = self.plugin.store.tasks()[0]

    def tearDown(self):
        self.plugin.store.close()
        self.temp.cleanup()

    def call_tasks(self, query):
        with patch("astrbot_plugin_looki_companion.main.request", SimpleNamespace(query=query)), patch(
            "astrbot_plugin_looki_companion.main.json_response", side_effect=lambda data: data
        ), patch(
            "astrbot_plugin_looki_companion.main.error_response",
            side_effect=lambda message, status_code: {"error": message, "status_code": status_code},
        ):
            return asyncio.run(self.plugin.api_tasks())

    def test_day_query_includes_completed_tasks_for_detail_view(self):
        self.plugin.store.set_task_done(self.task["id"], True, "2026-10-25")

        result = self.call_tasks({"start": "2026-10-25", "end": "2026-10-25", "today": "2026-11-19"})

        self.assertEqual(result["pending"], [])
        self.assertEqual([task["id"] for task in result["completed"]], [self.task["id"]])
        self.assertEqual(result["completed"][0]["done_day"], "2026-10-25")

    def test_cancel_endpoint_clears_done_day(self):
        self.plugin.store.set_task_done(self.task["id"], True, "2026-10-25")
        with patch(
            "astrbot_plugin_looki_companion.main.request",
            SimpleNamespace(json=AsyncMock(return_value={"done": False})),
        ), patch("astrbot_plugin_looki_companion.main.json_response", side_effect=lambda data: data):
            result = asyncio.run(self.plugin.api_task_toggle(self.task["id"]))

        self.assertFalse(result["done"])
        self.assertIsNone(result["done_day"])
        restored = self.plugin.store.tasks()[0]
        self.assertFalse(restored["done"])
        self.assertIsNone(restored["done_day"])


class SchedulePageTaskTests(unittest.TestCase):
    def test_detail_checkbox_uses_same_toggle_handler_as_task_page(self):
        page = (Path(__file__).parents[1] / "pages" / "schedule" / "index.html").read_text(encoding="utf-8")
        detail = page[page.index("function taskList(tasks,"):page.index("function appendJournals")]
        page_row = page[page.index("function taskPageRow(task,"):page.index("async function loadTasks")]

        self.assertIn("cb.onchange=()=>toggleTask(task,cb)", detail)
        self.assertIn("cb.onchange=()=>toggleTask(task,cb)", page_row)
        self.assertIn("cb.checked=!!task.done", page_row)
        self.assertNotIn("if(cb.checked)toggleTask", page_row)

    def test_day_load_merges_pending_and_completed_and_cancel_has_no_modal(self):
        page = (Path(__file__).parents[1] / "pages" / "schedule" / "index.html").read_text(encoding="utf-8")
        load_day = page[page.index("async function loadDay()"):page.index("function journalText")]
        toggle = page[page.index("async function toggleTask"):page.index("async function locateTask")]

        self.assertIn("dayTasks=[...data.pending,...data.completed,...(data.deleted||[])]", load_day)
        self.assertNotIn("confirm(", toggle)

    def test_page_keeps_calendar_outline_and_looki_theme(self):
        page = (Path(__file__).parents[1] / "pages" / "schedule" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="calGrid"', page)
        self.assertIn("async function loadCalendar()", page)
        self.assertIn("Bot 纠正总结 · 以最新注记为准", page)
        self.assertIn('id="taskPanel"', page)
        self.assertIn("当天时间轴", page)
        self.assertIn('id="outline"', page)
        self.assertNotIn('id="dayTasks"', page)
        self.assertIn("Looki 日程", page)
        self.assertIn("RHINE LAB", page)
        self.assertIn("正式页莱茵生命 A 版主题", page)

    def test_settings_view_uses_grouped_workbench(self):
        page = (Path(__file__).parents[1] / "pages" / "schedule" / "index.html").read_text(encoding="utf-8")

        self.assertIn('class="card rhine-config" id="viewSettings"', page)
        self.assertIn('class="config-page-intro"', page)
        self.assertIn('class="config-section-nav" id="setNav"', page)
        for panel in ("configGeneral", "configMedia"):
            self.assertIn(f'class="config-fold-panel" id="{panel}"', page)
            self.assertNotIn(f'class="config-fold-panel" id="{panel}" open', page)
        self.assertEqual(page.count('class="config-subfold"'), 2)
        self.assertIn("--rhine:#d6f65a", page)
        self.assertIn("--rhine-bg:#e7eae4", page)
        self.assertRegex(page, r"#viewSettings \.config-grid\{[^}]*grid-template-columns:repeat\(12")
        for sec in ("sync", "access", "journal", "reminder", "image", "privacy"):
            self.assertIn(f'id="sec-{sec}"', page)
        for field in ("cfgKey", "cfgAuto", "cfgScope", "cfgWhitelistUsers", "cfgShowJournals", "cfgImageCount",
                      "cfgImageOriginal", "cfgImageApiKey", "cfgRaw", "cfgCancel", "cfgSave"):
            self.assertIn(f'id="{field}"', page)
        self.assertIn("if(panel)openConfigPanel(panel)", page)
        self.assertIn("other.open=other===panel", page)
        self.assertIn("setActiveSection('sync')", page)

    def test_settings_include_single_task_reminder_controls(self):
        page = (Path(__file__).parents[1] / "pages" / "schedule" / "index.html").read_text(encoding="utf-8")
        for field in ("cfgTaskReminderInterval", "cfgTaskAutoDelete", "cfgTaskCompletion"):
            self.assertIn(f'id="{field}"', page)
        for key in ("task_reminder_interval_minutes", "task_auto_delete_low_value", "task_completion_reply_enabled"):
            self.assertIn(key, page)
        self.assertIn('id="sec-reminder"', page)

    def test_edge_toggles_stay_on_mobile_and_empty_corrections_hidden(self):
        page = (Path(__file__).parents[1] / "pages" / "schedule" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="edgeLeft"', page)
        self.assertIn('id="edgeRight"', page)
        self.assertRegex(page, r"@media\(max-width:960px\)\{[\s\S]*?\.edge\{display:grid")
        self.assertIn("sideEdge.onclick=()=>setSide(!sideOpen)", page)
        self.assertIn("taskEdge.onclick=()=>setTask(!taskOpen)", page)
        self.assertIn("if(item.corrections&&item.corrections.length)", page)

    def test_vendor_files_have_matching_checksums_and_upstream_licenses(self):
        root = Path(__file__).parents[1]
        vendor = root / "pages" / "schedule" / "vendor"
        manifest = json.loads((vendor / "licenses.json").read_text(encoding="utf-8"))

        self.assertEqual(set(manifest["files"]), {"marked.umd.js", "purify.min.js"})
        for filename, entry in manifest["files"].items():
            payload = (vendor / filename).read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), entry["sha256"])
            self.assertTrue((vendor / entry["license_file"]).is_file())
            self.assertIn(entry["version"], entry["source"])
        self.assertIn('vendor/marked.umd.js', (root / "pages" / "schedule" / "index.html").read_text(encoding="utf-8"))
        self.assertIn('vendor/purify.min.js', (root / "pages" / "schedule" / "index.html").read_text(encoding="utf-8"))

    def test_release_dependencies_and_documentation_links_are_complete(self):
        root = Path(__file__).parents[1]
        requirements = (root / "requirements.txt").read_text(encoding="utf-8")
        readme = (root / "README.md").read_text(encoding="utf-8")
        metadata = (root / "metadata.yaml").read_text(encoding="utf-8")

        for dependency in ("httpx", "pydantic", "Pillow"):
            self.assertIn(dependency, requirements)
        self.assertIn(requirements.strip(), readme)
        self.assertNotIn("yourname", metadata)
        self.assertNotIn("RabbitZheng4424", metadata + readme)
        self.assertIn("repo: https://github.com/Reybit4424/astrbot_plugin_looki_companion", metadata)
        self.assertIn("version: 0.9.0", metadata)
        self.assertIn('VERSION = "0.9.0"', (root / "main.py").read_text(encoding="utf-8"))
        self.assertNotIn("assets/aniclock", readme)
        import re
        for target in re.findall(r"\[[^]]*\]\(([^)]+)\)", readme):
            if "://" not in target and not target.startswith("#"):
                self.assertTrue((root / target.split("#", 1)[0]).exists(), target)


class EventPayloadTests(unittest.TestCase):
    def test_localize_event_converts_utc_to_configured_timezone(self):
        from astrbot_plugin_looki_companion.main import _localize_event

        payload = {"date": "2026-10-25", "start": "2026-10-25T13:00:00.000Z", "end": "2026-10-25T14:30:00.000Z"}
        _localize_event(payload, "Asia/Shanghai")
        self.assertEqual(payload["start"], "2026-10-25T21:00:00+08:00")
        self.assertEqual(payload["end"], "2026-10-25T22:30:00+08:00")


class JournalTaskEditTests(unittest.TestCase):
    """阶段二：日志本地编辑保护、任务软删/恢复/编辑/排序、纠正 CRUD"""

    DAY = "2026-10-25"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ScheduleStore(Path(self.temp.name) / "schedules.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _journal(self, journal_id="j-1", content="早上\n- [ ] 买牛奶\n- [x] 看书\n", **kw):
        value = {
            "id": journal_id,
            "type": "journal",
            "day": self.DAY,
            "title": "标题",
            "description": "描述",
            "content": content,
            "recorded_at": "2026-10-25T10:00:00+08:00",
            "match_state": "matched",
            "schedule_id": "s-1",
        }
        value.update(kw)
        from astrbot_plugin_looki_companion.journal_service import extract_journal_tasks
        if "tasks" not in kw:
            value["tasks"] = extract_journal_tasks(value["content"])
        return value

    def test_upsert_journals_extracts_task_variants(self):
        self.store.upsert_journals([self._journal(content="- [ ] a\n* [ ] b\n+ [x] c\n- [X] d\n")])
        tasks = self.store.tasks(self.DAY, self.DAY)
        bodies = {task["body"]: task["done"] for task in tasks}
        self.assertEqual(bodies, {"a": False, "b": False, "c": True, "d": True})

    def test_user_edited_journal_survives_resync(self):
        self.store.upsert_journals([self._journal()])
        self.store.update_journal("j-1", {"title": "本地标题", "content": "本地正文"})
        self.store.upsert_journals([self._journal(title="远端标题", content="远端正文")])
        row = self.store.journals(self.DAY, self.DAY)[0]
        self.assertEqual(row["title"], "本地标题")
        self.assertTrue(row["user_edited"])

    def test_deleted_task_marker_physically_removed(self):
        self.store.upsert_journals([self._journal()])
        before = self.store.tasks(self.DAY, self.DAY)
        self.assertEqual(len(before), 2)
        # 用户编辑 Markdown，删掉「买牛奶」那行
        from astrbot_plugin_looki_companion.journal_service import extract_journal_tasks
        self.store.update_journal("j-1", {"content": "- [x] 看书\n"}, extract_journal_tasks("- [x] 看书\n"))
        tasks = self.store.tasks(self.DAY, self.DAY)
        self.assertEqual([task["body"] for task in tasks], ["看书"])

    def test_remote_sync_preserves_legacy_non_checkbox_task(self):
        self.store.upsert_journals([self._journal(content="1. 买牛奶\n", tasks=[{
            "body": "买牛奶", "section": "今日建议", "source_line": "1. 买牛奶", "done": True,
        }])])
        self.store.upsert_journals([self._journal(content="今日建议\n1. 买牛奶\n", tasks=[])])
        tasks = self.store.tasks(self.DAY, self.DAY, include_deleted=True)
        self.assertEqual([task["body"] for task in tasks], ["买牛奶"])
        self.assertTrue(tasks[0]["done"])

    def test_remote_sync_removes_missing_checkbox_task(self):
        self.store.upsert_journals([self._journal(content="- [x] 买牛奶\n")])
        self.store.upsert_journals([self._journal(content="买牛奶\n", tasks=[])])
        self.assertEqual(self.store.tasks(self.DAY, self.DAY, include_deleted=True), [])

    def test_soft_delete_restore_and_aggregation(self):
        self.store.upsert_journals([self._journal(content="- [ ] a\n")])
        task = self.store.tasks(self.DAY, self.DAY)[0]
        self.store.delete_task(task["id"])
        self.assertEqual(self.store.tasks(self.DAY, self.DAY), [])
        deleted = self.store.tasks(self.DAY, self.DAY, include_deleted=True)
        self.assertEqual(len(deleted), 1)
        self.assertTrue(deleted[0]["deleted"])
        self.store.restore_task(task["id"])
        self.assertEqual(len(self.store.tasks(self.DAY, self.DAY)), 1)

    def test_deleted_task_excluded_from_pending_and_reminders(self):
        self.store.upsert_journals([self._journal(content="- [ ] a\n")])
        task = self.store.tasks(self.DAY, self.DAY)[0]
        self.store.delete_task(task["id"])
        self.assertEqual(self.store.tasks_today_pending(self.DAY), [])
        self.assertEqual(self.store.pending_task_reminders_for("u1", 10), [])

    def test_only_unreminded_task_can_be_automatically_deleted(self):
        self.store.upsert_journals([self._journal(content="- [ ] a\n- [ ] b\n")])
        reminded, unreminded = self.store.tasks(self.DAY, self.DAY)
        self.store.mark_tasks_reminded_for([reminded["id"]], "u1")
        self.assertFalse(self.store.delete_unreminded_task(reminded["id"]))
        self.assertTrue(self.store.delete_unreminded_task(unreminded["id"]))
        rows = self.store.tasks(self.DAY, self.DAY, include_deleted=True)
        self.assertFalse(next(task for task in rows if task["id"] == reminded["id"])["deleted"])
        self.assertTrue(next(task for task in rows if task["id"] == unreminded["id"])["deleted"])

    def test_completion_reply_completes_latest_unfinished_reminded_task(self):
        self.store.upsert_journals([self._journal(content="- [ ] a\n- [ ] b\n")])
        first, latest = self.store.tasks(self.DAY, self.DAY)
        self.store.mark_tasks_reminded_for([first["id"]], "u1")
        self.store.mark_tasks_reminded_for([latest["id"]], "u1")
        self.store.mark_tasks_reminded_for([first["id"]], "u2")

        completed = self.store.complete_latest_reminded_task("u1", "2026-11-21")
        self.assertEqual(completed["id"], latest["id"])
        self.assertEqual(completed["done_day"], "2026-11-21")
        self.assertTrue(completed["done"])
        self.assertIsNone(self.store.complete_latest_reminded_task("u3"))
        remaining = self.store.tasks(self.DAY, self.DAY, only_pending=True)
        self.assertEqual([task["id"] for task in remaining], [first["id"]])

    def test_update_task_body_and_move(self):
        self.store.upsert_journals([self._journal(content="- [ ] a\n- [ ] b\n- [ ] c\n")])
        tasks = self.store.tasks(self.DAY, self.DAY)
        self.store.update_task(tasks[0]["id"], "a 修改")
        self.assertEqual(self.store.tasks(self.DAY, self.DAY)[0]["body"], "a 修改")
        # 初始顺序按 sort_order 排列，下移一格即可验证重排生效
        first_body = "a 修改"
        self.store.move_task(tasks[0]["id"], "down")
        bodies = [t["body"] for t in self.store.tasks(self.DAY, self.DAY)]
        self.assertEqual(bodies.index(first_body), 1)
        # 旧库中 sort_order 全为 0 时，展示与移动也必须使用同一种同分顺序。
        with self.store.db:
            self.store.db.execute("UPDATE looki_tasks SET sort_order=0")
        legacy_first = self.store.tasks(self.DAY, self.DAY)[0]
        self.store.move_task(legacy_first["id"], "down")
        bodies = [t["body"] for t in self.store.tasks(self.DAY, self.DAY)]
        self.assertEqual(bodies.index(legacy_first["body"]), 1)
        with self.assertRaises(ValueError):
            self.store.update_task(tasks[0]["id"], "")
        with self.assertRaises(ValueError):
            self.store.move_task(tasks[0]["id"], "left")

    def test_correction_crud(self):
        item_id = self.store.save({"date": self.DAY, "start": "2026-10-25T09:00:00+08:00", "end": "2026-10-25T10:00:00+08:00", "title": "活动", "keywords": []})
        added = self.store.add_correction(item_id, "第一条")
        self.assertEqual(added["note"], "第一条")
        self.store.update_correction(added["id"], "修改后")
        row = self.store.query(self.DAY, self.DAY)[0]
        self.assertEqual(row["corrections"][0]["note"], "修改后")
        self.store.delete_correction(added["id"])
        self.assertEqual(self.store.query(self.DAY, self.DAY)[0]["corrections"], [])
        with self.assertRaises(ValueError):
            self.store.update_correction(added["id"], "x")
        with self.assertRaises(ValueError):
            self.store.add_correction(item_id, " ")


class InitialImportTests(unittest.TestCase):
    def setUp(self):
        from astrbot_plugin_looki_companion.main import LookiCompanionPlugin

        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "schedules.sqlite3"
        self.plugin = LookiCompanionPlugin.__new__(LookiCompanionPlugin)
        self.plugin.config = {
            "looki_api_key": "same-key",
            "looki_base_url": "https://open.looki.tech/api/v1",
            "backfill_days": 7,
            "timezone": "Asia/Shanghai",
            "auto_sync": False,
            "sync_interval_minutes": 5,
            "image_send_original": True,
        }
        self.plugin.context = SimpleNamespace(get_all_providers=lambda: [])
        self.plugin.store = ScheduleStore(self.db_path)
        from astrbot_plugin_looki_companion.intent_service import LookiIntentService

        self.plugin.intent = LookiIntentService(self.plugin.store, "Asia/Shanghai")
        self.plugin._sync_lock = asyncio.Lock()
        self.plugin._sync_task = None
        self.plugin._stopping = False
        self.plugin.schedule_provider = SimpleNamespace(fetch=AsyncMock(return_value=[]))

    def tearDown(self):
        self.plugin.store.close()
        self.temp.cleanup()

    async def choose(self, choice):
        with patch(
            "astrbot_plugin_looki_companion.main.request",
            SimpleNamespace(json=AsyncMock(return_value={"choice": choice}), query={}),
        ), patch(
            "astrbot_plugin_looki_companion.main.json_response",
            side_effect=lambda data: data,
        ), patch(
            "astrbot_plugin_looki_companion.main.error_response",
            side_effect=lambda message, status_code: {"error": message, "status_code": status_code},
        ):
            return await self.plugin.api_initial_import()

    def state(self):
        return asyncio.run(self.plugin._initial_import_state())

    def test_no_key_does_not_prompt_and_automatic_sync_waits_for_choice(self):
        async def run():
            self.plugin.config["looki_api_key"] = ""
            self.assertFalse((await self.plugin._initial_import_state())["initial_import_required"])

            self.plugin.config["looki_api_key"] = "same-key"
            result = await self.plugin.sync_once(automatic=True)
            self.assertTrue(result["skipped"])
            self.assertTrue(result["initial_import_required"])
            self.plugin.schedule_provider.fetch.assert_not_awaited()
            self.assertNotIn("last_attempt", self.plugin.store.meta())

        asyncio.run(run())

    def test_skip_is_global_permanent_and_key_changes_do_not_prompt_again(self):
        result = asyncio.run(self.choose("skip"))
        self.assertTrue(result["saved"])
        self.assertEqual(result["initial_import_choice"], "skipped")
        self.plugin.schedule_provider.fetch.assert_not_awaited()

        # 重新打开同一个数据库模拟插件重载，选择仍然保留。
        self.plugin.store.close()
        self.plugin.store = ScheduleStore(self.db_path)
        for key in ("", "same-key", "different-key", "same-key"):
            self.plugin.config["looki_api_key"] = key
            self.assertFalse(self.state()["initial_import_required"])
            self.assertEqual(self.state()["initial_import_choice"], "skipped")
        self.plugin.schedule_provider.fetch.assert_not_awaited()

    def test_existing_successfully_synced_user_is_not_prompted(self):
        self.plugin.store.set_meta("last_success", datetime.now().astimezone().isoformat())
        self.assertFalse(self.state()["initial_import_required"])
        result = asyncio.run(self.choose("import"))
        self.assertTrue(result["completed"])
        self.plugin.schedule_provider.fetch.assert_not_awaited()

    def test_import_uses_existing_sync_and_only_marks_choice_after_success(self):
        self.plugin.schedule_provider.fetch = AsyncMock(return_value=[{
            "looki_id": "initial-1",
            "date": datetime.now().astimezone().date().isoformat(),
            "start": "2026-11-17T09:00:00+08:00",
            "end": "2026-11-17T10:00:00+08:00",
            "title": "首次导入",
            "keywords": [],
        }])
        result = asyncio.run(self.choose("import"))
        self.assertEqual(result["inserted"], 1)
        self.assertFalse(result["initial_import_required"])
        self.assertEqual(result["initial_import_choice"], "imported")
        self.assertIn("last_success", self.plugin.store.meta())
        start_day, end_day, timezone = self.plugin.schedule_provider.fetch.await_args.args
        self.assertEqual((end_day - start_day).days, 6)
        self.assertEqual(timezone, "Asia/Shanghai")

        # 完成后再次点击不会重新请求，也不会改变首次选择。
        self.plugin.schedule_provider.fetch.reset_mock()
        again = asyncio.run(self.choose("skip"))
        self.assertTrue(again["completed"])
        self.assertEqual(self.state()["initial_import_choice"], "imported")
        self.plugin.schedule_provider.fetch.assert_not_awaited()

    def test_failed_import_keeps_prompt_and_does_not_mark_success(self):
        self.plugin.schedule_provider.fetch = AsyncMock(side_effect=RuntimeError("网络失败"))
        result = asyncio.run(self.choose("import"))
        self.assertIn("网络失败", result["error"])
        self.assertTrue(result["initial_import_required"])
        meta = self.plugin.store.meta()
        self.assertNotIn("last_success", meta)
        self.assertNotIn("initial_import_choice", meta)

    def test_concurrent_click_does_not_start_second_remote_request(self):
        async def run():
            await self.plugin._sync_lock.acquire()
            try:
                result = await self.choose("import")
            finally:
                self.plugin._sync_lock.release()
            self.assertTrue(result["skipped"])
            self.assertTrue(result["initial_import_required"])
            self.plugin.schedule_provider.fetch.assert_not_awaited()

        asyncio.run(run())

    def test_saving_first_key_does_not_fetch_even_with_auto_sync_enabled(self):
        async def run():
            self.plugin.config["looki_api_key"] = ""
            provider = SimpleNamespace(fetch=AsyncMock(return_value=[]))
            with patch(
                "astrbot_plugin_looki_companion.main.LookiScheduleProvider",
                return_value=provider,
            ), patch(
                "astrbot_plugin_looki_companion.main.request",
                SimpleNamespace(json=AsyncMock(return_value={
                    "looki_api_key": "first-key",
                    "auto_sync": True,
                    "sync_interval_minutes": 5,
                }), query={}),
            ), patch(
                "astrbot_plugin_looki_companion.main.json_response",
                side_effect=lambda data: data,
            ):
                result = await self.plugin.api_set_config()
                await asyncio.sleep(0)
            self.assertTrue(result["initial_import_required"])
            provider.fetch.assert_not_awaited()
            task = self.plugin._sync_task
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())


class SyncReliabilityTests(unittest.TestCase):
    def setUp(self):
        from astrbot_plugin_looki_companion.intent_service import LookiIntentService
        from astrbot_plugin_looki_companion.main import LookiCompanionPlugin

        self.temp = tempfile.TemporaryDirectory()
        self.plugin = LookiCompanionPlugin.__new__(LookiCompanionPlugin)
        self.plugin.config = {"backfill_days": 7, "timezone": "Asia/Shanghai", "task_expire_days": 3650}
        self.plugin.store = ScheduleStore(Path(self.temp.name) / "schedules.sqlite3")
        self.plugin.intent = LookiIntentService(self.plugin.store, "Asia/Shanghai")
        self.plugin.context = SimpleNamespace(send_message=AsyncMock(return_value=True))
        self.plugin.schedule_provider = SimpleNamespace(fetch=AsyncMock(return_value=[]))
        self.plugin._sync_lock = asyncio.Lock()

    def tearDown(self):
        self.plugin.store.close()
        self.temp.cleanup()

    def _journal(self, day, journal_id, content="记录", tasks=None):
        item = {"id": journal_id, "day": day, "type": "DIARY", "content": content, "recorded_at": ""}
        if tasks is not None:
            item["tasks"] = tasks
        return item

    def _task_journal(self):
        item = self._journal(
            "2026-11-20",
            "j-task",
            "今日建议\n- [ ] 买牛奶",
            tasks=[{"body": "买牛奶", "section": "今日建议", "source_line": "- [ ] 买牛奶"}],
        )
        # 未匹配日志下的任务不会进入提醒，这里显式挂到一条日程上
        item["schedule_id"] = "looki:task-schedule"
        return item

    def test_failed_sync_keeps_previous_success_and_records_error(self):
        old = "2026-11-19T08:00:00+08:00"
        self.plugin.store.set_meta("last_success", old)
        self.plugin.schedule_provider.fetch = AsyncMock(
            side_effect=RuntimeError("Looki moments 全部日期同步失败（7 天）")
        )
        result = asyncio.run(self.plugin.sync_once())
        self.assertIn("全部日期同步失败", result["error"])
        meta = self.plugin.store.meta()
        self.assertEqual(meta["last_success"], old)
        self.assertIn("全部日期同步失败", meta["last_error"])

    def test_short_window_refreshes_days_that_already_have_journals(self):
        """短窗口内即使已有日志也要整段刷新，Looki 会回补旧日志"""
        today = date(2026, 11, 20)
        for offset in range(7):
            day = (today - timedelta(days=offset)).isoformat()
            self.plugin.store.upsert_journals([self._journal(day, f"j-{offset}")])
        provider = SimpleNamespace(fetch_journals=AsyncMock(return_value=[]))
        asyncio.run(self.plugin._sync_journals(provider, today, 7, "Asia/Shanghai"))
        refresh = provider.fetch_journals.await_args.args[3]
        self.assertEqual(len(refresh), 7)

    def test_long_window_skips_known_days_but_keeps_recent_three(self):
        today = date(2026, 11, 20)
        known = today - timedelta(days=10)
        self.plugin.store.upsert_journals([self._journal(known.isoformat(), "j-known")])
        provider = SimpleNamespace(fetch_journals=AsyncMock(return_value=[]))
        asyncio.run(self.plugin._sync_journals(provider, today, 30, "Asia/Shanghai"))
        refresh = provider.fetch_journals.await_args.args[3]
        self.assertNotIn(known, refresh)
        self.assertIn(today, refresh)
        self.assertEqual(len(refresh), 29)

    def _two_task_journal(self):
        journal = self._task_journal()
        journal["content"] = "今日建议\n- [ ] 买牛奶\n- [ ] 随手关灯"
        journal["tasks"] = [
            {"body": "买牛奶", "section": "今日建议", "source_line": "- [ ] 买牛奶"},
            {"body": "随手关灯", "section": "今日建议", "source_line": "- [ ] 随手关灯"},
        ]
        return journal

    def _compose_reminder(self, result):
        """把模型的提醒决策固定为指定结果；result 为 None 表示模型不可用。"""
        async def compose(tasks, recipient=None):
            if result is None:
                return None
            selected = result(tasks) if callable(result) else result
            return selected

        self.plugin._compose_task_reminder = AsyncMock(side_effect=compose)

    def test_reminders_are_tracked_per_recipient(self):
        self.plugin.config.update({"enable_task_reminders": True, "task_reminder_user_ids": ["1001", "1002"]})
        self.plugin.store.upsert_journals([self._task_journal()])
        self._compose_reminder(lambda tasks: {"selected_ids": [tasks[0]["id"]], "message": "记得买牛奶哦"})
        asyncio.run(self.plugin._send_task_reminders())
        self.assertEqual(self.plugin.context.send_message.await_count, 2)
        self.assertEqual(self.plugin.store.pending_task_reminders_for("1001", 50), [])
        self.assertEqual(self.plugin.store.pending_task_reminders_for("1002", 50), [])

    def test_failed_recipient_keeps_task_pending_only_for_that_user(self):
        self.plugin.config.update({"enable_task_reminders": True, "task_reminder_user_ids": ["1001", "1002"]})
        self.plugin.store.upsert_journals([self._task_journal()])
        self._compose_reminder(lambda tasks: {"selected_ids": [tasks[0]["id"]], "message": "记得买牛奶哦"})
        self.plugin.context.send_message = AsyncMock(side_effect=[True, False])
        asyncio.run(self.plugin._send_task_reminders())
        self.assertEqual(self.plugin.store.pending_task_reminders_for("1001", 50), [])
        self.assertEqual(
            [task["body"] for task in self.plugin.store.pending_task_reminders_for("1002", 50)],
            ["买牛奶"],
        )

    def test_reminder_interval_skips_model_and_delivery_for_each_recipient(self):
        self.plugin.config.update({
            "enable_task_reminders": True,
            "task_reminder_user_ids": ["1001"],
            "task_reminder_interval_minutes": 60,
        })
        self.plugin.store.upsert_journals([self._task_journal()])
        self.plugin.store.mark_tasks_reminded_for(["previous-task"], "1001")
        self._compose_reminder(lambda tasks: {"selected_ids": [tasks[0]["id"]], "message": "记得买牛奶哦"})
        asyncio.run(self.plugin._send_task_reminders())
        self.plugin._compose_task_reminder.assert_not_awaited()
        self.plugin.context.send_message.assert_not_awaited()
        self.assertEqual(len(self.plugin.store.pending_task_reminders_for("1001", 50)), 1)

    def test_reminder_uses_model_message_and_deletes_unselected_task(self):
        """提醒文本必须来自模型；未选中的低价值任务软删除，不写入提醒记录"""
        self.plugin.config.update({
            "enable_task_reminders": True,
            "task_reminder_user_ids": ["1001"],
            "task_auto_delete_low_value": True,
        })
        self.plugin.store.upsert_journals([self._two_task_journal()])
        pending = self.plugin.store.pending_task_reminders_for("1001", 50)
        chosen = next(task for task in pending if task["body"] == "买牛奶")
        skipped = next(task for task in pending if task["body"] == "随手关灯")
        self._compose_reminder({"selected_ids": [chosen["id"]], "message": "牛奶快没了，记得买哦"})
        asyncio.run(self.plugin._send_task_reminders())

        sent_text = self.plugin.context.send_message.await_args.args[1].chain[0].text
        self.assertEqual(sent_text, "牛奶快没了，记得买哦")
        self.assertNotIn("Looki 任务提醒", sent_text)
        saved_reminder = self.plugin.store.db.execute(
            "SELECT message FROM task_reminders WHERE task_id=? AND user='1001'", (chosen["id"],)
        ).fetchone()
        self.assertEqual(saved_reminder["message"], "牛奶快没了，记得买哦")
        rows = {row["id"]: row for row in self.plugin.store.db.execute(
            "SELECT id,reminded_at,deleted FROM looki_tasks WHERE id IN (?,?)", (chosen["id"], skipped["id"])
        )}
        self.assertIsNotNone(rows[chosen["id"]]["reminded_at"])
        self.assertEqual(rows[chosen["id"]]["deleted"], 0)
        self.assertIsNone(rows[skipped["id"]]["reminded_at"])
        self.assertEqual(rows[skipped["id"]]["deleted"], 1)

    def test_auto_delete_disabled_keeps_unselected_task_for_later_evaluation(self):
        self.plugin.config.update({
            "enable_task_reminders": True,
            "task_reminder_user_ids": ["1001"],
            "task_auto_delete_low_value": False,
        })
        self.plugin.store.upsert_journals([self._two_task_journal()])
        pending = self.plugin.store.pending_task_reminders_for("1001", 50)
        chosen, skipped = pending
        self._compose_reminder({"selected_ids": [chosen["id"]], "message": "记得买牛奶哦"})
        asyncio.run(self.plugin._send_task_reminders())
        rows = self.plugin.store.tasks("2026-11-20", "2026-11-20", include_deleted=True)
        self.assertFalse(next(task for task in rows if task["id"] == skipped["id"])["deleted"])
        self.assertEqual(
            [task["id"] for task in self.plugin.store.pending_task_reminders_for("1001", 50)],
            [skipped["id"]],
        )

    def test_auto_delete_preserves_task_selected_for_another_recipient(self):
        self.plugin.config.update({
            "enable_task_reminders": True,
            "task_reminder_user_ids": ["1001", "1002"],
            "task_auto_delete_low_value": True,
        })
        self.plugin.store.upsert_journals([self._two_task_journal()])
        chosen, skipped = self.plugin.store.pending_task_reminders_for("1001", 50)
        self.plugin._compose_task_reminder = AsyncMock(side_effect=[
            {"selected_ids": [chosen["id"]], "message": "记得买牛奶哦"},
            {"selected_ids": [], "message": ""},
        ])
        asyncio.run(self.plugin._send_task_reminders())
        rows = self.plugin.store.tasks("2026-11-20", "2026-11-20", include_deleted=True)
        self.assertFalse(next(task for task in rows if task["id"] == chosen["id"])["deleted"])
        self.assertTrue(next(task for task in rows if task["id"] == skipped["id"])["deleted"])

    def test_reminder_skipped_without_marking_when_model_unavailable(self):
        """模型不可用：不发送、不标记，下次同步重试"""
        self.plugin.config.update({"enable_task_reminders": True, "task_reminder_user_ids": ["1001"]})
        self.plugin.store.upsert_journals([self._task_journal()])
        self._compose_reminder(None)
        asyncio.run(self.plugin._send_task_reminders())
        self.plugin.context.send_message.assert_not_awaited()
        self.assertEqual(len(self.plugin.store.pending_task_reminders_for("1001", 50)), 1)

    def test_reminder_deletes_all_evaluated_when_model_rejects_all(self):
        """模型认为都不值得提醒：不发送，并软删除本轮全部低价值任务"""
        self.plugin.config.update({
            "enable_task_reminders": True,
            "task_reminder_user_ids": ["1001"],
            "task_auto_delete_low_value": True,
        })
        self.plugin.store.upsert_journals([self._task_journal()])
        self._compose_reminder({"selected_ids": [], "message": ""})
        asyncio.run(self.plugin._send_task_reminders())
        self.plugin.context.send_message.assert_not_awaited()
        self.assertEqual(self.plugin.store.pending_task_reminders_for("1001", 50), [])
        self.assertTrue(self.plugin.store.tasks("2026-11-20", "2026-11-20", include_deleted=True)[0]["deleted"])

    def test_compose_reminder_uses_persona_and_parses_model_response(self):
        """生成链路：人设进入 system prompt，任务与来源日志进入 prompt，模型输出被解析"""
        journal = self._task_journal()
        journal["content"] = "上午发现牛奶快没了。\n今日建议\n- [ ] 买牛奶"
        self.plugin.store.upsert_journals([journal])
        pending = self.plugin.store.pending_task_reminders_for("1001", 50)
        response = SimpleNamespace(completion_text='{"selected_ids": ["%s"], "message": "记得买牛奶"}' % pending[0]["id"])
        provider = SimpleNamespace(text_chat=AsyncMock(return_value=response))
        self.plugin.context = SimpleNamespace(
            get_using_provider=lambda: provider,
            persona_manager=SimpleNamespace(
                get_default_persona_v3=AsyncMock(return_value={"prompt": "默认人格"}),
                resolve_selected_persona=AsyncMock(return_value={"prompt": "你是白希，说话温柔。"}),
            ),
        )
        result = asyncio.run(self.plugin._compose_task_reminder(pending, "default:FriendMessage:1001"))
        self.assertEqual(result, {"selected_ids": [pending[0]["id"]], "message": "记得买牛奶"})
        call = provider.text_chat.await_args.kwargs
        self.assertIn("你是白希，说话温柔。", call["system_prompt"])
        self.assertNotIn("默认人格", call["system_prompt"])
        self.plugin.context.persona_manager.resolve_selected_persona.assert_awaited_once_with("default:FriendMessage:1001")
        self.assertIn("买牛奶", call["prompt"])
        self.assertIn("上午发现牛奶快没了。", call["prompt"])

    def test_compose_reminder_returns_none_without_provider(self):
        self.plugin.store.upsert_journals([self._task_journal()])
        pending = self.plugin.store.pending_task_reminders_for("1001", 50)
        self.plugin.context = SimpleNamespace(get_using_provider=lambda: None)
        self.assertIsNone(asyncio.run(self.plugin._compose_task_reminder(pending)))

    def test_partial_sync_is_visible_and_not_marked_as_full_success(self):
        self.plugin.schedule_provider = SimpleNamespace(
            fetch=AsyncMock(return_value=[]),
            last_sync_error="Looki moments 部分日期同步失败（1 天）",
        )

        result = asyncio.run(self.plugin.sync_once(automatic=False))

        self.assertIn("部分日期同步失败", result["sync_warnings"][0])
        self.assertIn("部分日期同步失败", self.plugin.store.meta()["last_error"])
        self.assertIsNone(self.plugin.store.meta().get("last_success"))


class TaskReminderContextTests(unittest.TestCase):
    def setUp(self):
        from astrbot_plugin_looki_companion.main import LookiCompanionPlugin

        self.temp = tempfile.TemporaryDirectory()
        self.session = "default:FriendMessage:1001"
        self.plugin = LookiCompanionPlugin.__new__(LookiCompanionPlugin)
        self.plugin.config = {
            "enable_task_reminders": True,
            "task_reminder_user_ids": [self.session],
            # 提醒上下文由提醒开关独立控制，不受近期线索范围影响。
            "clue_scope": "关闭",
        }
        self.plugin.store = ScheduleStore(Path(self.temp.name) / "schedules.sqlite3")
        self.plugin.store.upsert_journals([{
            "id": "j-reminder-context", "day": "2026-11-20", "type": "DIARY",
            "content": "上午发现牛奶快没了。\n今日建议\n- [ ] 买牛奶", "recorded_at": "",
            "schedule_id": "looki:reminder-context-schedule",
            "tasks": [{"body": "买牛奶", "section": "今日建议", "source_line": "- [ ] 买牛奶"}],
        }])
        self.task = self.plugin.store.pending_task_reminders_for(self.session, 50)[0]
        self.plugin.store.mark_tasks_reminded_for(
            [self.task["id"]], self.session, "牛奶快没了，记得买哦"
        )

    def tearDown(self):
        self.plugin.store.close()
        self.temp.cleanup()

    def event(self, private=True, sender="1001", origin=None):
        return SimpleNamespace(
            is_private_chat=lambda: private,
            get_sender_id=lambda: sender,
            unified_msg_origin=origin or self.session,
        )

    def context_for(self, event=None):
        req = SimpleNamespace(extra_user_content_parts=[])
        asyncio.run(self.plugin.add_task_reminder_context(event or self.event(), req))
        return req

    def test_recipient_private_chat_gets_actual_reminder_task_and_source(self):
        req = self.context_for()
        self.assertEqual(len(req.extra_user_content_parts), 1)
        text = req.extra_user_content_parts[0].text
        self.assertIn("牛奶快没了，记得买哦", text)
        self.assertIn("买牛奶", text)
        self.assertIn("上午发现牛奶快没了。", text)

    def test_plain_sender_id_configuration_remains_supported(self):
        self.plugin.config["task_reminder_user_ids"] = ["1001"]
        self.plugin.store.mark_tasks_reminded_for([self.task["id"]], "1001", "记得带牛奶回家")
        text = self.context_for().extra_user_content_parts[0].text
        self.assertIn("记得带牛奶回家", text)

    def test_non_recipient_and_group_chat_do_not_get_reminder_context(self):
        other = self.event(sender="1002", origin="default:FriendMessage:1002")
        self.assertEqual(self.context_for(other).extra_user_content_parts, [])
        self.assertEqual(self.context_for(self.event(private=False)).extra_user_content_parts, [])

    def test_disabled_reminders_do_not_get_context(self):
        self.plugin.config["enable_task_reminders"] = False
        self.assertEqual(self.context_for().extra_user_content_parts, [])

    def test_completed_task_no_longer_gets_context(self):
        self.plugin.store.set_task_done(self.task["id"], True, "2026-11-24")
        self.assertEqual(self.context_for().extra_user_content_parts, [])

    def test_source_log_length_is_bounded(self):
        self.plugin.store.update_journal("j-reminder-context", {"content": "背景" * 5000})
        text = self.context_for().extra_user_content_parts[0].text
        self.assertLess(len(text), 5000)


class DatabaseThreadTests(unittest.TestCase):
    def test_worker_thread_uses_independent_connection(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ScheduleStore(Path(temp) / "threaded.sqlite3")
            try:
                main_connection = store.db
                worker_connections = []
                errors = []

                def worker():
                    try:
                        worker_connections.append(store.db)
                        store.set_meta("thread-check", "ok")
                    except Exception as exc:
                        errors.append(exc)

                thread = threading.Thread(target=worker)
                thread.start()
                thread.join(timeout=10)

                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertIsNot(worker_connections[0], main_connection)
                self.assertEqual(store.meta().get("thread-check"), "ok")
            finally:
                store.close()


class TaskCompletionReplyTests(unittest.TestCase):
    def setUp(self):
        from astrbot_plugin_looki_companion.main import LookiCompanionPlugin

        self.temp = tempfile.TemporaryDirectory()
        self.plugin = LookiCompanionPlugin.__new__(LookiCompanionPlugin)
        self.plugin.config = {
            "enable_task_reminders": True,
            "task_reminder_user_ids": ["1001", "1002"],
            "task_completion_reply_enabled": True,
            "timezone": "Asia/Shanghai",
        }
        self.plugin.store = ScheduleStore(Path(self.temp.name) / "schedules.sqlite3")
        self.plugin.store.upsert_journals([{
            "id": "j-completion", "day": "2026-11-20", "type": "DIARY",
            "content": "今日建议\n- [ ] 买牛奶", "recorded_at": "",
            "schedule_id": "looki:completion-schedule",
            "tasks": [{"body": "买牛奶", "section": "今日建议", "source_line": "- [ ] 买牛奶"}],
        }])
        self.task = self.plugin.store.tasks("2026-11-20", "2026-11-20")[0]
        self.plugin.store.mark_tasks_reminded_for([self.task["id"]], "1001", "记得买牛奶")

    def tearDown(self):
        self.plugin.store.close()
        self.temp.cleanup()

    def event(self, text, user_id="1001", private=True):
        event = SimpleNamespace(
            message_str=text,
            stopped=False,
            unified_msg_origin=f"default:FriendMessage:{user_id}",
        )
        event.get_sender_id = lambda: user_id
        event.is_private_chat = lambda: private
        event.plain_result = lambda text: SimpleNamespace(text=text)
        event.stop_event = lambda: setattr(event, "stopped", True)
        return event

    def reply(self, event):
        async def collect():
            response = self.plugin.on_task_completion_reply(event)
            if hasattr(response, "__aiter__"):
                return [item async for item in response]
            await response
            return []
        return asyncio.run(collect())

    def test_completion_marks_done_and_lets_current_persona_reply(self):
        event = self.event("搞定了！")
        results = self.reply(event)
        self.assertEqual(results, [])
        self.assertFalse(event.stopped)
        completed = self.plugin.store.tasks("2026-11-20", "2026-11-20")
        self.assertTrue(completed[0]["done"])
        req = SimpleNamespace(extra_user_content_parts=[])
        asyncio.run(self.plugin.add_task_reminder_context(event, req))
        text = req.extra_user_content_parts[0].text
        self.assertIn("刚刚说已经完成了", text)
        self.assertIn("买牛奶", text)
        self.assertIn("自然确认", text)

    def test_normal_chat_is_not_intercepted(self):
        event = self.event("今天吃什么")
        results = self.reply(event)
        self.assertEqual(results, [])
        self.assertFalse(event.stopped)
        self.assertEqual(len(self.plugin.store.tasks("2026-11-20", "2026-11-20")), 1)

    def test_group_chat_does_not_complete_private_reminder(self):
        event = self.event("完成了", private=False)
        self.assertEqual(self.reply(event), [])
        self.assertFalse(self.plugin.store.tasks("2026-11-20", "2026-11-20")[0]["done"])

    def test_full_umo_recipient_can_complete_task(self):
        session = "default:FriendMessage:1002"
        self.plugin.config["task_reminder_user_ids"] = [session]
        self.plugin.store.mark_tasks_reminded_for([self.task["id"]], session, "记得买牛奶")
        event = self.event("完成了", "1002")

        self.assertEqual(self.reply(event), [])
        self.assertTrue(self.plugin.store.tasks("2026-11-20", "2026-11-20")[0]["done"])

    def test_completion_phrase_without_reminder_does_not_intercept(self):
        event = self.event("完成了", "1002")
        results = self.reply(event)
        self.assertEqual(results, [])
        self.assertFalse(event.stopped)
        self.assertEqual(len(self.plugin.store.tasks("2026-11-20", "2026-11-20")), 1)

    def test_completion_reply_can_be_disabled(self):
        self.plugin.config["task_completion_reply_enabled"] = False
        event = self.event("完成了")
        self.assertEqual(self.reply(event), [])
        self.assertFalse(event.stopped)
        self.assertEqual(len(self.plugin.store.tasks("2026-11-20", "2026-11-20")), 1)


class TaskCompletionMessageTests(unittest.TestCase):
    def test_recognizes_explicit_completion_variants(self):
        from astrbot_plugin_looki_companion.main import _is_task_completion_message

        for text in ("完成了", "已完成。", "我做完了", "任务已完成", "我把这个任务搞定了！", "办好了"):
            with self.subTest(text=text):
                self.assertTrue(_is_task_completion_message(text))

    def test_rejects_normal_chat_and_unrelated_acknowledgements(self):
        from astrbot_plugin_looki_companion.main import _is_task_completion_message

        for text in ("今天吃什么", "这个任务怎么完成", "好了", "你完成了吗", "完成了就来夸夸你"):
            with self.subTest(text=text):
                self.assertFalse(_is_task_completion_message(text))


class TaskReminderResponseTests(unittest.TestCase):
    def test_parses_plain_and_fenced_json(self):
        from astrbot_plugin_looki_companion.main import _parse_task_reminder_response

        for text in (
            '{"selected_ids": ["a"], "message": "记得买牛奶"}',
            '```json\n{"selected_ids": ["a"], "message": "记得买牛奶"}\n```',
        ):
            result = _parse_task_reminder_response(text, {"a", "b"})
            self.assertEqual(result, {"selected_ids": ["a"], "message": "记得买牛奶"})

    def test_hallucinated_ids_are_dropped(self):
        from astrbot_plugin_looki_companion.main import _parse_task_reminder_response

        result = _parse_task_reminder_response(
            '{"selected_ids": ["ghost", "a", "a"], "message": "提醒"}', {"a"}
        )
        self.assertEqual(result, {"selected_ids": ["a"], "message": "提醒"})

    def test_invalid_outputs_return_none(self):
        from astrbot_plugin_looki_companion.main import _parse_task_reminder_response

        cases = [
            "没有值得提醒的任务",  # 非 JSON
            '{"selected_ids": "a", "message": "提醒"}',  # selected_ids 类型错误
            '{"selected_ids": ["a"], "message": ""}',  # 选中任务却没有提醒文本
            '{"selected_ids": ["ghost"], "message": "提醒"}',  # 全是幻觉 id，不能当作全部跳过
            '{"selected_ids": ["a", "b"], "message": "提醒"}',  # 每轮最多选择一个任务
            '{"selected_ids": [], "message": "有文本却没选任务"}',  # 矛盾输出
        ]
        for text in cases:
            self.assertIsNone(_parse_task_reminder_response(text, {"a", "b"}), text)

    def test_empty_selection_is_valid_skip(self):
        from astrbot_plugin_looki_companion.main import _parse_task_reminder_response

        result = _parse_task_reminder_response('{"selected_ids": [], "message": ""}', {"a"})
        self.assertEqual(result, {"selected_ids": [], "message": ""})


class ConfigApiTests(unittest.TestCase):
    def setUp(self):
        from astrbot_plugin_looki_companion.main import LookiCompanionPlugin

        self.plugin = LookiCompanionPlugin.__new__(LookiCompanionPlugin)
        self.plugin.config = {
            "looki_api_key": "old-looki-key",
            "image_api_key": "old-image-key",
            "auto_sync": False,
            "sync_interval_minutes": 5,
            "image_send_original": True,
        }
        self.plugin.context = SimpleNamespace(get_all_providers=lambda: [])
        self.plugin._sync_task = None
        self.plugin._stopping = False
        self.temp = tempfile.TemporaryDirectory()
        self.plugin.store = ScheduleStore(Path(self.temp.name) / "schedules.sqlite3")
        from astrbot_plugin_looki_companion.intent_service import LookiIntentService

        self.plugin.intent = LookiIntentService(self.plugin.store, "Asia/Shanghai")

    def tearDown(self):
        self.plugin.store.close()
        self.temp.cleanup()

    def set_providers(self, *providers):
        self.plugin.context.get_all_providers = lambda: [
            SimpleNamespace(provider_config={"id": provider_id, "modalities": modalities})
            for provider_id, modalities in providers
        ]

    def call(self, payload):
        async def run():
            with patch(
                "astrbot_plugin_looki_companion.main.request",
                SimpleNamespace(json=AsyncMock(return_value=payload), query={}),
            ), patch(
                "astrbot_plugin_looki_companion.main.json_response",
                side_effect=lambda data: data,
            ), patch(
                "astrbot_plugin_looki_companion.main.error_response",
                side_effect=lambda message, status_code: {"error": message, "status_code": status_code},
            ):
                return await self.plugin.api_set_config()

        return asyncio.run(run())

    def test_masked_keys_are_kept_and_blank_keys_are_cleared(self):
        result = self.call({"looki_api_key": "old-l••••-key", "image_api_key": "••••••", "image_candidate_count": 6})
        self.assertNotIn("looki_api_key", result["saved"])
        self.assertNotIn("image_api_key", result["saved"])
        self.assertEqual(self.plugin.config["looki_api_key"], "old-looki-key")
        self.assertEqual(self.plugin.config["image_api_key"], "old-image-key")

        result = self.call({"looki_api_key": "  ", "image_api_key": ""})
        self.assertEqual(self.plugin.config["looki_api_key"], "")
        self.assertEqual(self.plugin.config["image_api_key"], "")
        self.assertIsNone(self.plugin.schedule_provider)
        self.assertIn("looki_api_key", result["saved"])
        self.assertIn("image_api_key", result["saved"])

    def test_selection_provider_must_be_loaded_and_support_images(self):
        self.set_providers(("text-only", ["text"]), ("vision", ["text", "image"]))
        rejected = self.call({"image_selection_provider_id": "missing"})
        self.assertEqual(rejected["status_code"], 400)
        self.assertIn("挑图模型", rejected["error"])
        rejected = self.call({"image_selection_provider_id": "text-only"})
        self.assertEqual(rejected["status_code"], 400)
        accepted = self.call({"image_selection_provider_id": "vision"})
        self.assertTrue(accepted["saved"])
        self.assertEqual(self.plugin.config["image_selection_provider_id"], "vision")

    def test_original_mode_ignores_irrelevant_generation_settings(self):
        result = self.call({
            "image_generation_mode": "openai_compatible",
            "image_generation_provider_id": "missing",
            "image_api_base_url": "",
            "image_api_model": "",
        })
        self.assertTrue(result["saved"])

    def test_generation_mode_validates_only_its_own_required_fields(self):
        self.plugin.config["image_send_original"] = False
        self.plugin.config["image_generation_mode"] = "multimodal"
        rejected = self.call({"image_generation_provider_id": "missing"})
        self.assertEqual(rejected["status_code"], 400)
        self.assertIn("图生图模型", rejected["error"])

        rejected = self.call({"image_generation_mode": "openai_compatible"})
        self.assertEqual(rejected["status_code"], 400)
        self.assertIn("接口地址和模型名称", rejected["error"])
        accepted = self.call({
            "image_generation_mode": "openai_compatible",
            "image_api_base_url": "https://example.com/v1",
            "image_api_model": "image-model",
            "image_api_key": "",
        })
        self.assertTrue(accepted["saved"])

    def test_task_reminder_controls_are_validated_and_saved(self):
        for interval in (0, -1, 10081):
            result = self.call({"task_reminder_interval_minutes": interval})
            self.assertEqual(result["status_code"], 400)
            self.assertIn("提醒间隔", result["error"])
        result = self.call({
            "task_reminder_interval_minutes": 90,
            "task_auto_delete_low_value": False,
            "task_completion_reply_enabled": False,
        })
        self.assertTrue(result["saved"])
        self.assertEqual(self.plugin.config["task_reminder_interval_minutes"], 90)
        self.assertFalse(self.plugin.config["task_auto_delete_low_value"])
        self.assertFalse(self.plugin.config["task_completion_reply_enabled"])

    def test_sync_interval_change_restarts_running_task(self):
        async def run():
            self.plugin.config["auto_sync"] = True
            self.plugin._sync_task = asyncio.create_task(asyncio.sleep(3600))
            old_task = self.plugin._sync_task
            self.plugin._sync_loop = lambda: asyncio.sleep(3600)
            with patch(
                "astrbot_plugin_looki_companion.main.request",
                SimpleNamespace(json=AsyncMock(return_value={"sync_interval_minutes": 10}), query={}),
            ), patch(
                "astrbot_plugin_looki_companion.main.json_response",
                side_effect=lambda data: data,
            ):
                result = await self.plugin.api_set_config()
            new_task = self.plugin._sync_task
            self.assertIsNot(new_task, old_task)
            self.assertTrue(old_task.cancelled())
            self.assertFalse(new_task.done())
            self.assertEqual(self.plugin.config["sync_interval_minutes"], 10)
            new_task.cancel()
            try:
                await new_task
            except asyncio.CancelledError:
                pass
            return result

        self.assertTrue(asyncio.run(run())["saved"])


class JournalTaskTests(unittest.TestCase):
    def test_parse_journal_localizes_time_and_keeps_markdown_headings(self):
        from journal_service import parse_journal

        zone = ZoneInfo("Asia/Shanghai")
        journal = {
            "id": "j-1",
            "type": "audio_summary",
            "date": "2026-11-17",
            "title": "下午散步",
            "content": "# 下午散步\n\n在河边走了很久。\n\n## 细节\n- 买了咖啡",
            "recorded_at": "2026-11-17T10:00:00Z",
            "media_items": [{"id": "m1"}],
        }
        parsed = parse_journal(journal, zone)
        self.assertEqual(parsed["type"], "AUDIO_SUMMARY")
        self.assertEqual(parsed["recorded_at"], "2026-11-17T18:00:00+08:00")
        # 页面按 Markdown 标题分块，标题必须保留在正文里
        self.assertIn("# 下午散步", parsed["content"])
        self.assertIn("## 细节", parsed["content"])
        self.assertIn("买了咖啡", parsed["content"])
        self.assertEqual(parsed["media_count"], 1)
        self.assertIsNone(parse_journal({"id": "x", "type": "DIARY"}, zone))

    def test_parse_journal_falls_back_to_description_without_content(self):
        """日记 / 漫画页 / 视频日志没有正文，直接用简介作为展示文本"""
        from journal_service import parse_journal

        zone = ZoneInfo("Asia/Shanghai")
        parsed = parse_journal({
            "id": "j-2",
            "type": "DIARY",
            "date": "2026-11-17",
            "title": "宿舍代码时间",
            "description": "课间回宿舍对着屏幕敲几行代码",
        }, zone)
        self.assertEqual(parsed["content"], "课间回宿舍对着屏幕敲几行代码")

    def test_fetch_journals_filters_by_date_range(self):
        """Looki journals 接口只认 start_date/end_date，传 on_date 会返回全部历史并触发限流"""
        from astrbot_plugin_looki_companion.main import LookiScheduleProvider

        class JournalClient:
            instances = []

            def __init__(self, **kwargs):
                self.get = AsyncMock(return_value=FakeResponse({"data": {"items": [{
                    "id": "j-1", "type": "DIARY", "date": "2026-11-17",
                    "title": "宿舍代码时间", "description": "课间回宿舍敲代码",
                }], "next_cursor": ""}}))
                self.instances.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        provider = LookiScheduleProvider("https://open.looki.tech/api/v1/", "test-key")
        with patch("httpx.AsyncClient", JournalClient):
            items = asyncio.run(provider.fetch_journals(date(2026, 11, 17), date(2026, 11, 17), "Asia/Shanghai"))

        call = JournalClient.instances[-1].get.await_args
        self.assertEqual(call.args[0], "https://open.looki.tech/api/v1/journals")
        self.assertEqual(call.kwargs["params"], {"start_date": "2026-11-17", "end_date": "2026-11-17"})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["day"], "2026-11-17")
        self.assertEqual(items[0]["content"], "课间回宿舍敲代码")

    def test_match_schedule_prefers_most_specific_interval_and_rejects_unknown(self):
        from journal_service import match_schedule

        items = [
            {"id": "wide", "start_at": "2026-11-17T09:00:00+08:00", "end_at": "2026-11-17T12:00:00+08:00"},
            {"id": "narrow", "start_at": "2026-11-17T10:00:00+08:00", "end_at": "2026-11-17T10:30:00+08:00"},
            {"id": "later", "start_at": "2026-11-17T20:00:00+08:00", "end_at": "2026-11-17T21:00:00+08:00"},
        ]
        self.assertEqual(match_schedule("2026-11-17T10:12:01.225+08:00", items), "narrow")
        self.assertIsNone(match_schedule("2026-11-17T15:00:00+08:00", items))
        self.assertIsNone(match_schedule("", items))

    def test_extract_tasks_accepts_checkbox_variants_and_done_state(self):
        from journal_service import extract_journal_tasks

        content = "\n".join([
            "# 日志",
            "- [ ] 洗衣也算任务",
            "- [x] 预约体检",
            "* [ ] 星号也是任务",
            "+ [ ] 加号也是任务",
            "1. 编号列表不算任务",
            "- 普通列表不算任务",
            "## 任务",
            "- [ ] 买牛奶",
        ])
        tasks = extract_journal_tasks(content)
        self.assertEqual([task["body"] for task in tasks], ["洗衣也算任务", "预约体检", "星号也是任务", "加号也是任务", "买牛奶"])
        self.assertEqual([task["done"] for task in tasks], [False, True, False, False, False])
        self.assertEqual(tasks[0]["section"], "日志")
        self.assertEqual(tasks[-1]["section"], "任务")

    def test_extract_tasks_dedupes_within_one_journal(self):
        from journal_service import extract_journal_tasks

        tasks = extract_journal_tasks("- [ ] 买牛奶\n- [ ] 买牛奶\n- [ ]  **买牛奶** ")
        self.assertEqual([task["body"] for task in tasks], ["买牛奶"])


class JournalStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ScheduleStore(Path(self.temp.name) / "schedules.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def journal(self, **overrides):
        item = {
            "id": "j-1",
            "day": "2026-11-17",
            "type": "AUDIO_SUMMARY",
            "title": "下午散步",
            "description": "摘要",
            "content": "在河边走了很久。",
            "recorded_at": "2026-11-17T10:00:00+08:00",
            "schedule_id": "s-1",
            "media_count": 2,
            "tasks": [
                {"body": "买牛奶", "section": "任务", "source_line": "- [ ] 买牛奶", "done": False},
                {"body": "预约体检", "section": "任务", "source_line": "- [x] 预约体检", "done": True},
            ],
        }
        item.update(overrides)
        return item

    def test_upsert_is_idempotent_and_preserves_local_task_state(self):
        self.assertEqual(self.store.upsert_journals([self.journal()]), 1)
        self.store.upsert_journals([self.journal()])
        items = self.store.journals("2026-11-17", "2026-11-17")
        self.assertEqual(len(items), 1)
        self.assertEqual(len(items[0]["tasks"]), 2)
        self.assertEqual(items[0]["match_state"], "matched")

        task = next(t for t in items[0]["tasks"] if t["body"] == "买牛奶")
        self.store.set_task_done(task["id"], True, "2026-11-18")
        self.store.upsert_journals([self.journal()])
        refreshed = self.store.tasks("2026-11-17", "2026-11-17")
        milk = next(t for t in refreshed if t["body"] == "买牛奶")
        self.assertTrue(milk["done"])
        self.assertEqual(milk["done_day"], "2026-11-18")

    def test_removed_undone_task_is_deleted_but_done_task_is_kept(self):
        self.store.upsert_journals([self.journal()])
        self.store.upsert_journals([self.journal(tasks=[
            {"body": "预约体检", "section": "任务", "source_line": "- [x] 预约体检", "done": True},
        ])])
        rows = self.store.tasks("2026-11-17", "2026-11-17")
        self.assertEqual([row["body"] for row in rows], ["预约体检"])
        self.assertTrue(rows[0]["done"])

    def test_unmatched_journals_are_hidden_by_default_and_can_be_attached(self):
        self.store.upsert_journals([
            self.journal(),
            self.journal(id="j-2", type="YESTERDAY_RECAP", recorded_at="", schedule_id="", day="2026-11-17"),
        ])
        items = self.store.journals("2026-11-17", "2026-11-17")
        self.assertEqual([item["id"] for item in items], ["j-1"])
        unmatched = self.store.journals("2026-11-17", "2026-11-17", True)
        self.assertEqual([item["id"] for item in unmatched], ["j-2"])

        self.store.set_journal_match("j-2", "s-9")
        refreshed = self.store.journals("2026-11-17", "2026-11-17")
        self.assertEqual(sorted(item["id"] for item in refreshed), ["j-1", "j-2"])
        recap = next(item for item in refreshed if item["id"] == "j-2")
        self.assertEqual(recap["match_state"], "matched")
        self.assertEqual(recap["schedule_id"], "s-9")

    def test_tasks_from_unmatched_journal_are_never_exposed(self):
        self.store.upsert_journals([
            self.journal(id="j-blind", schedule_id="", recorded_at=""),
        ])

        self.assertEqual(self.store.tasks(only_pending=True), [])
        self.assertEqual(self.store.tasks_today_pending("2026-11-17"), [])
        self.assertEqual(self.store.pending_task_reminders(), [])
        self.assertEqual(self.store.pending_task_reminders_for("1001"), [])

    def test_expired_task_stays_visible_on_page_but_is_dropped_from_reminders(self):
        from datetime import date

        from schedule_store import is_task_expired

        self.store.upsert_journals([self.journal()])
        day = "2026-11-17"

        self.assertTrue(is_task_expired(day, date(2026, 11, 21), 3))
        self.assertFalse(is_task_expired(day, date(2026, 11, 20), 3))

        # 页面查询不过滤过期任务
        self.assertEqual([row["body"] for row in self.store.tasks(only_pending=True)], ["买牛奶"])
        # 提醒查询按今天过滤：第 4 天起不再提醒
        self.assertEqual(self.store.pending_task_reminders(today=date(2026, 11, 21), expire_days=3), [])
        self.assertEqual(
            [row["body"] for row in self.store.pending_task_reminders(today=date(2026, 11, 20), expire_days=3)],
            ["买牛奶"],
        )
        self.assertEqual(self.store.pending_task_reminders_for("1001", today=date(2026, 11, 21)), [])
        self.assertEqual(
            [row["body"] for row in self.store.pending_task_reminders_for("1001", today=date(2026, 11, 20))],
            ["买牛奶"],
        )

    def test_journals_for_model_drop_expired_task_and_its_source_line(self):
        from datetime import date

        self.store.upsert_journals([self.journal(content="- [ ] 买牛奶\n今天去了河边。")])
        page_view = self.store.journals("2026-11-17", "2026-11-17")
        self.assertEqual(len(page_view[0]["tasks"]), 2)
        self.assertIn("买牛奶", page_view[0]["content"])

        model_view = self.store.journals("2026-11-17", "2026-11-17", today=date(2026, 11, 21))
        self.assertEqual([task["body"] for task in model_view[0]["tasks"]], ["预约体检"])
        self.assertNotIn("买牛奶", model_view[0]["content"])
        self.assertIn("今天去了河边。", model_view[0]["content"])

    def test_tasks_today_pending_only_returns_today_undone(self):
        self.store.upsert_journals([
            self.journal(id="j-today", day="2026-11-20"),
            self.journal(id="j-old", day="2026-11-17"),
        ])
        self.assertEqual(
            [row["body"] for row in self.store.tasks_today_pending("2026-11-20")],
            ["买牛奶"],
        )
        self.assertEqual(self.store.tasks_today_pending("2026-11-19"), [])

    def test_completed_today_and_reminder_dedupe(self):
        self.store.upsert_journals([self.journal()])
        pending = self.store.pending_task_reminders()
        self.assertEqual([task["body"] for task in pending], ["买牛奶"])

        self.store.mark_tasks_reminded([pending[0]["id"]])
        self.assertEqual(self.store.pending_task_reminders(), [])

        self.store.set_task_done(pending[0]["id"], True, "2026-11-18")
        self.assertEqual([task["body"] for task in self.store.tasks_done_on("2026-11-18")], ["买牛奶"])
        self.assertEqual(self.store.tasks(only_pending=True), [])

        self.store.set_task_done(pending[0]["id"], False, "2026-11-18")
        self.assertEqual(self.store.tasks_done_on("2026-11-18"), [])
        self.assertEqual([task["body"] for task in self.store.tasks(only_pending=True)], ["买牛奶"])


if __name__ == "__main__":
    unittest.main()
