# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tokenless multipart ``POST /file-api/upload``: designer canvas media uploads.

The token-carrying form of the same route is the AgentServer upload bridge.
Without a token the designer posts a multipart ``file`` field; it is streamed
to ``<workspace>/uploads/designer`` and answered with the
``{"files": [...], "errors": [...]}`` shape of the AgentOS container route
and the Vite dev middleware.
"""

from __future__ import annotations

import logging
import mimetypes
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header

logger = logging.getLogger(__name__)

# Match the limits of the Vite dev middleware.
MAX_DESIGNER_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_DESIGNER_VIDEO_UPLOAD_BYTES = 1024 * 1024 * 1024
# Room for the boundary and part headers around the largest allowed file.
MAX_DESIGNER_UPLOAD_REQUEST_BYTES = MAX_DESIGNER_VIDEO_UPLOAD_BYTES + 64 * 1024

_READ_CHUNK_BYTES = 1024 * 1024
_UNSAFE_FILENAME_CHARS = '\\/:*?"<>|'


def _failure(status: int, error: str) -> tuple[int, dict[str, Any]]:
    return status, {"error": error, "files": [], "errors": [{"error": error}]}


def _safe_filename(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name
    cleaned = "".join("_" if ch < " " or ch in _UNSAFE_FILENAME_CHARS else ch for ch in name)
    return cleaned[:120] or "upload.bin"


class _DesignerUploadSink:
    """Multipart callbacks writing the first ``file`` field to disk under its media-kind limit."""

    def __init__(self, upload_dir: Path) -> None:
        self._upload_dir = upload_dir
        self._header_field = b""
        self._header_value = b""
        self._headers: dict[bytes, bytes] = {}
        self._handle: BinaryIO | None = None
        self._limit = 0
        self.path: Path | None = None
        self.filename = ""
        self.mime_type = ""
        self.size = 0
        self.too_large = False
        self.complete = False

    def callbacks(self) -> dict[str, Callable[..., None]]:
        return {
            "on_part_begin": self._on_part_begin,
            "on_header_field": self._on_header_field,
            "on_header_value": self._on_header_value,
            "on_header_end": self._on_header_end,
            "on_headers_finished": self._on_headers_finished,
            "on_part_data": self._on_part_data,
            "on_part_end": self.close,
            "on_end": self._on_end,
        }

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def discard(self) -> None:
        self.close()
        if self.path is not None:
            self.path.unlink(missing_ok=True)

    def _on_part_begin(self) -> None:
        self._headers = {}

    def _on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field += data[start:end]

    def _on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def _on_header_end(self) -> None:
        self._headers[self._header_field.lower()] = self._header_value
        self._header_field = b""
        self._header_value = b""

    def _on_headers_finished(self) -> None:
        _, options = parse_options_header(self._headers.get(b"content-disposition", b""))
        if self.path is not None or options.get(b"name") != b"file" or b"filename" not in options:
            return
        original = options[b"filename"].decode("utf-8", errors="replace")
        safe_name = _safe_filename(original)
        part_type = self._headers.get(b"content-type", b"").decode("latin-1").split(";")[0].strip().lower()
        self.filename = Path(original.replace("\\", "/")).name or safe_name
        self.mime_type = part_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        is_video = self.mime_type.startswith("video/")
        self._limit = MAX_DESIGNER_VIDEO_UPLOAD_BYTES if is_video else MAX_DESIGNER_UPLOAD_BYTES
        self._upload_dir.mkdir(parents=True, exist_ok=True)
        self.path = self._upload_dir / f"{uuid.uuid4()}_{safe_name}"
        self._handle = self.path.open("wb")

    def _on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._handle is None:
            return
        self.size += end - start
        if self.size > self._limit:
            self.too_large = True
            self.close()
            return
        self._handle.write(data[start:end])

    def _on_end(self) -> None:
        self.complete = True


def handle_designer_upload_http(
    *,
    content_type: str,
    body: BinaryIO,
    length: int,
    workspace_root: Path,
) -> tuple[int, dict[str, Any]]:
    """Stream the ``file`` field of a ``length``-byte multipart body to disk and describe it.

    The whole body is always read, so the client gets the JSON answer rather
    than a reset connection.

    Returns:
        ``(status, payload)``: 200 with ``{"ok": True, "files": [...], "errors": []}``,
        400 when the form has no file, 413 when the file exceeds its media-kind
        limit, or 500 when it cannot be written.
    """
    ctype, options = parse_options_header(content_type)
    boundary = options.get(b"boundary")
    sink = _DesignerUploadSink(workspace_root / "uploads" / "designer")
    parser: MultipartParser | None = None
    error = "missing_file"
    if ctype == b"multipart/form-data" and boundary:
        try:
            parser = MultipartParser(boundary, sink.callbacks())
            error = ""
        except FormParserError:
            pass
    remaining = length
    while remaining > 0:
        chunk = body.read(min(remaining, _READ_CHUNK_BYTES))
        if not chunk:
            break
        remaining -= len(chunk)
        if parser is None or error:
            continue
        try:
            parser.write(chunk)
        except FormParserError:
            error = "missing_file"
        except OSError:
            logger.exception("[file-api/upload] designer upload failed")
            error = "upload_failed"
    sink.close()
    if error or sink.too_large or not sink.complete or sink.path is None:
        sink.discard()
        if error == "upload_failed":
            return _failure(500, error)
        if sink.too_large:
            return _failure(413, "file_too_large")
        return _failure(400, "missing_file")
    return 200, {
        "ok": True,
        "files": [
            {
                "path": str(sink.path),
                "filename": sink.filename,
                "mime_type": sink.mime_type,
                "size": sink.size,
            }
        ],
        "errors": [],
    }


__all__ = [
    "MAX_DESIGNER_UPLOAD_BYTES",
    "MAX_DESIGNER_UPLOAD_REQUEST_BYTES",
    "MAX_DESIGNER_VIDEO_UPLOAD_BYTES",
    "handle_designer_upload_http",
]
