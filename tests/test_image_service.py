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
from astrbot_plugin_looki_companion.image_service import IMAGE_RETENTION_SECONDS, LookiImageService
from astrbot_plugin_looki_companion.main import SendLookiImageTool, _has_schedule_access


PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"
JPEG = b"\xff\xd8\xff" + b"generated-image"


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
                "image_candidate_count": 2,
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

    def test_file_url_accepts_images_and_rejects_video(self):
        image = {"file": {"media_type": "IMAGE", "temporary_url": "https://looki.test/image"}}
        video = {"file": {"media_type": "VIDEO", "temporary_url": "https://looki.test/video"}}
        self.assertEqual(self.service._file_image_url(image), "https://looki.test/image")
        self.assertEqual(self.service._file_image_url(video), "")

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
