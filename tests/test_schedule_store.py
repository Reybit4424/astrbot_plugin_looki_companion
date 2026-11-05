import asyncio
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
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
        for offset in range(3):
            day = today - timedelta(days=offset)
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


class EventPayloadTests(unittest.TestCase):
    def test_localize_event_converts_utc_to_configured_timezone(self):
        from astrbot_plugin_looki_companion.main import _localize_event

        payload = {"date": "2026-10-25", "start": "2026-10-25T13:00:00.000Z", "end": "2026-10-25T14:30:00.000Z"}
        _localize_event(payload, "Asia/Shanghai")
        self.assertEqual(payload["start"], "2026-10-25T21:00:00+08:00")
        self.assertEqual(payload["end"], "2026-10-25T22:30:00+08:00")


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


if __name__ == "__main__":
    unittest.main()
