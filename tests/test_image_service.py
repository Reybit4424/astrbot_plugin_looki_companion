import asyncio
import base64
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.message.components import Image
from astrbot_plugin_looki_companion.image_service import IMAGE_RETENTION_SECONDS, VIDEO_MAX_BYTES, LookiImageService
from astrbot_plugin_looki_companion.main import SendLookiImageTool, _has_schedule_access


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
        self.assertEqual(captured["limit"], 2)
        self.service._select_candidate.assert_awaited_once()
        self.service._generate_image.assert_not_awaited()
        current_event.send.assert_awaited_once()
        self.assertIn("只发送了最终 1 张图片", result)
        self.assertTrue(captured["work"].exists())
        self.assertEqual(len(list(captured["work"].glob("candidate_*.png"))), 2)
        self.assertEqual(list(self.service.temp_root.iterdir()), [captured["work"]])

    def test_outside_period_does_not_expand_or_download(self):
        self.service._download_candidates = AsyncMock()
        result = asyncio.run(self.service.send_period_image(
            event(private=True), "2026-10-27", "14:00", "15:00"
        ))
        self.assertIn("未扩展到其他时段", result)
        self.service._download_candidates.assert_not_awaited()
        self.assertEqual(list(self.service.temp_root.iterdir()), [])

    def test_activity_image_uses_matched_period_and_query_preference(self):
        self.plugin.store.find_latest_looki_activity = lambda query: {
            "start_at": "2026-10-24T19:00:00+08:00",
            "end_at": "2026-10-24T20:30:00+08:00",
            "title": "和朋友玩 Switch",
        }
        self.service.send_period_image = AsyncMock(return_value="已发送")
        current_event = event(private=True)

        result = asyncio.run(self.service.send_activity_image(current_event, "Switch"))

        self.service.send_period_image.assert_awaited_once_with(
            current_event, "2026-10-24", "19:00:00", "20:30:00", "Switch", "", "2026-10-24"
        )
        self.assertIn("2026-10-24 19:00:00–20:30:00", result)
        self.assertIn("和朋友玩 Switch", result)
        self.assertIn("已发送", result)

    def test_activity_image_supports_cross_midnight_period(self):
        self.plugin.store.find_latest_looki_activity = lambda query: {
            "start_at": "2026-10-22T21:41:11+08:00",
            "end_at": "2026-10-23T00:13:55+08:00",
            "title": "深夜食堂与Switch游戏局",
        }
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

        result = asyncio.run(self.service.send_activity_image(current_event, "Switch"))

        self.assertEqual(query_args, [("2026-10-22", "2026-10-23", "", 100)])
        self.assertEqual(
            [item["looki_id"] for item in captured["matches"]],
            ["before-midnight", "after-midnight"],
        )
        self.assertIn("2026-10-22 21:41:11–2026-10-23 00:13:55", result)
        current_event.send.assert_awaited_once()

    def test_activity_image_without_match_does_not_start_pipeline(self):
        self.plugin.store.find_latest_looki_activity = lambda query: None
        self.service._download_candidates = AsyncMock()
        self.service._select_candidate = AsyncMock()
        self.service._generate_image = AsyncMock()

        result = asyncio.run(self.service.send_activity_image(event(private=True), "Switch"))

        self.assertIn("未在已保存日程中找到", result)
        self.service._download_candidates.assert_not_awaited()
        self.service._select_candidate.assert_not_awaited()
        self.service._generate_image.assert_not_awaited()

    def test_file_url_classifies_images_and_videos_and_rejects_audio(self):
        image = {"file": {"media_type": "IMAGE", "temporary_url": "https://looki.test/image"}}
        video = {"file": {"media_type": "VIDEO", "temporary_url": "https://looki.test/video"}}
        audio = {"file": {"media_type": "AUDIO", "temporary_url": "https://looki.test/audio"}}
        unknown = {"file": {"temporary_url": "https://looki.test/unknown"}}
        self.assertEqual(self.service._file_media_url(image), ("image", "https://looki.test/image"))
        self.assertEqual(self.service._file_media_url(video), ("video", "https://looki.test/video"))
        self.assertEqual(self.service._file_media_url(audio), ("", ""))
        self.assertEqual(self.service._file_media_url(unknown), ("image", "https://looki.test/unknown"))

    def test_video_frame_uses_random_position_and_retains_source(self):
        target = self.root / "candidate_1"
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))

        async def ffmpeg(*args, **kwargs):
            Path(args[-1]).write_bytes(JPEG)
            return process

        with (
            patch("astrbot_plugin_looki_companion.image_service.get_media_duration", AsyncMock(return_value=10000)),
            patch("astrbot_plugin_looki_companion.image_service.random.uniform", return_value=2500.0) as random_position,
            patch("astrbot_plugin_looki_companion.image_service.asyncio.create_subprocess_exec", AsyncMock(side_effect=ffmpeg)) as execute,
        ):
            output = asyncio.run(self.service._download_video_frame(VideoClient(), "https://looki.test/video", target))

        self.assertEqual(output.read_bytes(), JPEG)
        self.assertEqual(target.with_suffix(".mp4").read_bytes(), b"video-data")
        random_position.assert_called_once_with(500.0, 9500.0)
        command = execute.await_args.args
        self.assertEqual(command[command.index("-ss") + 1], "2.500")

    def test_video_frame_falls_back_to_first_frame_without_duration(self):
        target = self.root / "candidate_1"
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))

        async def ffmpeg(*args, **kwargs):
            Path(args[-1]).write_bytes(JPEG)
            return process

        with (
            patch("astrbot_plugin_looki_companion.image_service.get_media_duration", AsyncMock(return_value=None)),
            patch("astrbot_plugin_looki_companion.image_service.random.uniform") as random_position,
            patch("astrbot_plugin_looki_companion.image_service.asyncio.create_subprocess_exec", AsyncMock(side_effect=ffmpeg)) as execute,
        ):
            asyncio.run(self.service._download_video_frame(VideoClient(), "https://looki.test/video", target))

        random_position.assert_not_called()
        command = execute.await_args.args
        self.assertEqual(command[command.index("-ss") + 1], "0.000")

    def test_video_download_rejects_content_length_over_limit(self):
        target = self.root / "candidate_1"
        client = VideoClient(headers={"Content-Length": str(VIDEO_MAX_BYTES + 1)})
        with self.assertRaisesRegex(ValueError, "50 MiB"):
            asyncio.run(self.service._download_video_frame(client, "https://looki.test/video", target))
        self.assertFalse(target.with_suffix(".mp4").exists())

    def test_mixed_candidates_preserve_order_limit_and_skip_failed_video(self):
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
            return await fake_download(url, target, ".jpg", JPEG)

        self.service._download_image = AsyncMock(side_effect=download_image)
        self.service._download_video_frame = AsyncMock(side_effect=download_video)
        work = self.root / "period-test"
        work.mkdir()
        with patch("astrbot_plugin_looki_companion.image_service.httpx.AsyncClient", lambda **kwargs: FilesClient()):
            candidates = asyncio.run(self.service._download_candidates([self.records[0]], 3, work))

        self.assertEqual([item["path"].name for item in candidates], ["candidate_2.png", "candidate_3.jpg", "candidate_4.png"])
        self.assertEqual(max_active, 2)
        self.assertEqual(len(started), 4)
        self.assertNotIn("https://looki.test/extra", started)
        self.assertEqual(self.service._download_video_frame.await_count, 2)
        self.assertEqual(self.service._download_image.await_count, 2)
        self.assertTrue(all(item["path"].exists() for item in candidates))

    def test_selection_uses_all_candidates_and_rejects_invalid_number(self):
        candidates = [
            {"path": self.root / f"{index}.png", "title": str(index), "start_at": "time"}
            for index in range(1, 4)
        ]
        self.context.llm_generate.return_value = SimpleNamespace(completion_text="2")
        selected = asyncio.run(self.service._select_candidate(candidates, "", event(private=True)))
        self.assertEqual(selected, 1)
        call = self.context.llm_generate.await_args
        self.assertEqual(call.kwargs["chat_provider_id"], "vision")
        self.assertEqual(len(call.kwargs["image_urls"]), 3)

        self.context.llm_generate.return_value = SimpleNamespace(completion_text="没有有效编号")
        with self.assertRaisesRegex(ValueError, "有效的候选编号"):
            asyncio.run(self.service._select_candidate(candidates, "", event(private=True)))

    def test_selection_failure_still_retains_temp_dir(self):
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
        self.assertTrue(captured["work"].exists())
        self.assertTrue((captured["work"] / "candidate.png").exists())

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
        result = asyncio.run(self.service.send_period_image(
            event(private=True), "2026-10-27", "14:00", "15:00"
        ))
        self.assertIn("未扩展到其他时段", result)
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

    def test_image_tool_routes_activity_and_validates_mode_parameters(self):
        image_service = SimpleNamespace(
            send_period_image=AsyncMock(),
            send_activity_image=AsyncMock(return_value="活动已发送"),
        )
        plugin = SimpleNamespace(config={"allow_group_query": False}, image_service=image_service)
        registry = SimpleNamespace(get_registered_star=lambda _: SimpleNamespace(star_cls=plugin))
        current_event = event(private=True)
        context = ContextWrapper(context=SimpleNamespace(context=registry, event=current_event))
        tool = SendLookiImageTool()

        result = asyncio.run(tool.call(context, query="Switch", preference="清晰画面"))
        self.assertEqual(result, "活动已发送")
        image_service.send_activity_image.assert_awaited_once_with(
            current_event, "Switch", "清晰画面", ""
        )
        image_service.send_period_image.assert_not_awaited()

        missing = asyncio.run(tool.call(context))
        self.assertIn("或提供 query", missing)
        partial = asyncio.run(tool.call(context, date="2026-10-27", start_time="10:00"))
        self.assertIn("需要同时提供", partial)
        image_service.send_activity_image.assert_awaited_once()

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
