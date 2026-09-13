"""Image resize and encode for vision parts. No HTTP."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

from g023v2.constants import (
    MAX_IMAGE_DIM,
    MAX_IMAGE_OUTPUT_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_SOURCE_BYTES,
)

try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    _PIL_LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS", None)
    if _PIL_LANCZOS is None:
        _PIL_LANCZOS = Image.LANCZOS
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    _PIL_LANCZOS = None
    Image = None  # type: ignore[assignment]


def _flatten_to_rgb(img):
    """Convert any Pillow image mode to RGB, compositing alpha onto white."""
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return img.convert("RGB")


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def sniff_image_mime(data: bytes) -> str | None:
    """Return an image MIME type from magic bytes, or None if not a known image."""
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def sniff_image_size(data: bytes) -> tuple[int, int] | None:
    """Return (width, height) from image headers, or None if unknown."""
    mime = sniff_image_mime(data)
    if mime == "image/png":
        return _png_size(data)
    if mime == "image/gif":
        return _gif_size(data)
    if mime == "image/jpeg":
        return _jpeg_size(data)
    if mime == "image/webp":
        return _webp_size(data)
    return None


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24:
        return None
    w = int.from_bytes(data[16:20], "big")
    h = int.from_bytes(data[20:24], "big")
    if w < 1 or h < 1:
        return None
    return w, h


def _gif_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10:
        return None
    w = int.from_bytes(data[6:8], "little")
    h = int.from_bytes(data[8:10], "little")
    if w < 1 or h < 1:
        return None
    return w, h


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    n = len(data)
    if n < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 3 < n:
        if data[i] != 0xFF:
            return None
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            return None
        marker = data[i]
        i += 1
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if i + 1 >= n:
            return None
        seglen = int.from_bytes(data[i : i + 2], "big")
        if seglen < 2 or i + seglen > n:
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if seglen < 7:
                return None
            h = int.from_bytes(data[i + 3 : i + 5], "big")
            w = int.from_bytes(data[i + 5 : i + 7], "big")
            if w < 1 or h < 1:
                return None
            return w, h
        i += seglen
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    fourcc = data[12:16]
    if fourcc == b"VP8X":
        w = 1 + int.from_bytes(data[24:27], "little")
        h = 1 + int.from_bytes(data[27:30], "little")
    elif fourcc == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        w = int.from_bytes(data[26:28], "little") & 0x3FFF
        h = int.from_bytes(data[28:30], "little") & 0x3FFF
    elif fourcc == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        w = (bits & 0x3FFF) + 1
        h = ((bits >> 14) & 0x3FFF) + 1
    else:
        return None
    if w < 1 or h < 1:
        return None
    return w, h


def _refuse(message: str) -> dict[str, Any]:
    return {"type": "input_text", "text": message}


def _refuse_oversize_without_pil(data: bytes, source_label: str) -> dict[str, Any] | None:
    """Without Pillow we cannot constrain-resize; refuse known oversized images."""
    size = sniff_image_size(data)
    if size is None:
        return None
    w, h = size
    if max(w, h) <= MAX_IMAGE_DIM:
        return None
    return _refuse(
        f"[image exceeds {MAX_IMAGE_DIM}px without Pillow: {source_label} is "
        f"{w}x{h}; install Pillow to constrain-resize]"
    )


def _encode_part(mime: str, data: bytes) -> dict[str, Any]:
    if len(data) > MAX_IMAGE_OUTPUT_BYTES:
        return _refuse(
            f"[image too large after processing: {len(data)} bytes, "
            f"limit {MAX_IMAGE_OUTPUT_BYTES}]"
        )
    encoded = base64.b64encode(data).decode("ascii")
    return {
        "type": "input_image",
        "image_url": f"data:{mime};base64,{encoded}",
        "detail": "high",
    }


def _resize_with_pil(data: bytes, source_label: str) -> dict[str, Any]:
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            w, h = img.size
            if w * h > MAX_IMAGE_PIXELS:
                return _refuse(
                    f"[image too many pixels: {source_label} is {w}x{h}, "
                    f"limit {MAX_IMAGE_PIXELS}]"
                )
            rgb = _flatten_to_rgb(img)
            w, h = rgb.size
            longest = max(w, h)
            if longest > MAX_IMAGE_DIM:
                scale = MAX_IMAGE_DIM / longest
                new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
                rgb = rgb.resize(new_size, _PIL_LANCZOS)
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=85, optimize=True)
            return _encode_part("image/jpeg", buf.getvalue())
    except Exception as e:
        return _refuse(
            f"[failed to process image {source_label}: {type(e).__name__}: {e}]"
        )


def prepare_image_part(
    source: str,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    """Return an input_image content part for a local path, http URL, or data URL.

    Local paths must resolve inside `project_root`. With Pillow, the long side
    is constrain-resized to `MAX_IMAGE_DIM` (800px). Without Pillow, bytes are
    sniffed and labeled with the real MIME type; unknown bytes are refused, and
    a known image whose long side exceeds 800px is refused rather than sent raw.
    """
    if source.startswith(("http://", "https://")):
        return {"type": "input_image", "image_url": source, "detail": "high"}

    if source.startswith("data:"):
        comma = source.find(",")
        if comma < 0:
            return _refuse("[invalid data: URL]")
        b64 = source[comma + 1 :]
        try:
            raw = base64.b64decode(b64, validate=False)
        except Exception as e:
            return _refuse(f"[invalid data: URL: {type(e).__name__}: {e}]")
        if len(raw) > MAX_IMAGE_SOURCE_BYTES:
            return _refuse(
                f"[image too large to process: data URL is {len(raw)} bytes, "
                f"limit {MAX_IMAGE_SOURCE_BYTES}]"
            )
        if HAS_PIL:
            return _resize_with_pil(raw, "data-url")
        mime = sniff_image_mime(raw)
        if mime is None:
            header = source[:comma]
            if "image/" not in header.lower():
                return _refuse("[data: URL is not a recognized image]")
            if len(raw) > MAX_IMAGE_OUTPUT_BYTES:
                return _refuse(
                    f"[image too large after processing: {len(raw)} bytes, "
                    f"limit {MAX_IMAGE_OUTPUT_BYTES}]"
                )
            return {"type": "input_image", "image_url": source, "detail": "high"}
        oversized = _refuse_oversize_without_pil(raw, "data-url")
        if oversized is not None:
            return oversized
        return _encode_part(mime, raw)

    if project_root is None:
        return _refuse(f"[image path refused (no project root): {source}]")
    root = Path(project_root).expanduser().resolve()
    p = Path(source).expanduser()
    if not p.is_absolute():
        p = (root / p).resolve()
    else:
        p = p.resolve()
    if not _is_under(p, root):
        return _refuse(f"[image path escapes project root: {source}]")
    if not p.exists():
        return _refuse(f"[missing image: {source}]")
    if not p.is_file():
        return _refuse(f"[image path is not a file: {source}]")

    raw_size = p.stat().st_size
    if raw_size > MAX_IMAGE_SOURCE_BYTES:
        return _refuse(
            f"[image too large to process: {source} is {raw_size} bytes, "
            f"limit {MAX_IMAGE_SOURCE_BYTES}]"
        )
    data = p.read_bytes()
    if HAS_PIL:
        return _resize_with_pil(data, source)
    mime = sniff_image_mime(data)
    if mime is None:
        return _refuse(f"[not a recognized image file: {source}]")
    oversized = _refuse_oversize_without_pil(data, source)
    if oversized is not None:
        return oversized
    return _encode_part(mime, data)
