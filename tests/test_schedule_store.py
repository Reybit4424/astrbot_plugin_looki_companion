import asyncio
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from schedule_store import ScheduleStore


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
        self.assertEqual(set(items[0]), {"looki_id", "date", "start", "end", "title", "keywords", "description", "audio_summary", "raw_json"})
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
        for offset in range(3):
            day = date(2026, 10, 25) - timedelta(days=offset)
            self.store.upsert_remote([self.item(looki_id=f"id-{offset}", date=day.isoformat(), start=f"{day.isoformat()}T21:00:00+08:00", end=f"{day.isoformat()}T22:30:00+08:00")])
        clues = self.store.clues(7, 3)
        self.assertTrue(any("晚间仍有活动记录" in clue for clue in clues))
        self.assertFalse(any("熬夜" in clue or "睡眠不足" in clue for clue in clues))

    def test_update_manual_delete_persists(self):
        item_id = self.store.save({"date": "2026-10-25", "start": "2026-10-25T09:00:00+08:00", "end": "2026-10-25T10:00:00+08:00", "title": "待办", "keywords": []})
        self.store.save({"date": "2026-10-25", "start": "2026-10-25T09:00:00+08:00", "end": "2026-10-25T10:00:00+08:00", "title": "修改", "keywords": ["工作"]}, item_id)
        self.assertEqual(self.store.query("2026-10-25", "2026-10-25")[0]["title"], "修改")
        self.store.delete(item_id)
        self.assertEqual(self.store.query("2026-10-25", "2026-10-25"), [])

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

    def test_search_no_keyword_returns_all(self):
        """search() 无关键词时返回全量，matched=False"""
        self.store.upsert_remote([self.item()])
        items, matched = self.store.search("2026-10-25", "2026-10-25", "")
        self.assertFalse(matched)
        self.assertEqual(len(items), 1)

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

    def test_db_user_version_is_schema_version(self):
        """DB user_version 应等于 SCHEMA_VERSION（版本管理）"""
        from schedule_store import SCHEMA_VERSION
        ver = self.store.db.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(ver, SCHEMA_VERSION)

    def test_clues_timezone_param_used(self):
        """clues() 使用传入 timezone 而非服务器本地时区"""
        from datetime import date, timedelta
        # 直接构造今天日期（Asia/Shanghai）写入数据，验证 clues 能读到
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        for i in range(3):
            day = today - timedelta(days=i)
            self.store.upsert_remote([self.item(
                looki_id=f"tz-{i}",
                date=day.isoformat(),
                start=f"{day.isoformat()}T21:00:00+08:00",
                end=f"{day.isoformat()}T22:30:00+08:00",
            )])
        clues = self.store.clues(7, 3, "Asia/Shanghai")
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
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        self.store.upsert_remote([self.item(date=today.isoformat(), start=f"{today.isoformat()}T09:00:00+08:00", end=f"{today.isoformat()}T10:00:00+08:00")])
        item_id = self.store.query(today.isoformat(), today.isoformat())[0]["id"]
        self.store.add_correction(item_id, "实际地点是教室，不是宿舍")
        clues = self.store.clues(7, 3, "Asia/Shanghai")
        self.assertTrue(any("对话校正：实际地点是教室，不是宿舍" in clue for clue in clues))

    def test_correction_tool_permissions_and_query_payload(self):
        from types import SimpleNamespace
        from astrbot.core.agent.run_context import ContextWrapper
        from astrbot_plugin_looki_companion.main import AddScheduleCorrectionTool, QueryScheduleTool

        self.store.upsert_remote([self.item(description="用户在宿舍")])
        plugin = SimpleNamespace(store=self.store, config={"allow_group_query": False})
        registry = SimpleNamespace(get_registered_star=lambda _: plugin)

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


if __name__ == "__main__":
    unittest.main()
