from __future__ import annotations

"""Freeze optional detail fetched from each public original article.

AIHOT's selected-feed contract intentionally returns a compact editorial
summary.  This module keeps the feed snapshot authoritative while allowing the
workflow to attach a separately audited, public-page detail snapshot.  The
browser skill writes the snapshot; the renderer only consumes this validated
JSON and never crawls a page itself.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


SOURCE_DETAIL_VERSION = "1.0"


class SourceDetailError(ValueError):
    """Raised when a public-source detail snapshot is unsafe to merge."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def document_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u3000", " ").strip())


def _text_list(value: Any, *, field: str, item_id: str, maximum: int = 120) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SourceDetailError(f"source detail {item_id} field {field} must be a list")
    if len(value) > maximum:
        raise SourceDetailError(f"source detail {item_id} field {field} is too large")
    result = [_clean(part) for part in value if _clean(part)]
    if any("<script" in part.casefold() or "javascript:" in part.casefold() for part in result):
        raise SourceDetailError(f"source detail {item_id} contains executable markup")
    return result


def _media_list(value: Any, *, item_id: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SourceDetailError(f"source detail {item_id} media must be a list")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value[:32]):
        if not isinstance(raw, Mapping):
            raise SourceDetailError(f"source detail {item_id} media[{index}] must be an object")
        url = _clean(raw.get("url"))
        local_path = _clean(raw.get("local_path"))
        if url and not re.match(r"^https?://", url, re.IGNORECASE):
            raise SourceDetailError(f"source detail {item_id} media[{index}] url must be http(s)")
        if local_path.startswith("/") or ".." in Path(local_path).parts:
            raise SourceDetailError(f"source detail {item_id} media[{index}] path must stay in the run directory")
        result.append({
            "url": url or None,
            "local_path": local_path or None,
            "alt": _clean(raw.get("alt")) or None,
            "caption": _clean(raw.get("caption")) or None,
            "sha256": _clean(raw.get("sha256")) or None,
            "width": raw.get("width"),
            "height": raw.get("height"),
        })
    return result


def _normalise_item(raw: Mapping[str, Any], selected: Mapping[str, Any]) -> dict[str, Any]:
    item_id = _clean(raw.get("item_id") or raw.get("id"))
    if not item_id:
        raise SourceDetailError("source detail item is missing item_id")
    selected_id = _clean(selected.get("id"))
    if item_id != selected_id:
        raise SourceDetailError(f"source detail item {item_id} is not bound to selected item {selected_id}")
    source_url = _clean(raw.get("source_url") or raw.get("url"))
    selected_url = _clean((selected.get("links") or {}).get("original"))
    if not source_url or source_url != selected_url:
        raise SourceDetailError(f"source detail item {item_id} source_url does not match frozen original link")
    status = _clean(raw.get("status") or "available").lower()
    if status not in {"available", "unavailable"}:
        raise SourceDetailError(f"source detail item {item_id} has unsupported status {status!r}")
    paragraphs = _text_list(raw.get("paragraphs"), field="paragraphs", item_id=item_id)
    captions = _text_list(raw.get("captions"), field="captions", item_id=item_id)
    if status == "available" and not paragraphs and not captions and not _clean(raw.get("title")):
        raise SourceDetailError(f"source detail item {item_id} is available but has no readable detail")
    return {
        "item_id": item_id,
        "source_url": source_url,
        "observed_url": _clean(raw.get("observed_url")) or source_url,
        "status": status,
        "title": _clean(raw.get("title")) or None,
        "paragraphs": paragraphs,
        "captions": captions,
        "media": _media_list(raw.get("media"), item_id=item_id),
        "observed_at": _clean(raw.get("observed_at")) or None,
        "content_sha256": _clean(raw.get("content_sha256")) or None,
    }


def validate_source_detail_snapshot(snapshot: Mapping[str, Any], editorial_input: Mapping[str, Any]) -> tuple[list[str], dict[str, dict[str, Any]]]:
    errors: list[str] = []
    if snapshot.get("version") != SOURCE_DETAIL_VERSION:
        errors.append(f"unsupported source detail snapshot version: {snapshot.get('version')!r}")
    selected = {
        _clean(item.get("id")): item
        for item in editorial_input.get("items") or []
        if isinstance(item, Mapping) and _clean(item.get("id"))
    }
    selected_ids = [str(value) for value in (editorial_input.get("selection") or {}).get("item_ids") or []]
    raw_items = snapshot.get("items")
    if not isinstance(raw_items, list):
        return errors + ["source detail snapshot has no items list"], {}
    details: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, Mapping):
            errors.append(f"source detail items[{index}] is not an object")
            continue
        item_id = _clean(raw.get("item_id") or raw.get("id"))
        try:
            if item_id not in selected:
                raise SourceDetailError(f"source detail item {item_id!r} is not selected")
            details[item_id] = _normalise_item(raw, selected[item_id])
        except SourceDetailError as exc:
            errors.append(str(exc))
    duplicate_ids = len(details) != len({str(raw.get("item_id") or raw.get("id")) for raw in raw_items if isinstance(raw, Mapping)})
    if duplicate_ids:
        errors.append("source detail snapshot repeats an item_id")
    missing = sorted(set(selected_ids) - set(details))
    if missing:
        errors.append("source detail snapshot is incomplete: " + ", ".join(missing))
    return errors, details


def merge_source_detail_snapshot(editorial_input: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return a new frozen editorial input with validated public detail."""

    errors, details = validate_source_detail_snapshot(snapshot, editorial_input)
    if errors:
        raise SourceDetailError("; ".join(errors[:8]))
    result = json.loads(json.dumps(editorial_input, ensure_ascii=False))
    result.pop("input_sha256", None)
    result["source_details"] = {
        "version": SOURCE_DETAIL_VERSION,
        "snapshot_sha256": document_sha256(snapshot),
        "items": [details[item_id] for item_id in (result.get("selection") or {}).get("item_ids") or []],
    }
    result["input_sha256"] = document_sha256(result)
    return result


def load_source_detail_snapshot(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceDetailError(f"could not read source detail snapshot: {path}") from exc
    if not isinstance(value, dict):
        raise SourceDetailError("source detail snapshot must be a JSON object")
    return value


def detail_text_for_item(editorial_input: Mapping[str, Any], item_id: str) -> str:
    details = (editorial_input.get("source_details") or {}).get("items") or []
    for detail in details:
        if isinstance(detail, Mapping) and str(detail.get("item_id")) == str(item_id):
            parts = [detail.get("title") or "", *(detail.get("paragraphs") or []), *(detail.get("captions") or [])]
            return "\n".join(_clean(part) for part in parts if _clean(part))
    return ""
