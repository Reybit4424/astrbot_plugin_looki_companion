import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from astrbot_plugin_looki_companion.intent_service import LookiIntentService, activity_period
from astrbot_plugin_looki_companion.schedule_store import ScheduleStore

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=TZ)


def looki(looki_id, day, start, end, title, media_types=("photo",)):
    return {
        "looki_id": looki_id,
        "date": day,
        "start": f"{day}T{start}:00+08:00",
        "end": f"{day}T{end}:00+08:00",
        "title": title,
        "keywords": [title],
        "media_types": list(media_types),
    }


def event(group="group-1", sender="user-1"):
    return SimpleNamespace(
        get_group_id=lambda: group,
        get_sender_id=lambda: sender,
    )


class IntentServiceTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.store = ScheduleStore(Path(self._temp.name) / "schedules.sqlite3")
        self.service = LookiIntentService(self.store, "Asia/Shanghai")

    def tearDown(self):
        self.store.close()
        self._temp.cleanup()

    def test_ambiguous_query_asks_for_date(self):
        self.store.upsert_remote([
            looki("birthday-1", "2026-11-01", "19:00", "21:00", "生日聚会"),
            looki("birthday-2", "2026-11-10", "19:00", "21:00", "生日聚会"),
        ])

        result = self.service.resolve("生日聚会", event(), NOW)

        self.assertEqual(result["status"], "clarify")
        self.assertIn("生日聚会", result["reply"])

    def test_match_returns_activity_and_remembers_context(self):
        self.store.upsert_remote([looki("switch", "2026-11-18", "19:00", "20:30", "Switch 游戏局")])
        current_event = event()

        result = self.service.resolve("Switch", current_event, NOW)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["activity"]["looki_id"], "switch")
        follow_up = self.service.resolve("那个", current_event, NOW)
        self.assertEqual(follow_up["status"], "ok")
        self.assertTrue(follow_up["from_context"])
        self.assertEqual(follow_up["activity"]["looki_id"], "switch")

    def test_explicit_query_does_not_reuse_referential_context(self):
        self.store.upsert_remote([
            looki("switch", "2026-11-18", "19:00", "20:30", "Switch 游戏局"),
            looki("birthday", "2026-11-10", "19:00", "21:00", "生日聚会"),
        ])
        current_event = event()
        self.service.resolve("Switch", current_event, NOW)

        result = self.service.resolve("那次生日聚会", current_event, NOW)

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["from_context"])
        self.assertEqual(result["activity"]["looki_id"], "birthday")

    def test_explicit_date_with_pronoun_does_not_reuse_context(self):
        self.store.upsert_remote([
            looki("switch", "2026-11-18", "19:00", "20:30", "Switch 游戏局"),
            looki("birthday", "2026-11-10", "19:00", "21:00", "生日聚会"),
        ])
        current_event = event()
        self.service.resolve("Switch", current_event, NOW)

        result = self.service.resolve("11月10日那次", current_event, NOW)

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["from_context"])
        self.assertEqual(result["activity"]["looki_id"], "birthday")

    def test_context_is_scoped_to_sender(self):
        self.store.upsert_remote([looki("switch", "2026-11-18", "19:00", "20:30", "Switch 游戏局")])
        self.service.resolve("Switch", event(sender="user-1"), NOW)

        result = self.service.resolve("", event(sender="user-2"), NOW)

        self.assertEqual(result["status"], "missing")
        self.assertIn("想找哪一天", result["reply"])

    def test_unmatched_journal_never_reaches_intent(self):
        self.store.upsert_journals([{
            "id": "journal-hotpot",
            "day": "2026-11-15",
            "type": "DIARY",
            "title": "",
            "description": "",
            "content": "晚上和同学去吃了火锅，很辣。",
            "recorded_at": "",
        }])

        result = self.service.resolve("火锅", event(), NOW)

        self.assertEqual(result["status"], "not_found")
        self.assertNotIn("日志", result["reply"])
        self.assertEqual(result["result"]["journal_evidence"], [])

    def test_manual_schedule_reports_no_looki_photos(self):
        self.store.save({
            "date": "2026-11-15",
            "start": "2026-11-15T10:00:00+08:00",
            "end": "2026-11-15T11:00:00+08:00",
            "title": "星期天集市",
            "keywords": [],
        })

        result = self.service.resolve("星期天集市", event(), NOW)

        self.assertEqual(result["status"], "manual")
        self.assertIn("人工日程", result["reply"])

    def test_activity_without_media_reports_no_media(self):
        self.store.upsert_remote([looki("walk", "2026-11-18", "18:00", "19:00", "散步", media_types=())])

        result = self.service.resolve("散步", event(), NOW)

        self.assertEqual(result["status"], "no_media")
        self.assertIn("没有可发送", result["reply"])


class ActivityPeriodTests(unittest.TestCase):
    def test_period_converts_to_configured_timezone(self):
        start, end = activity_period(
            {"start_at": "2026-10-24T00:30:00+08:00", "end_at": "2026-10-24T01:30:00+08:00"},
            "America/New_York",
        )

        self.assertEqual(start.date().isoformat(), "2026-10-23")
        self.assertEqual(start.strftime("%H:%M:%S"), "12:30:00")
        self.assertEqual(end.strftime("%H:%M:%S"), "13:30:00")


class QueryToolNaturalLanguageTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.store = ScheduleStore(Path(self._temp.name) / "schedules.sqlite3")

    def tearDown(self):
        self.store.close()
        self._temp.cleanup()

    def _context(self, search_result):
        from astrbot.core.agent.run_context import ContextWrapper
        from astrbot_plugin_looki_companion.main import QueryScheduleTool

        remembered = []
        plugin = SimpleNamespace(
            config={"allow_group_query": False},
            store=self.store,
            intent=SimpleNamespace(
                search=SimpleNamespace(search=lambda keyword: search_result),
                remember=lambda current, activity: remembered.append(activity),
            ),
        )
        registry = SimpleNamespace(get_registered_star=lambda _: SimpleNamespace(star_cls=plugin))
        current_event = SimpleNamespace(is_private_chat=lambda: True)
        return (
            ContextWrapper(context=SimpleNamespace(context=registry, event=current_event)),
            QueryScheduleTool(),
            remembered,
        )

    def test_keyword_path_returns_full_items_and_remembers_context(self):
        import json

        self.store.upsert_remote([looki("switch", "2026-11-18", "19:00", "20:30", "Switch 游戏局")])
        row = self.store.query("2026-11-18", "2026-11-18")[0]
        result = {
            "needs_clarification": False,
            "clarification": "",
            "candidates": [{"schedule_id": row["id"], "day": "2026-11-18", "looki_id": "switch"}],
            "journal_evidence": [{"day": "2026-11-18", "snippet": "朋友来家里玩 Switch"}],
        }
        context, tool, remembered = self._context(result)

        payload = json.loads(asyncio.run(tool.call(context, query="Switch")))

        self.assertEqual(payload["items"][0]["id"], row["id"])
        self.assertEqual(payload["journal_evidence"][0]["day"], "2026-11-18")
        self.assertEqual(remembered[0]["looki_id"], "switch")

    def test_clarification_reply_is_passed_through(self):
        result = {
            "needs_clarification": True,
            "clarification": "找到 2 条可能的记录，请补充日期。",
            "candidates": [],
            "journal_evidence": [],
        }
        context, tool, remembered = self._context(result)

        reply = asyncio.run(tool.call(context, query="生日聚会"))

        self.assertEqual(reply, "找到 2 条可能的记录，请补充日期。")
        self.assertEqual(remembered, [])

    def test_correction_prompt_stores_neutral_facts_not_bot_voice(self):
        from astrbot_plugin_looki_companion.main import AddScheduleCorrectionTool

        description = AddScheduleCorrectionTool().parameters["properties"]["note"]["description"]

        self.assertIn("中性事实", description)
        self.assertNotIn("用第一人称、", description)
        self.assertNotIn("人格与语气", description)

    def test_empty_query_and_dates_asks_for_input(self):
        context, tool, _ = self._context({"needs_clarification": False, "candidates": [], "journal_evidence": []})

        reply = asyncio.run(tool.call(context))

        self.assertIn("请提供日期范围", reply)


if __name__ == "__main__":
    unittest.main()
