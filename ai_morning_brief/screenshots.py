from __future__ import annotations

"""Optional source-visual intake for the AI morning brief.

The renderer deliberately knows nothing about browsers, cookies, or profiles.
The Codex in-app browser is an external capture surface that writes images into
the dated inbox described by ``source_visual_requests.json``. This module then
performs the deterministic, local part of the workflow:

* create one request and naming rule per frozen original URL;
* preserve browser originals in ``source-visuals/raw``;
* preserve validated originals as byte-identical presentation assets; and
* attach source-kind-appropriate claim-matched visuals to eligible stories.

No login state, cookie, browser profile, or provider token is read here.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .config import (
    DEFAULT_X_SCREENSHOT_MODE,
    SOURCE_VISUAL_CARD_CHARS_PER_SECOND,
    SOURCE_VISUAL_DURATION_SECONDS,
    SOURCE_VISUAL_MIN_CARD_READ_SECONDS,
    SOURCE_VISUAL_TRANSITION_SECONDS,
    X_SCREENSHOT_LEAD_SECONDS,
    X_SCREENSHOT_PAGE_DURATION_SECONDS,
)
from .media import write_json
from .models import SourceItem


SCREENSHOT_MODES = ("off", "manual", "auto")
SOURCE_VISUAL_MODES = SCREENSHOT_MODES
DEFAULT_PAGE_DURATION_SECONDS = X_SCREENSHOT_PAGE_DURATION_SECONDS
SCREENSHOT_LEAD_SECONDS = X_SCREENSHOT_LEAD_SECONDS
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_VIDEO_BYTES = 250 * 1024 * 1024
DISPLAY_MAX_WIDTH = 1728
DISPLAY_MAX_HEIGHT = 900
EXPANDED_VIEWPORT_WIDTH = 1440
EXPANDED_VIEWPORT_HEIGHT = 900
CAPTURE_STRATEGY = "expanded_responsive_viewport"
CAPTURE_METHOD = "iab-expanded-viewport-screenshot"
CAPTURE_CONTRACT_ID = "iab-expanded-viewport-v1"
# The in-app browser reports the scale of the page, while its screenshot
# backend may return CSS-pixel images.  The image dimensions in the receipt
# are the source of truth; requiring a fake 2x image was one of the causes of
# the repeated recapture loop.
MIN_CAPTURE_DEVICE_SCALE = 1.0
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
SUPPORTED_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm"}
SUPPORTED_SUFFIXES = SUPPORTED_IMAGE_SUFFIXES | SUPPORTED_VIDEO_SUFFIXES
TIMEZONE = "Asia/Shanghai"
SOURCE_VISUAL_MANIFEST_VERSION = "5.0"
# Request manifests carry the one-shot capture state machine introduced for
# unattended runs.  Keep the presentation manifest at schema 5 so existing
# render consumers can continue to read it.
SOURCE_VISUAL_REQUEST_VERSION = "5.0"
SOURCE_VISUAL_PREFLIGHT_VERSION = "1.0"
CAPTURE_EXECUTOR = "codex_in_app_browser"
CAPTURE_CAPABILITIES = (
    "codex_in_app_browser_control",
    "direct_project_file_write",
    "expanded_viewport_screenshot_1440x900",
)
TERMINAL_CAPTURE_STATES = {"validated", "unavailable"}
# Be proactive for ordinary articles: keep the complete main-content viewport
# and allow up to two directly relevant article images. X remains strictly one
# original-post viewport capture.
ARTICLE_IMAGE_LIMIT = 2


class ScreenshotError(RuntimeError):
    """Raised when a screenshot manifest cannot satisfy the render contract."""


class ScreenshotPending(ScreenshotError):
    """The user has not completed the selected screenshot workflow yet."""

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


def normalize_mode(value: str | None) -> str:
    mode = str(value or DEFAULT_X_SCREENSHOT_MODE).strip().lower()
    aliases = {"none": "off", "disabled": "off", "manual_capture": "manual", "automatic": "auto"}
    mode = aliases.get(mode, mode)
    if mode not in SCREENSHOT_MODES:
        raise ScreenshotError(f"unsupported source visual mode: {value!r}; expected one of {SCREENSHOT_MODES}")
    return mode


def source_visual_acceptance(
    *,
    mode: str,
    minimum_selected_stories: int = 0,
    selected_stories: int | None = None,
) -> dict[str, Any]:
    """Build the auditable acceptance policy for optional source visuals.

    A minimum of zero preserves the historical soft-enrichment behavior. A
    positive minimum marks a full-workflow acceptance run and cannot be paired
    with the disabled visual mode.
    """

    resolved_mode = normalize_mode(mode)
    try:
        minimum = int(minimum_selected_stories)
    except (TypeError, ValueError) as exc:
        raise ScreenshotError("source visual minimum must be an integer") from exc
    if minimum < 0:
        raise ScreenshotError("source visual minimum must be zero or greater")
    if minimum > 0 and resolved_mode == "off":
        raise ScreenshotError(
            "a full-workflow source visual requirement cannot use mode=off"
        )

    test_scope = (
        "full_workflow"
        if minimum > 0
        else "card_only_partial"
        if resolved_mode == "off"
        else "optional_enrichment"
    )
    result: dict[str, Any] = {
        "test_scope": test_scope,
        "minimum_selected_stories": minimum,
        "target_max_selected_stories": None,
    }
    if selected_stories is None:
        result.update(
            {
                "selected_stories": None,
                "requirement_met": True if minimum == 0 else None,
                "status": "pass" if minimum == 0 else "pending",
            }
        )
        return result

    try:
        selected = max(0, int(selected_stories))
    except (TypeError, ValueError) as exc:
        raise ScreenshotError("selected source visual story count must be an integer") from exc
    requirement_met = selected >= minimum
    result.update(
        {
            "selected_stories": selected,
            "requirement_met": requirement_met,
            "status": "pass" if requirement_met else "fail",
        }
    )
    return result


def is_x_url(url: str | None) -> bool:
    try:
        host = (urlparse(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"} or host.endswith(".x.com") or host.endswith(".twitter.com")


def source_kind(url: str | None) -> str:
    """Classify a frozen original URL without making a network request."""

    if is_x_url(url):
        return "x"
    try:
        host = (urlparse(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return "unknown"
    return "web" if host else "unknown"


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "")).strip("-")
    return cleaned[:80] or "item"


def _now() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).isoformat()


def _default_capture_preflight() -> dict[str, Any]:
    return {
        "version": SOURCE_VISUAL_PREFLIGHT_VERSION,
        "status": "required",
        "terminal_state": "pending",
        "required_capabilities": list(CAPTURE_CAPABILITIES),
        "checks": {capability: {"status": "unknown"} for capability in CAPTURE_CAPABILITIES},
        "error_code": None,
        "source": "ai-daily-news-studio",
        "generated_at": _now(),
    }


def _capture_preflight_path(run_dir: Path) -> Path:
    return run_dir / "artifacts" / "source_visual_preflight.json"


def _load_capture_preflight(run_dir: Path) -> dict[str, Any] | None:
    path = _capture_preflight_path(run_dir)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            **_default_capture_preflight(),
            "status": "unavailable",
            "terminal_state": "unavailable",
            "error_code": "browser_capture_preflight_invalid",
        }
    return value if isinstance(value, dict) else {
        **_default_capture_preflight(),
        "status": "unavailable",
        "terminal_state": "unavailable",
        "error_code": "browser_capture_preflight_invalid",
    }


def record_capture_preflight(
    run_dir: Path,
    *,
    browser_control: bool,
    direct_project_file_write: bool,
    expanded_viewport_screenshot: bool,
    source: str = "automation",
    error_code: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Persist a capability handshake before an automated visual capture.

    Python cannot introspect whether the current Codex task exposes an
    in-app-browser controller.  The runner therefore records the three
    capabilities it actually has.  The collector consumes this artifact and
    never invents a fallback executor.
    """

    values = {
        "codex_in_app_browser_control": bool(browser_control),
        "direct_project_file_write": bool(direct_project_file_write),
        "expanded_viewport_screenshot_1440x900": bool(expanded_viewport_screenshot),
    }
    passed = all(values.values())
    result = {
        "version": SOURCE_VISUAL_PREFLIGHT_VERSION,
        "status": "pass" if passed else "unavailable",
        "terminal_state": "validated" if passed else "unavailable",
        "required_capabilities": list(CAPTURE_CAPABILITIES),
        "checks": {
            capability: {"status": "available" if available else "unavailable"}
            for capability, available in values.items()
        },
        "error_code": None if passed else (error_code or "browser_capture_unavailable"),
        "reason": reason,
        "source": str(source or "automation"),
        "generated_at": _now(),
    }
    write_json(_capture_preflight_path(run_dir), result)
    return result


def _preflight_error_code(preflight: Mapping[str, Any] | None) -> str | None:
    if not preflight:
        return None
    status = str(preflight.get("status") or "required").strip().lower()
    if status in {"pass", "validated", "available"}:
        checks = preflight.get("checks") or {}
        if all(str((checks.get(capability) or {}).get("status") or "").lower() in {"available", "pass"} for capability in CAPTURE_CAPABILITIES):
            return None
        return "browser_capture_unavailable"
    if status in {"required", "pending", "unknown"}:
        return None
    return str(preflight.get("error_code") or "browser_capture_unavailable")


def _capture_terminal_state(value: Mapping[str, Any] | None) -> str:
    value = value or {}
    state = str(value.get("terminal_state") or value.get("status") or "pending").strip().lower()
    if state in TERMINAL_CAPTURE_STATES:
        return state
    return "pending"


def _capture_error_code(error: str | None, *, preflight: bool = False) -> str | None:
    if not error:
        return None
    if preflight:
        return "browser_capture_unavailable"
    text = str(error).lower()
    if "contract" in text or "viewport" in text or "capture_method" in text or "capture_scope" in text or "requires exactly" in text or "must contain" in text:
        return "capture_contract_invalid"
    if "receipt" in text or "capture.json" in text:
        return "capture_receipt_invalid"
    if "login" in text or "captcha" in text or "permission" in text or "security" in text:
        return "source_access_blocked"
    return "capture_validation_failed"


def _natural_key(path: Path) -> tuple[Any, ...]:
    return tuple((1, int(part)) if part.isdigit() else (0, part.lower()) for part in re.split(r"(\d+)", path.name))


def _image_dimensions(path: Path) -> tuple[int, int]:
    """Read dimensions without adding Pillow as a production dependency."""

    data = path.read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith(b"RIFF") and path.suffix.lower() == ".webp":
        if len(data) >= 30 and data[12:16] == b"VP8X":
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return width, height
        # WebP variants without a cheap, complete parser are still accepted
        # when ffprobe is available; the conservative fallback prevents a
        # false dimension from being reported as valid.
        try:
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            streams = json.loads(probe.stdout).get("streams") or []
            if probe.returncode == 0 and streams:
                return int(streams[0]["width"]), int(streams[0]["height"])
        except (OSError, subprocess.TimeoutExpired, KeyError, TypeError, ValueError, json.JSONDecodeError, IndexError):
            pass
    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(data):
                break
            length = int.from_bytes(data[index:index + 2], "big")
            if length < 2 or index + length > len(data):
                break
            if marker in set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0)):
                if length >= 7:
                    return int.from_bytes(data[index + 5:index + 7], "big"), int.from_bytes(data[index + 3:index + 5], "big")
                break
            index += length
    raise ScreenshotError(f"could not read image dimensions: {path.name}")


def _presentation_geometry(width: int, height: int, *, text_bearing: bool) -> dict[str, Any]:
    return _presentation_geometry_with_policy(width, height, text_bearing=text_bearing, reject_small=text_bearing)


def _presentation_geometry_with_policy(
    width: int,
    height: int,
    *,
    text_bearing: bool,
    reject_small: bool,
) -> dict[str, Any]:
    fit_scale = min(DISPLAY_MAX_WIDTH / width, DISPLAY_MAX_HEIGHT / height)
    # Main-content captures are deliberately tight and may be enlarged by the
    # template.  Keep the old rejection only for historical receipts that
    # explicitly opt into the legacy first-viewport contract.
    if text_bearing and reject_small and fit_scale > 1.0001:
        raise ScreenshotError(
            f"source pixels are insufficient for readable text ({width}x{height}); "
            f"legacy capture requires at least {round(width * fit_scale)}x{round(height * fit_scale)}"
        )
    effective_scale = fit_scale
    return {
        "display_width": max(1, round(width * effective_scale)),
        "display_height": max(1, round(height * effective_scale)),
        "effective_scale": round(effective_scale, 6),
        "upscaled": effective_scale > 1.0001,
        "display_safe_area": {"width": DISPLAY_MAX_WIDTH, "height": DISPLAY_MAX_HEIGHT},
    }


def _validate_image(path: Path, *, text_bearing: bool = True, reject_small: bool = True) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ScreenshotError(f"screenshot is missing or empty: {path}")
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ScreenshotError(f"screenshot is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MiB: {path.name}")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_IMAGE_SUFFIXES:
        raise ScreenshotError(f"unsupported source visual format {suffix!r}: {path.name}")
    raw = path.read_bytes()
    actual_format = "png" if raw.startswith(b"\x89PNG\r\n\x1a\n") else "jpeg" if raw.startswith(b"\xff\xd8") else "webp" if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP" else suffix.lstrip(".")
    width, height = _image_dimensions(path)
    # Browser screenshots must be decodable by the renderer, not merely have
    # a plausible header. ffprobe is already a required video-pipeline tool;
    # if it is unavailable, the strict header check above remains the safe
    # fallback for constrained environments.
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        probe = None
    if probe is not None:
        if probe.returncode != 0:
            raise ScreenshotError(f"renderer cannot decode screenshot: {path.name}")
        try:
            streams = json.loads(probe.stdout).get("streams") or []
            probed_width = int(streams[0]["width"])
            probed_height = int(streams[0]["height"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, IndexError) as exc:
            raise ScreenshotError(f"renderer returned invalid screenshot metadata: {path.name}") from exc
        if (probed_width, probed_height) != (width, height):
            raise ScreenshotError(f"screenshot dimensions are inconsistent: {path.name}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "media_type": "image",
        "width": width,
        "height": height,
        "bytes": path.stat().st_size,
        "sha256": digest,
        "format": actual_format,
        **_presentation_geometry_with_policy(width, height, text_bearing=text_bearing, reject_small=reject_small),
    }


def _validate_video(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ScreenshotError(f"source video is missing or empty: {path}")
    if path.stat().st_size > MAX_VIDEO_BYTES:
        raise ScreenshotError(f"source video is larger than {MAX_VIDEO_BYTES // (1024 * 1024)} MiB: {path.name}")
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScreenshotError(f"could not inspect source video: {path.name}") from exc
    if probe.returncode != 0:
        raise ScreenshotError(f"renderer cannot decode source video: {path.name}")
    try:
        payload = json.loads(probe.stdout)
        stream = (payload.get("streams") or [])[0]
        width, height = int(stream["width"]), int(stream["height"])
        duration = float((payload.get("format") or {}).get("duration") or 0.0)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, IndexError) as exc:
        raise ScreenshotError(f"renderer returned invalid source-video metadata: {path.name}") from exc
    if width <= 0 or height <= 0 or duration <= 0:
        raise ScreenshotError(f"source video has invalid geometry or duration: {path.name}")
    return {
        "media_type": "video",
        "width": width,
        "height": height,
        "duration_seconds": round(duration, 3),
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "format": path.suffix.lower().lstrip("."),
        **_presentation_geometry(width, height, text_bearing=False),
    }


def _validate_media(path: Path, *, text_bearing: bool, reject_small: bool = True) -> dict[str, Any]:
    if path.suffix.lower() in SUPPORTED_VIDEO_SUFFIXES:
        return _validate_video(path)
    return _validate_image(path, text_bearing=text_bearing, reject_small=reject_small)


def _actual_suffix(metadata: Mapping[str, Any]) -> str:
    value = str(metadata.get("format") or "").lower()
    return {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "webp": ".webp"}.get(value, f".{value}" if value else ".bin")


def _copy_original(source: Path, destination: Path, *, expected_sha256: str) -> None:
    """Byte-copy a presentation asset and prove no media transform occurred."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    if digest != expected_sha256:
        destination.unlink(missing_ok=True)
        raise ScreenshotError(f"source visual byte-copy hash mismatch: {source.name}")


def _write_presentation(
    source: Path,
    destination: Path,
    *,
    source_sha256: str,
    text_bearing: bool,
) -> dict[str, Any]:
    """Copy a capture without cropping, scaling, or format conversion."""

    _copy_original(source, destination, expected_sha256=source_sha256)
    width, height = _image_dimensions(destination) if destination.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES else (0, 0)
    if destination.suffix.lower() in SUPPORTED_VIDEO_SUFFIXES:
        media = _validate_video(destination)
        width, height = int(media["width"]), int(media["height"])
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    if not digest:
        destination.unlink(missing_ok=True)
        raise ScreenshotError(f"source visual presentation is empty: {source.name}")
    geometry = _presentation_geometry_with_policy(width, height, text_bearing=text_bearing, reject_small=False) if width and height else {
        "effective_scale": 1.0,
        "display_width": 0,
        "display_height": 0,
        "upscaled": False,
        "display_safe_area": {"width": DISPLAY_MAX_WIDTH, "height": DISPLAY_MAX_HEIGHT},
    }
    return {
        "path": destination,
        "presentation_sha256": digest,
        "presentation_dimensions": {"width": width, "height": height},
        "presentation_format": destination.suffix.lower().lstrip("."),
        "presentation_bytes": destination.stat().st_size,
        "presentation_effective_scale": geometry["effective_scale"],
        "presentation_display_width": geometry["display_width"],
        "presentation_display_height": geometry["display_height"],
        "presentation_upscaled": geometry["upscaled"],
        "source_sha256": source_sha256,
    }


def _request_for_item(item: SourceItem, *, mode: str) -> dict[str, Any]:
    item_slug = _safe_id(item.item_id)
    kind = source_kind(item.original_url)
    is_x = kind == "x"
    return {
        "request_id": f"source-{item_slug}",
        "item_id": item.item_id,
        "title": item.title,
        "source_name": item.source_name,
        "original_url": item.original_url,
        "aihot_url": item.aihot_url,
        "source_kind": source_kind(item.original_url),
        "mode": mode,
        "capture_scope": "original_post_only" if is_x else "main_content_with_optional_image",
        "capture_contract": {
            "capture_contract_id": CAPTURE_CONTRACT_ID,
            "tight_element_bounds": False,
            "main_content_in_viewport": not is_x,
            "first_viewport_only": True,
            "title_required": not is_x,
            "allow_scrolling": False,
            "follow_repost_to_original": is_x,
            "allowed_asset_roles": ["x_original_post"] if is_x else ["article_main_content", "article_image"],
            "maximum_assets": 1 if is_x else 1 + ARTICLE_IMAGE_LIMIT,
            "maximum_article_images": 0 if is_x else ARTICLE_IMAGE_LIMIT,
            "preserve_browser_screenshot_bytes": True,
            "minimum_device_scale_factor": MIN_CAPTURE_DEVICE_SCALE,
            "capture_type": "viewport_screenshot",
            "viewport_override": {"width": EXPANDED_VIEWPORT_WIDTH, "height": EXPANDED_VIEWPORT_HEIGHT},
            "restore_viewport_after_capture": True,
            "record_geometry": ["viewport", "viewport_override", "device_scale_factor", "crop_box", "content_bounds", "capture_type", "original_dimensions"],
            "recapture_attempts": 0,
            "post_processing": False,
        },
        "translate_preferred": True,
        "inbox": f"source-visuals/raw/{item_slug}",
        "presentation": f"source-visuals/presentation/{item_slug}",
        "naming_rule": f"source-{item_slug}-visual-{{index:02d}}.{{original_extension}}",
        "expected": "one x_original_post asset" if is_x else "one article_main_content asset plus optional one article_image asset",
        "status": "pending",
        "terminal_state": "pending",
        "attempts": 0,
        "capture_executor": None,
        "error_code": None,
    }


def _task_document(*, run_dir: Path, mode: str, requests: list[Mapping[str, Any]]) -> str:
    lines = [
        "# 原文视觉素材任务",
        "",
        f"- 运行目录：`{run_dir}`",
        f"- 截图模式：`{mode}`",
        "- 仅打开清单中的冻结原文地址；优先寻找与新闻事实直接相关的主图、图表、产品画面或正文关键区域。",
        "- 使用 Codex 内置浏览器；不登录、不读取或复制 Cookie、浏览器 Profile、Local Storage 或凭据。",
        "- 自动化开始前必须确认当前任务同时具备内置浏览器控制、直接项目文件写入和 1440×900 扩大视口截图能力；将结果写入 `artifacts/source_visual_preflight.json`。任一能力缺失就停止截图阶段并标记 unavailable。",
        "- 严禁使用 CUA 操作 Terminal、Chrome、base64/剪贴板中转、复制浏览器状态或任何替代截图流程；不要重试。每个原文最多一次尝试。",
        f"- 每次打开原文前，先将 Codex 内置浏览器临时扩大到 {EXPANDED_VIEWPORT_WIDTH}×{EXPANDED_VIEWPORT_HEIGHT}；截图完成后恢复默认视口。",
        f"- 只截取扩大后的当前视口（`tab.screenshot({{fullPage: false}})`），记录 viewport、viewport override、device scale、裁剪框、捕获类型与原始尺寸；流水线不再二次裁切、缩放或重编码。契约编号：`{CAPTURE_CONTRACT_ID}`。",
        "- 视口应优先展示可读主内容；不要滚动全文、使用 fullPage/clip 或拼接，也不要把无关页面外壳、评论区、推荐流或大片空白作为主体。",
        "",
        "## 截图任务",
        "",
    ]
    for index, request in enumerate(requests, 1):
        lines.extend([
            f"### {index}. {request['item_id']}",
            f"- 标题：{request['title']}",
            f"- 原帖地址：{request['original_url']}",
            f"- 来源类型：`{request['source_kind']}`",
            f"- 保存目录：`{request['inbox']}`",
            f"- 命名规则：`{request['naming_rule']}`",
            "- 文件名中的 index 从 01 连续递增；可以没有合格素材，失败时记录原因并跳过。",
            "- 状态只能从 `pending` 到 `validated` 或 `unavailable`；终态条目不得再次尝试。",
        ])
        if mode == "auto":
            if request["source_kind"] == "x":
                lines.extend([
                    "- X：只打开并截取冻结链接对应的原帖主体；绝不截取回复、他人转发、引用卡片或推荐内容。若冻结链接本身是转发/引用帖，跟随可验证的原帖链接，并在 receipt 中记录原帖 URL。",
                    "- X 只保存一个 `x_original_post` 素材；若无法确认原帖或遇到登录墙、验证码、权限/安全提示，标记 unavailable，不绕过限制。",
                ])
            else:
                lines.extend([
                    f"- 普通网页：直接打开原文，等待主内容稳定，在 {EXPANDED_VIEWPORT_WIDTH}×{EXPANDED_VIEWPORT_HEIGHT} 的扩大视口中保存一张 `article_main_content` 截图；视口中必须看见站点身份、文章标题和相邻首段或主图。不得截取全文或拼接。标题不可见时标记 unavailable。",
                    "- 若正文存在明确对应的主图，可额外保存一张 `article_image`；优先使用图片元素本身，排除 Logo、头像、广告、评论、推荐图和页面装饰图。找不到时只保留主内容截图。",
                ])
            lines.extend([
                "- 自动模式由 Codex 内置浏览器执行；遇到登录墙、验证码、权限或安全提示时标记 unavailable，不绕过限制。",
                f"- 自动完成后，在该目录写入 `capture.json`，至少包含 `complete`、`asset_count`（旧字段 `page_count` 可兼容）、`source_url`、`capture_executor`（必须为 `{CAPTURE_EXECUTOR}`）、`capture_method`（必须为 `{CAPTURE_METHOD}`）、`capture_contract_id`（必须为 `{CAPTURE_CONTRACT_ID}`）、`capture_scope`、`files`、`viewport`、`viewport_override`、`device_scale_factor`、`crop_box`、`original_dimensions` 和 `evidence_text`；文件记录必须包含 `asset_role`。主内容文件还应记录 `content_bounds`，用于审计视口中的正文位置。",
                "- 不做分辨率重捕、fullPage/clip 裁剪或本地放大；模板会把扩大视口截图按比例放大到原文视觉舞台，保持整张页面排版完整。",
            ])
        else:
            lines.append("- 手动模式：按同样的内容范围保存原始文件；流水线只逐字节复制 raw 文件，不做裁切、缩放或格式转换。")
        lines.append("")
    if not requests:
        lines.append("本期没有可用原文链接，不需要视觉素材。")
    return "\n".join(lines) + "\n"


def prepare_screenshot_requests(
    run_dir: Path,
    items: Iterable[SourceItem],
    *,
    mode: str,
    minimum_selected_stories: int = 0,
) -> dict[str, Any]:
    mode = normalize_mode(mode)
    acceptance = source_visual_acceptance(
        mode=mode,
        minimum_selected_stories=minimum_selected_stories,
    )
    root = run_dir / "source-visuals"
    root.mkdir(parents=True, exist_ok=True)
    # Keep the legacy marker location available for manual mode without
    # creating any browser-state files.
    (run_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    if mode == "off":
        manifest = {"version": SOURCE_VISUAL_REQUEST_VERSION, "schema_version": 4, "mode": "off", "enabled": False, "status": "disabled", "requests": [], "items": [], "generated_at": _now(), "browser": "codex_in_app_browser_only", "policy": {"presentation_copy": "byte_identical", "fixed_presentation_aspect_ratio": False, "post_processing": False, "capture_strategy": CAPTURE_STRATEGY, "viewport_override": {"width": EXPANDED_VIEWPORT_WIDTH, "height": EXPANDED_VIEWPORT_HEIGHT}}, "acceptance": acceptance}
        write_json(artifacts / "screenshot_requests.json", manifest)
        write_json(artifacts / "source_visual_requests.json", manifest)
        return manifest

    requests = [_request_for_item(item, mode=mode) for item in items if str(item.original_url or "").strip()]
    existing_path = artifacts / "screenshot_requests.json"
    existing: dict[str, Any] | None = None
    if existing_path.is_file():
        try:
            value = json.loads(existing_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                existing = value
        except (OSError, json.JSONDecodeError):
            existing = None
    existing_pairs = [(str(request.get("item_id")), str(request.get("original_url"))) for request in (existing or {}).get("requests", []) if isinstance(request, Mapping)]
    requested_pairs = [(str(request["item_id"]), str(request["original_url"])) for request in requests]
    existing_by_pair = {
        (str(request.get("item_id")), str(request.get("original_url"))): request
        for request in (existing or {}).get("requests", [])
        if isinstance(request, Mapping)
    }
    # Preserve the one-shot state when prepare is called again by an
    # unattended run.  Recreating the request list must never reset an
    # unavailable item back to pending.
    for request in requests:
        previous = existing_by_pair.get((str(request["item_id"]), str(request["original_url"])))
        if not previous:
            continue
        previous_state = _capture_terminal_state(previous)
        if previous_state in TERMINAL_CAPTURE_STATES:
            request["terminal_state"] = previous_state
            request["status"] = previous_state
            try:
                request["attempts"] = max(0, int(previous.get("attempts") or 0))
            except (TypeError, ValueError):
                request["attempts"] = 1 if previous_state in TERMINAL_CAPTURE_STATES else 0
            request["capture_executor"] = previous.get("capture_executor")
            request["error_code"] = previous.get("error_code")
        else:
            request["attempts"] = 0
    existing_minimum = int(
        ((existing or {}).get("acceptance") or {}).get("minimum_selected_stories") or 0
    )
    if (
        existing
        and existing.get("version") == SOURCE_VISUAL_REQUEST_VERSION
        and existing.get("mode") == mode
        and existing_pairs == requested_pairs
        and existing_minimum == acceptance["minimum_selected_stories"]
    ):
        # A dated run may already have a request manifest from the previous
        # screenshot contract.  Keep the inboxes and any existing raw files,
        # but refresh the request objects so the same URL list immediately
        # adopts the expanded responsive-viewport policy.
        existing["schema_version"] = 4
        existing["requests"] = requests
        existing["acceptance"] = acceptance
        existing["policy"] = {
            **dict(existing.get("policy") or {}),
            "presentation_copy": "byte_identical",
            "fixed_presentation_aspect_ratio": False,
            "post_processing": False,
            "capture_strategy": CAPTURE_STRATEGY,
            "capture_contract_id": CAPTURE_CONTRACT_ID,
            "capture_method": CAPTURE_METHOD,
            "viewport_override": {"width": EXPANDED_VIEWPORT_WIDTH, "height": EXPANDED_VIEWPORT_HEIGHT},
            "minimum_device_scale_factor": MIN_CAPTURE_DEVICE_SCALE,
            "maximum_recapture_attempts": 0,
        }
        existing["automation"] = {
            "provider": "codex_in_app_browser",
            "requires_active_user_visible_browser": True,
            "required_capabilities": list(CAPTURE_CAPABILITIES),
            "preflight_path": "artifacts/source_visual_preflight.json",
            "manual_handoff_supported": True,
            "never_copy_browser_state": True,
            "never_use_terminal_cua": True,
            "never_use_base64_fallback": True,
            "max_attempts_per_item": 1,
        }
        existing["policy"].pop("byte_identical_presentation_copy", None)
        preflight_path = artifacts / "source_visual_preflight.json"
        if not preflight_path.is_file():
            write_json(preflight_path, _default_capture_preflight())
        write_json(artifacts / "source_visual_requests.json", existing)
        (artifacts / "SOURCE_VISUAL_TASKS.md").write_text(_task_document(run_dir=run_dir, mode=mode, requests=requests), encoding="utf-8")
        return existing

    for request in requests:
        (run_dir / str(request["inbox"])).mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": SOURCE_VISUAL_REQUEST_VERSION,
        "schema_version": 4,
        "mode": mode,
        "enabled": True,
        "status": "pending",
        "generated_at": _now(),
        "ready_marker": "screenshots/READY",
        "requests": requests,
        "items": [],
        "policy": {
            "one_time_url_list": True,
            "max_video_visual_stories": None,
            "max_visuals_per_story": 1,
            "browser": "codex_in_app_browser_only",
            "preserve_raw_inbox": True,
            "presentation_copy": "byte_identical",
            "fixed_presentation_aspect_ratio": False,
            "post_processing": False,
            "capture_strategy": CAPTURE_STRATEGY,
            "capture_contract_id": CAPTURE_CONTRACT_ID,
            "capture_method": CAPTURE_METHOD,
            "viewport_override": {"width": EXPANDED_VIEWPORT_WIDTH, "height": EXPANDED_VIEWPORT_HEIGHT},
            "minimum_device_scale_factor": MIN_CAPTURE_DEVICE_SCALE,
            "maximum_recapture_attempts": 0,
            "no_cookie_or_profile_access": True,
            "auto_mode_skips_blocked_sources": True,
            "one_shot_capture_state": "pending_to_validated_or_unavailable",
        },
        "automation": {
            "provider": "codex_in_app_browser",
            "requires_active_user_visible_browser": True,
            "required_capabilities": list(CAPTURE_CAPABILITIES),
            "preflight_path": "artifacts/source_visual_preflight.json",
            "manual_handoff_supported": True,
            "never_copy_browser_state": True,
            "never_use_terminal_cua": True,
            "never_use_base64_fallback": True,
            "max_attempts_per_item": 1,
        },
        "acceptance": acceptance,
    }
    preflight_path = artifacts / "source_visual_preflight.json"
    if not preflight_path.is_file():
        write_json(preflight_path, _default_capture_preflight())
    write_json(existing_path, manifest)
    (artifacts / "SCREENSHOT_TASKS.md").write_text(_task_document(run_dir=run_dir, mode=mode, requests=requests), encoding="utf-8")
    # Canonical names for the generalized workflow, while retaining the old
    # names above for callers and dated runs that still expect them.
    write_json(artifacts / "source_visual_requests.json", manifest)
    (artifacts / "SOURCE_VISUAL_TASKS.md").write_text(_task_document(run_dir=run_dir, mode=mode, requests=requests), encoding="utf-8")
    return manifest


def load_screenshot_requests(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "artifacts" / "source_visual_requests.json"
    if not path.is_file():
        path = run_dir / "artifacts" / "screenshot_requests.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScreenshotError(f"could not read screenshot request manifest: {path}") from exc
    if not isinstance(value, dict):
        raise ScreenshotError("screenshot request manifest must be an object")
    return value


def _receipt_for(inbox: Path) -> dict[str, Any] | None:
    path = inbox / "capture.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScreenshotError(f"invalid capture receipt: {path}") from exc
    return value if isinstance(value, dict) else None


def _capture_metadata(receipt: Mapping[str, Any] | None, source_path: Path) -> dict[str, Any]:
    receipt = receipt or {}
    file_record: Mapping[str, Any] = {}
    for candidate in receipt.get("files") or []:
        if isinstance(candidate, Mapping) and str(candidate.get("file") or candidate.get("name") or "") == source_path.name:
            file_record = candidate
            break
    capture_type = str(file_record.get("capture_type") or receipt.get("capture_type") or "element_screenshot")
    capture_contract_id = str(file_record.get("capture_contract_id") or receipt.get("capture_contract_id") or "")
    asset_role = str(file_record.get("asset_role") or receipt.get("asset_role") or "")
    capture_executor = str(file_record.get("capture_executor") or receipt.get("capture_executor") or "")
    if not capture_executor and str(receipt.get("capture_method") or "").lower().startswith("iab"):
        capture_executor = CAPTURE_EXECUTOR
    evidence_text = str(file_record.get("evidence_text") or receipt.get("evidence_text") or "").strip()
    viewport = file_record.get("viewport") or receipt.get("viewport")
    crop_box = file_record.get("crop_box") or receipt.get("crop_box")
    content_bounds = file_record.get("content_bounds") or receipt.get("content_bounds")
    original_dimensions = file_record.get("original_dimensions") or receipt.get("original_dimensions")
    device_scale = file_record.get("device_scale_factor", receipt.get("device_scale_factor"))
    try:
        device_scale_value = float(device_scale)
    except (TypeError, ValueError):
        device_scale_value = 0.0
    text_bearing = bool(file_record.get("text_bearing", receipt.get("text_bearing", capture_type not in {"media", "image", "video", "hero_image"})))
    try:
        capture_attempt = int(file_record.get("capture_attempt") or receipt.get("capture_attempt") or 1)
    except (TypeError, ValueError):
        capture_attempt = 1
    return {
        "capture_type": capture_type,
        "capture_contract_id": capture_contract_id,
        "asset_role": asset_role,
        "capture_executor": capture_executor,
        "captured_url": str(file_record.get("captured_url") or receipt.get("captured_url") or ""),
        "canonical_post_url": str(file_record.get("canonical_post_url") or receipt.get("canonical_post_url") or ""),
        "title_visible": bool(file_record.get("title_visible", receipt.get("title_visible", False))),
        "viewport": viewport,
        "viewport_override": file_record.get("viewport_override") or receipt.get("viewport_override"),
        "device_scale_factor": device_scale_value,
        "crop_box": crop_box,
        "content_bounds": content_bounds,
        "original_dimensions": original_dimensions,
        "evidence_text": evidence_text,
        "text_bearing": text_bearing,
        "capture_attempt": max(1, capture_attempt),
    }


def _capture_scope_error(
    request: Mapping[str, Any],
    receipt: Mapping[str, Any] | None,
    captures: list[Mapping[str, Any]],
) -> str | None:
    """Validate the source-specific asset roles recorded by a new receipt."""

    if not receipt:
        return None
    scope = str(request.get("capture_scope") or "")
    # Historical request/receipt pairs used one generic key-visual contract.
    # They remain readable and are intentionally not reinterpreted here.
    if scope not in {"original_post_only", "first_viewport_with_optional_image", "main_content_with_optional_image"}:
        return None
    recorded_scope = str(receipt.get("capture_scope") or "").strip()
    if recorded_scope and recorded_scope != scope:
        return "capture receipt capture_scope does not match the request"
    roles = [str(capture.get("asset_role") or "") for capture in captures]
    capture_types = [str(capture.get("capture_type") or "").casefold() for capture in captures]
    capture_method = str(receipt.get("capture_method") or "").casefold()
    if scope == "original_post_only":
        if len(captures) != 1:
            return "X original_post_only requires exactly one asset"
        if roles != ["x_original_post"]:
            return "X capture must contain exactly one x_original_post asset"
        viewport = _capture_rect(captures[0].get("viewport"))
        crop_box = _capture_rect(captures[0].get("crop_box"))
        if str(captures[0].get("capture_type") or "").casefold() != "viewport_screenshot" or viewport is None or crop_box is None or any(abs(crop_box[index] - viewport[index]) > 0.5 for index in range(4)):
            return "X original-post capture must use one direct viewport screenshot"
        if any(role in {"x_reply", "x_repost", "x_quote", "reply", "repost", "quote_post"} for role in roles):
            return "X capture contains a reply, repost, or quote asset"
        if receipt.get("is_repost") or receipt.get("is_quote_post"):
            canonical = str(receipt.get("canonical_post_url") or receipt.get("captured_url") or "").strip()
            if not canonical or canonical == str(request.get("original_url") or ""):
                return "X repost or quote capture must record the resolved original post URL"
        return None

    if scope == "main_content_with_optional_image":
        primary = [capture for capture in captures if str(capture.get("asset_role") or "") == "article_main_content"]
        images = [capture for capture in captures if str(capture.get("asset_role") or "") == "article_image"]
        if len(primary) != 1:
            return "web capture requires exactly one article_main_content asset"
        if len(images) > ARTICLE_IMAGE_LIMIT:
            return f"web capture allows at most {ARTICLE_IMAGE_LIMIT} article_image asset"
        if not bool(primary[0].get("title_visible")):
            return "web main-content capture must visibly contain the article title"
        bounds = _capture_rect(primary[0].get("content_bounds"))
        if bounds is None:
            return "web main-content capture must record positive content_bounds"
        if str(primary[0].get("capture_type") or "").casefold() != "viewport_screenshot":
            return "web main-content capture must use one direct viewport screenshot"
        viewport = _capture_rect(primary[0].get("viewport"))
        crop_box = _capture_rect(primary[0].get("crop_box"))
        if viewport is None or crop_box is None or any(abs(crop_box[index] - viewport[index]) > 0.5 for index in range(4)):
            return "web main-content capture must use one direct viewport screenshot"
        if any(any(token in capture_type for token in ("full_page", "full-page", "stitched", "scroll")) for capture_type in capture_types) or any(token in capture_method for token in ("scroll", "stitch", "full_page", "full-page")):
            return "web main-content capture must use one direct viewport screenshot"
        return None

    primary = [capture for capture in captures if str(capture.get("asset_role") or "") == "article_first_viewport"]
    images = [capture for capture in captures if str(capture.get("asset_role") or "") == "article_image"]
    if len(primary) != 1:
        return "web capture requires exactly one article_first_viewport asset"
    if len(images) > ARTICLE_IMAGE_LIMIT:
        return f"web capture allows at most {ARTICLE_IMAGE_LIMIT} article_image asset"
    if not bool(primary[0].get("title_visible")):
        return "web first viewport must visibly contain the article title"
    if any(any(token in capture_type for token in ("full_page", "full-page", "stitched", "scroll")) for capture_type in capture_types) or any(token in capture_method for token in ("scroll", "stitch", "full_page", "full-page")):
        return "web capture must not use scrolling, full-page, or stitched capture"
    return None


def _capture_contract_error(
    request: Mapping[str, Any],
    receipt: Mapping[str, Any] | None,
    captures: list[Mapping[str, Any]],
) -> str | None:
    """Reject captures made by the old tight-element screenshot contract.

    Old receipts are still retained for audit, but they must never be silently
    treated as optional skips after the viewport contract changes.  A current
    auto capture is one direct 1440x900 in-app-browser viewport screenshot;
    article images remain the only allowed direct-image exception.
    """

    if not receipt:
        return "capture contract receipt is missing; recapture with the current expanded viewport contract"
    if str(receipt.get("capture_contract_id") or "") != CAPTURE_CONTRACT_ID:
        return "capture contract is stale; recapture with the current expanded viewport contract"
    if str(receipt.get("capture_method") or "") != CAPTURE_METHOD:
        return "capture contract capture_method is stale; recapture with the current expanded viewport contract"
    scope = str(request.get("capture_scope") or "")
    primary_roles = {"x_original_post"} if scope == "original_post_only" else {"article_main_content"}
    primary = [capture for capture in captures if str(capture.get("asset_role") or "") in primary_roles]
    if len(primary) != 1:
        return "capture contract requires exactly one current primary viewport asset"
    capture = primary[0]
    if str(capture.get("capture_contract_id") or "") != CAPTURE_CONTRACT_ID:
        return "capture contract file record is stale; recapture with the current expanded viewport contract"
    if str(capture.get("capture_type") or "").casefold() != "viewport_screenshot":
        return "capture contract requires capture_type=viewport_screenshot"
    expected = (0.0, 0.0, float(EXPANDED_VIEWPORT_WIDTH), float(EXPANDED_VIEWPORT_HEIGHT))
    viewport = _capture_rect(capture.get("viewport"))
    override = _capture_rect(capture.get("viewport_override"))
    crop_box = _capture_rect(capture.get("crop_box"))
    for name, rect in (("viewport", viewport), ("viewport_override", override), ("crop_box", crop_box)):
        if rect is None or any(abs(rect[index] - expected[index]) > 0.5 for index in range(4)):
            return f"capture contract {name} must be the complete 1440x900 viewport"
    original_dimensions = _capture_rect(capture.get("original_dimensions"))
    if original_dimensions is None:
        return "capture contract must record positive original_dimensions"
    return None


def _capture_rect(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        rect = tuple(float(value.get(key) or 0) for key in ("x", "y", "width", "height"))
    except (TypeError, ValueError):
        return None
    if rect[2] <= 0 or rect[3] <= 0:
        return None
    return rect  # type: ignore[return-value]


def _unavailable_item(
    request: Mapping[str, Any],
    *,
    attempts: int,
    capture_executor: str,
    error_code: str,
    error: str,
) -> dict[str, Any]:
    return {
        "item_id": str(request.get("item_id") or ""),
        "title": request.get("title"),
        "original_url": request.get("original_url"),
        "source_kind": request.get("source_kind"),
        "capture_scope": request.get("capture_scope"),
        "status": "unavailable",
        "terminal_state": "unavailable",
        "attempts": max(1, int(attempts)),
        "capture_executor": capture_executor,
        "error_code": error_code,
        "pages": [],
        "asset_count": 0,
        "page_count": 0,
        "translated": False,
        "complete": False,
        "error": error,
        "receipt": None,
    }


def _write_request_states(
    run_dir: Path,
    requests_manifest: Mapping[str, Any],
    request_updates: list[Mapping[str, Any]],
) -> None:
    """Persist terminal per-URL state in both request-manifest aliases."""

    updated = dict(requests_manifest)
    updated["requests"] = [dict(request) for request in request_updates]
    updated["updated_at"] = _now()
    write_json(run_dir / "artifacts" / "source_visual_requests.json", updated)
    write_json(run_dir / "artifacts" / "screenshot_requests.json", updated)


def collect_screenshots(run_dir: Path, *, mode: str | None = None, require_ready: bool | None = None) -> dict[str, Any]:
    requests_manifest = load_screenshot_requests(run_dir)
    resolved_mode = normalize_mode(mode or (requests_manifest or {}).get("mode") or DEFAULT_X_SCREENSHOT_MODE)
    if require_ready is None:
        require_ready = resolved_mode == "manual"
    if resolved_mode == "off":
        result = {"version": SOURCE_VISUAL_MANIFEST_VERSION, "schema_version": 5, "mode": "off", "enabled": False, "status": "disabled", "items": [], "total_pages": 0, "browser": "codex_in_app_browser_only", "policy": {"presentation_copy": "byte_identical", "fixed_presentation_aspect_ratio": False, "post_processing": False, "capture_strategy": CAPTURE_STRATEGY, "viewport_override": {"width": EXPANDED_VIEWPORT_WIDTH, "height": EXPANDED_VIEWPORT_HEIGHT}}, "acceptance": (requests_manifest or {}).get("acceptance") or source_visual_acceptance(mode="off")}
        write_json(run_dir / "artifacts" / "screenshot_manifest.json", result)
        write_json(run_dir / "artifacts" / "source_visual_manifest.json", result)
        return result
    if requests_manifest is None:
        raise ScreenshotError("source visual request manifest is missing; run prepare with --source-visual-mode first")
    requests = [request for request in requests_manifest.get("requests", []) if isinstance(request, Mapping)]
    previous_manifest: dict[str, Any] = {}
    try:
        previous_value = json.loads((run_dir / "artifacts" / "source_visual_manifest.json").read_text(encoding="utf-8"))
        if isinstance(previous_value, dict):
            previous_manifest = previous_value
    except (OSError, json.JSONDecodeError):
        previous_manifest = {}
    previous_items = {
        str(item.get("item_id") or ""): item
        for item in previous_manifest.get("items", [])
        if isinstance(item, Mapping)
    }
    preflight = _load_capture_preflight(run_dir) or _default_capture_preflight()
    preflight_error = _preflight_error_code(preflight)
    legacy_layout = any("screenshots/inbox" in str(request.get("inbox") or "") for request in requests_manifest.get("requests", []) if isinstance(request, Mapping))
    normalized_root = run_dir / ("screenshots/normalized" if legacy_layout else "source-visuals/presentation")
    normalized_root.mkdir(parents=True, exist_ok=True)
    run_root = run_dir.resolve()
    ready_marker = (run_dir / str(requests_manifest.get("ready_marker") or "screenshots/READY")).resolve()
    if ready_marker != run_root and run_root not in ready_marker.parents:
        raise ScreenshotError("ready marker path escapes the run directory")
    ready = ready_marker.is_file()
    ready_mtime = ready_marker.stat().st_mtime if ready else 0.0
    item_results: list[dict[str, Any]] = []
    pending: list[str] = []
    needs_recapture: list[str] = []
    errors: list[str] = []
    request_updates: list[Mapping[str, Any]] = []
    for request in requests:
        item_id = str(request.get("item_id") or "")
        request_state = _capture_terminal_state(request)
        previous_item = previous_items.get(item_id)

        def save_state(
            state: str,
            *,
            attempts: int,
            capture_executor: str | None,
            error_code: str | None,
        ) -> None:
            updated_request = dict(request)
            updated_request["status"] = state
            updated_request["terminal_state"] = state
            updated_request["attempts"] = max(0, int(attempts))
            updated_request["capture_executor"] = capture_executor
            updated_request["error_code"] = error_code
            request_updates.append(updated_request)

        # Auto capture has terminal per-URL state.  Once the runner has made
        # one decision, a later scheduled invocation must not inspect the URL
        # again or try a different executor.  Validated pages are reused from
        # the previous manifest; unavailable pages remain unavailable.
        if resolved_mode == "auto" and request_state in TERMINAL_CAPTURE_STATES:
            if request_state == "validated" and previous_item and previous_item.get("status") == "validated" and previous_item.get("pages"):
                reused = dict(previous_item)
                reused["terminal_state"] = "validated"
                reused["attempts"] = max(1, int(request.get("attempts") or 1))
                reused["capture_executor"] = request.get("capture_executor") or CAPTURE_EXECUTOR
                reused["error_code"] = None
                item_results.append(reused)
                request_updates.append(dict(request))
                continue
            error_code = str(request.get("error_code") or "capture_artifact_missing")
            item_results.append(_unavailable_item(
                request,
                attempts=max(1, int(request.get("attempts") or 1)),
                capture_executor=str(request.get("capture_executor") or "none"),
                error_code=error_code,
                error=str((previous_item or {}).get("error") or "source visual is terminally unavailable; no retry is permitted"),
            ))
            request_updates.append(dict(request))
            continue

        inbox = run_dir / str(request.get("inbox") or "")
        source_paths = sorted((path for path in inbox.rglob("*") if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in SUPPORTED_SUFFIXES), key=_natural_key) if inbox.is_dir() else []
        if ready and source_paths and max(path.stat().st_mtime for path in source_paths) > ready_mtime:
            # A capture changed after the human acknowledgement; require a
            # fresh marker instead of silently trusting the old completeness
            # decision.
            ready = False
        if not source_paths:
            if resolved_mode == "auto":
                reason = "browser capture capability preflight failed" if preflight_error else "no browser capture was written to the request inbox"
                code = preflight_error or "browser_capture_unavailable"
                item_results.append(_unavailable_item(request, attempts=1, capture_executor="none", error_code=code, error=reason))
                save_state("unavailable", attempts=1, capture_executor="none", error_code=code)
            else:
                pending.append(item_id)
                item_results.append({"item_id": item_id, "source_kind": request.get("source_kind"), "capture_scope": request.get("capture_scope"), "status": "pending", "terminal_state": "pending", "attempts": 0, "capture_executor": None, "error_code": None, "pages": [], "asset_count": 0, "page_count": 0, "inbox": str(inbox.relative_to(run_dir))})
                save_state("pending", attempts=0, capture_executor=None, error_code=None)
            continue
        if resolved_mode == "auto" and preflight_error:
            item_results.append(_unavailable_item(
                request,
                attempts=1,
                capture_executor="none",
                error_code=preflight_error,
                error="browser capture capability preflight failed; no fallback executor is permitted",
            ))
            save_state("unavailable", attempts=1, capture_executor="none", error_code=preflight_error)
            continue
        pages: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        item_error: str | None = None
        receipt = _receipt_for(inbox)
        receipt_captures = [_capture_metadata(receipt, path) for path in source_paths] if receipt else []
        validation_paths = [] if resolved_mode == "auto" and receipt is None else source_paths
        legacy_text_contract = str(request.get("capture_scope") or "") == "first_viewport_with_optional_image"
        for page_index, source_path in enumerate(validation_paths, 1):
            try:
                capture = _capture_metadata(receipt, source_path)
                if resolved_mode == "auto" and source_path.suffix.lower() not in SUPPORTED_VIDEO_SUFFIXES:
                    direct_image_download = (
                        capture["asset_role"] == "article_image"
                        and capture["capture_type"].casefold() in {"image_asset", "direct_image_download"}
                    )
                    if not direct_image_download:
                        required_device_scale = 2.0 if legacy_text_contract else MIN_CAPTURE_DEVICE_SCALE
                        if capture["device_scale_factor"] < required_device_scale:
                            raise ScreenshotError(f"capture device scale must be at least {MIN_CAPTURE_DEVICE_SCALE:g}x")
                        if not capture["viewport"] or not capture["crop_box"] or not capture["original_dimensions"]:
                            raise ScreenshotError("auto capture must record viewport, crop_box and original_dimensions")
                if resolved_mode == "auto" and not capture["evidence_text"]:
                    raise ScreenshotError("auto capture must record evidence_text for claim matching")
                metadata = _validate_media(
                    source_path,
                    text_bearing=bool(capture["text_bearing"]),
                    reject_small=legacy_text_contract,
                )
                if metadata["sha256"] in seen_hashes:
                    raise ScreenshotError(f"duplicate page content: {source_path.name}")
                seen_hashes.add(metadata["sha256"])
                suffix = _actual_suffix(metadata)
                normalized_name = (
                    f"x-{_safe_id(item_id)}-page-{page_index:03d}{suffix}"
                    if legacy_layout
                    else f"source-{_safe_id(item_id)}-visual-{page_index:02d}{suffix}"
                )
                normalized_path = normalized_root / normalized_name
                presentation = _write_presentation(
                    source_path,
                    normalized_path,
                    source_sha256=str(metadata["sha256"]),
                    text_bearing=bool(capture["text_bearing"]),
                )
                presentation_path = Path(presentation.pop("path"))
                pages.append({
                    "asset_id": f"source-{_safe_id(item_id)}-visual-{page_index:02d}",
                    "item_id": item_id,
                    "page": page_index,
                    "path": str(presentation_path.relative_to(run_dir)),
                    "source_path": str(source_path.relative_to(run_dir)),
                    "duration_seconds": DEFAULT_PAGE_DURATION_SECONDS,
                    **capture,
                    **metadata,
                    **presentation,
                })
            except (OSError, ScreenshotError) as exc:
                item_error = str(exc)
                break
        receipt_complete = bool(receipt and receipt.get("complete") is True)
        if resolved_mode == "auto" and not receipt_complete:
            item_error = item_error or "auto mode requires capture.json with complete=true"
        if resolved_mode == "auto" and (not receipt or (receipt.get("asset_count") is None and receipt.get("page_count") is None)):
            item_error = item_error or "auto mode requires capture.json with asset_count or page_count"
        if resolved_mode == "auto" and receipt and not str(receipt.get("capture_method") or "").lower().startswith("iab"):
            item_error = item_error or "auto mode requires an in-app-browser capture_method"
        if receipt and (receipt.get("asset_count") is not None or receipt.get("page_count") is not None):
            try:
                receipt_count = receipt.get("asset_count", receipt.get("page_count"))
                if int(receipt_count) != len(pages):
                    item_error = item_error or "capture receipt asset_count does not match the files in the inbox"
            except (TypeError, ValueError):
                item_error = item_error or "capture receipt asset_count is not an integer"
        if receipt and receipt.get("source_url") and str(receipt.get("source_url")) != str(request.get("original_url")):
            item_error = item_error or "capture receipt source_url does not match the frozen original URL"
        item_error = item_error or _capture_scope_error(request, receipt, receipt_captures)
        if resolved_mode == "auto":
            item_error = item_error or _capture_contract_error(request, receipt, receipt_captures)
        if item_error and resolved_mode != "auto":
            errors.append(f"{item_id}: {item_error}")
        # A present-but-invalid auto capture is terminally unavailable.  The
        # old recapture_required state invited the automation to repeat the
        # same URL or search for a forbidden fallback executor.
        try:
            receipt_attempt = int((receipt or {}).get("capture_attempt") or 1)
        except (TypeError, ValueError):
            receipt_attempt = 1
        capture_attempt = max((int(page.get("capture_attempt") or 1) for page in pages), default=receipt_attempt)
        if resolved_mode == "auto":
            if item_error:
                pages = []
                status = "unavailable"
                terminal_state = "unavailable"
                attempts = 1
                capture_executor = str((receipt or {}).get("capture_executor") or "none")
                error_code = _capture_error_code(item_error)
            else:
                status = "validated"
                terminal_state = "validated"
                attempts = 1
                capture_executor = str(next((page.get("capture_executor") for page in pages if page.get("capture_executor")), CAPTURE_EXECUTOR))
                error_code = None
        else:
            status = "validated" if pages and not item_error else "needs_review" if pages else "pending"
            terminal_state = "validated" if status == "validated" else "pending"
            attempts = 1 if pages else 0
            capture_executor = str(next((page.get("capture_executor") for page in pages if page.get("capture_executor")), "none"))
            error_code = _capture_error_code(item_error) if item_error else None
            if status == "pending" or status == "needs_review":
                pending.append(item_id)
        item_results.append({
            "item_id": item_id,
            "title": request.get("title"),
            "original_url": request.get("original_url"),
            "source_kind": request.get("source_kind"),
            "capture_scope": request.get("capture_scope"),
            "status": status,
            "terminal_state": terminal_state,
            "attempts": attempts,
            "capture_executor": capture_executor,
            "error_code": error_code,
            "pages": pages,
            "asset_count": len(pages),
            "page_count": len(pages),
            "translated": bool(receipt.get("translated")) if receipt else False,
            "complete": receipt_complete or (resolved_mode == "manual" and ready and bool(pages)),
            "error": item_error,
            "receipt": str((inbox / "capture.json").relative_to(run_dir)) if receipt else None,
        })
        save_state(terminal_state, attempts=attempts, capture_executor=capture_executor, error_code=error_code)

    all_valid = not requests or (not pending and not errors and (ready or not require_ready))
    if resolved_mode == "auto":
        validated_count = sum(1 for item in item_results if item.get("status") == "validated")
        status = "validated" if not item_results or validated_count == len(item_results) else "validated_with_skips"
        all_valid = True
        if preflight.get("status") in {"required", "pending", "unknown"}:
            if validated_count:
                preflight = record_capture_preflight(
                    run_dir,
                    browser_control=True,
                    direct_project_file_write=True,
                    expanded_viewport_screenshot=True,
                    source="receipt_verified",
                )
            else:
                preflight = record_capture_preflight(
                    run_dir,
                    browser_control=False,
                    direct_project_file_write=False,
                    expanded_viewport_screenshot=False,
                    source="collector",
                    error_code="browser_capture_unavailable",
                    reason="No compliant in-app-browser capture was available to validate",
                )
    else:
        status = "validated" if all_valid else "awaiting_capture"
    _write_request_states(run_dir, requests_manifest, request_updates)
    result = {
        "version": "1.0" if legacy_layout else SOURCE_VISUAL_MANIFEST_VERSION,
        "schema_version": 1 if legacy_layout else 5,
        "mode": resolved_mode,
        "enabled": True,
        "browser": "legacy_external_capture" if legacy_layout else "codex_in_app_browser",
        "status": status,
        "ready_marker": str(ready_marker.relative_to(run_root)),
        "ready": ready,
        "generated_at": _now(),
        "page_duration_seconds": DEFAULT_PAGE_DURATION_SECONDS,
        "page_lead_seconds": SCREENSHOT_LEAD_SECONDS,
        "policy": {
            "presentation_copy": "byte_identical",
            "fixed_presentation_aspect_ratio": False,
            "post_processing": False,
            "capture_strategy": CAPTURE_STRATEGY,
            "capture_contract_id": CAPTURE_CONTRACT_ID,
            "capture_method": CAPTURE_METHOD,
            "viewport_override": {"width": EXPANDED_VIEWPORT_WIDTH, "height": EXPANDED_VIEWPORT_HEIGHT},
            "preserve_raw_inbox": True,
            "maximum_recapture_attempts": 0,
            "x_max_assets": 1,
            "web_max_article_images": ARTICLE_IMAGE_LIMIT,
            "proactive_coverage_target_ratio": 0.75,
        },
        "items": item_results,
        "asset_count": sum(len(item.get("pages") or []) for item in item_results),
        "total_pages": sum(len(item.get("pages") or []) for item in item_results),
        "pending_item_ids": pending,
        "recapture_required_item_ids": [],
        "unavailable_item_ids": [str(item.get("item_id") or "") for item in item_results if item.get("status") == "unavailable"],
        "capture_preflight": preflight,
        "validated_stories": sum(1 for item in item_results if item.get("status") == "validated"),
        "errors": errors,
        "coverage": {
            "requested_items": len(requests),
            "validated_items": sum(1 for item in item_results if item.get("status") == "validated"),
            "unavailable_items": sum(1 for item in item_results if item.get("status") == "unavailable"),
            "target_ratio": 0.75,
            "ratio": round(
                sum(1 for item in item_results if item.get("status") == "validated") / len(requests),
                3,
            ) if requests else 1.0,
            "policy": "capture every accessible requested story; display every claim-matched asset",
        },
        "acceptance": (requests_manifest or {}).get("acceptance")
        or source_visual_acceptance(mode=resolved_mode),
    }
    write_json(run_dir / "artifacts" / "screenshot_manifest.json", result)
    write_json(run_dir / "artifacts" / "source_visual_manifest.json", result)
    if not all_valid:
        hint = f"请完成截图并创建 {ready_marker}，待处理条目：{', '.join(pending) or 'none'}"
        raise ScreenshotPending(hint, details=result)
    return result


def _card_read_seconds(segment: Mapping[str, Any]) -> float:
    """Estimate a readable card-first phase from authored display text."""

    cards = segment.get("cards") or []
    card_texts: list[str] = []
    for card in cards:
        if isinstance(card, Mapping):
            card_texts.extend(str(card.get(key) or "").strip() for key in ("label", "headline", "body"))
    if not card_texts:
        card_texts = [str(value).strip() for value in (segment.get("screen_points") or [])]
    characters = sum(len(value) for value in card_texts if value)
    estimate = 2.0 + characters / SOURCE_VISUAL_CARD_CHARS_PER_SECOND
    # This value is retained only for legacy script metadata.  New editorial
    # plans do not use a card-read floor at all; their visual is triggered by a
    # mapped narration beat and the audio track determines scene length.
    return round(max(SOURCE_VISUAL_MIN_CARD_READ_SECONDS, estimate), 3)


def _best_page(pages: list[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Choose one presentation page while retaining every raw page in audit."""

    if not pages:
        return None

    def score(page: Mapping[str, Any]) -> tuple[float, int]:
        try:
            explicit = float(page.get("visual_score") or page.get("candidate_score") or 0)
        except (TypeError, ValueError):
            explicit = 0.0
        try:
            area = int(page.get("width") or 0) * int(page.get("height") or 0)
        except (TypeError, ValueError):
            area = 0
        try:
            page_order = int(page.get("page") or 0)
        except (TypeError, ValueError):
            page_order = 0
        return (explicit + min(area / 10_000_000, 1.0), -page_order)

    return dict(max(pages, key=score))


def _pages_for_story(item: Mapping[str, Any], pages: list[Mapping[str, Any]], *, manifest_schema: int) -> list[dict[str, Any]]:
    """Return the assets that may be shown for one story in display order."""

    if not pages:
        return []
    # Schema 1–4 manifests retain their historical single-primary behavior.
    if manifest_schema < 5:
        selected = _best_page(pages)
        return [selected] if selected else []
    kind = str(item.get("source_kind") or "")
    if kind == "x":
        primary = [page for page in pages if str(page.get("asset_role") or "") == "x_original_post"]
        selected = primary[:1] or ([_best_page(pages)] if _best_page(pages) else [])
        return [dict(page) for page in selected]
    first_viewport = [page for page in pages if str(page.get("asset_role") or "") in {"article_main_content", "article_first_viewport"}]
    article_image = [page for page in pages if str(page.get("asset_role") or "") == "article_image"]
    if first_viewport:
        return [dict(first_viewport[0]), *[dict(page) for page in article_image[:ARTICLE_IMAGE_LIMIT]]]
    selected = _best_page(pages)
    return [selected] if selected else []


def attach_screenshots_to_script(script: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Attach claim-matched source visuals in their authored display order."""

    result = json.loads(json.dumps(script, ensure_ascii=False))
    mode = normalize_mode(str(manifest.get("mode") or "off"))
    by_item = {
        str(item.get("item_id")): item
        for item in manifest.get("items", [])
        if isinstance(item, Mapping) and item.get("status") not in {"skipped", "unavailable", "pending", "needs_review", "recapture_required"}
    }
    selected_stories = 0
    total_pages = 0
    plan_version = str((script.get("editorial") or {}).get("plan_version") or "")
    semantic_timing = plan_version in {"5.0", "4.0"}
    try:
        manifest_schema = int(manifest.get("schema_version") or 1)
    except (TypeError, ValueError):
        manifest_schema = 1
    for segment in result.get("segments", []):
        if not isinstance(segment, dict) or segment.get("kind") != "news":
            continue
        source_ids = [str(value) for value in (segment.get("source_item_ids") or [segment.get("source_item_id")]) if str(value)]
        pages: list[dict[str, Any]] = []
        # Brief groups are intentionally card-only: the shared scene must not
        # imply that one source's visual represents the other grouped items.
        if str(segment.get("visual_plan", {}).get("story_kind") or "single") != "brief_group":
            for item_id in source_ids:
                item = by_item.get(item_id)
                if not item:
                    continue
                candidate_pages = [dict(page) for page in item.get("pages", []) or [] if isinstance(page, Mapping)]
                pages.extend(_pages_for_story(item, candidate_pages, manifest_schema=manifest_schema))
        visual_plan = dict(segment.get("visual_plan") or {})
        visual_plan["screenshot_mode"] = mode
        selected: list[dict[str, Any]] = []
        beats = [dict(beat) for beat in segment.get("narration_beats") or [] if isinstance(beat, Mapping)]
        claims = [dict(claim) for claim in segment.get("source_fragments") or [] if isinstance(claim, Mapping)]
        for page in pages:
            evidence = re.sub(r"\s+", "", str(page.get("evidence_text") or ""))
            evidence_claim_ids = [
                str(claim.get("claim_id") or "")
                for claim in claims
                if claim.get("claim_id")
                and str(claim.get("source_item_id") or "") == str(page.get("item_id") or "")
                and evidence
                and (
                    re.sub(r"\s+", "", str(claim.get("source_text") or "")) in evidence
                    or evidence in re.sub(r"\s+", "", str(claim.get("source_text") or ""))
                )
            ]
            page["evidence_claim_ids"] = evidence_claim_ids
            page["asset_id"] = str(page.get("asset_id") or f"source-{_safe_id(str(page.get('item_id') or 'item'))}-visual-01")
        if plan_version == "5.0":
            used_pages: set[str] = set()
            planned_asset_ids = {
                str(beat.get("visual_asset_id") or "")
                for beat in beats
                if str(beat.get("visual_asset_id") or "")
            }
            for beat_index, beat in enumerate(beats):
                asset_id = str(beat.get("visual_asset_id") or "")
                if not asset_id or beat_index == 0:
                    continue
                beat_claims = {str(value) for value in beat.get("claim_ids") or [] if value}
                matches = [
                    page for page in pages
                    if str(page.get("asset_id") or "") not in used_pages
                    and beat_claims.intersection(str(value) for value in page.get("evidence_claim_ids") or [])
                    and (
                        str(page.get("asset_id") or "") == asset_id
                        or (
                            str(page.get("asset_role") or "") == "article_image"
                            and str(page.get("asset_id") or "") not in planned_asset_ids
                        )
                    )
                ]
                # Select the authored primary visual first, then retain all
                # relevant article images carrying the same grounded claim.
                matches.sort(key=lambda page: 0 if str(page.get("asset_id") or "") == asset_id else 1)
                for match in matches:
                    match["bound_beat_id"] = str(beat.get("beat_id") or "")
                    match["bound_claim_ids"] = sorted(beat_claims.intersection(str(value) for value in match.get("evidence_claim_ids") or []))
                    match["timing_policy"] = "claim-matched-visual-beat"
                    selected.append(match)
                    used_pages.add(str(match.get("asset_id") or ""))
        elif pages:
            selected = pages[:1]
        if selected:
            selected_stories += 1
            for sequence, asset in enumerate(selected, 1):
                asset["presentation_role"] = "source_visual"
                asset["visual_sequence"] = sequence
                if plan_version == "4.0":
                    trigger_ids = [str(value) for value in (beats[-1].get("card_ids") if beats else []) or []]
                    asset["show_on_card_ids"] = trigger_ids
                    asset["timing_policy"] = "authored-card-cue"
                    asset["duration_seconds"] = float(asset.get("duration_seconds") or 4.5)
                elif not semantic_timing:
                    read_seconds = _card_read_seconds(segment)
                    asset["card_read_seconds"] = read_seconds
                    asset["transition_seconds"] = SOURCE_VISUAL_TRANSITION_SECONDS
                    asset["duration_seconds"] = SOURCE_VISUAL_DURATION_SECONDS
                asset["selection_reason"] = "source-kind-policy_then_claim-matched_beat"
            total_pages += len(selected)
        visual_plan["screenshots"] = selected
        visual_plan["source_visual_status"] = "selected" if selected else "not_selected"
        segment["visual_plan"] = visual_plan
        if selected and not semantic_timing:
            floor = _card_read_seconds(segment) + SOURCE_VISUAL_TRANSITION_SECONDS + SOURCE_VISUAL_DURATION_SECONDS
            segment["minimum_duration_seconds"] = max(float(segment.get("minimum_duration_seconds") or 0.0), floor)
    summary = {
        "mode": mode,
        "status": manifest.get("status"),
        "page_duration_seconds": manifest.get("page_duration_seconds", DEFAULT_PAGE_DURATION_SECONDS),
        "page_lead_seconds": manifest.get("page_lead_seconds", SCREENSHOT_LEAD_SECONDS),
        "total_pages": total_pages,
        "selected_stories": selected_stories,
        "max_video_visual_stories": None,
        "items": list(manifest.get("items") or []),
    }
    result["screenshot"] = summary
    result["source_visual"] = summary
    return result


# Public generalized names; the screenshot names remain as compatibility
# aliases for existing callers and dated manifests.
prepare_source_visual_requests = prepare_screenshot_requests
collect_source_visuals = collect_screenshots
attach_source_visuals_to_script = attach_screenshots_to_script


__all__ = [
    "DEFAULT_PAGE_DURATION_SECONDS",
    "SCREENSHOT_LEAD_SECONDS",
    "SCREENSHOT_MODES",
    "SOURCE_VISUAL_MODES",
    "CAPTURE_CONTRACT_ID",
    "CAPTURE_METHOD",
    "CAPTURE_STRATEGY",
    "CAPTURE_EXECUTOR",
    "CAPTURE_CAPABILITIES",
    "SOURCE_VISUAL_PREFLIGHT_VERSION",
    "ScreenshotError",
    "ScreenshotPending",
    "attach_screenshots_to_script",
    "collect_screenshots",
    "is_x_url",
    "normalize_mode",
    "prepare_screenshot_requests",
    "source_visual_acceptance",
    "prepare_source_visual_requests",
    "collect_source_visuals",
    "attach_source_visuals_to_script",
    "source_kind",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate optional original-source visuals for an AI morning brief run.")
    parser.add_argument("command", choices=("verify", "mark-ready", "preflight"))
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=SCREENSHOT_MODES, default=None)
    parser.add_argument("--browser-control", choices=("available", "unavailable"))
    parser.add_argument("--file-write", dest="file_write", choices=("available", "unavailable"))
    parser.add_argument("--viewport-1440x900", dest="viewport_1440x900", choices=("available", "unavailable"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_dir = args.run_dir.expanduser().resolve()
    try:
        if args.command == "mark-ready":
            # Validation happens before the marker is written, so a typo or a
            # broken image can never turn into an apparently ready run.
            collect_screenshots(run_dir, mode=args.mode, require_ready=False)
            marker = run_dir / "screenshots" / "READY"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        if args.command == "preflight":
            values = (args.browser_control, args.file_write, args.viewport_1440x900)
            if any(value is None for value in values):
                raise ScreenshotError("preflight requires --browser-control, --file-write and --viewport-1440x900")
            preflight = record_capture_preflight(
                run_dir,
                browser_control=args.browser_control == "available",
                direct_project_file_write=args.file_write == "available",
                expanded_viewport_screenshot=args.viewport_1440x900 == "available",
            )
            print(json.dumps(preflight, ensure_ascii=False))
            return 0 if preflight["status"] == "pass" else 2
        manifest = collect_screenshots(run_dir, mode=args.mode, require_ready=True)
    except ScreenshotPending as exc:
        print(json.dumps({"status": "awaiting_capture", "error": str(exc), "details": exc.details}, ensure_ascii=False))
        return 2
    except ScreenshotError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": manifest.get("status"), "run_dir": str(run_dir), "total_pages": manifest.get("total_pages", 0)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
