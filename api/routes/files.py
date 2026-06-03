"""Local file serving route with Range support, path traversal protection,
and xECM Content Server proxy support.

Only active in local mode. Serves files from .llmwiki/cache/ (derived artifacts)
and from the workspace root (source files). xECM paths are proxied to Content Server.
"""

import logging
import mimetypes
import os
import platform
import subprocess
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from config import settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["files"])

_workspace_root: Path | None = None


def set_workspace_root(path: str) -> None:
    global _workspace_root
    _workspace_root = Path(path).resolve()


def _resolve_safe(key: str) -> Path:
    """Resolve a key to a safe path under the workspace. Rejects traversal."""
    if _workspace_root is None:
        raise HTTPException(status_code=501, detail="Local file serving not configured")

    # Try .llmwiki/cache/ first (derived artifacts), then workspace root (source files)
    cache_path = (_workspace_root / ".llmwiki" / "cache" / key).resolve()
    if cache_path.is_file() and cache_path.is_relative_to(_workspace_root):
        return cache_path

    root_path = (_workspace_root / key).resolve()
    if root_path.is_file() and root_path.is_relative_to(_workspace_root):
        return root_path

    raise HTTPException(status_code=404, detail="File not found")


async def _proxy_xecm_file(node_id: str, range_header: str | None = None):
    """Proxy a file request to xECM Content Server by node ID."""
    if not settings.XECM_ENABLED or not settings.XECM_URL:
        raise HTTPException(status_code=404, detail="xECM not configured")

    from infra.xecm import XecmClient
    xecm = XecmClient(
        url=settings.XECM_URL,
        username=settings.XECM_USERNAME,
        password=settings.XECM_PASSWORD,
    )
    try:
        ticket = await xecm.authenticate()
        client = httpx.AsyncClient(timeout=30.0)
        headers = {"OTCSTicket": ticket}
        if range_header:
            headers["Range"] = range_header

        url = f"{settings.XECM_URL.rstrip('/')}/api/v1/nodes/{node_id}/content"
        resp = await client.get(url, headers=headers, follow_redirects=True)

        content_type = resp.headers.get("content-type", "application/octet-stream")
        if resp.status_code == 206:
            return StreamingResponse(
                resp.aiter_bytes(8192),
                status_code=206,
                media_type=content_type,
                headers={
                    "Content-Range": resp.headers.get("content-range", ""),
                    "Accept-Ranges": "bytes",
                },
            )

        return StreamingResponse(
            resp.aiter_bytes(8192),
            media_type=content_type,
            headers={"Accept-Ranges": "bytes"},
        )
    finally:
        await xecm.close()


@router.get("/v1/files/{key:path}")
async def serve_file(key: str, request: Request):
    # xECM files: proxy to Content Server
    # Path format: xecm/{node_id} or xecm/{node_id}/sanitized-filename.ext
    if key.startswith("xecm/") or key.startswith("xecm\\"):
        range_header = request.headers.get("range")
        node_id = key.replace("\\", "/").split("/")[1]
        return await _proxy_xecm_file(node_id, range_header)

    path = _resolve_safe(key)

    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    file_size = path.stat().st_size
    mtime = path.stat().st_mtime
    etag = f'"{hash((path, mtime, file_size)):x}"'

    # Check If-None-Match for caching
    if_none_match = request.headers.get("if-none-match")
    if if_none_match == etag:
        return StreamingResponse(content=iter([]), status_code=304, headers={"ETag": etag})

    range_header = request.headers.get("range")

    if range_header and range_header.startswith("bytes="):
        ranges = range_header[6:]
        start_str, end_str = ranges.split("-", 1)
        start = int(start_str) if start_str else 0
        end = int(end_str) if end_str else file_size - 1
        end = min(end, file_size - 1)
        length = end - start + 1

        def iter_range():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(8192, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            content=iter_range(),
            status_code=206,
            media_type=content_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(length),
                "Accept-Ranges": "bytes",
                "ETag": etag,
            },
        )

    return FileResponse(
        path=str(path),
        media_type=content_type,
        headers={
            "Accept-Ranges": "bytes",
            "ETag": etag,
            "Content-Length": str(file_size),
        },
    )


class OpenFileRequest(BaseModel):
    path: str


@router.post("/v1/files/open")
async def open_file_externally(body: OpenFileRequest):
    """Open a file in the user's default application."""
    resolved = _resolve_safe(body.path)

    system = platform.system()
    if system == "Darwin":
        cmd = ["open", str(resolved)]
    elif system == "Linux":
        cmd = ["xdg-open", str(resolved)]
    elif system == "Windows":
        cmd = ["start", "", str(resolved)]
    else:
        raise HTTPException(status_code=501, detail=f"Unsupported platform: {system}")

    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to open file: {e}")

    return {"status": "opened", "path": body.path}
