"""小红书 Web 端评论配图：先上传图片 file_id，再在评论接口中带图。

说明：官方 Web 是否完整支持带图评论存在不确定性（参见 ReaJason/xhs#154）。
若持续返回参数错误，可切换 config「reply.comment_image_payload」或仅用 App 端接口（需另行对接）。
"""
from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

import config

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resolve_disk_paths(paths: list[str]) -> list[Path]:
    """相对路径相对项目根目录。"""
    out: list[Path] = []
    for p in paths:
        if not p or not str(p).strip():
            continue
        path = Path(p.strip())
        full = path if path.is_absolute() else (PROJECT_ROOT / path).resolve()
        if not full.is_file():
            raise FileNotFoundError(f"评论配图不存在: {full}")
        out.append(full)
    return out


def _mime_for_path(path: Path) -> tuple[str, str]:
    ext = path.suffix.lower()
    if ext == ".png":
        return "image/png", "image/png"
    if ext == ".webp":
        return "image/webp", "image/webp"
    if ext in (".jpg", ".jpeg", ".jpe"):
        return "image/jpeg", "image/jpeg"
    return "image/jpeg", "image/jpeg"


def build_image_entries(client, local_paths: list[Path]) -> list[dict]:
    """与 xhs XhsClient.create_image_note 中单图结构一致，供评论接口复用。"""
    entries: list[dict] = []
    for full in local_paths:
        content_type, mime = _mime_for_path(full)
        image_id, token = client.get_upload_files_permit("image")
        client.upload_file(image_id, token, str(full), content_type=content_type)
        extra = json.dumps({"mimeType": mime}, separators=(",", ":"))
        entries.append(
            {
                "file_id": image_id,
                "metadata": {"source": -1},
                "stickers": {"version": 2, "floating": []},
                "extra_info_json": extra,
            }
        )
        logger.debug(f"评论配图已上传 file_id 前缀: {image_id[:16]}…")
    return entries


def post_web_comment(
    client,
    note_id: str,
    content: str,
    image_entries: list[dict] | None,
) -> object:
    """
    调用 /api/sns/web/v1/comment/post，按配置尝试不同载荷（带图 or 纯文）。
    """
    uri = "/api/sns/web/v1/comment/post"
    text = (content or "").strip()
    if not text and not image_entries:
        raise ValueError("评论文字与图片不能同时为空")

    # 仅文字
    if not image_entries:
        return client.comment_note(note_id, text)

    style = (config.get("reply.comment_image_payload", "image_info") or "image_info").lower()
    fallbacks = (config.get("reply.comment_image_payload_fallback", True) is not False)

    def _body_image_info() -> dict:
        return {
            "note_id": note_id,
            "content": text,
            "at_users": [],
            "image_info": {"images": image_entries},
        }

    def _body_pictures() -> dict:
        return {
            "note_id": note_id,
            "content": text,
            "at_users": [],
            "pictures": [{"file_id": e["file_id"]} for e in image_entries],
        }

    order: list[str]
    if style == "pictures":
        order = ["pictures", "image_info"] if fallbacks else ["pictures"]
    else:
        order = ["image_info", "pictures"] if fallbacks else ["image_info"]

    last_err: Exception | None = None
    for key in order:
        data = _body_pictures() if key == "pictures" else _body_image_info()
        try:
            return client.post(uri, data)
        except Exception as e:
            last_err = e
            if not fallbacks or key == order[-1]:
                break
            logger.warning(f"评论带图使用「{key}」载荷失败，尝试下一种: {e}")

    if last_err:
        raise last_err
    raise RuntimeError("post_web_comment: 未发送")
