import asyncio
import json
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from astrbot_plugin_looki_companion.main import (
    capture_mode_of,
    extract_moment_rows,
    moment_media_types,
    parse_moment,
)
from astrbot_plugin_looki_companion.schedule_store import filter_task_lines

FIXTURES = Path(__file__).parent / "fixtures"
ZONE = ZoneInfo("Asia/Shanghai")


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ParseTests(unittest.TestCase):
    def test_extract_rows_supports_list_and_dict_shapes(self):
        flat = load("moments_on_date.json")
        self.assertEqual(len(extract_moment_rows(flat)), 3)
        self.assertEqual(len(extract_moment_rows({"data": {"moments": [{"id": "a"}]}})), 1)
        self.assertEqual(len(extract_moment_rows({"data": {"items": [{"id": "b"}]}})), 1)
        self.assertEqual(len(extract_moment_rows({"data": {"data": {"items": [{"id": "c"}]}}})), 1)
        with self.assertRaises(ValueError):
            extract_moment_rows({"data": "invalid"})

    def test_parse_moment_keeps_media_and_ignores_unknown_fields(self):
        rows = extract_moment_rows(load("moments_on_date.json"))
        parsed = [parse_moment(row, ZONE)[0] for row in rows]
        self.assertEqual([item["capture_mode"] for item in parsed], ["photo", "video", "auto"])
        self.assertEqual(parsed[0]["media_types"], ["photo"])
        self.assertEqual(parsed[1]["media_types"], ["video"])
        self.assertEqual(parsed[2]["media_types"], ["photo", "video"])
        self.assertEqual(parsed[1]["audio_summary"], "期间伴有简短对话，涉及作业进度与明天的安排。")
        self.assertIn("unknown_block", parsed[2]["raw_json"])

    def test_parse_moment_returns_diagnostic_reason(self):
        item, reason = parse_moment({"title": "没有 id"}, ZONE)
        self.assertIsNone(item)
        self.assertEqual(reason, "缺少 id")
        item, reason = parse_moment({"id": "x", "start_time": "bad", "end_time": "bad"}, ZONE)
        self.assertIsNone(item)
        self.assertEqual(reason, "起止时间格式无效")

    def test_parse_moment_uses_local_date_after_timezone_conversion(self):
        moment = {
            "id": "utc-midnight",
            "date": "2026-10-24",
            "start_time": "2026-10-23T16:30:00+00:00",
            "end_time": "2026-10-23T17:30:00+00:00",
        }

        item, reason = parse_moment(moment, ZONE)

        self.assertEqual(reason, "")
        self.assertEqual(item["date"], "2026-10-24")
        self.assertEqual(item["start"], "2026-10-24T00:30:00+08:00")
        self.assertEqual(item["end"], "2026-10-24T01:30:00+08:00")

    def test_capture_mode_prefers_declared_value(self):
        self.assertEqual(capture_mode_of({"capture_mode": "AUTO"}, []), "auto")
        self.assertEqual(capture_mode_of({"capture_mode": "weird"}, []), "unknown")
        self.assertEqual(capture_mode_of({}, ["photo", "video"]), "mixed")
        self.assertEqual(capture_mode_of({}, []), "unknown")

    def test_moment_media_types_normalizes_unknown(self):
        self.assertEqual(moment_media_types({"media_types": ["SENSOR"]}), ["unknown"])
        self.assertEqual(moment_media_types({}), [])
        self.assertEqual(moment_media_types({"cover_file": {"media_type": "IMAGE"}}), ["photo"])


class PartialSyncTests(unittest.TestCase):
    def test_moments_partial_failure_is_not_reported_as_success(self):
        from astrbot_plugin_looki_companion.main import LookiScheduleProvider

        provider = LookiScheduleProvider.__new__(LookiScheduleProvider)
        provider.timeout = 1
        provider._fetch_day = AsyncMock(side_effect=[[], RuntimeError("network")])

        result = asyncio.run(provider.fetch(date(2026, 11, 20), date(2026, 11, 21), "Asia/Shanghai"))

        self.assertEqual(result, [])
        self.assertIn("部分日期同步失败", provider.last_sync_error)

    def test_journals_partial_failure_is_not_reported_as_success(self):
        from astrbot_plugin_looki_companion.main import LookiScheduleProvider

        provider = LookiScheduleProvider.__new__(LookiScheduleProvider)
        provider.timeout = 1
        provider._fetch_journals_day = AsyncMock(side_effect=[[], RuntimeError("network")])

        result = asyncio.run(provider.fetch_journals(date(2026, 11, 20), date(2026, 11, 21), "Asia/Shanghai"))

        self.assertEqual(result, [])
        self.assertIn("部分日期同步失败", provider.last_sync_error)


class TaskLineFilterTests(unittest.TestCase):
    def test_only_empty_task_lines_are_dropped(self):
        text = "- [ ]\n- [x]   \n- [ ] 打开课本\n今天在教室看书\n- 正常列表"
        kept = filter_task_lines(text)
        self.assertEqual(kept, "- [ ] 打开课本\n今天在教室看书\n- 正常列表")

    def test_real_world_summary_list_is_preserved(self):
        text = "- **关于近况与宿舍**: 和室友聊了周末安排\n- **社交互动**: 一起去了食堂"
        self.assertEqual(filter_task_lines(text), text)


if __name__ == "__main__":
    unittest.main()
