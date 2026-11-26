import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from life_search_service import LifeSearchService, extract_terms, parse_date_hint
from journal_service import extract_journal_tasks
from schedule_store import ScheduleStore

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=TZ)


def looki(looki_id, day, start, end, title, keywords=(), description="", audio_summary="", media_types=()):
    return {
        "looki_id": looki_id,
        "date": day,
        "start": f"{day}T{start}:00+08:00",
        "end": f"{day}T{end}:00+08:00",
        "title": title,
        "keywords": list(keywords),
        "description": description,
        "audio_summary": audio_summary,
        "media_types": list(media_types),
        "capture_mode": "photo",
    }


class LifeSearchServiceTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.store = ScheduleStore(Path(self._temp.name) / "schedules.sqlite3")
        self.service = LifeSearchService(self.store)

    def tearDown(self):
        self.store.close()
        self._temp.cleanup()

    def test_matches_schedule_title_and_same_day_journal_content(self):
        self.store.upsert_remote([
            looki("xmas-1", "2025-12-25", "09:00", "11:00", "圣诞节班级活动", ["圣诞节", "礼物"], media_types=["photo"]),
        ])
        schedule_id = self.store.query("2025-12-25", "2025-12-25")[0]["id"]
        self.store.upsert_journals([{
            "id": "journal-xmas",
            "day": "2025-12-25",
            "type": "AUDIO_SUMMARY",
            "title": "圣诞节上午",
            "description": "",
            "content": "学校给每人发了糖和礼物，大家拍了合照。",
            "recorded_at": "",
            "schedule_id": schedule_id,
        }])

        result = self.service.search("上次圣诞节学校送的糖", NOW)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(len(result["candidates"]), 1)
        top = result["candidates"][0]
        self.assertEqual(top["looki_id"], "xmas-1")
        self.assertIn("journal_content", top["matched_fields"])
        self.assertTrue(top["has_photos"])
        self.assertTrue(top["is_looki"])
        self.assertEqual(result["journal_evidence"][0]["journal_id"], "journal-xmas")

    def test_strict_relative_day_excludes_other_days(self):
        self.store.upsert_remote([
            looki("basketball-y", "2026-11-19", "18:00", "19:00", "篮球训练", ["篮球"]),
            looki("basketball-t", "2026-11-20", "09:00", "10:00", "篮球训练", ["篮球"]),
        ])

        result = self.service.search("昨天的篮球训练", NOW)

        self.assertEqual([item["looki_id"] for item in result["candidates"]], ["basketball-y"])
        self.assertEqual(result["date_hint"]["start"], "2026-11-19")
        self.assertEqual(result["date_hint"]["end"], "2026-11-19")

    def test_days_ago_phrase(self):
        self.store.upsert_remote([
            looki("coffee", "2026-11-17", "15:00", "15:30", "咖啡时间", ["咖啡"], description="喝了拿铁"),
        ])

        result = self.service.search("3天前喝的咖啡", NOW)

        self.assertEqual(result["date_hint"]["start"], "2026-11-17")
        self.assertEqual(len(result["candidates"]), 1)
        self.assertFalse(result["needs_clarification"])

    def test_similar_candidates_need_clarification(self):
        self.store.upsert_remote([
            looki("birthday-1", "2026-11-01", "19:00", "21:00", "生日聚会", ["生日"]),
            looki("birthday-2", "2026-11-10", "19:00", "21:00", "生日聚会", ["生日"]),
        ])

        result = self.service.search("生日聚会", NOW)

        self.assertTrue(result["needs_clarification"])
        self.assertIn("生日聚会", result["clarification"])
        self.assertEqual(len(result["candidates"]), 2)

    def test_no_match_returns_empty_without_clarification(self):
        self.store.upsert_remote([
            looki("walk", "2026-11-19", "18:00", "19:00", "散步", ["散步"]),
        ])

        result = self.service.search("潜水艇", NOW)

        self.assertEqual(result["candidates"], [])
        self.assertFalse(result["needs_clarification"])

    def test_photo_intent_with_week_range_returns_day_candidates(self):
        self.store.upsert_remote([
            looki("week-1", "2026-11-10", "09:00", "10:00", "社团活动", ["社团"], media_types=["photo"]),
            looki("other", "2026-11-02", "09:00", "10:00", "社团活动", ["社团"], media_types=["photo"]),
        ])

        result = self.service.search("上周的照片", NOW)

        self.assertEqual(result["media_intent"], "photo")
        self.assertEqual(result["date_hint"]["start"], "2026-11-09")
        self.assertEqual(result["date_hint"]["end"], "2026-11-15")
        self.assertEqual([item["looki_id"] for item in result["candidates"]], ["week-1"])
        self.assertTrue(result["candidates"][0]["has_photos"])

    def test_explicit_date_range_is_strict_and_searches_description(self):
        self.store.upsert_remote([
            looki("gift-a", "2025-12-24", "09:00", "10:00", "班级活动", ["班级"], description="提前发了礼物"),
            looki("gift-b", "2025-12-26", "09:00", "10:00", "班级活动", ["班级"], description="又发了礼物"),
        ])

        result = self.service.search("礼物", NOW, "2025-12-24", "2025-12-24")

        self.assertEqual([item["looki_id"] for item in result["candidates"]], ["gift-a"])
        self.assertIn("description", result["candidates"][0]["matched_fields"])
        self.assertEqual(result["date_hint"]["start"], "2025-12-24")
        self.assertEqual(result["date_hint"]["end"], "2025-12-24")

    def test_explicit_date_range_rejects_reversed_bounds(self):
        with self.assertRaises(ValueError):
            self.service.search("礼物", NOW, "2025-12-26", "2025-12-24")

    def test_manual_schedule_is_not_looki_evidence(self):
        self.store.save({
            "date": "2026-11-15",
            "start": "2026-11-15T10:00:00+08:00",
            "end": "2026-11-15T11:00:00+08:00",
            "title": "星期天集市",
            "keywords": [],
        })

        result = self.service.search("星期天集市", NOW)

        self.assertEqual(len(result["candidates"]), 1)
        top = result["candidates"][0]
        self.assertEqual(top["source"], "manual")
        self.assertFalse(top["is_looki"])
        self.assertFalse(top["has_photos"])

    def test_description_only_weak_match_asks_confirmation(self):
        self.store.upsert_remote([
            looki("library", "2026-11-18", "14:00", "16:00", "图书馆自习", ["图书馆"], description="在图书馆三楼准备考试"),
        ])

        result = self.service.search("考试复习", NOW)

        self.assertEqual(len(result["candidates"]), 1)
        self.assertIn("description", result["candidates"][0]["matched_fields"])
        self.assertTrue(result["needs_clarification"])
        self.assertIn("图书馆自习", result["clarification"])

    def test_unmatched_journal_never_reaches_search_evidence(self):
        self.store.upsert_remote([
            looki("dance-1", "2026-11-18", "19:00", "20:00", "舞蹈排练", ["舞蹈"]),
        ])
        self.store.upsert_journals([{
            "id": "journal-dance",
            "day": "2026-11-18",
            "type": "AUDIO_SUMMARY",
            "title": "舞蹈排练",
            "description": "",
            "content": "今天排练了新舞。",
            "recorded_at": "",
        }])

        result = self.service.search("舞蹈排练", NOW)

        self.assertEqual(result["journal_evidence"], [])

    def test_recent_task_is_searchable_but_expired_task_is_not(self):
        tasks = extract_journal_tasks("- [ ] 补交物理作业")
        self.store.upsert_remote([
            looki("study-old", "2026-11-15", "19:00", "20:00", "晚间自习", []),
            looki("study-new", "2026-11-18", "19:00", "20:00", "午后自习", []),
        ])
        ids = {row["looki_id"]: row["id"] for row in self.store.query("2026-11-15", "2026-11-18")}
        self.store.upsert_journals([
            {
                "id": "journal-old", "day": "2026-11-15", "type": "AUDIO_SUMMARY",
                "title": "晚间自习", "description": "", "recorded_at": "",
                "schedule_id": ids["study-old"], "content": "- [ ] 补交物理作业", "tasks": tasks,
            },
            {
                "id": "journal-new", "day": "2026-11-18", "type": "AUDIO_SUMMARY",
                "title": "午后自习", "description": "", "recorded_at": "",
                "schedule_id": ids["study-new"], "content": "- [ ] 补交物理作业", "tasks": tasks,
            },
        ])

        result = self.service.search("补交物理作业", NOW)

        self.assertEqual([item["looki_id"] for item in result["candidates"]], ["study-new"])
        self.assertEqual([item["journal_id"] for item in result["journal_evidence"]], ["journal-new"])

    def test_life_schedules_includes_corrections(self):
        item_id = self.store.save({
            "date": "2026-11-16",
            "start": "2026-11-16T10:00:00+08:00",
            "end": "2026-11-16T11:00:00+08:00",
            "title": "实验室开会",
            "keywords": [],
        })
        self.store.add_correction(item_id, "实际地点是教室，不是宿舍")

        items = self.store.life_schedules("2026-11-01", "2026-11-30")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["corrections"][0]["note"], "实际地点是教室，不是宿舍")


class LifeSearchParsingTests(unittest.TestCase):
    def test_extract_terms_splits_long_chinese_runs(self):
        terms = extract_terms("上次圣诞节学校送的糖")

        self.assertIn("糖", terms)
        self.assertIn("学校", terms)
        self.assertIn("圣诞", terms)
        self.assertNotIn("上次", terms)

    def test_date_hint_holiday_is_not_strict(self):
        hint = parse_date_hint("圣诞节学校送的糖", NOW.date())

        self.assertEqual(hint.start.isoformat(), "2025-12-25")
        self.assertFalse(hint.strict)


if __name__ == "__main__":
    unittest.main()
