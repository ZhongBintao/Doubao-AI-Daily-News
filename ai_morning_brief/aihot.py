from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

from .config import DEFAULT_AIHOT_SKILL_VERSION, DEFAULT_AIHOT_URL
from .models import SourceItem, SourceValidationError


class AIHotError(RuntimeError):
    """A safe, user-facing AIHOT retrieval or contract error."""


@dataclass(frozen=True)
class AIHotResponse:
    url: str
    payload: dict[str, Any]
    items: tuple[SourceItem, ...]
    etag: str | None
    from_cache: bool
    category_payloads: Mapping[str, Mapping[str, Any]] | None = None


class AIHotClient:
    """Anonymous read-only client for the public AIHOT v1 items endpoint."""

    def __init__(self, *, base_url: str = DEFAULT_AIHOT_URL, cache_dir: Path | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache_dir = cache_dir
        actor_id = ""
        actor_path = Path.home() / ".agents" / "skills" / "aihot" / ".aihot-actor-id"
        if actor_path.is_file():
            try:
                candidate = actor_path.read_text(encoding="utf-8").strip()
                UUID(candidate, version=4)
                actor_id = candidate
            except (OSError, ValueError):
                actor_id = ""
        suffix = f" aihot-actor/{actor_id}" if actor_id else ""
        self.user_agent = f"aihot-skill/{DEFAULT_AIHOT_SKILL_VERSION} (+https://aihot.virxact.com/aihot-skill/)" + suffix

    def fetch_selected_24h(self, *, limit: int = 20, force: bool = False) -> AIHotResponse:
        if not 1 <= limit <= 100:
            raise ValueError("AIHOT limit must be between 1 and 100")
        payload, url, etag, from_cache = self._fetch_page(
            {"mode": "selected", "window": "24h", "by": "timeline", "limit": str(limit)},
            force=force,
        )
        items = self._parse_items(payload)
        return AIHotResponse(url=url, payload=payload, items=items, etag=etag, from_cache=from_cache)

    def fetch_selected_24h_by_category(
        self,
        categories: tuple[str, ...],
        *,
        limit: int = 50,
        force: bool = False,
    ) -> AIHotResponse:
        """Fetch every page of the selected rolling-24h pool per category."""

        if not categories:
            raise ValueError("AIHOT category list cannot be empty")
        if not 1 <= limit <= 100:
            raise ValueError("AIHOT category page size must be between 1 and 100")
        category_payloads: dict[str, dict[str, Any]] = {}
        all_items: list[dict[str, Any]] = []
        etags: list[str] = []
        cache_hits = True
        for category in categories:
            cursor: str | None = None
            seen_cursors: set[str] = set()
            pages: list[dict[str, Any]] = []
            while True:
                params: dict[str, str] = {
                    "mode": "selected",
                    "window": "24h",
                    "by": "timeline",
                    "category": category,
                    "limit": str(limit),
                }
                if cursor:
                    params["cursor"] = cursor
                payload, _url, etag, from_cache = self._fetch_page(params, force=force if not cursor else False)
                cache_hits = cache_hits and from_cache
                pages.append(payload)
                if etag:
                    etags.append(etag)
                page_items = payload.get("items")
                if isinstance(page_items, list):
                    all_items.extend(item for item in page_items if isinstance(item, Mapping))
                page = payload.get("page")
                if not isinstance(page, Mapping) or not page.get("hasMore"):
                    break
                next_cursor = page.get("nextCursor")
                if not isinstance(next_cursor, str) or not next_cursor:
                    raise AIHotError(f"AIHOT category {category!r} reported hasMore without nextCursor")
                if next_cursor in seen_cursors:
                    raise AIHotError(f"AIHOT category {category!r} returned a repeated cursor")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            merged = dict(pages[0]) if pages else {}
            merged["items"] = [item for page in pages for item in page.get("items", []) if isinstance(item, Mapping)]
            merged["pages"] = pages
            merged["page"] = {"count": len(merged["items"]), "hasMore": False, "nextCursor": None}
            category_payloads[category] = merged

        unique_raw: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in all_items:
            item_id = str(item.get("id") or "")
            if item_id and item_id not in seen:
                seen.add(item_id)
                unique_raw.append(dict(item))
        aggregate_query = {
            "mode": "selected",
            "window": "24h",
            "by": "timeline",
            "categories": list(categories),
            "ordering": "timelineDesc",
        }
        aggregate_payload: dict[str, Any] = {
            "schemaVersion": 1,
            "query": aggregate_query,
            "items": unique_raw,
            "page": {"count": len(unique_raw), "hasMore": False, "nextCursor": None},
            "categoryResponses": category_payloads,
        }
        return AIHotResponse(
            url=f"{self.base_url}?{urllib.parse.urlencode({'mode': 'selected', 'window': '24h', 'by': 'timeline', 'categories': ','.join(categories)})}",
            payload=aggregate_payload,
            items=self._parse_items(aggregate_payload),
            etag=','.join(etags) if etags else None,
            from_cache=cache_hits,
            category_payloads=category_payloads,
        )

    def _fetch_page(
        self,
        params: Mapping[str, str],
        *,
        force: bool,
    ) -> tuple[dict[str, Any], str, str | None, bool]:
        query = urllib.parse.urlencode(params)
        url = f"{self.base_url}?{query}"
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        body_path = self.cache_dir / f"{key}.json" if self.cache_dir else None
        etag_path = self.cache_dir / f"{key}.etag" if self.cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        old_etag = etag_path.read_text(encoding="utf-8").strip() if etag_path and etag_path.is_file() else ""
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        if old_etag and not force:
            headers["If-None-Match"] = old_etag
        request = urllib.request.Request(url, headers=headers, method="GET")
        from_cache = False
        etag = old_etag or None
        raw_payload: bytes
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw_payload = response.read()
                etag = response.headers.get("ETag") or etag
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and body_path and body_path.is_file():
                raw_payload = body_path.read_bytes()
                from_cache = True
            else:
                detail = ""
                try:
                    detail = exc.read(300).decode("utf-8", errors="replace")
                except Exception:
                    pass
                raise AIHotError(f"AIHOT request failed with HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise AIHotError(f"AIHOT request failed: {exc}") from exc

        try:
            payload = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AIHotError("AIHOT returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise AIHotError("AIHOT returned a non-object response")
        query_shape = payload.get("query")
        if not isinstance(query_shape, Mapping) or query_shape.get("mode") != "selected" or query_shape.get("window") != "24h":
            raise AIHotError("AIHOT response did not match the selected 24h contract")
        self._parse_items(payload)
        if body_path and not from_cache:
            body_path.write_bytes(raw_payload)
            if etag_path and etag:
                etag_path.write_text(etag, encoding="utf-8")
        return payload, url, etag, from_cache

    @staticmethod
    def _parse_items(payload: Mapping[str, Any]) -> tuple[SourceItem, ...]:
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise AIHotError("AIHOT response has no items list")
        try:
            return tuple(SourceItem.from_mapping(item) for item in raw_items if isinstance(item, Mapping))
        except SourceValidationError as exc:
            raise AIHotError(str(exc)) from exc


def load_fixture(path: Path) -> AIHotResponse:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AIHotError(f"fixture is not an object: {path}")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise AIHotError(f"fixture has no items list: {path}")
    try:
        items = tuple(SourceItem.from_mapping(item) for item in raw_items if isinstance(item, Mapping))
    except SourceValidationError as exc:
        raise AIHotError(str(exc)) from exc
    return AIHotResponse(
        url=f"fixture://{path.name}",
        payload=payload,
        items=items,
        etag=None,
        from_cache=True,
    )
