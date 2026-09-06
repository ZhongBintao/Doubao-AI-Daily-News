from __future__ import annotations

import html
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Mapping

from .media import write_json
from .config import CATEGORY_LABELS, CATEGORY_ORDER, OVERVIEW_PAGE_DURATION_SECONDS


TEMPLATE_PATH = Path(__file__).resolve().parent / "template" / "index.html"

# Keep product/code identifiers atomic (for example ``Q4_K_M``). Salient
# number-plus-unit phrases are wrapped as a whole when the editorial plan
# supplies the inline ``metric`` hint below.
_HIGHLIGHT_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9_.+#/-]*|"
    r"(?<![A-Za-z])\d+(?:[.,，]\d+)?"
    r"(?:[A-Za-z][A-Za-z0-9_.+#/-]*|\s+[A-Za-z][A-Za-z0-9_.+#/-]*)?%?"
)


def _esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _safe_id(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "-" for char in value)


def _highlight_generic(text: str) -> str:
    """Escape text while highlighting atomic product/code/data tokens."""

    parts: list[str] = []
    cursor = 0
    for match in _HIGHLIGHT_RE.finditer(text):
        parts.append(_esc(text[cursor:match.start()]))
        parts.append(f'<mark class="token-highlight" data-layout-allow-overlap="true">{_esc(match.group(0))}</mark>')
        cursor = match.end()
    parts.append(_esc(text[cursor:]))
    return "".join(parts)


def _find_inline_span(text: str, needle: str) -> re.Match[str] | None:
    """Find a case-insensitive metric while allowing editorial whitespace."""

    needle = str(needle or "").strip()
    if not needle:
        return None
    pattern = "".join(r"\s*" if char.isspace() else re.escape(char) for char in needle)
    return re.search(pattern, text, flags=re.IGNORECASE)


def _highlight_text(value: Any, *, feature: str | None = None) -> str:
    """Highlight one optional feature phrase exactly once, then other tokens."""

    text = str(value or "")
    match = _find_inline_span(text, feature or "") if feature else None
    if match is None:
        return _highlight_generic(text)
    highlighted = f'<mark class="token-highlight token-feature" data-layout-allow-overlap="true">{_esc(match.group(0))}</mark>'
    return _highlight_generic(text[:match.start()]) + highlighted + _highlight_generic(text[match.end():])


def _normalise_inline_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _point_heading(point: str, *, index: int, category: str) -> str:
    if re.search(r"\d|%|价格|投资|参数|版本", point):
        return "关键数据"
    if re.search(r"研究|论文|评测|方法", point):
        return "研究方法"
    if re.search(r"调试|调用|部署|工具|开发", point):
        return "开发实践"
    if re.search(r"开放|上线|发布|推出|推出|新增", point):
        return "核心进展"
    category_headings = {
        "ai-models": "模型能力",
        "ai-products": "产品进展",
        "industry": "行业观察",
        "paper": "技术洞察",
        "tip": "实践建议",
        "other": "后续动态",
    }
    return category_headings.get(category, ("重点信息", "补充信息")[index % 2])


def _icon_svg(kind: str) -> str:
    icons = {
        "spark": '<svg viewBox="0 0 48 48" aria-hidden="true"><path d="M24 4l3.6 13.2L41 21l-13.4 3.8L24 38l-3.6-13.2L7 21l13.4-3.8L24 4Z"/><path d="M39 32l1.6 5.4L46 39l-5.4 1.6L39 46l-1.6-5.4L32 39l5.4-1.6L39 32Z"/></svg>',
        "bars": '<svg viewBox="0 0 48 48" aria-hidden="true"><path d="M9 39V25h7v14H9Zm12 0V12h7v27h-7Zm12 0V6h7v33h-7Z"/></svg>',
        "chart": '<svg viewBox="0 0 48 48" aria-hidden="true"><rect x="7" y="7" width="34" height="34" rx="3"/><path d="M14 33v-8M23 33V15M32 33v-13M13 19h9M22 24h10"/></svg>',
        "clock": '<svg viewBox="0 0 48 48" aria-hidden="true"><circle cx="24" cy="25" r="14"/><path d="M24 17v9l6 4M18 7h12M24 7v4"/></svg>',
        "code": '<svg viewBox="0 0 48 48" aria-hidden="true"><path d="m19 14-10 11 10 11M29 14l10 11-10 11M27 9l-6 32"/></svg>',
    }
    return icons.get(kind, icons["spark"])


def _point_icon(point: str, index: int) -> str:
    if re.search(r"\d|%|价格|投资|参数|版本", point):
        return "bars"
    if re.search(r"研究|论文|评测|方法", point):
        return "chart"
    if re.search(r"日期|月|日|截止|时间|生效", point):
        return "clock"
    if re.search(r"开发|代码|工具|部署|接口", point):
        return "code"
    return ("spark", "chart", "code")[index % 3]


def _points_markup(points: list[str], *, category: str, plan: Mapping[str, Any] | None = None) -> str:
    colors = ("terracotta", "amber", "teal", "red", "ochre")
    plan = plan if isinstance(plan, Mapping) else {}
    variant = str(plan.get("variant") or "headline-stack")
    density = str(plan.get("density") or "regular")
    authored_cards = [card for card in plan.get("cards", []) if isinstance(card, Mapping)]
    if not points and not authored_cards:
        return '<div class="point-grid"><div class="point-card empty"><p>重点信息正在播报</p></div></div>'
    roles = {int(card.get("index")): str(card.get("role") or "support") for card in authored_cards if str(card.get("index", "")).isdigit()}
    visible_cards = authored_cards if authored_cards else [{"body": point, "role": roles.get(index, "support")} for index, point in enumerate(points)]
    page_markup: list[str] = []
    page_size = 4 if str(plan.get("story_kind") or "single") == "brief_group" else 5
    pages = [visible_cards[index:index + page_size] for index in range(0, len(visible_cards), page_size)]
    for page_index, page_cards in enumerate(pages):
        cards: list[str] = []
        for local_index, card in enumerate(page_cards):
            index = page_index * page_size + local_index
            raw_point = str(card.get("body") or card.get("text") or (points[index] if index < len(points) else ""))
            role = str(card.get("role") or roles.get(index, "support"))
            card_id = str(card.get("id") or f"card-{index + 1:02d}")
            subject = str(card.get("subject") or "").strip()
            icon = _icon_svg(_point_icon(raw_point, index))
            label = _esc(card.get("label") or _point_heading(raw_point, index=index, category=category))
            headline = _esc(card.get("headline") or "")
            metric_text = str(card.get("metric") or "").strip()
            metric = metric_text if metric_text and _normalise_inline_text(metric_text) in _normalise_inline_text(raw_point) else None
            span = card.get("span", 2 if len(page_cards) >= 3 else 3)
            try:
                span = max(1, min(6, int(span)))
            except (TypeError, ValueError):
                span = 2
            headline_markup = f'<h3>{headline}</h3>' if headline else ""
            subject_markup = f'<div class="point-subject" data-layout-allow-overlap="true">{_esc(subject)}</div>' if subject else ""
            headline_markup = f'<h3 data-layout-allow-overlap="true">{headline}</h3>' if headline else ""
            cards.append(
                f'<article class="point-card {colors[index % len(colors)]} role-{_safe_id(role)} span-{span}" data-card-index="{index}" data-card-id="{_esc(card_id)}"><div class="point-icon">{icon}</div>'
                f'<div class="point-copy">{subject_markup}<div class="point-label" data-layout-allow-overlap="true">{label}</div>{headline_markup}<p data-layout-allow-overlap="true">{_highlight_text(raw_point, feature=metric)}</p></div></article>'
            )
        page_markup.append(
            f'<div class="story-card-page" data-card-page-index="{page_index}" aria-hidden="{"false" if page_index == 0 else "true"}">'
            f'<div class="point-grid count-{len(page_cards)} variant-{_safe_id(variant)} density-{_safe_id(density)}">{"".join(cards)}</div></div>'
        )
    return f'<div class="story-card-pages" data-card-page-count="{len(pages)}">{"".join(page_markup)}</div>'


def _prepare_screenshot_assets(project_dir: Path, segments: list[Mapping[str, Any]], workspace: Path) -> dict[str, list[dict[str, Any]]]:
    """Copy validated source visuals into the self-contained workspace."""

    project_root = project_dir.resolve()
    destination_root = workspace / "assets" / "source-visuals"
    copied: dict[str, list[dict[str, Any]]] = {}
    for segment in segments:
        segment_id = str(segment.get("id") or "")
        visual_plan = segment.get("visual_plan") if isinstance(segment.get("visual_plan"), Mapping) else {}
        pages = visual_plan.get("screenshots") if isinstance(visual_plan, Mapping) else []
        if not isinstance(pages, list) or not pages:
            continue
        destination_pages: list[dict[str, Any]] = []
        for index, page in enumerate(pages, 1):
            if not isinstance(page, Mapping) or not page.get("path"):
                raise ValueError(f"segment {segment_id} has a malformed screenshot page")
            relative = Path(str(page["path"]))
            if relative.is_absolute():
                raise ValueError(f"screenshot path must be relative: {relative}")
            source = (project_dir / relative).resolve()
            if source != project_root and project_root not in source.parents:
                raise ValueError(f"screenshot path escapes the run directory: {relative}")
            if not source.is_file() or source.stat().st_size == 0:
                raise ValueError(f"validated screenshot is missing: {relative}")
            # New schema-5 pages carry a hash for the exact presentation file.
            # Historical fixtures may use placeholder source hashes, so keep
            # their read-only compatibility path unchanged.
            expected_sha256 = str(page.get("presentation_sha256") or "")
            if expected_sha256 and hashlib.sha256(source.read_bytes()).hexdigest() != expected_sha256:
                raise ValueError(f"validated screenshot hash does not match manifest: {relative}")
            item_slug = _safe_id(str(page.get("item_id") or segment_id))
            suffix = source.suffix.lower() or ".png"
            filename = f"source-{item_slug}-visual-{index:02d}{suffix}"
            destination = destination_root / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if expected_sha256 and hashlib.sha256(destination.read_bytes()).hexdigest() != expected_sha256:
                raise ValueError(f"materialized screenshot hash does not match manifest: {relative}")
            destination_pages.append({
                **dict(page),
                "asset_path": f"assets/source-visuals/{filename}",
                "page": index,
                "duration_seconds": max(0.5, float(page.get("duration_seconds") or 4.5)),
            })
        copied[segment_id] = destination_pages
    return copied


def _screenshot_markup(pages: list[Mapping[str, Any]]) -> str:
    if not pages:
        return ""
    figures: list[str] = []
    for index, page in enumerate(pages, 1):
        asset_path = str(page.get("asset_path") or "")
        alt = str(page.get("alt") or "原文关键视觉")
        asset_id = str(page.get("asset_id") or f"source-visual-{index:02d}")
        media_type = str(page.get("media_type") or "image")
        media = (
            f'<video src="{_esc(asset_path)}" aria-label="{_esc(alt)}" muted playsinline preload="auto"></video>'
            if media_type == "video"
            else f'<img src="{_esc(asset_path)}" alt="{_esc(alt)}">'
        )
        figures.append(
            f'<figure class="source-visual-item" data-asset-id="{_esc(asset_id)}" data-media-type="{_esc(media_type)}">{media}</figure>'
        )
    return f'<div class="source-visual-layer" data-source-visual-count="{len(pages)}">{"".join(figures)}</div>'


def _overview_grid_markup(groups: list[Mapping[str, Any]]) -> str:
    cards: list[str] = []
    accents = ("terracotta", "amber", "teal", "red", "ochre")
    for index, group in enumerate(groups):
        category = str(group.get("category") or "other")
        label = str(group.get("label") or CATEGORY_LABELS.get(category, category))
        items = list(group.get("items") or [])
        bullets = "".join(
            f'<li data-item-id="{_esc(item.get("item_id"))}"><strong data-layout-allow-overlap="true">{_esc(item.get("title"))}</strong><span data-layout-allow-overlap="true">{_esc(item.get("text") or item.get("title"))}</span></li>'
            for item in items if isinstance(item, Mapping)
        )
        cards.append(
            f'<article class="overview-card {accents[index % len(accents)]}"><div class="overview-card-head">'
            f'<div class="overview-icon">{_icon_svg(("spark", "bars", "chart", "clock", "code")[index % 5])}</div>'
            f'<h2 data-layout-allow-overlap="true">{_esc(label)}</h2></div><ul>{bullets}</ul></article>'
        )
    if not cards:
        return '<div class="overview-grid"><article class="overview-card empty"><p data-layout-allow-overlap="true">今日暂无可播报资讯</p></article></div>'
    return f'<div class="overview-grid count-{len(cards)}">' + "".join(cards) + "</div>"


def _overview_markup(pages: list[Mapping[str, Any]], *, total_duration: float | None = None) -> str:
    if not pages:
        return _overview_grid_markup([])
    page_durations = [OVERVIEW_PAGE_DURATION_SECONDS for _ in pages]
    starts_at = 0.0
    markup: list[str] = []
    for index, page in enumerate(pages):
        duration = page_durations[index]
        groups = [group for group in page.get("groups") or [] if isinstance(group, Mapping)]
        markup.append(
            f'<div class="overview-page" data-overview-page="{index}" data-page-start="{starts_at:.3f}" '
            f'data-page-duration="{duration:.3f}" data-layout-basis="{_esc(page.get("layout_basis") or "legacy")}" '
            f'aria-hidden="{"false" if index == 0 else "true"}">{_overview_grid_markup(groups)}</div>'
        )
        starts_at += duration
    return f'<div class="overview-pages" data-overview-page-count="{len(pages)}">{"".join(markup)}</div>'


def _hero_markup(segment: Mapping[str, Any], *, kind: str, segment_id: str) -> str:
    title = segment.get("title") or ("AI每日早报" if kind == "intro" else "明天见")
    return (
        f'<section id="scene-{segment_id}" class="scene hero-scene {kind}-scene" data-kind="{kind}" '
        f'data-category="{kind}"><div class="scene-inner hero-inner"><div class="hero-orbit"></div><div class="hero-content">'
        f'<div class="hero-rule"></div><p class="hero-kicker" data-layout-allow-overlap="true">AI每日编辑部</p><h1 class="hero-title" data-layout-allow-overlap="true">{_esc(title)}</h1>'
        f'</div></div></section>'
    )


def _scene_markup(segment: Mapping[str, Any], *, index: int, start: float, duration: float, screenshot_pages: list[Mapping[str, Any]] | None = None) -> str:
    kind = str(segment.get("kind") or "news")
    segment_id = _safe_id(str(segment.get("id") or f"scene-{index:02d}"))
    if kind == "intro":
        return _hero_markup(segment, kind="intro", segment_id=segment_id)
    if kind == "overview":
        pages = [page for page in (segment.get("screen_pages") or []) if isinstance(page, Mapping)]
        if not pages:
            groups = [group for group in (segment.get("screen_groups") or []) if isinstance(group, Mapping)]
            pages = [{"duration_seconds": duration, "groups": groups}]
        return (
            f'<section id="scene-{segment_id}" class="scene overview-scene" data-kind="overview" data-category="overview">'
            f'<div class="scene-inner overview-inner"><div class="overview-heading"><p class="section-kicker" data-layout-allow-overlap="true">AI DAILY NEWS / 今日编辑室</p>'
            f'<h1 data-layout-allow-overlap="true">{_esc(segment.get("title") or "资讯概览")}</h1></div>'
            f'{_overview_markup(pages, total_duration=duration)}</div></section>'
        )
    if kind == "outro":
        return _hero_markup(segment, kind="outro", segment_id=segment_id)
    category = str(segment.get("category") or "other")
    layout = _esc(segment.get("layout_type") or "headline-stack")
    points = segment.get("screen_points") or []
    screenshots = _screenshot_markup(screenshot_pages or [])
    card_markup = _points_markup([str(point) for point in points], category=category, plan=segment.get("visual_plan"))
    visual_markup = f'<div class="story-card-stage">{card_markup}</div>{screenshots}'
    return (
        f'<section id="scene-{segment_id}" class="scene news-scene layout-{layout}" data-kind="news" '
        f'data-category="{_esc(category)}"><div class="scene-inner detail-inner"><div class="detail-heading"><p class="section-kicker" data-layout-allow-overlap="true">{_esc(CATEGORY_LABELS.get(category, category))} / AI DAILY NEWS</p>'
        f'<h2 data-layout-allow-overlap="true">{_esc(segment.get("title"))}</h2></div><div class="detail-visual">{visual_markup}<div class="signal-sweep"></div></div></div>'
        f'</section>'
    )


def _caption_markup(cues: list[Mapping[str, Any]]) -> str:
    return "".join(
        f'<div class="clip caption" id="caption-{index:04d}" data-start="{float(cue.get("start", 0)):.3f}" '
        f'data-duration="{max(0.12, float(cue.get("end", 0)) - float(cue.get("start", 0))):.3f}" '
        f'data-card-ids="{_esc(",".join(str(value) for value in cue.get("card_ids") or []))}" '
        f'data-beat-id="{_esc(cue.get("beat_id"))}" data-claim-ids="{_esc(",".join(str(value) for value in cue.get("claim_ids") or []))}" '
        f'data-visual-asset-id="{_esc(cue.get("visual_asset_id"))}" '
        f'data-track-index="{200 + index}" data-layout-allow-overlap="true" aria-hidden="true">{_esc(cue.get("text"))}</div>'
        for index, cue in enumerate(cues, 1)
    )


def _progress_label(segment: Mapping[str, Any], *, fallback: str) -> str:
    return str(segment.get("progress_label") or fallback)


def _nav_motion_specs(segments: list[Mapping[str, Any]], timings: Mapping[str, Mapping[str, float]]) -> list[dict[str, Any]]:
    """Describe each scene that owns a top-navigation position."""

    specs: list[dict[str, Any]] = []
    semantic_navigation = any(
        segment.get("kind") == "news" and segment.get("presentation_order") is not None
        for segment in segments
    )
    for segment in segments:
        kind = str(segment.get("kind") or "")
        if kind not in {"intro", "overview", "news", "outro"}:
            continue
        segment_id = str(segment["id"])
        specs.append({
            "id": segment_id,
            "start": float(timings[segment_id]["start"]),
            "nav_key": segment_id if kind == "news" and semantic_navigation else str(segment.get("category") or "other") if kind == "news" else "",
            "visible": kind in {"overview", "news"},
        })
    return specs


def _template_contract(script: Mapping[str, Any], nav_motion_specs: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Return deterministic, render-independent template safety checks."""

    metric_errors: list[str] = []
    card_pagination_errors: list[str] = []
    visual_errors: list[str] = []
    plan_version = str((script.get("editorial") or {}).get("plan_version") or "")
    for segment in script.get("segments", []):
        if segment.get("kind") != "news":
            continue
        for index, card in enumerate(segment.get("cards") or []):
            if not isinstance(card, Mapping) or not card.get("metric"):
                continue
            metric = _normalise_inline_text(card.get("metric"))
            body = _normalise_inline_text(card.get("body"))
            headline = _normalise_inline_text(card.get("headline"))
            label = _normalise_inline_text(card.get("label"))
            if body.count(metric) != 1 or metric in headline or metric in label:
                metric_errors.append(f"{segment.get('id')}.cards[{index}]")
        planned_cards = list((segment.get("visual_plan") or {}).get("cards") or [])
        if planned_cards and len(planned_cards) != len(segment.get("cards") or []):
            card_pagination_errors.append(str(segment.get("id")))
        for page in (segment.get("visual_plan") or {}).get("screenshots") or []:
            if not isinstance(page, Mapping):
                visual_errors.append(f"{segment.get('id')}: malformed visual")
                continue
            # Expanded responsive-viewport captures may be smaller than the
            # 1920x1080 composition.  The template enlarges the complete
            # viewport proportionally; rejecting that here recreates the old
            # tiny-text failure mode.
            presentation_dimensions = page.get("presentation_dimensions")
            if page.get("presentation_sha256") and presentation_dimensions:
                try:
                    if int(presentation_dimensions["width"]) <= 0 or int(presentation_dimensions["height"]) <= 0:
                        visual_errors.append(f"{segment.get('id')}: source visual presentation dimensions are invalid")
                except (KeyError, TypeError, ValueError):
                    visual_errors.append(f"{segment.get('id')}: source visual presentation dimensions are invalid")
            if plan_version == "5.0" and (not page.get("bound_beat_id") or not page.get("bound_claim_ids")):
                visual_errors.append(f"{segment.get('id')}: source visual is not claim-matched to a beat")

    overview_errors: list[str] = []
    overview_segment = next((segment for segment in script.get("segments", []) if segment.get("kind") == "overview"), None)
    if plan_version == "5.0":
        pages = list((overview_segment or {}).get("screen_pages") or [])
        overview_ids = [
            str(item.get("item_id") or "")
            for page in pages if isinstance(page, Mapping)
            for group in page.get("groups") or [] if isinstance(group, Mapping)
            for item in group.get("items") or [] if isinstance(item, Mapping)
        ]
        expected_ids = [str(value) for value in script.get("source_item_ids") or []]
        if sorted(overview_ids) != sorted(expected_ids) or len(overview_ids) != len(set(overview_ids)):
            overview_errors.append("overview must contain every selected item exactly once")
        for page_index, page in enumerate(pages):
            duration = float(page.get("duration_seconds") or 0.0)
            expected_duration = OVERVIEW_PAGE_DURATION_SECONDS
            if abs(duration - expected_duration) > 0.01:
                overview_errors.append(f"overview page {page_index + 1} must be exactly {expected_duration:g} seconds")
            for group in page.get("groups") or []:
                for item in group.get("items") or []:
                    if not str(item.get("text") or "").strip() or not item.get("claim_ids"):
                        overview_errors.append(f"overview item {item.get('item_id')} lacks rich grounded copy")

    category_rank = {category: index for index, category in enumerate(CATEGORY_ORDER)}
    previous_rank: int | None = None
    nav_regressions = 0
    for spec in nav_motion_specs:
        key = str(spec.get("nav_key") or "")
        if key not in category_rank:
            continue
        rank = category_rank[key]
        if previous_rank is not None and rank < previous_rank:
            nav_regressions += 1
        previous_rank = rank
    errors = []
    if metric_errors:
        errors.append(f"metric duplicates: {metric_errors}")
    if nav_regressions:
        errors.append(f"navigation target regressions: {nav_regressions}")
    if overview_errors:
        errors.extend(overview_errors)
    if card_pagination_errors:
        errors.append(f"detail cards were dropped before pagination: {card_pagination_errors}")
    if visual_errors:
        errors.extend(visual_errors)
    return {
        "status": "passed" if not errors else "failed",
        "overview_completeness_pass": not overview_errors,
        "overview_reading_time_pass": not overview_errors,
        "single_top_navigation_pass": True,
        "detail_card_pagination_pass": not card_pagination_errors,
        "source_visual_resolution_pass": not visual_errors,
        "source_visual_claim_match_pass": not visual_errors,
        "visible_metric_duplicates": len(metric_errors),
        "nav_target_regressions": nav_regressions,
        "errors": errors,
    }


def _overview_timing_contract(
    script: Mapping[str, Any],
    timings: Mapping[str, Mapping[str, float]],
) -> dict[str, Any]:
    """Validate the authored v5 overview against the fixed five-second pages."""

    overview = next((segment for segment in script.get("segments", []) if segment.get("kind") == "overview"), None)
    pages = list((overview or {}).get("screen_pages") or [])
    plan_version = str((script.get("editorial") or {}).get("plan_version") or "")
    expected_page_durations = [float(OVERVIEW_PAGE_DURATION_SECONDS) for _ in pages]
    actual_page_durations = [float(page.get("duration_seconds") or 0.0) for page in pages if isinstance(page, Mapping)]
    expected_total = round(sum(expected_page_durations), 3)
    actual_total = round(float((timings.get("overview") or {}).get("duration") or 0.0), 3)
    errors: list[str] = []
    if plan_version == "5.0":
        if actual_page_durations != expected_page_durations:
            errors.append("overview pages must each be exactly 5 seconds")
        if abs(actual_total - expected_total) > 0.05:
            errors.append(
                f"overview scene duration must equal {expected_total:g} seconds for {len(pages)} fixed pages; got {actual_total:g}"
            )
    return {
        "status": "passed" if not errors else "failed",
        "page_count": len(pages),
        "expected_page_duration_seconds": float(OVERVIEW_PAGE_DURATION_SECONDS),
        "page_durations": actual_page_durations,
        "expected_total_duration_seconds": expected_total,
        "actual_scene_duration_seconds": actual_total,
        "errors": errors,
        "enforced": plan_version == "5.0",
    }


def materialize(project_dir: Path, script: Mapping[str, Any], durations: Mapping[str, float], cues: list[Mapping[str, Any]]) -> dict[str, Any]:
    project_dir.mkdir(parents=True, exist_ok=True)
    segments = list(script.get("segments") or [])
    workspace = project_dir / "hyperframes"
    workspace.mkdir(parents=True, exist_ok=True)
    screenshot_assets = _prepare_screenshot_assets(project_dir, segments, workspace)
    timings: dict[str, dict[str, float]] = {}
    cursor = 0.0
    scene_parts: list[str] = []
    category_slugs: list[str] = []
    for index, segment in enumerate(segments):
        segment_id = str(segment["id"])
        duration = max(0.2, float(durations.get(segment_id, 0.2)))
        timings[segment_id] = {"start": round(cursor, 3), "end": round(cursor + duration, 3), "duration": round(duration, 3)}
        scene_parts.append(_scene_markup(segment, index=index, start=cursor, duration=duration, screenshot_pages=screenshot_assets.get(segment_id)))
        category = str(segment.get("category") or "other")
        if segment.get("kind") == "news" and category not in category_slugs:
            category_slugs.append(category)
        cursor += duration
    total = round(cursor, 3)
    # The only navigation surface is the top story rail.  Intro/outro hide it;
    # the overview shows the rail without activating a story.
    ordered_categories = [category for category in CATEGORY_ORDER if category in category_slugs]
    nav_entries: list[tuple[str, str]] = []
    semantic_navigation = any(
        segment.get("kind") == "news" and segment.get("presentation_order") is not None
        for segment in segments
    )
    if semantic_navigation:
        nav_entries.extend(
            (
                str(segment["id"]),
                str(segment.get("navigation_title") or segment.get("subject") or segment.get("progress_label") or f"资讯{index}"),
            )
            for index, segment in enumerate(segments, 1)
            if segment.get("kind") == "news"
        )
    else:
        nav_entries.extend((category, CATEGORY_LABELS.get(category, category)) for category in ordered_categories)
    nav_markup = '<span class="nav-marker" aria-hidden="true"></span>' + "".join(
        f'<span class="nav-chip" id="nav-chip-{_safe_id(category)}" data-category="{_esc(category)}">'
        f'<span class="nav-chip-base" data-layout-allow-overlap="true">{_esc(label)}</span></span>'
        for category, label in nav_entries
    )
    nav_motion_specs = _nav_motion_specs(segments, timings)
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "__TOTAL_DURATION__": f"{total:.3f}",
        "__DATE__": _esc(str(script.get("date") or "").replace("-", ".")),
        "__SHOW_NAME__": _esc(script.get("show_name") or "AI每日早报"),
        "__SHOW_NAME_EN__": _esc(script.get("show_name_en") or "AI Daily News"),
        "__NAV_MARKUP__": nav_markup,
        "__NAV_MOTION_SPECS__": json.dumps(nav_motion_specs, ensure_ascii=False, separators=(",", ":")),
        "__SCENES__": "".join(scene_parts),
        "__CAPTIONS__": _caption_markup(cues),
        "__CUE_SPECS__": json.dumps(list(cues), ensure_ascii=False, separators=(",", ":")),
        "__SCENE_SPECS__": json.dumps([
            {"id": f"scene-{_safe_id(str(s['id']))}", **timings[str(s['id'])], "kind": s.get("kind"), "category": s.get("category", "")}
            for s in segments
        ], ensure_ascii=False, separators=(",", ":")),
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    unresolved = [token for token in ("__TOTAL_DURATION__", "__DATE__", "__SHOW_NAME__", "__SHOW_NAME_EN__", "__NAV_MARKUP__", "__NAV_MOTION_SPECS__", "__SCENES__", "__CAPTIONS__", "__CUE_SPECS__", "__SCENE_SPECS__") if token in template]
    if unresolved:
        raise ValueError(f"unresolved template placeholders: {unresolved}")

    (workspace / "index.html").write_text(template, encoding="utf-8")
    write_json(workspace / "hyperframes.json", {"project": project_dir.name, "width": 1920, "height": 1080, "fps": 30, "template_id": "ai-signal-morning-brief", "display_name": "AI每日早报"})
    (workspace / "DESIGN.md").write_text((Path(__file__).resolve().parent / "template" / "DESIGN.md").read_text(encoding="utf-8"), encoding="utf-8")
    for relative in (
        "assets/audio/final-mix.wav",
        "assets/audio/narration-track.wav",
        "assets/audio/transition-whoosh.wav",
        "assets/audio/category-chime.wav",
        "assets/subtitles/subtitles.srt",
        "assets/music/opening.mp3",
        "assets/music/middle-loop.mp3",
        "assets/music/ending.mp3",
        "assets/music/README.md",
    ):
        source = project_dir / relative
        destination = workspace / relative
        if source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    scene_plan = {
        "version": "1.0",
        "template_id": "ai-signal-morning-brief",
        "render_runtime": "hyperframes",
        "composition_mode": "templated",
        "editorial": dict(script.get("editorial") or {}),
        "navigation": {"top": nav_entries, "motion": nav_motion_specs, "single_surface": True},
        "scenes": [
            {
                "id": f"scene-{_safe_id(str(segment['id']))}",
                "type": segment.get("kind"),
                "category": segment.get("category"),
                "layout_type": segment.get("layout_type"),
                "event_key": segment.get("event_key"),
                "card_count": len(segment.get("cards") or []),
                "source_visual_count": len(screenshot_assets.get(str(segment["id"]), [])),
                **timings[str(segment["id"])]
            }
            for segment in segments
        ],
    }
    template_contract = _template_contract(script, nav_motion_specs)
    overview_timing = _overview_timing_contract(script, timings)
    template_contract["overview_timing"] = overview_timing
    if overview_timing["status"] != "passed":
        template_contract["status"] = "failed"
        template_contract["overview_reading_time_pass"] = False
        template_contract["errors"] = [
            *list(template_contract.get("errors") or []),
            *list(overview_timing.get("errors") or []),
        ]
    scene_plan["template_contract"] = template_contract
    edit_decisions = {
        "version": "1.0",
        "render_runtime": "hyperframes",
        "composition_mode": "templated",
        "cuts": [{"id": f"cut-{segment['id']}", "source": f"scene-{_safe_id(str(segment['id']))}", "in_seconds": timings[str(segment['id'])]["start"], "out_seconds": timings[str(segment['id'])]["end"], "transition": "editorial-push"} for segment in segments],
        "audio": {
            "narration": "assets/audio/narration-track.wav",
            "music": {"opening": "assets/music/opening.mp3", "middle": "assets/music/middle-loop.mp3", "ending": "assets/music/ending.mp3"},
            "transition_whoosh": "assets/audio/transition-whoosh.wav",
            "category_chime": "assets/audio/category-chime.wav",
            "mixed": "assets/audio/final-mix.wav",
        },
        "subtitles": {"enabled": True, "source": "assets/subtitles/subtitles.srt", "style": "phrase"},
    }
    asset_manifest = {
        "version": "1.0",
        "assets": [
            {"id": "audio-final-mix", "type": "audio", "path": "assets/audio/final-mix.wav"},
            {"id": "music-opening", "type": "audio", "path": "assets/music/opening.mp3", "source": "Publication Podcast Studio bundled background music"},
            {"id": "music-middle", "type": "audio", "path": "assets/music/middle-loop.mp3", "source": "Publication Podcast Studio bundled background music"},
            {"id": "music-ending", "type": "audio", "path": "assets/music/ending.mp3", "source": "Publication Podcast Studio bundled background music"},
            {"id": "transition-whoosh", "type": "audio", "path": "assets/audio/transition-whoosh.wav", "source": "deterministic-local-sfx"},
            {"id": "category-chime", "type": "audio", "path": "assets/audio/category-chime.wav", "source": "deterministic-local-sfx"},
            {"id": "subtitles", "type": "subtitle", "path": "assets/subtitles/subtitles.srt", "cue_count": len(cues)},
        ],
        "metadata": {"template_id": "ai-signal-morning-brief", "display_name": "AI每日早报", "source_visuals_optional": True, "no_secrets_in_artifacts": True},
    }
    for segment_id, pages in screenshot_assets.items():
        for index, page in enumerate(pages, 1):
            asset_manifest["assets"].append({
                "id": f"source-visual-{_safe_id(segment_id)}-{index:02d}",
                "type": str(page.get("media_type") or "image"),
                "path": page["asset_path"],
                "source": page.get("source_path"),
                "sha256": page.get("sha256"),
                "width": page.get("width"),
                "height": page.get("height"),
            })
    artifacts = project_dir / "artifacts"
    write_json(artifacts / "scene_plan.json", scene_plan)
    write_json(artifacts / "edit_decisions.json", edit_decisions)
    write_json(artifacts / "asset_manifest.json", asset_manifest)
    write_json(project_dir / "project.json", {"version": "1.0", "project_id": project_dir.name, "title": script.get("title"), "template_id": "ai-signal-morning-brief", "render_runtime": "hyperframes", "composition_mode": "templated", "width": 1920, "height": 1080, "fps": 30, "source_date": script.get("date")})
    return {
        "workspace": workspace,
        "timings": timings,
        "total_duration_seconds": total,
        "scene_count": len(segments),
        "category_names": [CATEGORY_LABELS[category] for category in ordered_categories],
        "category_slugs": ordered_categories,
        "nav_motion_specs": nav_motion_specs,
        "template_contract": template_contract,
        "overview_timing": overview_timing,
    }
