#!/usr/bin/env python3
"""Album cache pipeline.

Phase 1: Extract a deduped albums manifest from an Apple Music/iTunes Library.xml
Phase 2: Resolve collection IDs and artwork URLs using iTunes Search API
Phase 3: Download source artwork from resolved Apple artwork CDN URLs
Phase 4: Generate widescreen Frame TV artwork with a protected centered album square
Review: Interactively resolve ambiguous/unresolved rows and create local overrides
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import plistlib
import random
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests import Response
from requests.exceptions import RequestException

try:
    from PIL import Image
    from PIL import ImageChops
    from PIL import ImageDraw
    from PIL import ImageOps

    PIL_AVAILABLE = True
except Exception:
    Image = None
    ImageChops = None
    ImageDraw = None
    ImageOps = None
    PIL_AVAILABLE = False

SEARCH_ENDPOINT = "https://itunes.apple.com/search"
LOOKUP_ENDPOINT = "https://itunes.apple.com/lookup"

DELUXE_KEYWORDS = (
    "deluxe",
    "expanded",
    "bonus",
    "special",
    "edition",
)
REMASTER_KEYWORDS = ("remaster", "remastered")
ANNIVERSARY_KEYWORDS = ("anniversary",)
ORIGINAL_AVOID_KEYWORDS = REMASTER_KEYWORDS + ANNIVERSARY_KEYWORDS + ("reissue",)
EXPLICIT_KEYWORDS = ("explicit",)
CLEAN_KEYWORDS = ("clean",)
MIN_IMAGE_BYTES_DEFAULT = 5 * 1024
WIDESCREEN_SKIP_BYTES_DEFAULT = 100 * 1024
OPENAI_IMAGES_EDITS_ENDPOINT = "https://api.openai.com/v1/images/edits"
OPENAI_GPT_IMAGE_SIZES = {"auto", "1024x1024", "1536x1024", "1024x1536"}
EDIT_WIDTH_DEFAULT = 1536
EDIT_HEIGHT_DEFAULT = 1024
FRAME_FINAL_WIDTH = 3840
FRAME_FINAL_HEIGHT = 2160
HA_EDIT_WIDTH = 1536
HA_EDIT_HEIGHT = 1024
HA_CENTER_SIZE = 1024
HA_PASTE_X = 256
HA_PASTE_Y = 0
HA_EDIT_SIZE = f"{HA_EDIT_WIDTH}x{HA_EDIT_HEIGHT}"
HA_OUTPAINT_PROMPT = (
    "Preserve the original album artwork's subject matter, composition, palette, and style. "
    "Do not replace the scene with unrelated content. "
    "Remove ALL text, typography, logos, label marks, and signatures from editable regions only. "
    "Do not add any new text. Reconstruct removed areas as coherent artwork matching the existing style. "
    "Then outpaint into seamless full-bleed 16:9 by extending naturally beyond the left/right edges only. "
    "No borders, no mat, no frames, no captions."
)
REFERENCE_BACKGROUND_PROMPT = (
    "Create an original seamless 16:9 full-bleed background inspired by the reference album cover's "
    "color palette, lighting, mood, and visual texture. "
    "Keep the result atmospheric and cohesive for a TV backdrop. "
    "Do not include any text, logos, labels, signatures, watermarks, faces, or copyrighted characters. "
    "Do not recreate the exact album cover composition."
)


@dataclass
class RequestState:
    consecutive_429: int = 0


@dataclass
class Candidate:
    score: float
    result: Dict[str, Any]


class ModerationBlockedError(RuntimeError):
    """Raised when image edit is blocked after the single allowed retry."""


def normalize_text(value: str) -> str:
    """Normalize text for matching and dedupe keys."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = text.lower().strip()
    text = text.replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def has_any_keyword(value: str, keywords: Tuple[str, ...]) -> bool:
    if not value:
        return False
    lower = value.lower()
    for keyword in keywords:
        if re.search(rf"\\b{re.escape(keyword)}\\b", lower):
            return True
    return False


def strip_editions(value: str) -> str:
    """Strip common edition/version qualifiers from album titles."""
    if not value:
        return ""
    text = value
    text = re.sub(
        r"\s*[\[(].*?(deluxe|expanded|remaster|remastered|edition|anniversary|mono|stereo|bonus|special|version|live).*?[\])]\s*",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\s*-\s*(deluxe|expanded|remaster|remastered|edition|anniversary|mono|stereo|bonus|special|version|live).*",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    return " ".join(text.split())


def ensure_out_dir(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)


def _track_sort_key(value: str) -> Tuple[int, str]:
    try:
        return int(value), value
    except ValueError:
        return (2**31 - 1), value


def _sleep_seconds(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def derive_artwork_url(url: str, size: int) -> str:
    if not url:
        return ""
    replaced = re.sub(r"/\d+x\d+bb", f"/{size}x{size}bb", url, count=1)
    if replaced == url:
        replaced = re.sub(r"\d+x\d+", f"{size}x{size}", url, count=1)
    return replaced


def derive_hires_artwork_url(url: str) -> str:
    if not url:
        return ""
    replaced = re.sub(r"/\d+x\d+bb", "/100000x100000-999", url, count=1)
    if replaced == url:
        replaced = re.sub(r"\d+x\d+bb", "100000x100000-999", url, count=1)
    return replaced


def choose_artwork_url(entry: Dict[str, Any], prefer_size: str) -> str:
    artwork_100 = str(entry.get("artworkUrl100", "") or "")
    artwork_600 = str(entry.get("artworkUrl600", "") or "")
    artwork_3000 = str(entry.get("artworkUrl3000", "") or "")

    if prefer_size == "hires":
        if artwork_100:
            return derive_hires_artwork_url(artwork_100)
        if artwork_600 and re.search(r"\d+x\d+bb", artwork_600):
            return derive_hires_artwork_url(artwork_600)
        return artwork_3000 or artwork_600 or artwork_100

    if prefer_size == "3000":
        return artwork_3000 or artwork_600 or artwork_100

    if prefer_size == "600":
        if artwork_600:
            return artwork_600
        if artwork_100:
            return derive_artwork_url(artwork_100, 600)
        return artwork_3000 or artwork_100

    return artwork_100 or artwork_600 or artwork_3000


def safe_filename(text_key: str, collection_id: Optional[int], limit: int = 120) -> str:
    normalized = unicodedata.normalize("NFKD", text_key or "").encode("ascii", "ignore").decode("ascii")
    normalized = normalized.lower().strip()
    normalized = normalized.replace(" — ", "__").replace(" - ", "__")
    normalized = re.sub(r"[^a-z0-9_-]+", "-", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-_")
    if not normalized:
        normalized = "album"
    cid = f"cid-{collection_id}" if collection_id is not None else "cid-unknown"
    max_base = max(16, limit - len(cid) - 2)
    if len(normalized) > max_base:
        normalized = normalized[:max_base].rstrip("-_")
    return f"{normalized}__{cid}"


def _resolve_widescreen_content_id(record: Dict[str, Any]) -> Optional[int]:
    collection_id = _safe_int(record.get("collectionId"))
    if collection_id is not None:
        return collection_id
    return _safe_int(record.get("contentId"))


def _widescreen_output_stem(content_id: Optional[int], text_key: str) -> str:
    if content_id is not None:
        return str(content_id)
    text_hash = hashlib.sha1((text_key or "").encode("utf-8")).hexdigest()[:12]
    return f"unknown__{text_hash}"


def _widescreen_output_name(content_id: Optional[int], text_key: str, width: int, height: int) -> str:
    return f"{_widescreen_output_stem(content_id, text_key)}__{width}x{height}.png"


def _legacy_widescreen_output_name(text_key: str, width: int, height: int) -> str:
    return f"{text_key}__{width}x{height}.png"


def _extension_from_url(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return suffix.lstrip(".")
    return "jpg"


def _extension_from_content_type(content_type: str) -> Optional[str]:
    value = (content_type or "").split(";", 1)[0].strip().lower()
    mapping = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }
    return mapping.get(value)


def _valid_existing(path: Path, min_bytes: int) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > min_bytes


def _find_existing_file(out_dir: Path, stem: str, format_mode: str, chosen_url: str, min_bytes: int) -> Optional[Path]:
    if format_mode == "jpg":
        path = out_dir / f"{stem}.jpg"
        return path if _valid_existing(path, min_bytes) else None
    if format_mode == "png":
        path = out_dir / f"{stem}.png"
        return path if _valid_existing(path, min_bytes) else None

    preferred = out_dir / f"{stem}.{_extension_from_url(chosen_url)}"
    if _valid_existing(preferred, min_bytes):
        return preferred
    for path in sorted(out_dir.glob(f"{stem}.*")):
        if _valid_existing(path, min_bytes):
            return path
    return None


def _verify_with_pillow(path: Path) -> Optional[str]:
    if not PIL_AVAILABLE:
        return None
    try:
        assert Image is not None
        with Image.open(path) as image:
            image.verify()
    except Exception as exc:
        return f"image verify failed: {exc}"
    return None


def _write_manifest(path: Path, entries: Dict[str, Dict[str, Any]]) -> None:
    ordered = {key: entries[key] for key in sorted(entries.keys())}
    payload = {
        "generatedAt": _utc_now_iso(),
        "entries": ordered,
    }
    write_json_atomic(path, payload)


def _write_widescreen_manifest_and_index(
    out_dir: Path,
    manifest_path: Path,
    entries: Dict[str, Dict[str, Any]],
) -> None:
    _write_manifest(manifest_path, entries)
    index_path = out_dir / "index.json"
    index: Dict[str, Dict[str, Any]] = {}
    for text_key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        collection_id = entry.get("collection_id")
        if collection_id in ("", None):
            continue
        collection_key = str(collection_id)
        index[collection_key] = {
            "text_key": text_key,
            "output_path": entry.get("output_path", ""),
            "status": entry.get("status", ""),
            "prompt_variant": entry.get("prompt_variant", ""),
            "request_id": entry.get("request_id", ""),
        }
    write_json_atomic(
        index_path,
        {
            "generatedAt": _utc_now_iso(),
            "entries": {key: index[key] for key in sorted(index.keys())},
        },
    )


def _resample_lanczos() -> Any:
    assert Image is not None
    if hasattr(Image, "Resampling"):
        return Image.Resampling.LANCZOS
    return Image.LANCZOS


def _draw_debug_crosshair(image: Any) -> None:
    assert ImageDraw is not None
    draw = ImageDraw.Draw(image)
    cx = image.width // 2
    cy = image.height // 2
    radius = 28
    draw.line((cx - radius, cy, cx + radius, cy), fill=(255, 0, 0), width=2)
    draw.line((cx, cy - radius, cx, cy + radius), fill=(255, 0, 0), width=2)


def _image_to_png_bytes(image: Any) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def validate_mask_pair(image: Any, mask: Any) -> None:
    if image.size != mask.size:
        raise RuntimeError(f"mask/image size mismatch {image.size} vs {mask.size}")


def _normalize_mask_for_openai(mask: Any) -> Any:
    """Return an RGBA mask where alpha=0 marks editable pixels."""
    assert Image is not None and ImageOps is not None
    if getattr(mask, "mode", "") == "RGBA":
        return mask
    if getattr(mask, "mode", "") == "L":
        alpha = ImageOps.invert(mask)
    else:
        alpha = ImageOps.invert(mask.convert("L"))
    rgba = Image.new("RGBA", mask.size, (0, 0, 0, 0))
    rgba.putalpha(alpha)
    return rgba


def _parse_b64_image_from_edits_payload(payload: Dict[str, Any]) -> Optional[bytes]:
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    b64_data = first.get("b64_json")
    if isinstance(b64_data, str) and b64_data:
        try:
            return base64.b64decode(b64_data)
        except Exception:
            return None
    return None


def _extract_openai_error_text(response: Response) -> Tuple[str, Optional[Dict[str, Any]]]:
    payload: Optional[Dict[str, Any]] = None
    detail = response.text.strip()
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            payload = parsed
            error = parsed.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "").strip()
                error_type = str(error.get("type") or "").strip()
                error_code = str(error.get("code") or "").strip()
                parts = [part for part in (message, error_type, error_code) if part]
                if parts:
                    detail = " | ".join(parts)
    except ValueError:
        pass

    if len(detail) > 350:
        detail = detail[:350]
    return detail, payload


def _extract_openai_request_id(response: Response, payload: Optional[Dict[str, Any]]) -> str:
    if isinstance(payload, dict):
        for key in ("request_id", "requestId", "x_request_id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        error = payload.get("error")
        if isinstance(error, dict):
            for key in ("request_id", "requestId"):
                value = error.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    for header_name in ("x-request-id", "request-id"):
        value = response.headers.get(header_name, "").strip()
        if value:
            return value
    return ""


def _is_model_not_supported_error(status_code: int, detail: str, payload: Optional[Dict[str, Any]]) -> bool:
    if status_code not in {400, 404, 422}:
        return False

    haystack_parts = [detail]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            for key in ("message", "type", "code", "param"):
                value = error.get(key)
                if isinstance(value, str):
                    haystack_parts.append(value)

    haystack = " ".join(haystack_parts).lower()
    return any(
        token in haystack
        for token in (
            "invalid model",
            "model_not_found",
            "unsupported model",
            "does not support",
            "not available",
            "unknown model",
        )
    )


def _is_moderation_blocked_error(status_code: int, detail: str, payload: Optional[Dict[str, Any]]) -> bool:
    if status_code != 400:
        return False
    haystack_parts = [detail]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            for key in ("message", "type", "code", "param"):
                value = error.get(key)
                if isinstance(value, str):
                    haystack_parts.append(value)
    haystack = " ".join(haystack_parts).lower()
    return "moderation_blocked" in haystack or "safety system" in haystack


def _is_invalid_mask_image_error_message(error_message: Optional[str]) -> bool:
    haystack = (error_message or "").lower()
    return (
        "invalid_mask_image_format" in haystack
        or "invalid mask image format" in haystack
        or "mask size does not match image size" in haystack
    )


def _is_gpt_image_model(model: str) -> bool:
    normalized = (model or "").strip().lower()
    return normalized.startswith("gpt-image-")


def _derive_openai_size(target_width: int, target_height: int) -> Optional[str]:
    size = f"{target_width}x{target_height}"
    if size in OPENAI_GPT_IMAGE_SIZES:
        return size
    return None


def _request_openai_image_edit_once(
    canvas_png: bytes,
    mask_png: bytes,
    prompt: str,
    model: str,
    seed: Optional[int],
    timeout_s: int,
    size: Optional[str],
    endpoint: str,
    api_key: str,
    multipart_mode: str,
    debug_upload_keys: bool,
    upload_debug_summary: Optional[str] = None,
) -> Tuple[Optional[bytes], Optional[str], bool, bool, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
    }

    data: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
    }
    if seed is not None:
        data["seed"] = str(seed)
    if size:
        data["size"] = size
    if multipart_mode not in {"ha", "std"}:
        raise ValueError(f"invalid multipart_mode: {multipart_mode}")

    image_field = "image[]" if multipart_mode == "ha" else "image"
    files: List[Tuple[str, Tuple[str, BytesIO, str]]] = [
        (image_field, ("input.png", BytesIO(canvas_png), "image/png")),
        ("mask", ("mask.png", BytesIO(mask_png), "image/png")),
    ]
    multipart_keys = [key for key, _ in files]
    image_like_keys = [key for key in multipart_keys if key.startswith("image")]
    if len(image_like_keys) != 1:
        raise RuntimeError(f"invalid multipart image keys: {multipart_keys}")
    if "mask" not in multipart_keys:
        raise RuntimeError(f"missing multipart mask key: {multipart_keys}")
    if multipart_mode == "ha" and "image" in multipart_keys:
        raise RuntimeError(f"ha mode forbids 'image' key in files: {multipart_keys}")
    if multipart_mode == "std" and "image[]" in multipart_keys:
        raise RuntimeError(f"std mode forbids 'image[]' key in files: {multipart_keys}")
    if "image" in data or "image[]" in data or "mask" in data:
        raise RuntimeError("multipart keys must not appear in form data")

    if debug_upload_keys:
        multipart_keys_display = "[" + ",".join(f"'{key}'" for key in multipart_keys) + "]"
        print(f"UPLOAD multipart keys: {multipart_keys_display}")

    try:
        response = requests.post(endpoint, headers=headers, data=data, files=files, timeout=timeout_s)
    except RequestException as exc:
        return None, f"image-edit request error: {exc}", False, False, ""

    if response.status_code != 200:
        detail, payload = _extract_openai_error_text(response)
        unsupported_model = _is_model_not_supported_error(response.status_code, detail, payload)
        moderation_blocked = _is_moderation_blocked_error(response.status_code, detail, payload)
        request_id = _extract_openai_request_id(response, payload)
        error_text = f"image-edit HTTP {response.status_code}: {detail}"
        if response.status_code == 400:
            body_preview = response.text.strip()
            if len(body_preview) > 800:
                body_preview = body_preview[:800]
            request_id_for_msg = request_id or "n/a"
            error_text = (
                f"{error_text} | request_id={request_id_for_msg} | body[:800]={body_preview}"
            )
            if upload_debug_summary:
                error_text = f"{error_text} | {upload_debug_summary}"
        return None, error_text, unsupported_model, moderation_blocked, request_id

    try:
        payload = response.json()
    except ValueError as exc:
        return None, f"invalid JSON from image-edit endpoint: {exc}", False, False, ""
    request_id = _extract_openai_request_id(response, payload)

    image_bytes = _parse_b64_image_from_edits_payload(payload)
    if not image_bytes:
        return None, "image-edit response missing b64_json image payload", False, False, request_id
    return image_bytes, None, False, False, request_id


def _request_openai_image_edit(
    canvas_png: bytes,
    mask_png: bytes,
    prompt: str,
    model: str,
    seed: Optional[int],
    timeout_s: int,
    size: Optional[str],
    multipart_mode: str,
    debug_upload_keys: bool,
    upload_debug_summary: Optional[str] = None,
) -> Tuple[Optional[bytes], Optional[str], Optional[str], bool, str]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None, "missing OPENAI_API_KEY", None, False, ""

    endpoint = os.environ.get("OPENAI_IMAGES_EDITS_URL", OPENAI_IMAGES_EDITS_ENDPOINT).strip()
    if not endpoint:
        endpoint = OPENAI_IMAGES_EDITS_ENDPOINT

    requested_model = (model or "").strip()
    if not requested_model:
        return None, "missing model", None, False, ""

    # HA pipeline is a single request with multipart image[] + mask.
    enforced_mode = "ha"
    if multipart_mode != enforced_mode and debug_upload_keys:
        print(f"Ignoring multipart_mode={multipart_mode}; enforcing {enforced_mode}")

    image_bytes, error_message, _unsupported_model, moderation_blocked, request_id = _request_openai_image_edit_once(
        canvas_png=canvas_png,
        mask_png=mask_png,
        prompt=prompt,
        model=requested_model,
        seed=seed,
        timeout_s=timeout_s,
        size=size,
        endpoint=endpoint,
        api_key=api_key,
        multipart_mode=enforced_mode,
        debug_upload_keys=debug_upload_keys,
        upload_debug_summary=upload_debug_summary,
    )
    if image_bytes:
        return image_bytes, None, requested_model, False, request_id
    return None, error_message or "image-edit request failed", requested_model, moderation_blocked, request_id


def _request_openai_reference_background_once(
    canvas_png: bytes,
    prompt: str,
    model: str,
    seed: Optional[int],
    timeout_s: int,
    size: Optional[str],
    endpoint: str,
    api_key: str,
    debug_upload_keys: bool,
    upload_debug_summary: Optional[str] = None,
) -> Tuple[Optional[bytes], Optional[str], bool, bool, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
    }
    data: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
    }
    if seed is not None:
        data["seed"] = str(seed)
    if size:
        data["size"] = size

    files: List[Tuple[str, Tuple[str, BytesIO, str]]] = [
        ("image[]", ("input.png", BytesIO(canvas_png), "image/png")),
    ]
    multipart_keys = [key for key, _ in files]
    if multipart_keys != ["image[]"]:
        raise RuntimeError(f"invalid multipart keys for reference mode: {multipart_keys}")
    if "image" in data or "image[]" in data or "mask" in data:
        raise RuntimeError("multipart keys must not appear in form data")

    if debug_upload_keys:
        print("UPLOAD multipart keys: ['image[]']")

    try:
        response = requests.post(endpoint, headers=headers, data=data, files=files, timeout=timeout_s)
    except RequestException as exc:
        return None, f"image-reference request error: {exc}", False, False, ""

    if response.status_code != 200:
        detail, payload = _extract_openai_error_text(response)
        unsupported_model = _is_model_not_supported_error(response.status_code, detail, payload)
        moderation_blocked = _is_moderation_blocked_error(response.status_code, detail, payload)
        request_id = _extract_openai_request_id(response, payload)
        error_text = f"image-reference HTTP {response.status_code}: {detail}"
        if response.status_code == 400:
            body_preview = response.text.strip()
            if len(body_preview) > 800:
                body_preview = body_preview[:800]
            request_id_for_msg = request_id or "n/a"
            error_text = (
                f"{error_text} | request_id={request_id_for_msg} | body[:800]={body_preview}"
            )
            if upload_debug_summary:
                error_text = f"{error_text} | {upload_debug_summary}"
        return None, error_text, unsupported_model, moderation_blocked, request_id

    try:
        payload = response.json()
    except ValueError as exc:
        return None, f"invalid JSON from image-reference endpoint: {exc}", False, False, ""
    request_id = _extract_openai_request_id(response, payload)

    image_bytes = _parse_b64_image_from_edits_payload(payload)
    if not image_bytes:
        return None, "image-reference response missing b64_json image payload", False, False, request_id
    return image_bytes, None, False, False, request_id


def _request_openai_reference_background(
    canvas_png: bytes,
    prompt: str,
    model: str,
    seed: Optional[int],
    timeout_s: int,
    size: Optional[str],
    debug_upload_keys: bool,
    upload_debug_summary: Optional[str] = None,
) -> Tuple[Optional[bytes], Optional[str], Optional[str], bool, str]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None, "missing OPENAI_API_KEY", None, False, ""

    endpoint = os.environ.get("OPENAI_IMAGES_EDITS_URL", OPENAI_IMAGES_EDITS_ENDPOINT).strip()
    if not endpoint:
        endpoint = OPENAI_IMAGES_EDITS_ENDPOINT

    requested_model = (model or "").strip()
    if not requested_model:
        return None, "missing model", None, False, ""

    image_bytes, error_message, _unsupported_model, moderation_blocked, request_id = (
        _request_openai_reference_background_once(
            canvas_png=canvas_png,
            prompt=prompt,
            model=requested_model,
            seed=seed,
            timeout_s=timeout_s,
            size=size,
            endpoint=endpoint,
            api_key=api_key,
            debug_upload_keys=debug_upload_keys,
            upload_debug_summary=upload_debug_summary,
        )
    )
    if image_bytes:
        return image_bytes, None, requested_model, False, request_id
    return None, error_message or "image-reference request failed", requested_model, moderation_blocked, request_id


def _load_album_square(source_path: Path, album_px: int) -> Any:
    assert PIL_AVAILABLE and Image is not None
    with Image.open(source_path) as source_image:
        album_rgb = source_image.convert("RGB")
    return album_rgb.resize((album_px, album_px), _resample_lanczos())


def build_ha_outpaint_canvas_and_mask(src_album_path: str) -> tuple[Image.Image, Image.Image]:
    assert PIL_AVAILABLE and Image is not None
    with Image.open(src_album_path) as source_image:
        source_rgb = source_image.convert("RGB")
    cover = source_rgb.resize((HA_CENTER_SIZE, HA_CENTER_SIZE), _resample_lanczos())
    canvas = Image.new("RGB", (HA_EDIT_WIDTH, HA_EDIT_HEIGHT), (0, 0, 0))
    canvas.paste(cover, (HA_PASTE_X, HA_PASTE_Y))
    # Editable gutters only: protect the centered 1024x1024 album square.
    # In our OpenAI normalization path, L=255 becomes alpha=0 (editable),
    # and L=0 becomes alpha=255 (protected).
    mask = Image.new("L", (HA_EDIT_WIDTH, HA_EDIT_HEIGHT), 255)
    ImageDraw.Draw(mask).rectangle(
        (HA_PASTE_X, HA_PASTE_Y, HA_PASTE_X + HA_CENTER_SIZE - 1, HA_PASTE_Y + HA_CENTER_SIZE - 1),
        fill=0,
    )
    return canvas, mask


def ha_edit_to_frame(
    img_edit: Image.Image,
    final_w: int = FRAME_FINAL_WIDTH,
    final_h: int = FRAME_FINAL_HEIGHT,
) -> Image.Image:
    assert PIL_AVAILABLE and Image is not None
    if img_edit.size != (HA_EDIT_WIDTH, HA_EDIT_HEIGHT):
        raise RuntimeError(
            f"invalid output size {img_edit.width}x{img_edit.height}; expected {HA_EDIT_WIDTH}x{HA_EDIT_HEIGHT}"
        )
    scale = final_w / HA_EDIT_WIDTH
    scaled_h = round(HA_EDIT_HEIGHT * scale)
    img_scaled = img_edit.resize((final_w, scaled_h), _resample_lanczos())
    if scaled_h < final_h:
        raise RuntimeError(f"scaled height too small for centered crop: {scaled_h} < {final_h}")
    crop_y = (scaled_h - final_h) // 2
    return img_scaled.crop((0, crop_y, final_w, crop_y + final_h))


def _pixel_identical_region(lhs: Any, rhs: Any, x: int, y: int, w: int, h: int) -> bool:
    assert ImageChops is not None
    left = lhs.crop((x, y, x + w, y + h))
    right = rhs.crop((x, y, x + w, y + h))
    diff = ImageChops.difference(left, right)
    return diff.getbbox() is None


def _replace_and_verify_album_region(image: Any, album_patch: Any, x: int, y: int, album_px: int) -> bool:
    assert ImageChops is not None
    image.paste(album_patch, (x, y))
    actual = image.crop((x, y, x + album_px, y + album_px))
    return ImageChops.difference(actual, album_patch).getbbox() is None


def _find_source_art_path(
    source_art_dir: Path,
    source_manifest_entries: Dict[str, Dict[str, Any]],
    text_key: str,
    collection_id: Optional[int],
) -> Optional[Path]:
    manifest_entry = source_manifest_entries.get(text_key, {})
    if isinstance(manifest_entry, dict):
        saved_path = manifest_entry.get("saved_path")
        if isinstance(saved_path, str) and saved_path:
            path = Path(saved_path)
            if path.exists() and path.is_file():
                return path

    stem = safe_filename(text_key, collection_id)
    for candidate in sorted(source_art_dir.glob(f"{stem}.*")):
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def generate_widescreen_artwork(
    source_art_dir: Path,
    out_dir: Path,
    map_json: Path,
    target_width: int,
    target_height: int,
    album_px: int,
    model: str,
    style_profile: str,
    resume: bool,
    sleep_ms: int,
    seed: Optional[int],
    debug_save_canvas: bool,
    debug_upload_bytes: bool,
    limit: Optional[int],
    preserve_album: bool,
    multipart_mode: str,
    background_mode: str,
) -> None:
    if not PIL_AVAILABLE or Image is None or ImageChops is None or ImageDraw is None:
        raise RuntimeError("Pillow is required for generate-widescreen. Install: pip install pillow")

    if target_width <= 0 or target_height <= 0 or album_px <= 0:
        raise ValueError("target-width, target-height, and album-px must be positive integers")
    if album_px > target_width or album_px > target_height:
        raise ValueError("album-px must be <= target dimensions")
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0")

    ensure_out_dir(out_dir)
    final_w = FRAME_FINAL_WIDTH
    final_h = FRAME_FINAL_HEIGHT
    edit_w = HA_EDIT_WIDTH
    edit_h = HA_EDIT_HEIGHT
    if target_width != final_w or target_height != final_h:
        raise ValueError(
            f"generate-widescreen currently requires target {final_w}x{final_h} for Frame TV centering"
        )
    album_px_edit = HA_CENTER_SIZE

    manifest_path = out_dir / "manifest.json"
    entries: Dict[str, Dict[str, Any]] = {}
    if manifest_path.exists():
        existing_manifest = load_json_dict(manifest_path)
        existing_entries = existing_manifest.get("entries")
        if isinstance(existing_entries, dict):
            for key, value in existing_entries.items():
                if isinstance(key, str) and isinstance(value, dict):
                    entries[key] = value

    source_manifest_entries: Dict[str, Dict[str, Any]] = {}
    source_manifest_path = source_art_dir / "manifest.json"
    if source_manifest_path.exists():
        source_manifest = load_json_dict(source_manifest_path)
        source_entries = source_manifest.get("entries")
        if isinstance(source_entries, dict):
            for key, value in source_entries.items():
                if isinstance(key, str) and isinstance(value, dict):
                    source_manifest_entries[key] = value

    album_map = load_album_map(map_json)
    text_keys = sorted(album_map.keys())
    total = len(text_keys)

    if total == 0:
        _write_widescreen_manifest_and_index(out_dir, manifest_path, entries)
        print(f"No albums found in map: {map_json}")
        print(f"Wrote: {manifest_path}")
        return

    ok_count = 0
    skip_count = 0
    err_count = 0
    generated_new_count = 0
    openai_size = HA_EDIT_SIZE

    def _build_widescreen_entry(
        *,
        text_key_value: str,
        content_id_value: Optional[int],
        source_art_path_value: str,
        output_path_value: Path,
        bytes_value: int,
        status_value: str,
        error_message_value: str,
        model_used_value: str,
        prompt_variant_value: str,
        request_id_value: str,
        geometry_value: Optional[Dict[str, Any]] = None,
        blocked_attempts_value: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "text_key": text_key_value,
            "collection_id": content_id_value or "",
            "collectionId": content_id_value or "",
            "source_art_path": source_art_path_value,
            "source_path": source_art_path_value,
            "output_path": str(output_path_value),
            "bytes": bytes_value,
            "status": status_value,
            "error_message": error_message_value,
            "timestamp": _utc_now_iso(),
            "model_requested": model,
            "model_used": model_used_value,
            "style_profile": style_profile,
            "prompt_variant": prompt_variant_value,
            "request_id": request_id_value,
            "mask_mode": (
                "gutter_outpaint_center_protected"
                if background_mode == "ha-masked-outpaint"
                else "none"
            ),
            "edit_size": f"{edit_w}x{edit_h}",
            "final_size": f"{final_w}x{final_h}",
            "album_px_edit": album_px_edit,
            "album_px_final": album_px,
            "pipeline": (
                "ha_outpaint"
                if background_mode == "ha-masked-outpaint"
                else "reference_no_mask"
            ),
            "postprocess": "scale_to_width_center_crop",
            "preserve_album": preserve_album,
        }
        if geometry_value is not None:
            entry["geometry"] = geometry_value
        if blocked_attempts_value:
            entry["blocked_attempts"] = blocked_attempts_value
        return entry

    for index, text_key in enumerate(text_keys, start=1):
        if limit is not None and generated_new_count >= limit:
            print(f"Reached --limit={limit}; stopping run.")
            break

        record = album_map.get(text_key, {})
        content_id = _resolve_widescreen_content_id(record)
        output_path = out_dir / _widescreen_output_name(content_id, text_key, target_width, target_height)
        legacy_output_path = out_dir / _legacy_widescreen_output_name(text_key, target_width, target_height)

        existing_output_path: Optional[Path] = None
        if resume:
            candidate_paths: List[Path] = [output_path, legacy_output_path]
            prior_output_path = entries.get(text_key, {}).get("output_path")
            if isinstance(prior_output_path, str) and prior_output_path.strip():
                candidate_paths.append(Path(prior_output_path))
            seen: set[str] = set()
            for candidate in candidate_paths:
                candidate_key = str(candidate)
                if candidate_key in seen:
                    continue
                seen.add(candidate_key)
                if (
                    candidate.exists()
                    and candidate.is_file()
                    and candidate.stat().st_size > WIDESCREEN_SKIP_BYTES_DEFAULT
                ):
                    existing_output_path = candidate
                    break

        if existing_output_path is not None:
            prior = entries.get(text_key, {})
            prior_used_model = ""
            if isinstance(prior.get("model_used"), str):
                prior_used_model = str(prior.get("model_used") or "")
            elif isinstance(prior.get("model"), str):
                prior_used_model = str(prior.get("model") or "")
            source_art_path = str(
                prior.get("source_art_path")
                or prior.get("source_path")
                or ""
            )
            prompt_variant = str(prior.get("prompt_variant") or "")
            request_id = str(prior.get("request_id") or "")
            existing_entry = _build_widescreen_entry(
                text_key_value=text_key,
                content_id_value=content_id,
                source_art_path_value=source_art_path,
                output_path_value=existing_output_path,
                bytes_value=existing_output_path.stat().st_size,
                status_value="skip",
                error_message_value="",
                model_used_value=prior_used_model or model,
                prompt_variant_value=prompt_variant,
                request_id_value=request_id,
                geometry_value={
                    "edit_width": edit_w,
                    "edit_height": edit_h,
                    "final_width": final_w,
                    "final_height": final_h,
                    "album_px_edit": album_px_edit,
                    "album_px_final": album_px,
                    "x0_edit": HA_PASTE_X,
                    "y0_edit": HA_PASTE_Y,
                    "x0_final": (final_w - album_px) // 2,
                    "y0_final": (final_h - album_px) // 2,
                },
            )
            entries[text_key] = existing_entry
            _write_widescreen_manifest_and_index(out_dir, manifest_path, entries)
            skip_count += 1
            print(f"Widescreen {index}/{total} (ok: {ok_count}, skip: {skip_count}, err: {err_count})")
            if index < total and sleep_ms > 0:
                _sleep_seconds(sleep_ms / 1000.0)
            continue

        source_path = _find_source_art_path(source_art_dir, source_manifest_entries, text_key, content_id)
        if source_path is None:
            entries[text_key] = _build_widescreen_entry(
                text_key_value=text_key,
                content_id_value=content_id,
                source_art_path_value="",
                output_path_value=output_path,
                bytes_value=0,
                status_value="error",
                error_message_value="source artwork not found",
                model_used_value="",
                prompt_variant_value="",
                request_id_value="",
            )
            _write_widescreen_manifest_and_index(out_dir, manifest_path, entries)
            err_count += 1
            print(f"Widescreen {index}/{total} (ok: {ok_count}, skip: {skip_count}, err: {err_count})")
            if index < total and sleep_ms > 0:
                _sleep_seconds(sleep_ms / 1000.0)
            continue

        temp_path = out_dir / f".{output_path.name}.part-{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
        used_model_for_entry = ""
        blocked_attempts: List[Dict[str, Any]] = []
        prompt_variant_used = ""
        request_id_for_entry = ""
        try:
            canvas, mask = build_ha_outpaint_canvas_and_mask(str(source_path))
            geometry = {
                "edit_width": edit_w,
                "edit_height": edit_h,
                "final_width": final_w,
                "final_height": final_h,
                "album_px_edit": HA_CENTER_SIZE,
                "album_px_final": album_px,
                "x0_edit": HA_PASTE_X,
                "y0_edit": HA_PASTE_Y,
            }

            if debug_save_canvas:
                debug_canvas = canvas.copy()
                _draw_debug_crosshair(debug_canvas)
                output_stem = _widescreen_output_stem(content_id, text_key)
                debug_canvas.save(out_dir / f"{output_stem}__debug_canvas.png", format="PNG")
                if background_mode == "ha-masked-outpaint":
                    mask.save(out_dir / f"{output_stem}__debug_mask.png", format="PNG")
                debug_last_canvas_path = out_dir / "debug_last_canvas.png"
                debug_last_mask_path = out_dir / "debug_last_mask.png"
                canvas.save(debug_last_canvas_path, format="PNG")
                if background_mode == "ha-masked-outpaint":
                    mask.save(debug_last_mask_path, format="PNG")
                    print(
                        f"Debug canvas/mask saved: {debug_last_canvas_path} ({canvas.size}), "
                        f"{debug_last_mask_path} ({mask.size})"
                    )
                else:
                    print(f"Debug canvas saved: {debug_last_canvas_path} ({canvas.size})")

            if canvas.size != (edit_w, edit_h):
                raise RuntimeError(
                    f"canvas size mismatch: canvas={canvas.size} expected={(edit_w, edit_h)}"
                )
            if openai_size != f"{edit_w}x{edit_h}":
                raise RuntimeError(
                    f"size mismatch: data[size]={openai_size} expected={edit_w}x{edit_h}"
                )

            img_bytes = _image_to_png_bytes(canvas)
            with Image.open(BytesIO(img_bytes)) as upload_image_check:
                upload_img_size = upload_image_check.size
                upload_img_mode = upload_image_check.mode
            if upload_img_size != (edit_w, edit_h):
                raise RuntimeError(
                    f"serialized upload size mismatch: upload={upload_img_size} expected={(edit_w, edit_h)}"
                )

            upload_debug_summary = f"upload_image_size={upload_img_size} upload_image_mode={upload_img_mode} edit_size={openai_size}"

            if debug_upload_bytes:
                debug_upload_dir = Path("out/_debug")
                debug_upload_dir.mkdir(parents=True, exist_ok=True)
                upload_image_path = debug_upload_dir / "upload_image.png"
                upload_image_path.write_bytes(img_bytes)
                with Image.open(upload_image_path) as saved_upload_image:
                    print(f"UPLOAD image size={saved_upload_image.size} mode={saved_upload_image.mode}")
                if background_mode == "ha-masked-outpaint":
                    upload_mask = _normalize_mask_for_openai(mask)
                    msk_bytes = _image_to_png_bytes(upload_mask)
                    upload_mask_path = debug_upload_dir / "upload_mask.png"
                    upload_mask_path.write_bytes(msk_bytes)
                    with Image.open(upload_mask_path) as saved_upload_mask:
                        print(f"UPLOAD mask  size={saved_upload_mask.size} mode={saved_upload_mask.mode}")
                print(f"UPLOAD intended edit size={openai_size}")

            generated_bytes: Optional[bytes] = None
            api_error: Optional[str] = None
            if background_mode == "ha-masked-outpaint":
                upload_mask = _normalize_mask_for_openai(mask)
                msk_bytes = _image_to_png_bytes(upload_mask)
                with Image.open(BytesIO(msk_bytes)) as upload_mask_check:
                    upload_mask_size = upload_mask_check.size
                    upload_mask_mode = upload_mask_check.mode
                if upload_img_size != upload_mask_size:
                    raise RuntimeError(
                        f"serialized upload mismatch: image={upload_img_size} mask={upload_mask_size}"
                    )
                prompt_variant_used = "ha_outpaint"
                upload_debug_summary_masked = (
                    f"{upload_debug_summary} upload_mask_size={upload_mask_size} "
                    f"upload_mask_mode={upload_mask_mode}"
                )
                generated_bytes, api_error, used_model, moderation_blocked, request_id = _request_openai_image_edit(
                    canvas_png=img_bytes,
                    mask_png=msk_bytes,
                    prompt=HA_OUTPAINT_PROMPT,
                    model=model,
                    seed=seed,
                    timeout_s=180,
                    size=openai_size,
                    multipart_mode=multipart_mode,
                    debug_upload_keys=debug_upload_bytes,
                    upload_debug_summary=upload_debug_summary_masked if debug_upload_bytes else None,
                )
            else:
                prompt_variant_used = "reference_background_nomask"
                generated_bytes, api_error, used_model, moderation_blocked, request_id = (
                    _request_openai_reference_background(
                        canvas_png=img_bytes,
                        prompt=REFERENCE_BACKGROUND_PROMPT,
                        model=model,
                        seed=seed,
                        timeout_s=180,
                        size=openai_size,
                        debug_upload_keys=debug_upload_bytes,
                        upload_debug_summary=upload_debug_summary if debug_upload_bytes else None,
                    )
                )
            request_id_for_entry = request_id or request_id_for_entry
            if used_model:
                used_model_for_entry = used_model
            elif not used_model_for_entry:
                used_model_for_entry = model

            if moderation_blocked:
                blocked_attempt = {
                    "text_key": text_key,
                    "attempt": 1,
                    "prompt_variant": prompt_variant_used,
                    "request_id": request_id,
                }
                blocked_attempts.append(blocked_attempt)
                print(
                    f"Blocked: text_key={text_key} attempt=1 "
                    f"prompt_variant={prompt_variant_used} request_id={request_id or 'n/a'}"
                )
                entries[text_key] = _build_widescreen_entry(
                    text_key_value=text_key,
                    content_id_value=content_id,
                    source_art_path_value=str(source_path),
                    output_path_value=output_path,
                    bytes_value=0,
                    status_value="blocked",
                    error_message_value=api_error or "moderation_blocked",
                    model_used_value=used_model_for_entry,
                    prompt_variant_value=prompt_variant_used,
                    request_id_value=request_id_for_entry,
                    blocked_attempts_value=blocked_attempts,
                )
                err_count += 1
                raise ModerationBlockedError(api_error or "moderation_blocked")

            if not generated_bytes:
                raise RuntimeError(api_error or "image-edit returned empty output")

            generated_edit = Image.open(io.BytesIO(generated_bytes)).convert("RGBA")
            if generated_edit.size != (edit_w, edit_h):
                raise RuntimeError(
                    f"invalid output size {generated_edit.width}x{generated_edit.height}; expected {edit_w}x{edit_h}"
                )

            generated_final = ha_edit_to_frame(generated_edit, final_w=final_w, final_h=final_h).convert("RGB")
            fx = (final_w - album_px) // 2
            fy = (final_h - album_px) // 2
            if preserve_album:
                expected_album_patch_final = _load_album_square(source_path, album_px)
                generated_final.paste(expected_album_patch_final, (fx, fy))

            generated_final.save(temp_path, format="PNG")
            temp_path.replace(output_path)

            if output_path.stat().st_size <= MIN_IMAGE_BYTES_DEFAULT:
                raise RuntimeError(f"generated file too small: {output_path.stat().st_size} bytes")

            # Final validation: exact expected dimensions.
            with Image.open(output_path) as final_image:
                if final_image.size != (final_w, final_h):
                    raise RuntimeError(
                        f"saved file has invalid size {final_image.width}x{final_image.height}; "
                        f"expected {final_w}x{final_h}"
                    )

            geometry["final_width"] = final_w
            geometry["final_height"] = final_h
            geometry["album_px_final"] = album_px
            geometry["x0_final"] = fx
            geometry["y0_final"] = fy
            entries[text_key] = _build_widescreen_entry(
                text_key_value=text_key,
                content_id_value=content_id,
                source_art_path_value=str(source_path),
                output_path_value=output_path,
                bytes_value=output_path.stat().st_size,
                status_value="ok",
                error_message_value="",
                model_used_value=used_model_for_entry,
                prompt_variant_value=prompt_variant_used,
                request_id_value=request_id_for_entry,
                geometry_value=geometry,
                blocked_attempts_value=blocked_attempts,
            )
            ok_count += 1
            generated_new_count += 1
        except ModerationBlockedError:
            continue
        except Exception as exc:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            entries[text_key] = _build_widescreen_entry(
                text_key_value=text_key,
                content_id_value=content_id,
                source_art_path_value=str(source_path),
                output_path_value=output_path,
                bytes_value=0,
                status_value="error",
                error_message_value=str(exc),
                model_used_value=used_model_for_entry,
                prompt_variant_value=prompt_variant_used,
                request_id_value=request_id_for_entry,
                blocked_attempts_value=blocked_attempts,
            )
            err_count += 1
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            _write_widescreen_manifest_and_index(out_dir, manifest_path, entries)
            print(f"Widescreen {index}/{total} (ok: {ok_count}, skip: {skip_count}, err: {err_count})")
            if index < total and sleep_ms > 0:
                _sleep_seconds(sleep_ms / 1000.0)

    print(f"Wrote: {manifest_path}")


def download_one(
    text_key: str,
    record: Dict[str, Any],
    chosen_url: str,
    out_dir: Path,
    format_mode: str,
    timeout_s: int,
    retries: int,
    resume: bool,
    min_bytes: int,
) -> Dict[str, Any]:
    collection_id = _safe_int(record.get("collectionId"))
    stem = safe_filename(text_key, collection_id)
    timestamp = _utc_now_iso()
    if not chosen_url:
        return {
            "text_key": text_key,
            "collectionId": collection_id or "",
            "chosen_url": chosen_url,
            "saved_path": "",
            "bytes": 0,
            "status": "error",
            "error_message": "missing artwork URL",
            "timestamp": timestamp,
        }

    existing = _find_existing_file(out_dir, stem, format_mode, chosen_url, min_bytes) if resume else None
    if existing is not None:
        return {
            "text_key": text_key,
            "collectionId": collection_id or "",
            "chosen_url": chosen_url,
            "saved_path": str(existing),
            "bytes": existing.stat().st_size,
            "status": "skip",
            "error_message": "",
            "timestamp": timestamp,
        }

    last_error = "download failed"
    max_attempts = max(1, retries + 1)
    for attempt in range(max_attempts):
        temp_path: Optional[Path] = None
        try:
            response = requests.get(chosen_url, timeout=timeout_s, stream=True)
            status = response.status_code
            if status != 200:
                if status in {408, 429} or status >= 500:
                    last_error = f"HTTP {status}"
                    backoff = min(2**attempt, 8) + random.uniform(0, 0.35)
                    _sleep_seconds(backoff)
                    continue
                return {
                    "text_key": text_key,
                    "collectionId": collection_id or "",
                    "chosen_url": chosen_url,
                    "saved_path": "",
                    "bytes": 0,
                    "status": "error",
                    "error_message": f"HTTP {status}",
                    "timestamp": _utc_now_iso(),
                }

            content_type = response.headers.get("Content-Type", "")
            if not content_type.lower().startswith("image/"):
                return {
                    "text_key": text_key,
                    "collectionId": collection_id or "",
                    "chosen_url": chosen_url,
                    "saved_path": "",
                    "bytes": 0,
                    "status": "error",
                    "error_message": f"invalid content-type: {content_type or 'unknown'}",
                    "timestamp": _utc_now_iso(),
                }

            extension = (
                format_mode
                if format_mode in {"jpg", "png"}
                else (_extension_from_content_type(content_type) or _extension_from_url(chosen_url))
            )
            final_path = out_dir / f"{stem}.{extension}"
            temp_path = out_dir / f"{final_path.name}.part-{int(time.time() * 1000)}-{random.randint(1000, 9999)}"

            bytes_written = 0
            with temp_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    bytes_written += len(chunk)

            if bytes_written <= min_bytes:
                last_error = f"file too small: {bytes_written} bytes"
                temp_path.unlink(missing_ok=True)
                backoff = min(2**attempt, 8) + random.uniform(0, 0.35)
                _sleep_seconds(backoff)
                continue

            verify_error = _verify_with_pillow(temp_path)
            if verify_error:
                last_error = verify_error
                temp_path.unlink(missing_ok=True)
                backoff = min(2**attempt, 8) + random.uniform(0, 0.35)
                _sleep_seconds(backoff)
                continue

            temp_path.replace(final_path)
            return {
                "text_key": text_key,
                "collectionId": collection_id or "",
                "chosen_url": chosen_url,
                "saved_path": str(final_path),
                "bytes": bytes_written,
                "status": "ok",
                "error_message": "",
                "timestamp": _utc_now_iso(),
            }
        except RequestException as exc:
            last_error = f"request error: {exc}"
            backoff = min(2**attempt, 8) + random.uniform(0, 0.35)
            _sleep_seconds(backoff)
        except OSError as exc:
            last_error = f"file error: {exc}"
            backoff = min(2**attempt, 8) + random.uniform(0, 0.35)
            _sleep_seconds(backoff)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    return {
        "text_key": text_key,
        "collectionId": collection_id or "",
        "chosen_url": chosen_url,
        "saved_path": "",
        "bytes": 0,
        "status": "error",
        "error_message": last_error,
        "timestamp": _utc_now_iso(),
    }


def _parse_retry_after(response: Response) -> Optional[int]:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def request_json_with_retries(
    session: requests.Session,
    url: str,
    params: Dict[str, Any],
    max_retries: int,
    state: RequestState,
    max_429_in_row: int,
    cooldown_seconds: int,
    verbose_429: bool,
    verbose_http: bool,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Perform a GET request with retries and polite 429 handling."""
    last_error: Optional[str] = None
    for attempt in range(max_retries):
        try:
            if verbose_http:
                print(f"GET {url} params={params}")
            response = session.get(url, params=params, timeout=20)
        except RequestException as exc:
            last_error = f"request error: {exc}"
            backoff = min(2**attempt, 60)
            _sleep_seconds(backoff)
            continue

        status = response.status_code
        if status == 200:
            state.consecutive_429 = 0
            try:
                return response.json(), None
            except ValueError as exc:
                return None, f"invalid json: {exc}"

        if status == 429:
            state.consecutive_429 += 1
            retry_after = _parse_retry_after(response)
            wait_seconds = min(retry_after if retry_after is not None else 2**attempt, 60)
            if verbose_429:
                if retry_after is not None:
                    print(f"429 Retry-After={retry_after}s -> sleeping {wait_seconds}s")
                else:
                    print(f"429 backoff -> sleeping {wait_seconds}s")
            _sleep_seconds(wait_seconds)
            if state.consecutive_429 >= max_429_in_row:
                if verbose_429:
                    print(f"429 cooldown -> sleeping {cooldown_seconds}s")
                _sleep_seconds(cooldown_seconds)
                state.consecutive_429 = 0
            last_error = "HTTP 429"
            continue

        if status >= 500 or status == 408:
            backoff = min(2**attempt, 60)
            _sleep_seconds(backoff)
            last_error = f"HTTP {status}"
            continue

        return None, f"HTTP {status}"

    return None, last_error or "max retries exceeded"


def extract_albums(library_path: Path, out_dir: Path) -> None:
    """Parse Library.xml and emit albums.json + albums.csv."""
    ensure_out_dir(out_dir)
    with library_path.open("rb") as handle:
        plist = plistlib.load(handle)

    tracks: Dict[str, Dict[str, Any]] = plist.get("Tracks", {})
    total_tracks = len(tracks)

    albums: Dict[str, Dict[str, Any]] = {}
    for track_id in sorted(tracks.keys(), key=_track_sort_key):
        track = tracks[track_id]
        album = track.get("Album")
        if not album:
            continue
        album_artist = track.get("Album Artist") or track.get("Artist")
        if not album_artist:
            continue

        text_key = f"{normalize_text(album_artist)} — {normalize_text(album)}"
        if not text_key.strip(" —"):
            continue

        entry = albums.get(text_key)
        if entry is None:
            entry = {
                "text_key": text_key,
                "album_artist": album_artist,
                "album": album,
                "representative_track": None,
            }
            albums[text_key] = entry

        if entry["representative_track"] is None:
            if all(track.get(field) for field in ("Name", "Artist", "Album", "Total Time")):
                entry["representative_track"] = {
                    "title": track.get("Name"),
                    "artist": track.get("Artist"),
                    "duration_ms": track.get("Total Time"),
                }

    album_list = sorted(
        albums.values(),
        key=lambda item: (normalize_text(item["album_artist"]), normalize_text(item["album"])),
    )

    albums_json_path = out_dir / "albums.json"
    with albums_json_path.open("w", encoding="utf-8") as handle:
        json.dump(album_list, handle, indent=2, ensure_ascii=False)

    albums_csv_path = out_dir / "albums.csv"
    with albums_csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "text_key",
                "album_artist",
                "album",
                "rep_track_title",
                "rep_track_artist",
                "rep_track_duration_ms",
            ],
        )
        writer.writeheader()
        for entry in album_list:
            rep = entry.get("representative_track") or {}
            writer.writerow(
                {
                    "text_key": entry["text_key"],
                    "album_artist": entry["album_artist"],
                    "album": entry["album"],
                    "rep_track_title": rep.get("title", ""),
                    "rep_track_artist": rep.get("artist", ""),
                    "rep_track_duration_ms": rep.get("duration_ms", ""),
                }
            )

    print(f"Total tracks: {total_tracks}")
    print(f"Total unique albums: {len(album_list)}")
    print(f"Wrote: {albums_json_path}")
    print(f"Wrote: {albums_csv_path}")


def score_song_candidate(
    result: Dict[str, Any],
    rep_track_title: str,
    rep_track_artist: str,
    rep_album: str,
    rep_duration_ms: Optional[int],
) -> float:
    score = 0.0
    track_name = result.get("trackName", "")
    artist_name = result.get("artistName", "")
    collection_name = result.get("collectionName", "")

    norm_track = normalize_text(track_name)
    norm_rep_track = normalize_text(rep_track_title)
    if norm_track and norm_track == norm_rep_track:
        score += 6.0
    elif norm_rep_track and (norm_rep_track in norm_track or norm_track in norm_rep_track):
        score += 3.0

    norm_artist = normalize_text(artist_name)
    norm_rep_artist = normalize_text(rep_track_artist)
    if norm_artist and norm_artist == norm_rep_artist:
        score += 4.0
    elif norm_rep_artist and (norm_rep_artist in norm_artist or norm_artist in norm_rep_artist):
        score += 2.0

    norm_collection = normalize_text(strip_editions(collection_name))
    norm_rep_album = normalize_text(strip_editions(rep_album))
    if norm_collection and norm_collection == norm_rep_album:
        score += 3.0
    elif norm_rep_album and (norm_rep_album in norm_collection or norm_collection in norm_rep_album):
        score += 1.5

    if rep_duration_ms and result.get("trackTimeMillis"):
        diff = abs(int(result["trackTimeMillis"]) - int(rep_duration_ms))
        if diff <= 2000:
            score += 2.0
        elif diff <= 5000:
            score += 1.0
        elif diff <= 10000:
            score += 0.5

    return score


def score_album_candidate(result: Dict[str, Any], album_title: str, album_artist: str) -> float:
    score = 0.0
    collection_name = result.get("collectionName", "")
    artist_name = result.get("artistName", "")

    lib_album_full = normalize_text(album_title)
    lib_album_core = normalize_text(strip_editions(album_title))
    cand_album_full = normalize_text(collection_name)
    cand_album_core = normalize_text(strip_editions(collection_name))

    if lib_album_full and cand_album_full == lib_album_full:
        score += 6.0
    elif lib_album_full and (lib_album_full in cand_album_full or cand_album_full in lib_album_full):
        score += 3.0

    if lib_album_core and cand_album_core == lib_album_core:
        score += 3.0
    elif lib_album_core and (lib_album_core in cand_album_core or cand_album_core in lib_album_core):
        score += 1.5

    lib_artist = normalize_text(album_artist)
    cand_artist = normalize_text(artist_name)
    if lib_artist and cand_artist == lib_artist:
        score += 3.0
    elif lib_artist and (lib_artist in cand_artist or cand_artist in lib_artist):
        score += 1.5

    return score


def _candidate_sort_key(item: Candidate) -> Tuple[float, int, str]:
    collection_id = _safe_int(item.result.get("collectionId")) or 2**31 - 1
    collection_name = normalize_text(item.result.get("collectionName", ""))
    return (-item.score, collection_id, collection_name)


def _duration_distance(candidate: Candidate, rep_duration_ms: Optional[int]) -> Optional[int]:
    if not rep_duration_ms:
        return None
    track_time = _safe_int(candidate.result.get("trackTimeMillis"))
    if not track_time:
        return None
    return abs(track_time - rep_duration_ms)


def _explicit_hint(candidate: Candidate) -> bool:
    blob = f"{candidate.result.get('collectionName', '')} {candidate.result.get('trackName', '')}"
    return has_any_keyword(blob, EXPLICIT_KEYWORDS)


def _edition_hint(candidate: Candidate) -> bool:
    blob = f"{candidate.result.get('collectionName', '')} {candidate.result.get('trackName', '')}"
    return has_any_keyword(blob, DELUXE_KEYWORDS)


def _original_avoid_hint(candidate: Candidate) -> bool:
    blob = f"{candidate.result.get('collectionName', '')} {candidate.result.get('trackName', '')}"
    return has_any_keyword(blob, ORIGINAL_AVOID_KEYWORDS)


def choose_with_tiebreakers(
    best: Candidate,
    second: Candidate,
    album_title: str,
    rep_duration_ms: Optional[int],
    prefer_explicit: bool,
    prefer_non_deluxe: bool,
    prefer_original: bool,
) -> Optional[Candidate]:
    lib_album_norm = normalize_text(album_title)
    lib_album_core = normalize_text(strip_editions(album_title))

    def exact_album_match(candidate: Candidate) -> bool:
        return normalize_text(candidate.result.get("collectionName", "")) == lib_album_norm

    def contains_album_core(candidate: Candidate) -> bool:
        collection_norm = normalize_text(candidate.result.get("collectionName", ""))
        return bool(lib_album_core and lib_album_core in collection_norm)

    checks = [
        exact_album_match,
        contains_album_core,
    ]

    for check in checks:
        left = check(best)
        right = check(second)
        if left != right:
            return best if left else second

    if prefer_explicit:
        library_wants_explicit = has_any_keyword(album_title, EXPLICIT_KEYWORDS)
        library_mentions_clean = has_any_keyword(album_title, CLEAN_KEYWORDS)
        best_explicit = _explicit_hint(best)
        second_explicit = _explicit_hint(second)
        if best_explicit != second_explicit and (library_wants_explicit or not library_mentions_clean):
            return best if best_explicit else second

    if prefer_non_deluxe and not has_any_keyword(album_title, DELUXE_KEYWORDS):
        best_deluxe = _edition_hint(best)
        second_deluxe = _edition_hint(second)
        if best_deluxe != second_deluxe:
            return second if best_deluxe else best

    if prefer_original and not has_any_keyword(album_title, ORIGINAL_AVOID_KEYWORDS):
        best_avoid = _original_avoid_hint(best)
        second_avoid = _original_avoid_hint(second)
        if best_avoid != second_avoid:
            return second if best_avoid else best

    best_diff = _duration_distance(best, rep_duration_ms)
    second_diff = _duration_distance(second, rep_duration_ms)
    if best_diff is not None and second_diff is not None and best_diff != second_diff:
        return best if best_diff < second_diff else second

    return None


def search_candidates(
    session: requests.Session,
    state: RequestState,
    term: str,
    entity: str,
    country: str,
    search_limit: int,
    max_retries: int,
    max_429_in_row: int,
    cooldown_seconds: int,
    verbose_429: bool,
    verbose_http: bool,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    params = {
        "term": term,
        "entity": entity,
        "country": country,
        "limit": search_limit,
    }
    data, error = request_json_with_retries(
        session=session,
        url=SEARCH_ENDPOINT,
        params=params,
        max_retries=max_retries,
        state=state,
        max_429_in_row=max_429_in_row,
        cooldown_seconds=cooldown_seconds,
        verbose_429=verbose_429,
        verbose_http=verbose_http,
    )
    if error or not data:
        return [], error or "no response"
    return data.get("results", []), None


def lookup_collection_once(
    session: requests.Session,
    state: RequestState,
    collection_id: int,
    max_retries: int,
    max_429_in_row: int,
    cooldown_seconds: int,
    verbose_429: bool,
    verbose_http: bool,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    params = {"id": collection_id, "entity": "album"}
    data, error = request_json_with_retries(
        session=session,
        url=LOOKUP_ENDPOINT,
        params=params,
        max_retries=max_retries,
        state=state,
        max_429_in_row=max_429_in_row,
        cooldown_seconds=cooldown_seconds,
        verbose_429=verbose_429,
        verbose_http=verbose_http,
    )
    if error or not data:
        return None, error or "no response"
    for item in data.get("results", []):
        if item.get("wrapperType") == "collection":
            return item, None
    return None, "collection not found"


def load_json_dict(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        return {}
    return data


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    ensure_out_dir(path.parent)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    temp_path.replace(path)


def load_album_map(path: Path) -> Dict[str, Dict[str, Any]]:
    data = load_json_dict(path)
    output: Dict[str, Dict[str, Any]] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, dict):
            output[key] = value
    return output


def write_album_map(path: Path, data: Dict[str, Dict[str, Any]]) -> None:
    write_json_atomic(path, data)


def load_overrides(path: Path) -> Dict[str, int]:
    data = load_json_dict(path)
    overrides: Dict[str, int] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            continue
        num = _safe_int(value)
        if num is not None:
            overrides[key] = num
    return overrides


def write_overrides(path: Path, overrides: Dict[str, int]) -> None:
    normalized = {key: int(value) for key, value in sorted(overrides.items())}
    write_json_atomic(path, normalized)


def _candidate_row_fields(candidate: Optional[Candidate]) -> Dict[str, Any]:
    if not candidate:
        return {
            "best_collectionId": "",
            "best_collectionName": "",
            "best_artistName": "",
            "best_score": "",
        }
    return {
        "best_collectionId": candidate.result.get("collectionId", ""),
        "best_collectionName": candidate.result.get("collectionName", ""),
        "best_artistName": candidate.result.get("artistName", ""),
        "best_score": f"{candidate.score:.2f}",
    }


def _build_record_from_result(
    result: Dict[str, Any],
    score: Optional[float],
    resolution_path: str,
) -> Dict[str, Any]:
    artwork_100 = result.get("artworkUrl100", "") or ""
    return {
        "collectionId": result.get("collectionId", ""),
        "collectionName": result.get("collectionName", ""),
        "artistName": result.get("artistName", ""),
        "artworkUrl100": artwork_100,
        "artworkUrl600": derive_artwork_url(artwork_100, 600),
        "artworkUrl3000": derive_artwork_url(artwork_100, 3000),
        "resolution_path": resolution_path,
        "best_collectionId": result.get("collectionId", ""),
        "best_collectionName": result.get("collectionName", ""),
        "best_artistName": result.get("artistName", ""),
        "best_score": f"{score:.2f}" if score is not None else "",
    }


def _base_row(entry: Dict[str, Any]) -> Dict[str, Any]:
    rep = entry.get("representative_track") or {}
    return {
        "text_key": entry.get("text_key", ""),
        "album_artist": entry.get("album_artist", ""),
        "album": entry.get("album", ""),
        "rep_track": rep.get("title", ""),
        "rep_track_artist": rep.get("artist", ""),
        "rep_duration_ms": rep.get("duration_ms", ""),
    }


def resolve_albums(
    albums_json: Path,
    out_dir: Path,
    country: str,
    sleep_ms: int,
    resume: bool,
    search_limit: int,
    min_score: float,
    min_score_album: float,
    ambiguity_gap: float,
    max_429_in_row: int,
    cooldown_seconds: int,
    max_retries: int,
    verbose_429: bool,
    verbose_http: bool,
    lookup: bool,
    progress_every: int,
    overrides_path: Path,
    write_overrides_flag: bool,
    album_fallback: bool,
    fallback_only_on_empty: bool,
    prefer_explicit: bool,
    prefer_non_deluxe: bool,
    prefer_original: bool,
) -> None:
    ensure_out_dir(out_dir)

    with albums_json.open("r", encoding="utf-8") as handle:
        albums: List[Dict[str, Any]] = json.load(handle)

    album_map_path = out_dir / "album_id_map.json"
    album_id_map: Dict[str, Dict[str, Any]] = load_album_map(album_map_path) if resume else {}
    overrides = load_overrides(overrides_path)

    resolved_rows_run: List[Dict[str, Any]] = []
    ambiguous_rows: List[Dict[str, Any]] = []
    unresolved_rows: List[Dict[str, Any]] = []

    session = requests.Session()
    state = RequestState()

    processed = 0
    skipped = 0
    overrides_updates = 0

    for entry in albums:
        text_key = entry.get("text_key", "")
        if not text_key:
            continue
        override_id = overrides.get(text_key)
        if (
            resume
            and text_key in album_id_map
            and (
                override_id is None
                or _safe_int(album_id_map[text_key].get("collectionId")) == override_id
            )
        ):
            skipped += 1
            continue

        processed += 1
        row_base = _base_row(entry)
        album_title = row_base["album"]
        album_artist = row_base["album_artist"]
        rep_title = row_base["rep_track"]
        rep_artist = row_base["rep_track_artist"]
        rep_duration_ms = _safe_int(row_base["rep_duration_ms"])

        resolution_record: Optional[Dict[str, Any]] = None
        unresolved_error = ""
        best_primary: Optional[Candidate] = None
        second_primary: Optional[Candidate] = None
        ambiguous_resolution_path = "song_primary"

        if override_id is not None:
            existing = album_id_map.get(text_key, {}) if resume else {}
            resolution_record = {
                "collectionId": override_id,
                "collectionName": existing.get("collectionName", ""),
                "artistName": existing.get("artistName", ""),
                "artworkUrl100": existing.get("artworkUrl100", ""),
                "artworkUrl600": existing.get("artworkUrl600", ""),
                "artworkUrl3000": existing.get("artworkUrl3000", ""),
                "resolution_path": "override",
                "best_collectionId": override_id,
                "best_collectionName": existing.get("collectionName", ""),
                "best_artistName": existing.get("artistName", ""),
                "best_score": existing.get("best_score", ""),
            }

            if not resolution_record.get("artworkUrl100"):
                lookup_item, lookup_error = lookup_collection_once(
                    session=session,
                    state=state,
                    collection_id=override_id,
                    max_retries=max_retries,
                    max_429_in_row=max_429_in_row,
                    cooldown_seconds=cooldown_seconds,
                    verbose_429=verbose_429,
                    verbose_http=verbose_http,
                )
                if lookup_item:
                    artwork_100 = lookup_item.get("artworkUrl100", "") or ""
                    resolution_record["collectionName"] = lookup_item.get("collectionName", "")
                    resolution_record["artistName"] = lookup_item.get("artistName", "")
                    resolution_record["best_collectionName"] = lookup_item.get("collectionName", "")
                    resolution_record["best_artistName"] = lookup_item.get("artistName", "")
                    resolution_record["artworkUrl100"] = artwork_100
                    resolution_record["artworkUrl600"] = derive_artwork_url(artwork_100, 600)
                    resolution_record["artworkUrl3000"] = derive_artwork_url(artwork_100, 3000)
                elif lookup_error:
                    unresolved_error = f"override artwork lookup failed: {lookup_error}"

            if write_overrides_flag:
                # Optional mode for future extension: preserve deterministic, local-only overrides.
                write_overrides(overrides_path, overrides)

        elif not rep_title or not rep_artist:
            unresolved_error = "missing representative track"

        else:
            primary_results, primary_error = search_candidates(
                session=session,
                state=state,
                term=f"{rep_title} {rep_artist}",
                entity="song",
                country=country,
                search_limit=search_limit,
                max_retries=max_retries,
                max_429_in_row=max_429_in_row,
                cooldown_seconds=cooldown_seconds,
                verbose_429=verbose_429,
                verbose_http=verbose_http,
            )
            if primary_error:
                unresolved_error = f"song_primary search failed: {primary_error}"

            primary_candidates: List[Candidate] = []
            for result in primary_results:
                if not result.get("collectionId") or not result.get("trackName"):
                    continue
                score = score_song_candidate(result, rep_title, rep_artist, album_title, rep_duration_ms)
                primary_candidates.append(Candidate(score=score, result=result))
            primary_candidates.sort(key=_candidate_sort_key)

            if primary_candidates:
                best_primary = primary_candidates[0]
                second_primary = primary_candidates[1] if len(primary_candidates) > 1 else None

            if best_primary and best_primary.score >= min_score:
                if second_primary and (best_primary.score - second_primary.score) < ambiguity_gap:
                    tie_winner = choose_with_tiebreakers(
                        best=best_primary,
                        second=second_primary,
                        album_title=album_title,
                        rep_duration_ms=rep_duration_ms,
                        prefer_explicit=prefer_explicit,
                        prefer_non_deluxe=prefer_non_deluxe,
                        prefer_original=prefer_original,
                    )
                    if tie_winner:
                        resolution_record = _build_record_from_result(
                            tie_winner.result,
                            tie_winner.score,
                            "song_primary",
                        )
                else:
                    resolution_record = _build_record_from_result(
                        best_primary.result,
                        best_primary.score,
                        "song_primary",
                    )

            if not resolution_record and album_fallback:
                should_run_album_fallback = False
                if not primary_candidates:
                    should_run_album_fallback = True
                elif not fallback_only_on_empty and best_primary and best_primary.score < min_score:
                    should_run_album_fallback = True

                if should_run_album_fallback:
                    album_results, album_error = search_candidates(
                        session=session,
                        state=state,
                        term=f"{album_title} {album_artist}",
                        entity="album",
                        country=country,
                        search_limit=search_limit,
                        max_retries=max_retries,
                        max_429_in_row=max_429_in_row,
                        cooldown_seconds=cooldown_seconds,
                        verbose_429=verbose_429,
                        verbose_http=verbose_http,
                    )
                    if album_error:
                        unresolved_error = (
                            f"{unresolved_error}; " if unresolved_error else ""
                        ) + f"album_fallback search failed: {album_error}"

                    album_candidates: List[Candidate] = []
                    for result in album_results:
                        if not result.get("collectionId"):
                            continue
                        score = score_album_candidate(result, album_title, album_artist)
                        album_candidates.append(Candidate(score=score, result=result))
                    album_candidates.sort(key=_candidate_sort_key)

                    if album_candidates and album_candidates[0].score >= min_score_album:
                        winner = album_candidates[0]
                        resolution_record = _build_record_from_result(
                            winner.result,
                            winner.score,
                            "album_fallback",
                        )

            if not resolution_record:
                song_album_results, song_album_error = search_candidates(
                    session=session,
                    state=state,
                    term=f"{album_title} {album_artist}",
                    entity="song",
                    country=country,
                    search_limit=search_limit,
                    max_retries=max_retries,
                    max_429_in_row=max_429_in_row,
                    cooldown_seconds=cooldown_seconds,
                    verbose_429=verbose_429,
                    verbose_http=verbose_http,
                )
                if song_album_error:
                    unresolved_error = (
                        f"{unresolved_error}; " if unresolved_error else ""
                    ) + f"song_album_term search failed: {song_album_error}"

                song_album_candidates: List[Candidate] = []
                for result in song_album_results:
                    if not result.get("collectionId") or not result.get("trackName"):
                        continue
                    score = score_song_candidate(result, rep_title, rep_artist, album_title, rep_duration_ms)
                    song_album_candidates.append(Candidate(score=score, result=result))
                song_album_candidates.sort(key=_candidate_sort_key)

                if song_album_candidates and song_album_candidates[0].score >= min_score:
                    winner = song_album_candidates[0]
                    runner_up = song_album_candidates[1] if len(song_album_candidates) > 1 else None
                    if runner_up and (winner.score - runner_up.score) < ambiguity_gap:
                        tie_winner = choose_with_tiebreakers(
                            best=winner,
                            second=runner_up,
                            album_title=album_title,
                            rep_duration_ms=rep_duration_ms,
                            prefer_explicit=prefer_explicit,
                            prefer_non_deluxe=prefer_non_deluxe,
                            prefer_original=prefer_original,
                        )
                        if tie_winner:
                            resolution_record = _build_record_from_result(
                                tie_winner.result,
                                tie_winner.score,
                                "song_album_term",
                            )
                        else:
                            best_primary = winner
                            second_primary = runner_up
                            ambiguous_resolution_path = "song_album_term"
                    else:
                        resolution_record = _build_record_from_result(
                            winner.result,
                            winner.score,
                            "song_album_term",
                        )

        if resolution_record:
            collection_id = _safe_int(resolution_record.get("collectionId"))
            if lookup and collection_id:
                lookup_item, lookup_error = lookup_collection_once(
                    session=session,
                    state=state,
                    collection_id=collection_id,
                    max_retries=max_retries,
                    max_429_in_row=max_429_in_row,
                    cooldown_seconds=cooldown_seconds,
                    verbose_429=verbose_429,
                    verbose_http=verbose_http,
                )
                if lookup_item:
                    if lookup_item.get("primaryGenreName"):
                        resolution_record["primaryGenreName"] = lookup_item.get("primaryGenreName")
                    if lookup_item.get("releaseDate"):
                        resolution_record["releaseDate"] = lookup_item.get("releaseDate")
                    if not resolution_record.get("collectionName"):
                        resolution_record["collectionName"] = lookup_item.get("collectionName", "")
                    if not resolution_record.get("artistName"):
                        resolution_record["artistName"] = lookup_item.get("artistName", "")
                elif lookup_error and not unresolved_error:
                    unresolved_error = f"lookup failed: {lookup_error}"

            if unresolved_error:
                resolution_record["error"] = unresolved_error
            album_id_map[text_key] = resolution_record
            resolved_rows_run.append(
                {
                    **row_base,
                    "resolution_path": resolution_record.get("resolution_path", ""),
                    "best_collectionId": resolution_record.get("best_collectionId", ""),
                    "best_collectionName": resolution_record.get("best_collectionName", ""),
                    "best_artistName": resolution_record.get("best_artistName", ""),
                    "best_score": resolution_record.get("best_score", ""),
                    "collectionId": resolution_record.get("collectionId", ""),
                    "collectionName": resolution_record.get("collectionName", ""),
                    "artistName": resolution_record.get("artistName", ""),
                    "artworkUrl100": resolution_record.get("artworkUrl100", ""),
                    "artworkUrl600": resolution_record.get("artworkUrl600", ""),
                    "artworkUrl3000": resolution_record.get("artworkUrl3000", ""),
                    "error": resolution_record.get("error", ""),
                }
            )
        else:
            ambiguous = False
            if best_primary and second_primary:
                if best_primary.score >= min_score and (best_primary.score - second_primary.score) < ambiguity_gap:
                    ambiguous = True

            if ambiguous:
                ambiguous_rows.append(
                    {
                        **row_base,
                        "resolution_path": ambiguous_resolution_path,
                        "best_collectionId": best_primary.result.get("collectionId", ""),
                        "best_collectionName": best_primary.result.get("collectionName", ""),
                        "best_artistName": best_primary.result.get("artistName", ""),
                        "best_score": f"{best_primary.score:.2f}",
                        "second_collectionId": second_primary.result.get("collectionId", ""),
                        "second_collectionName": second_primary.result.get("collectionName", ""),
                        "second_artistName": second_primary.result.get("artistName", ""),
                        "second_score": f"{second_primary.score:.2f}",
                        "error": unresolved_error,
                    }
                )
            else:
                unresolved_rows.append(
                    {
                        **row_base,
                        "resolution_path": ambiguous_resolution_path if best_primary else "",
                        **_candidate_row_fields(best_primary),
                        "error": unresolved_error or "no winner",
                    }
                )

            if write_overrides_flag:
                url_value = entry.get("override_url")
                parsed = parse_collection_id_from_text(str(url_value)) if url_value else None
                if parsed is not None:
                    overrides[text_key] = parsed
                    overrides_updates += 1

        if processed % progress_every == 0:
            write_album_map(album_map_path, album_id_map)
            print(
                "Processed "
                f"{processed}/{len(albums)} "
                f"(resolved this run: {len(resolved_rows_run)}, ambiguous: {len(ambiguous_rows)}, "
                f"unresolved: {len(unresolved_rows)}, skipped: {skipped})"
            )

        _sleep_seconds(sleep_ms / 1000.0)

    write_album_map(album_map_path, album_id_map)
    if write_overrides_flag and overrides_updates:
        write_overrides(overrides_path, overrides)

    resolved_rows_all: List[Dict[str, Any]] = []
    for album in albums:
        album_key = album.get("text_key")
        if not album_key or album_key not in album_id_map:
            continue
        row_base = _base_row(album)
        record = album_id_map[album_key]
        resolved_rows_all.append(
            {
                **row_base,
                "resolution_path": record.get("resolution_path", ""),
                "best_collectionId": record.get("best_collectionId", record.get("collectionId", "")),
                "best_collectionName": record.get("best_collectionName", record.get("collectionName", "")),
                "best_artistName": record.get("best_artistName", record.get("artistName", "")),
                "best_score": record.get("best_score", ""),
                "collectionId": record.get("collectionId", ""),
                "collectionName": record.get("collectionName", ""),
                "artistName": record.get("artistName", ""),
                "artworkUrl100": record.get("artworkUrl100", ""),
                "artworkUrl600": record.get("artworkUrl600", ""),
                "artworkUrl3000": record.get("artworkUrl3000", ""),
                "error": record.get("error", ""),
            }
        )

    resolved_csv = out_dir / "resolved.csv"
    with resolved_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "text_key",
                "album_artist",
                "album",
                "rep_track",
                "rep_track_artist",
                "rep_duration_ms",
                "resolution_path",
                "best_collectionId",
                "best_collectionName",
                "best_artistName",
                "best_score",
                "collectionId",
                "collectionName",
                "artistName",
                "artworkUrl100",
                "artworkUrl600",
                "artworkUrl3000",
                "error",
            ],
        )
        writer.writeheader()
        for row in resolved_rows_all:
            writer.writerow(row)

    ambiguous_csv = out_dir / "ambiguous.csv"
    with ambiguous_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "text_key",
                "album_artist",
                "album",
                "rep_track",
                "rep_track_artist",
                "rep_duration_ms",
                "resolution_path",
                "best_collectionId",
                "best_collectionName",
                "best_artistName",
                "best_score",
                "second_collectionId",
                "second_collectionName",
                "second_artistName",
                "second_score",
                "error",
            ],
        )
        writer.writeheader()
        for row in ambiguous_rows:
            writer.writerow(row)

    unresolved_csv = out_dir / "unresolved.csv"
    with unresolved_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "text_key",
                "album_artist",
                "album",
                "rep_track",
                "rep_track_artist",
                "rep_duration_ms",
                "resolution_path",
                "best_collectionId",
                "best_collectionName",
                "best_artistName",
                "best_score",
                "error",
            ],
        )
        writer.writeheader()
        for row in unresolved_rows:
            writer.writerow(row)

    print(f"Wrote: {album_map_path}")
    print(f"Wrote: {resolved_csv}")
    print(f"Wrote: {ambiguous_csv}")
    print(f"Wrote: {unresolved_csv}")


def parse_collection_id_from_text(value: str) -> Optional[int]:
    text = (value or "").strip()
    if not text:
        return None

    direct = _safe_int(text)
    if direct is not None:
        return direct

    match = re.search(r"/id(\d+)(?:\b|[/?#])", text)
    if match:
        return int(match.group(1))

    match = re.search(r"\b(\d{6,})\b", text)
    if match:
        return int(match.group(1))

    return None


def _load_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]


def _prompt_choice(prompt: str, allowed: Tuple[str, ...]) -> str:
    while True:
        try:
            value = input(prompt).strip().lower()
        except EOFError:
            return "quit"
        if value in allowed:
            return value
        print(f"Invalid choice. Allowed: {', '.join(allowed)}")


def review_rows(
    ambiguous_csv: Path,
    unresolved_csv: Path,
    overrides_path: Path,
    mode: str,
) -> None:
    overrides = load_overrides(overrides_path)
    ensure_out_dir(overrides_path.parent)

    ambiguous_rows = _load_csv_rows(ambiguous_csv) if mode in {"ambiguous", "both"} else []
    unresolved_rows = _load_csv_rows(unresolved_csv) if mode in {"unresolved", "both"} else []

    print(f"Loaded overrides: {len(overrides)} from {overrides_path}")

    for row in ambiguous_rows:
        text_key = row.get("text_key", "")
        print("\n--- Ambiguous ---")
        print(f"text_key: {text_key}")
        print(f"album_artist: {row.get('album_artist', '')}")
        print(f"album: {row.get('album', '')}")
        print(f"rep_track: {row.get('rep_track', '')}")
        print(
            "1) "
            f"{row.get('best_collectionId', '')} | {row.get('best_collectionName', '')} | "
            f"{row.get('best_artistName', '')} | score={row.get('best_score', '')}"
        )
        print(
            "2) "
            f"{row.get('second_collectionId', '')} | {row.get('second_collectionName', '')} | "
            f"{row.get('second_artistName', '')} | score={row.get('second_score', '')}"
        )
        choice = _prompt_choice("Choose 1/2/skip/quit: ", ("1", "2", "skip", "quit"))
        if choice == "quit":
            break
        if choice == "skip":
            continue

        selected_id = row.get("best_collectionId", "") if choice == "1" else row.get("second_collectionId", "")
        collection_id = _safe_int(selected_id)
        if collection_id is None:
            print("Selected candidate has no numeric collectionId; skipped.")
            continue

        overrides[text_key] = collection_id
        write_overrides(overrides_path, overrides)
        print(f"Saved override: {text_key} -> {collection_id}")

    for row in unresolved_rows:
        text_key = row.get("text_key", "")
        print("\n--- Unresolved ---")
        print(f"text_key: {text_key}")
        print(f"album_artist: {row.get('album_artist', '')}")
        print(f"album: {row.get('album', '')}")
        print(f"rep_track: {row.get('rep_track', '')}")
        print(f"error: {row.get('error', '')}")
        try:
            value = input("Paste Apple Music album URL or collectionId (or skip/quit): ").strip()
        except EOFError:
            break
        if value.lower() == "quit":
            break
        if value.lower() == "skip" or not value:
            continue

        collection_id = parse_collection_id_from_text(value)
        if collection_id is None:
            print("Could not parse a numeric collectionId.")
            continue

        overrides[text_key] = collection_id
        write_overrides(overrides_path, overrides)
        print(f"Saved override: {text_key} -> {collection_id}")

    print(f"Final overrides count: {len(overrides)}")
    print(f"Wrote: {overrides_path}")


def download_artwork(
    map_json: Path,
    out_dir: Path,
    prefer_size: str,
    concurrency: int,
    timeout_s: int,
    retries: int,
    sleep_ms: int,
    resume: bool,
    format_mode: str,
    manifest_path: Path,
    min_bytes: int,
) -> None:
    ensure_out_dir(out_dir)
    ensure_out_dir(manifest_path.parent)

    album_map = load_album_map(map_json)
    entries: Dict[str, Dict[str, Any]] = {}
    if manifest_path.exists():
        existing_manifest = load_json_dict(manifest_path)
        manifest_entries = existing_manifest.get("entries")
        if isinstance(manifest_entries, dict):
            for key, value in manifest_entries.items():
                if isinstance(key, str) and isinstance(value, dict):
                    entries[key] = value

    jobs: List[Tuple[str, Dict[str, Any], str]] = []
    for text_key in sorted(album_map.keys()):
        record = album_map[text_key]
        jobs.append((text_key, record, choose_artwork_url(record, prefer_size)))

    total = len(jobs)
    if total == 0:
        _write_manifest(manifest_path, entries)
        print(f"No albums found in map: {map_json}")
        print(f"Wrote: {manifest_path}")
        return

    ok_count = 0
    skip_count = 0
    err_count = 0
    completed = 0

    def _worker(job: Tuple[str, Dict[str, Any], str]) -> Dict[str, Any]:
        text_key, record, chosen_url = job
        try:
            return download_one(
                text_key=text_key,
                record=record,
                chosen_url=chosen_url,
                out_dir=out_dir,
                format_mode=format_mode,
                timeout_s=timeout_s,
                retries=retries,
                resume=resume,
                min_bytes=min_bytes,
            )
        except Exception as exc:
            return {
                "text_key": text_key,
                "collectionId": _safe_int(record.get("collectionId")) or "",
                "chosen_url": chosen_url,
                "saved_path": "",
                "bytes": 0,
                "status": "error",
                "error_message": f"unexpected error: {exc}",
                "timestamp": _utc_now_iso(),
            }

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        for result in executor.map(_worker, jobs):
            text_key = str(result.get("text_key", ""))
            entries[text_key] = result
            _write_manifest(manifest_path, entries)

            status = result.get("status")
            if status == "ok":
                ok_count += 1
            elif status == "skip":
                skip_count += 1
            else:
                err_count += 1

            completed += 1
            print(f"Downloaded {completed}/{total} (ok: {ok_count}, skip: {skip_count}, err: {err_count})")

            if completed < total and sleep_ms > 0:
                base = sleep_ms / 1000.0
                jitter = random.uniform(0, base * 0.5)
                _sleep_seconds(base + jitter)

    print(f"Wrote: {manifest_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Album cache pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser("extract", help="Extract album manifest")
    extract_parser.add_argument("--library", required=True, type=Path, help="Path to Library.xml")
    extract_parser.add_argument("--out", type=Path, default=Path("python/out"), help="Output directory")

    resolve_parser = subparsers.add_parser("resolve", help="Resolve album IDs and artwork")
    resolve_parser.add_argument(
        "--albums-json",
        type=Path,
        default=Path("python/out/albums.json"),
        help="Albums JSON from extract phase",
    )
    resolve_parser.add_argument("--out", type=Path, default=Path("python/out"), help="Output directory")
    resolve_parser.add_argument("--country", default="us", help="iTunes country code")
    resolve_parser.add_argument("--sleep-ms", type=int, default=1500, help="Sleep between albums (ms)")
    resolve_parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Resume using existing album_id_map.json (default true)",
    )
    resolve_parser.add_argument("--search-limit", type=int, default=25, help="Search results limit")
    resolve_parser.add_argument("--min-score", type=float, default=8.0, help="Minimum score to resolve songs")
    resolve_parser.add_argument(
        "--min-score-album",
        type=float,
        default=5.0,
        help="Minimum score to resolve album fallback search",
    )
    resolve_parser.add_argument(
        "--ambiguity-gap",
        type=float,
        default=0.75,
        help="Min gap between top scores",
    )
    resolve_parser.add_argument("--max-429-in-row", type=int, default=3, help="Max consecutive 429s")
    resolve_parser.add_argument("--cooldown-seconds", type=int, default=600, help="Cooldown after 429s")
    resolve_parser.add_argument("--max-retries", type=int, default=4, help="Max retries per request")
    resolve_parser.add_argument("--verbose-429", action="store_true", help="Log 429 handling")
    resolve_parser.add_argument("--verbose-http", action="store_true", help="Log HTTP requests")
    resolve_parser.add_argument("--lookup", action="store_true", help="Enable lookup hydration")
    resolve_parser.add_argument(
        "--progress-every",
        type=int,
        default=25,
        help="Print progress + flush map every N",
    )
    resolve_parser.add_argument(
        "--overrides",
        type=Path,
        default=Path("out/overrides.json"),
        help="Local text_key->collectionId overrides file",
    )
    resolve_parser.add_argument(
        "--write-overrides",
        action="store_true",
        help="Allow resolve to write parsed override URLs when present",
    )
    resolve_parser.add_argument(
        "--album-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable album-only fallback search when needed (default true)",
    )
    resolve_parser.add_argument(
        "--fallback-only-on-empty",
        action="store_true",
        help="Run album fallback only when primary song search has 0 candidates",
    )
    resolve_parser.add_argument(
        "--prefer-explicit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Prefer explicit variants during tie-breaks",
    )
    resolve_parser.add_argument(
        "--prefer-non-deluxe",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Prefer non-deluxe variants during tie-breaks",
    )
    resolve_parser.add_argument(
        "--prefer-original",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Prefer original (non-remaster/non-anniversary) during tie-breaks",
    )

    review_parser = subparsers.add_parser(
        "review",
        help="Interactively review ambiguous/unresolved rows and build overrides",
    )
    review_parser.add_argument(
        "--ambiguous-csv",
        type=Path,
        default=Path("out/ambiguous.csv"),
        help="Ambiguous CSV from resolve",
    )
    review_parser.add_argument(
        "--unresolved-csv",
        type=Path,
        default=Path("out/unresolved.csv"),
        help="Unresolved CSV from resolve",
    )
    review_parser.add_argument(
        "--overrides",
        type=Path,
        default=Path("out/overrides.json"),
        help="Overrides JSON path",
    )
    review_parser.add_argument(
        "--mode",
        choices=("ambiguous", "unresolved", "both"),
        default="both",
        help="Rows to review",
    )

    download_parser = subparsers.add_parser(
        "download-art",
        help="Download source album art into a resumable local cache",
    )
    download_parser.add_argument(
        "--map-json",
        type=Path,
        default=Path("out/album_id_map.json"),
        help="Resolved album map JSON",
    )
    download_parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("out/source_art"),
        help="Output directory for downloaded artwork",
    )
    download_parser.add_argument(
        "--prefer-size",
        choices=("hires", "3000", "600", "100"),
        default="hires",
        help="Preferred source URL size strategy",
    )
    download_parser.add_argument("--concurrency", type=int, default=2, help="Concurrent downloads")
    download_parser.add_argument("--timeout-s", type=int, default=30, help="Per-request timeout in seconds")
    download_parser.add_argument("--retries", type=int, default=3, help="Retries for transient failures")
    download_parser.add_argument("--sleep-ms", type=int, default=150, help="Sleep+jitter between completions")
    download_parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip existing non-empty files (default true)",
    )
    download_parser.add_argument(
        "--format",
        choices=("jpg", "png", "keep"),
        default="keep",
        help="Output extension mode",
    )
    download_parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("out/source_art/manifest.json"),
        help="Manifest output path",
    )
    download_parser.add_argument(
        "--min-bytes",
        type=int,
        default=MIN_IMAGE_BYTES_DEFAULT,
        help="Minimum bytes required for a valid download",
    )

    widescreen_parser = subparsers.add_parser(
        "generate-widescreen",
        help="Generate 16:9 Frame TV artwork from square source art",
    )
    widescreen_parser.add_argument(
        "--source-art-dir",
        type=Path,
        default=Path("out/source_art"),
        help="Directory with downloaded square source artwork",
    )
    widescreen_parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("out/widescreen"),
        help="Output directory for widescreen PNGs + manifest",
    )
    widescreen_parser.add_argument(
        "--map-json",
        type=Path,
        default=Path("out/album_id_map.json"),
        help="Resolved album map JSON",
    )
    widescreen_parser.add_argument("--target-width", type=int, default=3840, help="Output width in pixels")
    widescreen_parser.add_argument("--target-height", type=int, default=2160, help="Output height in pixels")
    widescreen_parser.add_argument("--album-px", type=int, default=1536, help="Centered album square size in pixels")
    widescreen_parser.add_argument("--model", required=True, help="Image generation model name")
    widescreen_parser.add_argument(
        "--style-profile",
        choices=("neutral", "ambient", "minimal", "painterly"),
        default="neutral",
        help="Background style profile",
    )
    widescreen_parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip existing outputs larger than 100KB (default true)",
    )
    widescreen_parser.add_argument("--sleep-ms", type=int, default=2500, help="Sleep between generations (ms)")
    widescreen_parser.add_argument("--seed", type=int, default=None, help="Optional deterministic seed")
    widescreen_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional max number of newly generated images in this run",
    )
    widescreen_parser.add_argument(
        "--debug-save-canvas",
        action="store_true",
        help="Save debug canvas + mask with a center crosshair",
    )
    widescreen_parser.add_argument(
        "--debug-upload-bytes",
        action="store_true",
        help="Write exact uploaded edit image/mask PNG bytes to out/_debug and log dimensions/modes",
    )
    widescreen_parser.add_argument(
        "--multipart-mode",
        choices=("ha", "std"),
        default="ha",
        help="Legacy option; HA pipeline always uses image[] + mask multipart upload",
    )
    widescreen_parser.add_argument(
        "--preserve-album",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Paste the original square album art back into the exact center of the final output (default true)",
    )
    widescreen_parser.add_argument(
        "--background-mode",
        choices=("ha-masked-outpaint", "reference-no-mask"),
        default="ha-masked-outpaint",
        help="Background generation route: masked outpaint or no-mask reference generation",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "extract":
        extract_albums(args.library, args.out)
        return

    if args.command == "resolve":
        resolve_albums(
            albums_json=args.albums_json,
            out_dir=args.out,
            country=args.country,
            sleep_ms=args.sleep_ms,
            resume=args.resume,
            search_limit=args.search_limit,
            min_score=args.min_score,
            min_score_album=args.min_score_album,
            ambiguity_gap=args.ambiguity_gap,
            max_429_in_row=args.max_429_in_row,
            cooldown_seconds=args.cooldown_seconds,
            max_retries=args.max_retries,
            verbose_429=args.verbose_429,
            verbose_http=args.verbose_http,
            lookup=args.lookup,
            progress_every=args.progress_every,
            overrides_path=args.overrides,
            write_overrides_flag=args.write_overrides,
            album_fallback=args.album_fallback,
            fallback_only_on_empty=args.fallback_only_on_empty,
            prefer_explicit=args.prefer_explicit,
            prefer_non_deluxe=args.prefer_non_deluxe,
            prefer_original=args.prefer_original,
        )
        return

    if args.command == "review":
        try:
            review_rows(
                ambiguous_csv=args.ambiguous_csv,
                unresolved_csv=args.unresolved_csv,
                overrides_path=args.overrides,
                mode=args.mode,
            )
        except KeyboardInterrupt:
            print("\nReview interrupted.", file=sys.stderr)
        return

    if args.command == "download-art":
        download_artwork(
            map_json=args.map_json,
            out_dir=args.out_dir,
            prefer_size=args.prefer_size,
            concurrency=args.concurrency,
            timeout_s=args.timeout_s,
            retries=args.retries,
            sleep_ms=args.sleep_ms,
            resume=args.resume,
            format_mode=args.format,
            manifest_path=args.manifest,
            min_bytes=args.min_bytes,
        )
        return

    if args.command == "generate-widescreen":
        generate_widescreen_artwork(
            source_art_dir=args.source_art_dir,
            out_dir=args.out_dir,
            map_json=args.map_json,
            target_width=args.target_width,
            target_height=args.target_height,
            album_px=args.album_px,
            model=args.model,
            style_profile=args.style_profile,
            resume=args.resume,
            sleep_ms=args.sleep_ms,
            seed=args.seed,
            debug_save_canvas=args.debug_save_canvas,
            debug_upload_bytes=args.debug_upload_bytes,
            limit=args.limit,
            preserve_album=args.preserve_album,
            multipart_mode=args.multipart_mode,
            background_mode=args.background_mode,
        )
        return


if __name__ == "__main__":
    main()
