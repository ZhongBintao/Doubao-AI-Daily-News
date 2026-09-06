from __future__ import annotations

"""Deterministic contract fingerprint for dated AI Daily News builds.

The dated output directory is intentionally reusable, but only when the
runtime and plugin contract that produced it are identical to the current
one.  This module keeps that decision local and auditable instead of relying
on a caller remembering to pass ``--force`` after every implementation fix.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .config import OVERVIEW_PAGE_DURATION_SECONDS, REPO_ROOT
from .screenshots import (
    CAPTURE_CONTRACT_ID,
    CAPTURE_METHOD,
    CAPTURE_STRATEGY,
    EXPANDED_VIEWPORT_HEIGHT,
    EXPANDED_VIEWPORT_WIDTH,
    SOURCE_VISUAL_MANIFEST_VERSION,
    SOURCE_VISUAL_REQUEST_VERSION,
)


BUILD_CONTRACT_VERSION = "1.0"
CONTRACT_FILES = (
    "ai_morning_brief/config.py",
    "ai_morning_brief/aihot.py",
    "ai_morning_brief/selection.py",
    "ai_morning_brief/editorial.py",
    "ai_morning_brief/writing.py",
    "ai_morning_brief/pipeline.py",
    "ai_morning_brief/script.py",
    "ai_morning_brief/materialize.py",
    "ai_morning_brief/screenshots.py",
    "ai_morning_brief/template/index.html",
)


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plugin_metadata() -> dict[str, Any]:
    path = REPO_ROOT / "plugins" / "ai-daily-news-studio" / ".codex-plugin" / "plugin.json"
    if not path.is_file():
        return {"version": None, "sha256": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    return {
        "version": payload.get("version") if isinstance(payload, Mapping) else None,
        "sha256": _sha256(path),
    }


def current_build_contract(*, pipeline_version: str) -> dict[str, Any]:
    """Return the current build contract and its canonical fingerprint."""

    runtime_files = {
        relative: _sha256(REPO_ROOT / relative)
        for relative in CONTRACT_FILES
    }
    payload: dict[str, Any] = {
        "version": BUILD_CONTRACT_VERSION,
        "pipeline_version": str(pipeline_version),
        "overview_page_duration_seconds": float(OVERVIEW_PAGE_DURATION_SECONDS),
        "source_visual": {
            "request_version": SOURCE_VISUAL_REQUEST_VERSION,
            "manifest_version": SOURCE_VISUAL_MANIFEST_VERSION,
            "capture_contract_id": CAPTURE_CONTRACT_ID,
            "capture_strategy": CAPTURE_STRATEGY,
            "capture_method": CAPTURE_METHOD,
            "expanded_viewport": {
                "width": EXPANDED_VIEWPORT_WIDTH,
                "height": EXPANDED_VIEWPORT_HEIGHT,
            },
        },
        "plugin": _plugin_metadata(),
        "runtime_files": runtime_files,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "version": BUILD_CONTRACT_VERSION,
        "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "payload": payload,
    }
