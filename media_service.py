from __future__ import annotations

import asyncio
from typing import Any

import httpx

from astrbot.api import logger

# 与 main.moment_media_types 的归一化保持一致：photo/video/audio/transcript/unknown
_MEDIA_ALIASES = (
    ("image", "photo"),
    ("photo", "photo"),
    ("picture", "photo"),
    ("video", "video"),
    ("movie", "video"),
    ("audio", "audio"),
    ("voice", "audio"),
    ("sound", "audio"),
    ("transcript", "transcript"),
    ("text", "transcript"),
)


def normalize_media_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    for alias, normalized in _MEDIA_ALIASES:
        if text.startswith(alias):
            return normalized
    return "unknown"


def _items_of(payload: Any) -> list[Any]:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    for _ in range(3):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("items", "files"):
                rows = data.get(key)
                if isinstance(rows, list):
                    return rows
            data = data.get("data")
            continue
        break
    return []


def _file_object(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    file_obj = raw.get("file")
    if isinstance(file_obj, dict):
        return file_obj
    return {key: value for key, value in raw.items() if key not in ("thumbnail",)}


def _url_in(file_obj: dict[str, Any]) -> str:
    for key in ("temporary_url", "url", "signed_url", "download_url"):
        value = file_obj.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return ""


def describe_entry(raw: Any, index: int) -> dict[str, Any] | None:
    """把 /files 的一项转成证据描述。AUDIO/SENSOR 等未知类型也返回，不再静默丢弃。"""
    file_obj = _file_object(raw)
    if not file_obj:
        return None
    remote_id = str(raw.get("id") or file_obj.get("id") or "").strip() if isinstance(raw, dict) else ""
    mime = str(file_obj.get("mime_type") or file_obj.get("content_type") or file_obj.get("mime") or "").strip()
    kind = file_obj.get("media_type") or file_obj.get("file_type") or mime
    media_type = normalize_media_type(kind)
    metadata = file_obj.get("metadata") if isinstance(file_obj.get("metadata"), dict) else {}

    def _int(name: str) -> int | None:
        try:
            value = metadata.get(name)
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    duration_ms = _int("duration_ms")
    if duration_ms is None:
        seconds = _int("duration")
        duration_ms = seconds * 1000 if seconds is not None else None
    thumbnail = raw.get("thumbnail") if isinstance(raw, dict) else None
    has_thumbnail = isinstance(thumbnail, dict) and bool(_url_in(thumbnail))
    if media_type == "unknown":
        availability = "unsupported"
    else:
        availability = "available" if _url_in(file_obj) else "denied"
    key = f"file:{remote_id}" if remote_id else f"file:{index}:{media_type}:{mime}"
    return {
        "evidence_key": key,
        "media_type": media_type,
        "remote_file_id": remote_id,
        "mime_type": mime,
        "width": _int("width"),
        "height": _int("height"),
        "duration_ms": duration_ms,
        "has_thumbnail": has_thumbnail,
        "availability": availability,
        "text_excerpt": "",
    }


def summary_entry(looki_id: str, text: str) -> dict[str, Any] | None:
    """只有文本摘要、没有原始音频文件时的 summary 证据（不伪造音频）。"""
    excerpt = " ".join(str(text or "").split())
    if not excerpt:
        return None
    return {
        "evidence_key": "summary",
        "media_type": "summary",
        "remote_file_id": "",
        "mime_type": "text/plain",
        "width": None,
        "height": None,
        "duration_ms": None,
        "has_thumbnail": False,
        "availability": "available",
        "text_excerpt": excerpt[:300],
    }


async def fetch_moment_evidence(provider: Any, looki_id: str, max_pages: int = 2) -> list[dict[str, Any]] | None:
    """读取 /moments/{id}/files，最多翻 max_pages 页（默认 2 页，覆盖 20-40 个文件）。

    成功（包括"确实没有文件"）返回列表；请求失败返回 None，以便调用方不把失败标记为已同步。
    """
    entries: list[dict[str, Any]] = []
    cursor: str | None = None
    index = 0
    fetched = False
    async with httpx.AsyncClient(timeout=provider.timeout) as client:
        for _ in range(max(1, max_pages)):
            params: dict[str, Any] = {"limit": 20}
            if cursor:
                params["cursor"] = cursor
            try:
                response = await client.get(
                    f"{provider.base_url}/moments/{looki_id}/files",
                    params=params,
                    headers={"x-api-key": provider.api_key},
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                logger.info("[Looki] 读取媒体列表失败 %s：%s", looki_id[:16], type(exc).__name__)
                if not fetched:
                    return None
                break
            fetched = True
            data = payload.get("data", payload) if isinstance(payload, dict) else payload
            items = _items_of(payload)
            for raw in items:
                entry = describe_entry(raw, index)
                index += 1
                if entry:
                    entries.append(entry)
            cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not isinstance(data, dict) or not data.get("has_more") or not cursor:
                break
            await asyncio.sleep(0.2)
    return entries if fetched else None
