"""单张游戏头图下载：路径受限于图片目录，失败不写假路径。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..contracts import CoverResult, NonRetryableUpstream, ResponseShapeError
from .client import HttpClient

#: 只允许从这些主机下载图片，防止被引到内网或本地地址
ALLOWED_HOST_SUFFIXES = (
    "steamstatic.com",
    "steampowered.com",
    "akamaihd.net",
)

_CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

MAX_BYTES = 5 * 1024 * 1024


def _host_allowed(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_HOST_SUFFIXES)


def _safe_app_dir(images_dir: Path, appid: int) -> Path:
    target = (images_dir / "games" / str(int(appid))).resolve()
    root = images_dir.resolve()
    if root not in target.parents and target != root:
        raise ResponseShapeError("图片目录解析越界", detail={"path": str(target)})
    return target


def _read_sidecar(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def download_cover(
    client: HttpClient,
    appid: int,
    image_url: str | None,
    *,
    images_dir: Path,
    force: bool = False,
    target_dir: Path | None = None,
) -> CoverResult:
    """下载并落盘。已有同一 URL 的文件时跳过。"""
    if not image_url:
        return CoverResult(appid=appid, url=None, local_path=None, status="failed", note="没有头图地址")
    if not _host_allowed(image_url):
        return CoverResult(
            appid=appid,
            url=image_url,
            local_path=None,
            status="failed",
            note="图片主机不在允许列表内",
        )

    app_dir = target_dir or _safe_app_dir(images_dir, appid)
    app_dir.mkdir(parents=True, exist_ok=True)
    sidecar = app_dir / "cover.json"
    meta = _read_sidecar(sidecar)

    if not force and meta.get("url") == image_url:
        existing = app_dir / str(meta.get("file", ""))
        if meta.get("file") and existing.is_file() and existing.stat().st_size > 0:
            return CoverResult(
                appid=appid,
                url=image_url,
                local_path=str(existing),
                status="skipped",
                byte_size=existing.stat().st_size,
                note="地址未变且文件已存在",
            )

    try:
        response = client.get(image_url)
    except NonRetryableUpstream as exc:
        return CoverResult(
            appid=appid, url=image_url, local_path=None, status="failed", note=str(exc)
        )

    if not _host_allowed(response.url):
        return CoverResult(
            appid=appid,
            url=image_url,
            local_path=None,
            status="failed",
            note=f"重定向到不允许的主机: {response.url}",
        )

    body = response.content or b""
    if not body:
        return CoverResult(appid=appid, url=image_url, local_path=None, status="failed", note="空响应")
    if len(body) > MAX_BYTES:
        return CoverResult(
            appid=appid, url=image_url, local_path=None, status="failed", note="图片超过 5MB"
        )

    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    ext = _CONTENT_TYPE_EXT.get(content_type)
    if ext is None:
        return CoverResult(
            appid=appid,
            url=image_url,
            local_path=None,
            status="failed",
            note=f"不是可识别图片类型: {content_type!r}",
        )

    target = app_dir / f"header{ext}"
    fd, tmp_name = tempfile.mkstemp(dir=str(app_dir), suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except OSError as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        return CoverResult(
            appid=appid, url=image_url, local_path=None, status="failed", note=f"写入失败: {exc}"
        )

    digest = hashlib.sha256(body).hexdigest()
    sidecar.write_text(
        json.dumps(
            {
                "appid": appid,
                "url": image_url,
                "file": target.name,
                "sha256": digest,
                "bytes": len(body),
                "saved_at": int(time.time()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return CoverResult(
        appid=appid,
        url=image_url,
        local_path=str(target),
        status="downloaded",
        byte_size=len(body),
    )
