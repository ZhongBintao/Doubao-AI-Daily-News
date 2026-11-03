"""Read-only client for the AIHOT 2.0 JSON API (https://aihot.news/api/v1).

The v6 newsroom uses only AIHOT's own data (never the original article links):

* ``/items``            selected (and optionally all) items with AIHOT scores;
                        ``q=`` keyword search finds more AIHOT coverage of an event
* ``/hot-topics``       current multi-source events (real-time only, no history)
* ``/stories/{id}``     one event's AI digest, per-source reports and related events
* ``/dailies/{date}``   the AIHOT daily report (published 08:00 Asia/Shanghai)

Responses are treated as untrusted data. Every request is a GET, honours
``Retry-After`` on 429/503, retries a 5xx once, and caches bodies by URL with
ETag revalidation. ``fixture_dir`` replaces the network with JSON files for
offline tests (see ``ai_morning_brief/fixtures/aihot_v2``).
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from .config import AIHOT_API_BASE, AIHOT_API_USER_AGENT


class AIHotV2Error(RuntimeError):
    """A safe, user-facing AIHOT retrieval error."""


class AIHotV2Client:
    def __init__(
        self,
        *,
        base_url: str = AIHOT_API_BASE,
        cache_dir: Path | None = None,
        fixture_dir: Path | None = None,
        user_agent: str = AIHOT_API_USER_AGENT,
        min_interval: float = 1.1,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache_dir = cache_dir
        self.fixture_dir = fixture_dir
        self.user_agent = user_agent
        self.min_interval = min_interval
        self._last_request = 0.0
        self.requests: list[dict[str, Any]] = []

    # -- public endpoints -------------------------------------------------
    def items(self, *, mode: str = "selected", window: str = "24h", limit: int = 100, max_pages: int = 20) -> list[dict[str, Any]]:
        """Return every item of a rolling window, following cursors."""

        if mode not in {"selected", "all"} or window not in {"24h", "7d"}:
            raise ValueError("unsupported AIHOT items query")
        collected: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        cursor: str | None = None
        for _ in range(max_pages):
            params = {"mode": mode, "window": window, "by": "timeline", "limit": str(limit)}
            if cursor:
                params["cursor"] = cursor
            payload = self._get("items", params)
            for item in payload.get("items") or []:
                if isinstance(item, Mapping) and str(item.get("id") or "") not in seen_ids:
                    seen_ids.add(str(item.get("id")))
                    collected.append(dict(item))
            page = payload.get("page") or {}
            if not page.get("hasMore"):
                break
            cursor = str(page.get("nextCursor") or "")
            if not cursor:
                raise AIHotV2Error("AIHOT reported hasMore without nextCursor")
        return collected

    def search(self, query: str, *, window: str = "7d", limit: int = 20) -> list[dict[str, Any]]:
        """Keyword search over every public AIHOT item (selected or not) of a window.

        Used only to gather more AIHOT coverage of an event already in the
        candidate pool; a missing fixture or a 404 yields an empty list.
        """

        query = " ".join(str(query or "").split())[:200]
        if len(query) < 2 or window not in {"24h", "7d"}:
            return []
        payload = self._get("items", {"mode": "all", "window": window, "by": "timeline", "q": query, "limit": str(max(1, min(limit, 100)))},
                            allow_missing=True)
        return [dict(item) for item in (payload or {}).get("items") or [] if isinstance(item, Mapping)]

    def hot_topics(self) -> list[dict[str, Any]]:
        payload = self._get("hot-topics", {})
        return [dict(item) for item in payload.get("items") or [] if isinstance(item, Mapping)]

    def story(self, public_id: str) -> dict[str, Any] | None:
        if not public_id:
            return None
        payload = self._get(f"stories/{urllib.parse.quote(public_id)}", {}, allow_missing=True)
        story = (payload or {}).get("story")
        return dict(story) if isinstance(story, Mapping) else None

    def daily(self, date_text: str) -> dict[str, Any] | None:
        """Return the daily report for a Shanghai date, or None if not published yet."""

        payload = self._get(f"dailies/{date_text}", {}, allow_missing=True)
        report = (payload or {}).get("report")
        return dict(report) if isinstance(report, Mapping) else None

    # -- transport --------------------------------------------------------
    def _fixture(self, path: str, params: Mapping[str, str]) -> dict[str, Any] | None:
        assert self.fixture_dir is not None
        name = path.replace("/", "_")
        if path == "items" and params.get("q"):
            slug = hashlib.sha256(str(params["q"]).encode("utf-8")).hexdigest()[:12]
            name = f"items_search_{slug}"
        elif path == "items":
            name = f"items_{params.get('mode')}_{params.get('window')}"
        candidate = self.fixture_dir / f"{name}.json"
        if not candidate.is_file():
            return None
        return json.loads(candidate.read_text(encoding="utf-8"))

    def _get(self, path: str, params: Mapping[str, str], *, allow_missing: bool = False) -> dict[str, Any] | None:
        if self.fixture_dir is not None:
            data = self._fixture(path, params)
            if data is None and not allow_missing:
                raise AIHotV2Error(f"fixture missing for {path}")
            self.requests.append({"path": path, "params": dict(params), "fixture": True, "found": data is not None})
            return data
        query = urllib.parse.urlencode(params)
        url = f"{self.base_url}/{path}" + (f"?{query}" if query else "")
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        body_path = self.cache_dir / f"{key}.json" if self.cache_dir else None
        etag_path = self.cache_dir / f"{key}.etag" if self.cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        headers = {"Accept": "application/json", "User-Agent": self.user_agent, "Accept-Encoding": "identity"}
        if etag_path and etag_path.is_file() and body_path and body_path.is_file():
            headers["If-None-Match"] = etag_path.read_text(encoding="utf-8").strip()
        attempts = 0
        while True:
            attempts += 1
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read()
                    etag = response.headers.get("ETag")
                self.requests.append({"url": url, "status": 200})
                if body_path:
                    body_path.write_bytes(raw)
                    if etag and etag_path:
                        etag_path.write_text(etag, encoding="utf-8")
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 304 and body_path and body_path.is_file():
                    raw = body_path.read_bytes()
                    self.requests.append({"url": url, "status": 304})
                    break
                if exc.code == 404 and allow_missing:
                    self.requests.append({"url": url, "status": 404})
                    return None
                if exc.code in {429, 503} and attempts <= 3:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    time.sleep(min(60.0, float(retry_after)) if retry_after and retry_after.isdigit() else 5.0)
                    continue
                if 500 <= exc.code < 600 and attempts <= 1:
                    time.sleep(3.0)
                    continue
                raise AIHotV2Error(f"AIHOT {path} failed with HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempts <= 1:
                    time.sleep(3.0)
                    continue
                raise AIHotV2Error(f"AIHOT {path} unreachable: {exc}") from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AIHotV2Error(f"AIHOT {path} returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise AIHotV2Error(f"AIHOT {path} returned a non-object response")
        return data
