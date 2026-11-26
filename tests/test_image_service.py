import asyncio
import base64
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image as PILImage
from PIL import ImageDraw, ImageFilter

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.message.components import Image
from astrbot_plugin_looki_companion.image_service import IMAGE_RETENTION_SECONDS, VIDEO_MAX_BYTES, LookiImageService
from astrbot_plugin_looki_companion.main import LookiCompanionPlugin, SendLookiImageTool, _has_schedule_access


PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"
JPEG = b"\xff\xd8\xff" + b"generated-image"


class VideoStream:
    def __init__(self, chunks=(b"video-data",), headers=None):
        self.chunks = chunks
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk


class VideoClient:
    def __init__(self, chunks=(b"video-data",), headers=None):
        self.stream_response = VideoStream(chunks, headers)

    def stream(self, *args):
        return self.stream_response


def event(private=False, sender="user-1", group="group-1"):
    return SimpleNamespace(
        is_private_chat=lambda: private,
        get_sender_id=lambda: sender,
        get_group_id=lambda: group,
        unified_msg_origin="test-origin",
        send=AsyncMock(),
    )


class ImageToolGuidanceTests(unittest.TestCase):
    def test_whitelist_scope_applies_to_admin_in_group(self):
        plugin = SimpleNamespace(
            config={"clue_scope": "仅对白名单用户/群启用", "whitelist_group_ids": "allowed-group"},
            store=SimpleNamespace(clues=lambda *args: ["private clue"]),
        )
        current_event = SimpleNamespace(
            is_admin=lambda: True, is_private_chat=lambda: False,
            get_sender_id=lambda: "admin", get_group_id=lambda: "other-group",
        )
        req = SimpleNamespace(extra_user_content_parts=[])
        asyncio.run(LookiCompanionPlugin.add_schedule_clues(plugin, current_event, req))
        self.assertEqual(req.extra_user_content_parts, [])

        current_event.get_group_id = lambda: "allowed-group"
        asyncio.run(LookiCompanionPlugin.add_schedule_clues(plugin, current_event, req))
        self.assertTrue(any("private clue" in part.text for part in req.extra_user_content_parts))

    def test_clues_text_states_fact_boundary_and_sync_freshness(self):
        plugin = SimpleNamespace(
            config={"clue_scope": "仅对管理员私聊开放", "timezone": "Asia/Shanghai"},
            store=SimpleNamespace(
                clues=lambda *args: ["2026-11-24 09:00-10:00《开会》[Looki 记录]"],
                meta=lambda: {"last_success": "2026-11-25T08:00:00+08:00"},
            ),
        )
        current_event = SimpleNamespace(is_admin=lambda: True, is_private_chat=lambda: True)
        req = SimpleNamespace(extra_user_content_parts=[])
        asyncio.run(LookiCompanionPlugin.add_schedule_clues(plugin, current_event, req))
        text = "\n".join(part.text for part in req.extra_user_content_parts)
        self.assertIn("是用户本人的真实经历", text)
        self.assertIn("不是你自己（AI）的亲身经历", text)
        self.assertIn("最近成功同步", text)
        self.assertIn("《开会》", text)

    def test_clues_text_reports_missing_sync_state(self):
        plugin = SimpleNamespace(
            config={"clue_scope": "仅对管理员私聊开放"},
            store=SimpleNamespace(clues=lambda *args: ["clue"], meta=lambda: {}),
        )
        current_event = SimpleNamespace(is_admin=lambda: True, is_private_chat=lambda: True)
        req = SimpleNamespace(extra_user_content_parts=[])
        asyncio.run(LookiCompanionPlugin.add_schedule_clues(plugin, current_event, req))
        text = "\n".join(part.text for part in req.extra_user_content_parts)
        self.assertIn("尚无可用的成功同步记录", text)

    def test_admin_private_guidance_injected_and_off_disables(self):
        plugin = SimpleNamespace(
            config={"clue_scope": "仅对管理员私聊开放"},
            store=SimpleNamespace(clues=lambda *args: []),
        )
        current_event = SimpleNamespace(is_admin=lambda: True, is_private_chat=lambda: True)
        req = SimpleNamespace(extra_user_content_parts=[])

        asyncio.run(LookiCompanionPlugin.add_schedule_clues(plugin, current_event, req))

        text = "\n".join(part.text for part in req.extra_user_content_parts)
        self.assertIn("send_looki_image", text)
        self.assertIn("不要以不在现场或无法亲自拍摄为由拒绝", text)

        plugin.config["clue_scope"] = "关闭"
        req_off = SimpleNamespace(extra_user_content_parts=[])
        asyncio.run(LookiCompanionPlugin.add_schedule_clues(plugin, current_event, req_off))
        self.assertEqual(req_off.extra_user_content_parts, [])


class ImageServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.records = [
            {
                "looki_id": "inside",
                "start_at": "2026-10-27T09:30:00+08:00",
                "end_at": "2026-10-27T10:30:00+08:00",
                "title": "时段内",
            },
            {
                "looki_id": "outside",
                "start_at": "2026-10-27T12:00:00+08:00",
                "end_at": "2026-10-27T13:00:00+08:00",
                "title": "时段外",
            },
            {
                "looki_id": "",
                "start_at": "2026-10-27T10:00:00+08:00",
                "end_at": "2026-10-27T11:00:00+08:00",
                "title": "手动记录",
            },
        ]
        self.context = SimpleNamespace(
            llm_generate=AsyncMock(),
            get_using_provider=lambda _: None,
        )
        self.plugin = SimpleNamespace(
            config={
                "timezone": "Asia/Shanghai",
                "image_send_original": True,
                "image_selection_provider_id": "vision",
            },
            schedule_provider=SimpleNamespace(base_url="https://looki.test", api_key="key", timeout=20),
            store=SimpleNamespace(query=lambda *args: self.records),
            context=self.context,
        )
        self.service = LookiImageService(self.plugin, self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_original_mode_selects_one_and_retains_temp_dir(self):
        captured = {}

        async def download(matches, limit, work):
            captured.update(matches=matches, limit=limit, work=work)
            candidates = []
            for index in range(limit):
                path = work / f"candidate_{index + 1}.png"
                path.write_bytes(PNG)
                candidates.append({"path": path, "title": "图", "start_at": "2026-10-27T09:30:00+08:00"})
            return candidates

        self.service._download_candidates = AsyncMock(side_effect=download)
        self.service._select_candidate = AsyncMock(return_value=1)
        self.service._generate_image = AsyncMock()
        current_event = event(private=True)
        result = asyncio.run(self.service.send_period_image(
            current_event, "2026-10-27", "10:00", "11:00", "偏好全景"
        ))

        self.assertEqual([item["looki_id"] for item in captured["matches"]], ["inside"])
        self.assertEqual(captured["limit"], 6)
        self.service._select_candidate.assert_awaited_once()
        self.assertEqual(self.service._download_candidates.await_count, 1)
        self.assertEqual(result, "已发送 2026-10-27 09:30:00「图」的照片。")
        self.service._generate_image.assert_not_awaited()
        current_event.send.assert_awaited_once()
        self.assertTrue(captured["work"].exists())
        self.assertEqual(len(list(captured["work"].glob("candidate_*.png"))), 6)
        self.assertEqual(list(self.service.temp_root.iterdir()), [captured["work"]])

    def test_period_without_same_day_records_does_not_start_pipeline(self):
        self.plugin.store.query = lambda *args: []
        self.service._download_candidates = AsyncMock()
        result = asyncio.run(self.service.send_period_image(
            event(private=True), "2026-10-27", "14:00", "15:00"
        ))
        self.assertIn("该日期范围内没有", result)
        self.service._download_candidates.assert_not_awaited()
        self.assertEqual(list(self.service.temp_root.iterdir()), [])

    def test_target_period_candidates_are_not_mixed_with_neighbours(self):
        self.records.insert(1, {
            "looki_id": "nearest",
            "start_at": "2026-10-27T11:15:00+08:00",
            "end_at": "2026-10-27T11:30:00+08:00",
            "title": "最近活动",
        })
        self.plugin.config["image_candidate_count"] = 3
        calls = []

        async def download(matches, limit, work):
            calls.append(([item["looki_id"] for item in matches], limit))
            path = work / "candidate.png"
            path.write_bytes(PNG)
            return [{
                "path": path,
                "title": matches[0]["title"],
                "start_at": matches[0]["start_at"],
                "end_at": matches[0]["end_at"],
            }]

        self.service._download_candidates = AsyncMock(side_effect=download)
        self.service._select_candidate = AsyncMock(return_value=0)
        result = asyncio.run(self.service.send_period_image(
            event(private=True), "2026-10-27", "10:00", "11:00"
        ))

        self.assertEqual(calls, [(["inside"], 3)])
        self.assertIn("时段内", result)
        self.assertNotIn("附近", result)

    def test_nearby_activity_is_used_only_when_target_has_no_media(self):
        self.records.insert(1, {
            "looki_id": "nearest",
            "start_at": "2026-10-27T11:15:00+08:00",
            "end_at": "2026-10-27T11:30:00+08:00",
            "title": "最近活动",
        })
        calls = []

        async def download(matches, limit, work):
            calls.append([item["looki_id"] for item in matches])
            if matches[0]["looki_id"] == "inside":
                return []
            path = work / "candidate.png"
            path.write_bytes(PNG)
            return [{
                "path": path,
                "title": "最近活动",
                "start_at": "2026-10-27T11:15:00+08:00",
                "end_at": "2026-10-27T11:30:00+08:00",
            }]

        self.service._download_candidates = AsyncMock(side_effect=download)
        self.service._select_candidate = AsyncMock(return_value=0)
        result = asyncio.run(self.service.send_period_image(
            event(private=True), "2026-10-27", "10:00", "11:00"
        ))

        self.assertEqual(calls, [["inside"], ["nearest", "outside"]])
        self.assertIn("附近", result)
        self.assertIn("不在你指定的时段内", result)

    def test_image_tool_uses_resolved_activity_period(self):
        image_service = SimpleNamespace(send_period_image=AsyncMock(return_value="已发送具体照片"))
        activity = {
            "start_at": "2026-10-24T19:00:00+08:00",
            "end_at": "2026-10-24T20:30:00+08:00",
            "title": "和朋友玩 Switch",
        }
        intent = SimpleNamespace(resolve=lambda query, current: {
            "status": "ok", "activity": activity, "from_context": False, "reply": "", "result": None,
        })
        plugin = SimpleNamespace(
            config={"allow_group_query": False, "timezone": "Asia/Shanghai"},
            image_service=image_service,
            intent=intent,
        )
        registry = SimpleNamespace(get_registered_star=lambda _: SimpleNamespace(star_cls=plugin))
        current_event = event(private=True)
        context = ContextWrapper(context=SimpleNamespace(context=registry, event=current_event))

        result = asyncio.run(SendLookiImageTool().call(context, query="Switch", preference="清晰人像"))

        self.assertEqual(result, "已发送具体照片")
        image_service.send_period_image.assert_awaited_once_with(
            current_event, "2026-10-24", "19:00:00", "20:30:00", "清晰人像", "", "2026-10-24",
            query="Switch",
        )

    def test_image_tool_converts_activity_offset_to_configured_timezone(self):
        image_service = SimpleNamespace(send_period_image=AsyncMock(return_value="已发送"))
        activity = {
            "start_at": "2026-10-24T00:30:00+08:00",
            "end_at": "2026-10-24T01:30:00+08:00",
            "title": "跨时区活动",
        }
        intent = SimpleNamespace(resolve=lambda query, current: {
            "status": "ok", "activity": activity, "from_context": False, "reply": "", "result": None,
        })
        plugin = SimpleNamespace(
            config={"allow_group_query": False, "timezone": "America/New_York"},
            image_service=image_service,
            intent=intent,
        )
        registry = SimpleNamespace(get_registered_star=lambda _: SimpleNamespace(star_cls=plugin))
        current_event = event(private=True)
        context = ContextWrapper(context=SimpleNamespace(context=registry, event=current_event))

        asyncio.run(SendLookiImageTool().call(context, query="跨时区"))

        image_service.send_period_image.assert_awaited_once_with(
            current_event, "2026-10-23", "12:30:00", "13:30:00", "", "", "2026-10-23",
            query="跨时区",
        )

    def test_period_image_supports_cross_midnight_window(self):
        query_args = []
        records = [
            {
                "looki_id": "before-midnight",
                "start_at": "2026-10-22T22:00:00+08:00",
                "end_at": "2026-10-22T23:30:00+08:00",
                "title": "午夜前",
            },
            {
                "looki_id": "after-midnight",
                "start_at": "2026-10-23T00:00:00+08:00",
                "end_at": "2026-10-23T00:10:00+08:00",
                "title": "午夜后",
            },
        ]

        def query(*args):
            query_args.append(args)
            return records

        self.plugin.store.query = query
        captured = {}

        async def download(matches, limit, work):
            captured["matches"] = matches
            path = work / "candidate.png"
            path.write_bytes(PNG)
            return [{"path": path, "title": "Switch", "start_at": "time"}]

        self.service._download_candidates = AsyncMock(side_effect=download)
        self.service._select_candidate = AsyncMock(return_value=0)
        current_event = event(private=True)

        result = asyncio.run(self.service.send_period_image(
            current_event, "2026-10-22", "21:41:11", "00:13:55", end_date="2026-10-23"
        ))

        self.assertEqual(query_args, [("2026-10-22", "2026-10-23", "", 100)])
        self.assertEqual(
            [item["looki_id"] for item in captured["matches"]],
            ["before-midnight", "after-midnight"],
        )
        self.assertIn("「Switch」", result)
        current_event.send.assert_awaited_once()

    def test_file_url_classifies_images_and_videos_and_keeps_audio_visible(self):
        image = {"file": {"media_type": "IMAGE", "temporary_url": "https://looki.test/image"}}
        video = {"file": {"media_type": "VIDEO", "temporary_url": "https://looki.test/video"}}
        audio = {"file": {"media_type": "AUDIO", "temporary_url": "https://looki.test/audio"}}
        unknown = {"file": {"temporary_url": "https://looki.test/unknown"}}
        self.assertEqual(self.service._file_media_url(image), ("image", "https://looki.test/image"))
        self.assertEqual(self.service._file_media_url(video), ("video", "https://looki.test/video"))
        # 音频不再静默丢弃：分类仍返回 audio，由调用方交给证据层或跳过图像流水线
        self.assertEqual(self.service._file_media_url(audio), ("audio", "https://looki.test/audio"))
        self.assertEqual(self.service._file_media_url(unknown), ("image", "https://looki.test/unknown"))

    def test_image_download_streams_and_rejects_over_limit_without_writing(self):
        class ImageStream(VideoStream):
            def __init__(self):
                super().__init__((PNG[:8], b"x" * (20 * 1024 * 1024)))

        class ImageClient:
            def __init__(self):
                self.stream_response = ImageStream()
                self.stream_calls = []
                self.get_called = False

            def stream(self, *args):
                self.stream_calls.append(args)
                return self.stream_response

            async def get(self, *args, **kwargs):
                self.get_called = True
                raise AssertionError("image download must be streamed")

        client = ImageClient()
        target = self.root / "candidate"
        with self.assertRaisesRegex(ValueError, "20 MB"):
            asyncio.run(self.service._download_image(client, "https://looki.test/image", target))

        self.assertEqual(client.stream_calls, [("GET", "https://looki.test/image")])
        self.assertFalse(client.get_called)
        self.assertFalse(list(self.root.glob("candidate.*")))

    def test_video_frames_use_three_fixed_positions_and_retain_source(self):
        target = self.root / "candidate_1"
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))

        async def ffmpeg(*args, **kwargs):
            Path(args[-1]).write_bytes(JPEG)
            return process

        with (
            patch("astrbot_plugin_looki_companion.image_service.get_media_duration", AsyncMock(return_value=10000)),
            patch("astrbot_plugin_looki_companion.image_service.asyncio.create_subprocess_exec", AsyncMock(side_effect=ffmpeg)) as execute,
        ):
            output = asyncio.run(self.service._download_video_frames(VideoClient(), "https://looki.test/video", target))

        self.assertEqual([position for _, position in output], [2.0, 5.0, 8.0])
        self.assertTrue(all(path.read_bytes() == JPEG for path, _ in output))
        self.assertEqual(target.with_suffix(".mp4").read_bytes(), b"video-data")
        positions = [call.args[call.args.index("-ss") + 1] for call in execute.await_args_list]
        self.assertEqual(positions, ["2.000", "5.000", "8.000"])

    def test_video_frames_fall_back_to_first_frame_without_duration(self):
        target = self.root / "candidate_1"
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))

        async def ffmpeg(*args, **kwargs):
            Path(args[-1]).write_bytes(JPEG)
            return process

        with (
            patch("astrbot_plugin_looki_companion.image_service.get_media_duration", AsyncMock(return_value=None)),
            patch("astrbot_plugin_looki_companion.image_service.asyncio.create_subprocess_exec", AsyncMock(side_effect=ffmpeg)) as execute,
        ):
            output = asyncio.run(self.service._download_video_frames(VideoClient(), "https://looki.test/video", target))

        self.assertEqual(len(output), 1)
        self.assertEqual(output[0][1], 0.0)
        command = execute.await_args.args
        self.assertEqual(command[command.index("-ss") + 1], "0.000")

    def test_video_frames_keep_other_frames_when_one_extraction_fails(self):
        target = self.root / "candidate_1"
        attempt = 0

        async def ffmpeg(*args, **kwargs):
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                return SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(b"", b"bad frame")))
            Path(args[-1]).write_bytes(JPEG)
            return SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))

        with (
            patch("astrbot_plugin_looki_companion.image_service.get_media_duration", AsyncMock(return_value=10000)),
            patch("astrbot_plugin_looki_companion.image_service.asyncio.create_subprocess_exec", AsyncMock(side_effect=ffmpeg)),
        ):
            output = asyncio.run(self.service._download_video_frames(VideoClient(), "https://looki.test/video", target))

        self.assertEqual([position for _, position in output], [5.0, 8.0])
        self.assertEqual(len(output), 2)

    def test_video_download_rejects_content_length_over_limit(self):
        target = self.root / "candidate_1"
        client = VideoClient(headers={"Content-Length": str(VIDEO_MAX_BYTES + 1)})
        with self.assertRaisesRegex(ValueError, "50 MiB"):
            asyncio.run(self.service._download_video_frames(client, "https://looki.test/video", target))
        self.assertFalse(target.with_suffix(".mp4").exists())

    def test_video_frames_cancel_kills_process_and_removes_video(self):
        target = self.root / "candidate_1"
        process = SimpleNamespace(
            returncode=None,
            communicate=AsyncMock(side_effect=asyncio.CancelledError),
            kill=unittest.mock.Mock(),
            wait=AsyncMock(),
        )

        async def ffmpeg(*args, **kwargs):
            return process

        with (
            patch("astrbot_plugin_looki_companion.image_service.get_media_duration", AsyncMock(return_value=10000)),
            patch("astrbot_plugin_looki_companion.image_service.asyncio.create_subprocess_exec", AsyncMock(side_effect=ffmpeg)),
        ):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(self.service._download_video_frames(VideoClient(), "https://looki.test/video", target))

        process.kill.assert_called_once_with()
        process.wait.assert_awaited_once()
        self.assertFalse(target.with_suffix(".mp4").exists())

    def test_mixed_candidates_photo_first_and_stop_at_limit(self):
        files = [
            {"file": {"media_type": "VIDEO", "temporary_url": "https://looki.test/bad-video"}},
            {"file": {"media_type": "IMAGE", "temporary_url": "https://looki.test/image-1"}},
            {"file": {"media_type": "VIDEO", "temporary_url": "https://looki.test/video-2"}},
            {"file": {"media_type": "IMAGE", "temporary_url": "https://looki.test/image-3"}},
            {"file": {"media_type": "IMAGE", "temporary_url": "https://looki.test/extra"}},
        ]

        class FilesClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, *args, **kwargs):
                return SimpleNamespace(
                    raise_for_status=lambda: None,
                    json=lambda: {"files": files},
                )

        active = 0
        max_active = 0
        started = []

        async def fake_download(url, target, suffix, data):
            nonlocal active, max_active
            started.append(url)
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.sleep(0.01)
                if "bad" in url:
                    raise ValueError("bad video")
                path = target.with_suffix(suffix)
                path.write_bytes(data)
                return path
            finally:
                active -= 1

        async def download_image(client, url, target):
            return await fake_download(url, target, ".png", PNG)

        async def download_video(client, url, target):
            path = await fake_download(url, target.with_name(target.name + "_1"), ".jpg", JPEG)
            return [(path, 2.0)]

        self.service._download_image = AsyncMock(side_effect=download_image)
        self.service._download_video_frames = AsyncMock(side_effect=download_video)
        work = self.root / "period-test"
        work.mkdir()
        with (
            patch("astrbot_plugin_looki_companion.image_service.httpx.AsyncClient", lambda **kwargs: FilesClient()),
            patch("astrbot_plugin_looki_companion.image_service.LookiImageService._image_quality", return_value=(True, 1.0)),
        ):
            candidates = asyncio.run(self.service._download_candidates([self.records[0]], 3, work))

        self.assertEqual([item["path"].name for item in candidates], ["candidate_1.png", "candidate_2.png", "candidate_3_1.jpg"])
        self.assertEqual(max_active, 2)
        self.assertEqual(len(started), 4)
        # 照片优先：先取 3 张 IMAGE，再取 1 个 VIDEO 抽帧，limit 达成后不再尝试坏视频
        self.assertEqual(started, [
            "https://looki.test/image-1", "https://looki.test/extra",
            "https://looki.test/video-2", "https://looki.test/image-3",
        ])
        self.assertEqual(self.service._download_video_frames.await_count, 1)
        self.assertEqual(self.service._download_image.await_count, 3)
        self.assertTrue(all(item["path"].exists() for item in candidates))

    def test_candidates_round_robin_activities_and_space_files(self):
        matches = [dict(self.records[0]), dict(self.records[1])]
        files = {
            "inside": [f"https://looki.test/a-{index}" for index in range(5)],
            "outside": [f"https://looki.test/b-{index}" for index in range(5)],
        }

        class FilesClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, url, **kwargs):
                looki_id = url.split("/moments/")[1].split("/")[0]
                return SimpleNamespace(
                    raise_for_status=lambda: None,
                    json=lambda: {"files": [{"file": {"media_type": "IMAGE", "temporary_url": item}} for item in files[looki_id]]},
                )

        started = []

        async def download_image(client, url, target):
            started.append(url)
            path = target.with_suffix(".png")
            path.write_bytes(PNG)
            return path

        self.service._download_image = AsyncMock(side_effect=download_image)
        work = self.root / "spread-test"
        work.mkdir()
        with (
            patch("astrbot_plugin_looki_companion.image_service.httpx.AsyncClient", lambda **kwargs: FilesClient()),
            patch("astrbot_plugin_looki_companion.image_service.LookiImageService._image_quality", return_value=(True, 1.0)),
        ):
            candidates = asyncio.run(self.service._download_candidates(matches, 4, work))

        self.assertEqual(started, [
            "https://looki.test/a-0", "https://looki.test/b-0",
            "https://looki.test/a-4", "https://looki.test/b-4",
        ])
        self.assertEqual([item["title"] for item in candidates], ["时段内", "时段外", "时段内", "时段外"])

    def test_image_quality_rejects_black_blank_and_blurry_frames(self):
        detailed_path = self.root / "detailed.png"
        detailed = PILImage.new("L", (64, 64), 255)
        draw = ImageDraw.Draw(detailed)
        for offset in range(0, 64, 8):
            draw.rectangle((offset, 0, offset + 3, 63), fill=0)
        detailed.save(detailed_path)
        black_path = self.root / "black.png"
        PILImage.new("L", (64, 64), 0).save(black_path)
        blurred_path = self.root / "blurred.png"
        detailed.filter(ImageFilter.GaussianBlur(8)).save(blurred_path)

        self.assertTrue(self.service._image_quality(detailed_path)[0])
        self.assertFalse(self.service._image_quality(black_path)[0])
        self.assertFalse(self.service._image_quality(blurred_path)[0])

    def test_video_quality_fallback_keeps_sharpest_frame(self):
        files = [{"file": {"media_type": "VIDEO", "temporary_url": "https://looki.test/video"}}]

        class FilesClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, *args, **kwargs):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"files": files})

        async def download_video(client, url, target):
            frames = []
            for index in range(1, 4):
                path = target.with_name(f"{target.name}_{index}.jpg")
                path.write_bytes(JPEG)
                frames.append((path, float(index)))
            return frames

        scores = {"candidate_1_1.jpg": 0.1, "candidate_1_2.jpg": 0.9, "candidate_1_3.jpg": 0.2}
        self.service._download_video_frames = AsyncMock(side_effect=download_video)
        work = self.root / "quality-test"
        work.mkdir()
        with (
            patch("astrbot_plugin_looki_companion.image_service.httpx.AsyncClient", lambda **kwargs: FilesClient()),
            patch("astrbot_plugin_looki_companion.image_service.LookiImageService._image_quality", side_effect=lambda path: (False, scores[path.name])),
        ):
            candidates = asyncio.run(self.service._download_candidates([self.records[0]], 2, work))

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["path"].name, "candidate_1_2.jpg")
        self.assertEqual(candidates[0]["frame_position"], 2.0)

    def test_selection_uses_all_candidates_and_rejects_invalid_number(self):
        candidates = [
            {"path": self.root / f"{index}.png", "title": str(index), "start_at": "time"}
            for index in range(1, 4)
        ]
        self.context.llm_generate.return_value = SimpleNamespace(completion_text="2")
        selected = asyncio.run(self.service._select_candidate(candidates, "偏好人像", event(private=True), query="Switch"))
        self.assertEqual(selected, 1)
        call = self.context.llm_generate.await_args
        self.assertEqual(call.kwargs["chat_provider_id"], "vision")
        self.assertEqual(len(call.kwargs["image_urls"]), 3)
        self.assertIn("目标活动关键词：Switch", call.kwargs["prompt"])
        self.assertIn("用户本次选图偏好：偏好人像", call.kwargs["prompt"])
        self.assertIn("先根据画面内容匹配目标活动", call.kwargs["prompt"])

        self.context.llm_generate.return_value = SimpleNamespace(completion_text="没有有效编号")
        with self.assertRaisesRegex(ValueError, "有效的候选编号"):
            asyncio.run(self.service._select_candidate(candidates, "", event(private=True)))

    def test_selection_failure_cleans_temp_dir(self):
        captured = {}

        async def download(matches, limit, work):
            captured["work"] = work
            path = work / "candidate.png"
            path.write_bytes(PNG)
            return [{"path": path, "title": "图", "start_at": "time"}]

        self.service._download_candidates = AsyncMock(side_effect=download)
        self.service._select_candidate = AsyncMock(side_effect=ValueError("bad selection"))
        with self.assertRaisesRegex(ValueError, "bad selection"):
            asyncio.run(self.service.send_period_image(
                event(private=True), "2026-10-27", "10:00", "11:00"
            ))
        self.assertFalse(captured["work"].exists())

    def test_expired_images_clean_on_start_and_call(self):
        expired = self.service.temp_root / "period-expired"
        recent = self.service.temp_root / "period-recent"
        for directory in (expired, recent):
            directory.mkdir()
            (directory / "candidate.png").write_bytes(PNG)
        old = time.time() - IMAGE_RETENTION_SECONDS - 60
        os.utime(expired, (old, old))

        LookiImageService(self.plugin, self.root)
        self.assertFalse(expired.exists())
        self.assertTrue(recent.exists())

        expired_on_call = self.service.temp_root / "period-expired-on-call"
        expired_on_call.mkdir()
        (expired_on_call / "candidate.png").write_bytes(PNG)
        os.utime(expired_on_call, (old, old))
        self.plugin.store.query = lambda *args: []
        result = asyncio.run(self.service.send_period_image(
            event(private=True), "2026-10-27", "14:00", "15:00"
        ))
        self.assertIn("该日期范围内没有", result)
        self.assertFalse(expired_on_call.exists())
        self.assertTrue(recent.exists())

    def test_image_tool_unwraps_star_metadata(self):
        image_service = SimpleNamespace(send_period_image=AsyncMock(return_value="已发送"))
        plugin = SimpleNamespace(config={"allow_group_query": False}, image_service=image_service)
        registry = SimpleNamespace(
            get_registered_star=lambda _: SimpleNamespace(star_cls=plugin)
        )
        current_event = event(private=True)
        context = ContextWrapper(context=SimpleNamespace(context=registry, event=current_event))

        result = asyncio.run(SendLookiImageTool().call(
            context, date="2026-10-27", start_time="10:00", end_time="11:00"
        ))

        self.assertEqual(result, "已发送")
        image_service.send_period_image.assert_awaited_once_with(
            current_event, "2026-10-27", "10:00", "11:00", "", ""
        )

    def test_image_tool_passes_through_clarify_and_missing_replies(self):
        image_service = SimpleNamespace(send_period_image=AsyncMock(return_value="已发送"))
        replies = {
            "": {"status": "missing", "activity": None, "from_context": False, "reply": "想找哪一天、或者哪件事的照片？", "result": None},
            "生日": {"status": "clarify", "activity": None, "from_context": False, "reply": "找到多条可能的记录，请补充日期。", "result": None},
            "Switch": {"status": "ok", "activity": {"start_at": "2026-10-24T19:00:00+08:00", "end_at": "2026-10-24T20:00:00+08:00", "title": "Switch"}, "from_context": False, "reply": "", "result": None},
        }
        intent = SimpleNamespace(resolve=lambda query, current: replies[query])
        plugin = SimpleNamespace(
            config={"allow_group_query": False, "timezone": "Asia/Shanghai"},
            image_service=image_service,
            intent=intent,
        )
        registry = SimpleNamespace(get_registered_star=lambda _: SimpleNamespace(star_cls=plugin))
        current_event = event(private=True)
        context = ContextWrapper(context=SimpleNamespace(context=registry, event=current_event))
        tool = SendLookiImageTool()

        self.assertIn("想找哪一天", asyncio.run(tool.call(context)))
        image_service.send_period_image.assert_not_awaited()
        self.assertIn("找到多条", asyncio.run(tool.call(context, query="生日")))
        image_service.send_period_image.assert_not_awaited()
        partial = asyncio.run(tool.call(context, date="2026-10-27", start_time="10:00"))
        self.assertIn("需要同时提供", partial)
        image_service.send_period_image.assert_not_awaited()
        self.assertEqual(asyncio.run(tool.call(context, query="Switch")), "已发送")
        image_service.send_period_image.assert_awaited_once()

    def test_multimodal_generation_uses_reference_and_returns_image(self):
        reference = self.root / "reference.png"
        reference.write_bytes(PNG)
        work = self.root / "work"
        work.mkdir()
        self.plugin.config.update({
            "image_send_original": False,
            "image_generation_mode": "multimodal",
            "image_generation_provider_id": "image-model",
            "image_style_prompt": "二次元",
        })
        self.context.llm_generate.return_value = SimpleNamespace(
            result_chain=SimpleNamespace(chain=[Image.fromBytes(PNG)])
        )
        output = asyncio.run(self.service._generate_image(reference, work, "柔和水彩", event(private=True)))
        self.assertEqual(output.read_bytes(), PNG)
        call = self.context.llm_generate.await_args
        self.assertEqual(call.kwargs["chat_provider_id"], "image-model")
        self.assertEqual(call.kwargs["image_urls"], [str(reference)])
        self.assertIn("二次元", call.kwargs["prompt"])
        self.assertIn("柔和水彩", call.kwargs["prompt"])

    def test_openai_compatible_generation_uses_single_image_edit(self):
        reference = self.root / "reference.png"
        reference.write_bytes(PNG)
        work = self.root / "work"
        work.mkdir()
        self.plugin.config.update({
            "image_api_base_url": "https://image.test/v1",
            "image_api_model": "edit-model",
            "image_api_key": "secret",
        })

        class Response:
            content = b""

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"b64_json": base64.b64encode(JPEG).decode()}]}

        class Client:
            instance = None

            def __init__(self, **kwargs):
                self.post = AsyncMock(return_value=Response())
                Client.instance = self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        with patch("astrbot_plugin_looki_companion.image_service.httpx.AsyncClient", Client):
            output = asyncio.run(self.service._generate_with_api(reference, work, "prompt"))
        self.assertEqual(output.read_bytes(), JPEG)
        call = Client.instance.post.await_args
        self.assertEqual(call.args[0], "https://image.test/v1/images/edits")
        self.assertEqual(call.kwargs["data"]["model"], "edit-model")
        self.assertIn("image", call.kwargs["files"])
        self.assertNotIn("image[]", call.kwargs["files"])

    def test_generations_generation_uses_data_url_and_downloads_result(self):
        reference = self.root / "reference.jpg"
        reference.write_bytes(JPEG)
        work = self.root / "work"
        work.mkdir()
        self.plugin.config.update({
            "image_api_base_url": "https://image.test/api/plan/v3/images/generations",
            "image_api_model": "seedream-model",
            "image_api_key": "secret",
        })

        class Response:
            def __init__(self, content=b"", payload=None):
                self.content = content
                self.payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self.payload

        class Client:
            instance = None

            def __init__(self, **kwargs):
                self.post = AsyncMock(return_value=Response(payload={"data": [{"url": "https://image.test/generated.jpg"}]}))
                self.get = AsyncMock(return_value=Response(content=JPEG))
                Client.instance = self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        with patch("astrbot_plugin_looki_companion.image_service.httpx.AsyncClient", Client):
            output = asyncio.run(self.service._generate_with_api(reference, work, "prompt"))
        self.assertEqual(output.read_bytes(), JPEG)
        call = Client.instance.post.await_args
        self.assertEqual(call.args[0], "https://image.test/api/plan/v3/images/generations")
        self.assertEqual(call.kwargs["json"]["model"], "seedream-model")
        self.assertTrue(call.kwargs["json"]["image"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(call.kwargs["json"]["size"], "1K")
        self.assertFalse(call.kwargs["json"]["watermark"])
        Client.instance.get.assert_awaited_once_with("https://image.test/generated.jpg")


class WhitelistTests(unittest.TestCase):
    def test_private_group_switch_user_and_group_whitelist(self):
        plugin = SimpleNamespace(config={
            "allow_group_query": False,
            "whitelist_user_ids": "allowed-user\nuser-with-comma,，user-2",
            "whitelist_group_ids": ["allowed-group"],
        })
        self.assertTrue(_has_schedule_access(plugin, event(private=True)))
        self.assertFalse(_has_schedule_access(plugin, event()))
        self.assertTrue(_has_schedule_access(plugin, event(sender="allowed-user", group="other")))
        self.assertTrue(_has_schedule_access(plugin, event(sender="other", group="allowed-group")))
        plugin.config["allow_group_query"] = True
        self.assertTrue(_has_schedule_access(plugin, event()))


if __name__ == "__main__":
    unittest.main()
