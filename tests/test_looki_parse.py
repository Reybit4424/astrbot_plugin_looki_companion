import json
import unittest
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot_plugin_looki_companion.main import (
    capture_mode_of,
    extract_moment_rows,
    moment_media_types,
    parse_moment,
)
from astrbot_plugin_looki_companion.media_service import describe_entry, normalize_media_type, summary_entry
from astrbot_plugin_looki_companion.schedule_store import ScheduleStore, filter_task_lines

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

    def test_capture_mode_prefers_declared_value(self):
        self.assertEqual(capture_mode_of({"capture_mode": "AUTO"}, []), "auto")
        self.assertEqual(capture_mode_of({"capture_mode": "weird"}, []), "unknown")
        self.assertEqual(capture_mode_of({}, ["photo", "video"]), "mixed")
        self.assertEqual(capture_mode_of({}, []), "unknown")

    def test_moment_media_types_normalizes_unknown(self):
        self.assertEqual(moment_media_types({"media_types": ["SENSOR"]}), ["unknown"])
        self.assertEqual(moment_media_types({}), [])
        self.assertEqual(moment_media_types({"cover_file": {"media_type": "IMAGE"}}), ["photo"])


class MediaServiceTests(unittest.TestCase):
    def test_normalize_media_type(self):
        self.assertEqual(normalize_media_type("IMAGE"), "photo")
        self.assertEqual(normalize_media_type("audio/mp4"), "audio")
        self.assertEqual(normalize_media_type("VIDEO"), "video")
        self.assertEqual(normalize_media_type("SENSOR"), "unknown")

    def test_describe_entries_from_real_shape(self):
        items = load("files_page1.json")["data"]["items"]
        entries = [describe_entry(item, index) for index, item in enumerate(items)]
        self.assertEqual([entry["media_type"] for entry in entries], ["photo", "video", "audio", "unknown"])
        self.assertEqual(entries[0]["width"], 1600)
        self.assertTrue(entries[0]["has_thumbnail"])
        self.assertEqual(entries[1]["duration_ms"], 5119)
        self.assertEqual(entries[2]["mime_type"], "audio/mp4")
        self.assertEqual(entries[2]["duration_ms"], 90000)
        self.assertEqual(entries[2]["availability"], "available")
        self.assertEqual(entries[3]["availability"], "unsupported")
        self.assertEqual(entries[3]["remote_file_id"], "file-004")

    def test_summary_entry_is_text_evidence(self):
        entry = summary_entry("moment-1", " 期间伴有  简短对话 ")
        self.assertEqual(entry["media_type"], "summary")
        self.assertEqual(entry["text_excerpt"], "期间伴有 简短对话")
        self.assertIsNone(summary_entry("moment-1", "   "))


class TaskLineFilterTests(unittest.TestCase):
    def test_only_empty_task_lines_are_dropped(self):
        text = "- [ ]\n- [x]   \n- [ ] 打开课本\n今天在教室看书\n- 正常列表"
        kept = filter_task_lines(text)
        self.assertEqual(kept, "- [ ] 打开课本\n今天在教室看书\n- 正常列表")

    def test_real_world_summary_list_is_preserved(self):
        text = "- **关于近况与宿舍**: 和室友聊了周末安排\n- **社交互动**: 一起去了食堂"
        self.assertEqual(filter_task_lines(text), text)


class MediaEvidenceStoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.temp = tempfile.TemporaryDirectory()
        self.store = ScheduleStore(Path(self.temp.name) / "schedules.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _seed(self):
        self.store.upsert_remote([{
            "looki_id": "moment-1", "date": "2026-11-12",
            "start": "2026-11-12T08:10:00+08:00", "end": "2026-11-12T08:40:00+08:00",
            "title": "课前安静的教室", "description": "", "audio_summary": "简短对话",
            "keywords": [], "media_types": ["photo"], "capture_mode": "photo",
        }])

    def test_upsert_is_idempotent_and_summary_counts(self):
        self._seed()
        items = load("files_page1.json")["data"]["items"]
        entries = [describe_entry(item, index) for index, item in enumerate(items)]
        self.assertEqual(self.store.sync_media_evidence("moment-1", entries), 4)
        self.assertEqual(self.store.sync_media_evidence("moment-1", entries), 4)
        grouped = self.store.media_evidence(["moment-1"])["moment-1"]
        self.assertEqual(len(grouped), 4)
        counts = self.store.media_summary("2026-11-12", "2026-11-12")["moment-1"]
        self.assertEqual(counts, {"photo": 1, "video": 1, "audio": 1, "unknown": 1})

    def test_audio_is_kept_not_dropped(self):
        self._seed()
        audio = describe_entry({"id": "a1", "file": {"media_type": "AUDIO", "mime_type": "audio/mpeg"}}, 0)
        self.store.sync_media_evidence("moment-1", [audio])
        grouped = self.store.media_evidence(["moment-1"])["moment-1"]
        self.assertEqual(grouped[0]["media_type"], "audio")

    def test_pending_media_moments_marks_after_sync(self):
        self._seed()
        pending = self.store.pending_media_moments("2026-11-12", "2026-11-12", 6)
        self.assertEqual([row["looki_id"] for row in pending], ["moment-1"])
        self.store.sync_media_evidence("moment-1", [])
        self.assertEqual(self.store.pending_media_moments("2026-11-12", "2026-11-12", 6), [])


if __name__ == "__main__":
    unittest.main()
