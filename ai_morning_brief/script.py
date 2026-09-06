from __future__ import annotations

import re
import math
from collections import OrderedDict
from datetime import date
from typing import Any, Iterable, Mapping

from .config import CATEGORY_LABELS, CATEGORY_ORDER, DEFAULT_SHOW_NAME, DEFAULT_SHOW_NAME_EN, EDITORIAL_DIMENSION_LABELS, OVERVIEW_PAGE_DURATION_SECONDS
from .editorial import validate_editorial_plan
from .models import ScriptSegment, SelectionResult, SourceItem
from .source_detail import detail_text_for_item
from .writing import build_pronunciation_ledger, caption_visible_units, normalize_with_ledger, normalize_display_text, split_caption_units, validate_spoken_text


_SENTENCE_RE = re.compile(r"[^。！？!?；;]+[。！？!?；;]?", re.UNICODE)
_NUMBER_RE = re.compile(r"(?<![A-Za-z])\d+(?:[.,]\d+)?(?:\s?[万亿千万百十%％倍个名家月日年美元港元欧元元桶/日]+)?", re.UNICODE)
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9.+#/-]*")
_DATE_RE = re.compile(r"\b(?:19|20)\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?\b")
_WEEKDAY_NAMES = "一二三四五六日"
_LABEL_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9.+#/-]{1,30}|[\u4e00-\u9fff]{2,16}")


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("\u3000", " ").strip())


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_RE.findall(_clean(text)) if part.strip()]


def _without_terminal_punctuation(text: str) -> str:
    return text.rstrip("。！？!?；;，, ")


def _caption_units_for_text(text: str) -> tuple[dict[str, Any], ...]:
    units: list[dict[str, Any]] = []
    for index, part in enumerate(split_caption_units(text), 1):
        normalized = normalize_with_ledger(part)
        units.append({"unit_id": f"narration-caption-{index:02d}", "beat_type": "narration", "display_text": normalized.display_text, "spoken_text": normalized.spoken_text, "claim_ids": [], "card_ids": []})
    return tuple(units)


def _dedupe_fragments(title: str, summary: str | None) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = [{"source_field": "title", "source_text": _clean(title)}]
    if summary:
        candidates.extend({"source_field": "summary", "source_text": _clean(sentence)} for sentence in _sentences(summary))
    fragments: list[dict[str, str]] = []
    seen_normalized: set[str] = set()
    title_core = _without_terminal_punctuation(_clean(title))
    for fragment in candidates:
        text = fragment["source_text"]
        normalized = _without_terminal_punctuation(text).replace(" ", "")
        if not normalized or normalized in seen_normalized:
            continue
        if fragment["source_field"] == "summary" and normalized == title_core.replace(" ", ""):
            continue
        seen_normalized.add(normalized)
        fragments.append(fragment)
    return fragments


def build_broadcast(item: SourceItem, *, maximum_chars: int = 155) -> tuple[str, tuple[dict[str, str], ...]]:
    fragments = _dedupe_fragments(item.title, item.summary)
    if not fragments:
        fragments = [{"source_field": "title", "source_text": _clean(item.title)}]
    chosen: list[dict[str, str]] = []
    total = 0
    for fragment in fragments:
        text = fragment["source_text"]
        extra = len(text) + (1 if chosen else 0)
        if chosen and total + extra > maximum_chars:
            break
        chosen.append(fragment)
        total += extra
        if len(chosen) >= 3:
            break
    broadcast = "。".join(_without_terminal_punctuation(part["source_text"]) for part in chosen) + "。"
    return broadcast, tuple(chosen)


def screen_points(item: SourceItem, fragments: Iterable[Mapping[str, str]]) -> tuple[str, ...]:
    clauses: list[str] = []
    for fragment in fragments:
        for clause in re.split(r"[，,。；;]", fragment["source_text"]):
            clause = clause.strip()
            if clause and clause not in clauses:
                clauses.append(clause)
    with_numbers = [clause for clause in clauses if _NUMBER_RE.search(clause) or _DATE_RE.search(clause)]
    ordered = with_numbers + [clause for clause in clauses if clause not in with_numbers]
    return tuple(ordered[:5])


def _overview_groups(
    items: Iterable[SourceItem],
    *,
    title_overrides: Mapping[str, str] | None = None,
    text_overrides: Mapping[str, str] | None = None,
    claim_overrides: Mapping[str, list[str]] | None = None,
    category_order: Iterable[str] | None = None,
    category_labels: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], ...]:
    grouped: "OrderedDict[str, list[dict[str, str]]]" = OrderedDict()
    title_overrides = title_overrides or {}
    text_overrides = text_overrides or {}
    claim_overrides = claim_overrides or {}
    for item in items:
        category = item.category or "other"
        grouped.setdefault(category, []).append(
            {
                "item_id": item.item_id,
                "title": _clean(title_overrides.get(item.item_id) or item.title),
                "text": _clean(text_overrides.get(item.item_id) or item.title),
                "claim_ids": list(claim_overrides.get(item.item_id) or []),
            }
        )
    ordered = tuple(category_order or CATEGORY_ORDER)
    category_labels = category_labels or CATEGORY_LABELS
    ordered_categories = [category for category in ordered if category in grouped]
    ordered_categories.extend(category for category in grouped if category not in ordered_categories)
    return tuple(
        {
            "category": category,
            "label": category_labels.get(category, CATEGORY_LABELS.get(category, category)),
            "items": list(grouped[category]),
        }
        for category in ordered_categories
    )


OVERVIEW_LAYOUT_WIDTH = 1760
OVERVIEW_LAYOUT_HEIGHT = 724
OVERVIEW_GRID_GAP = 22
OVERVIEW_CARD_GAP = 12


def _display_width_units(text: str) -> float:
    """Approximate the CSS width of mixed CJK/Latin text without truncating it."""

    total = 0.0
    for char in str(text or ""):
        if char.isspace():
            continue
        total += 1.0 if ord(char) >= 0x2E80 else 0.56
    return total


def _overview_card_height(group: Mapping[str, Any], *, card_width: float) -> float:
    """Estimate the card's actual CSS height using the overview template metrics.

    This mirrors the template's padding, header, line-height and list spacing.
    It is intentionally a height calculation rather than a character-count
    page quota, so short items can share a page and long items naturally push
    the next item onto the following page.
    """

    content_width = max(180.0, card_width - 58.0)
    body_width_units = content_width / 22.0
    title_width_units = content_width / 18.0
    height = 2.0 + 43.0 + 48.0 + 13.0
    item_count = 0
    for item in group.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        item_count += 1
        title = str(item.get("title") or "")
        text = str(item.get("text") or title)
        title_lines = max(1, math.ceil(_display_width_units(title) / title_width_units))
        body_lines = max(1, math.ceil(_display_width_units(text) / body_width_units))
        height += 12.0 + title_lines * 21.96 + 2.0 + body_lines * 30.36
    return max(180.0, height if item_count else 180.0)


def _overview_grid_height(groups: Iterable[Mapping[str, Any]]) -> float:
    """Measure the two-column overview grid using the template's card geometry."""

    group_list = [group for group in groups if isinstance(group, Mapping)]
    if not group_list:
        return 0.0
    card_width = (OVERVIEW_LAYOUT_WIDTH - OVERVIEW_GRID_GAP) / 2.0
    heights = [_overview_card_height(group, card_width=card_width) for group in group_list]
    rows = [heights[index:index + 2] for index in range(0, len(heights), 2)]
    return sum(max(row) for row in rows) + OVERVIEW_GRID_GAP * max(0, len(rows) - 1)


def _overview_pages(
    groups: Iterable[Mapping[str, Any]],
    *,
    capacity: int = 128,
    layout_mode: str = "legacy",
) -> tuple[dict[str, Any], ...]:
    """Paginate overview items by rendered card height for v5 plans.

    ``legacy`` remains available for v2-v4 fixtures and historical replays.
    New plans use ``content_height``: the next item is appended while the
    measured two-column grid still fits the available content height.
    """

    pages: list[dict[str, Any]] = []
    current_groups: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    current_units = 0

    def flush() -> None:
        nonlocal current_groups, current_units
        if not current_groups:
            return
        visible_units = max(1, current_units)
        pages.append({
            "index": len(pages) + 1,
            "groups": [dict(group) for group in current_groups.values()],
            "visible_units": visible_units,
            "duration_seconds": OVERVIEW_PAGE_DURATION_SECONDS,
            "layout_basis": "content_height" if layout_mode == "content_height" else "visible_units",
            "layout_height_px": round(_overview_grid_height(current_groups.values()), 2),
            "layout_available_height_px": OVERVIEW_LAYOUT_HEIGHT,
        })
        current_groups = OrderedDict()
        current_units = 0

    for group in groups:
        category = str(group.get("category") or "other")
        label = str(group.get("label") or category)
        for item in group.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            item_units = max(1, caption_visible_units(str(item.get("text") or item.get("title") or "")))
            starts_new_category = category not in current_groups
            candidate_groups = OrderedDict((key, {**value, "items": list(value.get("items") or [])}) for key, value in current_groups.items())
            candidate = candidate_groups.setdefault(category, {"category": category, "label": label, "items": []})
            candidate["items"].append(dict(item))
            if layout_mode == "content_height":
                over_height = _overview_grid_height(candidate_groups.values()) > OVERVIEW_LAYOUT_HEIGHT
                if current_groups and over_height:
                    flush()
                    candidate_groups = OrderedDict()
                    candidate = candidate_groups.setdefault(category, {"category": category, "label": label, "items": []})
                    candidate["items"].append(dict(item))
                current_groups = candidate_groups
                current_units += item_units
                continue
            if current_groups and (current_units + item_units > capacity or (starts_new_category and len(current_groups) >= 4)):
                flush()
            target = current_groups.setdefault(category, {"category": category, "label": label, "items": []})
            target["items"].append(dict(item))
            current_units += item_units
    flush()
    return tuple(pages)


def _category_rank(category: str | None) -> int:
    """Return the approved presentation order for a source category."""

    try:
        return CATEGORY_ORDER.index(category or "other")
    except ValueError:
        return len(CATEGORY_ORDER)


def _ordered_source_items(items: Iterable[SourceItem]) -> list[SourceItem]:
    """Group presentation scenes by the fixed top-navigation order.

    Selection and persistence retain API order; only the visual sequence is
    grouped so the top navigation can advance monotonically to the right.
    """

    indexed = list(enumerate(items))
    indexed.sort(key=lambda pair: (_category_rank(pair[1].category), pair[0]))
    return [item for _, item in indexed]


def layout_for(item: SourceItem, broadcast: str) -> str:
    category = item.category or "other"
    if _NUMBER_RE.search(broadcast):
        return "numeric-spotlight"
    if _DATE_RE.search(broadcast):
        return "timeline"
    if category == "paper":
        return "research-abstract"
    if category in {"ai-models", "ai-products"}:
        return "launch-card"
    return "headline-stack"


def progress_label(title: str, *, used: set[str] | None = None) -> str:
    """Build a compact source-derived label for the bottom story rail."""

    used = used if used is not None else set()
    cleaned = _clean(title).strip(" -—:：，,。！？!?()（）[]【】")
    tokens = _LABEL_TOKEN_RE.findall(cleaned)
    ascii_tokens = [token for token in tokens if re.search(r"[A-Za-z]", token)]
    chinese_tokens = [token for token in tokens if re.search(r"[\u4e00-\u9fff]", token)]
    label = next((token for token in ascii_tokens if token not in used), "")
    if not label:
        prefix = re.split(r"(?:发布|推出|宣布|开放|下调|新增|分享|提出|表示|支持|达到|上线)", cleaned, maxsplit=1)[0].strip()
        label = next((token for token in chinese_tokens if token not in used), "")
        if not label and len(prefix) >= 2 and prefix not in used:
            label = prefix
    if not label:
        label = next((token for token in tokens if token not in used), cleaned[:16])
    label = label[:16] or "资讯"
    base = label
    suffix = 2
    while label in used:
        suffix_text = f" · {suffix}"
        label = f"{base[:max(1, 16 - len(suffix_text))]}{suffix_text}"
        suffix += 1
    used.add(label)
    return label


def visual_plan(points: Iterable[str], broadcast: str, *, category: str) -> dict[str, Any]:
    """Select a deterministic card composition from content density."""

    visible = [str(point).strip() for point in points if str(point).strip()][:5]
    count = len(visible)
    total_chars = sum(len(point) for point in visible) + len(broadcast)
    density = "spacious" if total_chars <= 90 else "regular" if total_chars <= 180 else "dense"
    if count <= 1:
        variant = "hero"
    elif count == 2:
        variant = "split"
    elif count == 3:
        variant = "lead-and-stack"
    elif count == 4:
        variant = "quad"
    else:
        variant = "masonry"
    ranked = sorted(range(count), key=lambda index: (-len(visible[index]), index))
    cards = []
    for index, point in enumerate(visible):
        role = "lead" if index == ranked[0] and count >= 3 else "support"
        cards.append({"index": index, "role": role, "text": point})
    return {
        "version": "1.0",
        "variant": variant,
        "density": density,
        "category": category,
        "card_count": count,
        "cards": cards,
    }


def build_script(selection: SelectionResult, *, run_date: date, show_name: str = DEFAULT_SHOW_NAME) -> dict[str, Any]:
    segments: list[ScriptSegment] = []
    used_progress_labels: set[str] = set()
    for index, item in enumerate(_ordered_source_items(selection.items), 1):
        broadcast, fragments = build_broadcast(item)
        spoken = normalize_with_ledger(broadcast).spoken_text
        category = item.category or "other"
        points = screen_points(item, fragments)
        segments.append(
            ScriptSegment(
                segment_id=f"story-{index:02d}",
                kind="news",
                title=item.title,
                category=category,
                source_item_id=item.item_id,
                source_name=item.source_name,
                broadcast_text=broadcast,
                source_fragments=fragments,
                screen_points=points,
                layout_type=layout_for(item, broadcast),
                progress_label=progress_label(item.title, used=used_progress_labels),
                visual_plan=visual_plan(points, broadcast, category=category),
                aihot_url=item.aihot_url,
                original_url=item.original_url,
                display_text=broadcast,
                spoken_text=spoken,
            )
        )
    intro = ScriptSegment(
        segment_id="intro",
        kind="intro",
        title=show_name,
        category="开场",
        source_item_id=None,
        source_name=None,
        broadcast_text=(
            f"各位观众早上好，今天是{run_date.month}月{run_date.day}日。"
            "欢迎收看AI早报。"
        ),
        source_fragments=tuple(),
        screen_points=tuple(),
        layout_type="intro",
        display_text=(
            f"各位观众早上好，今天是{run_date.month}月{run_date.day}日。"
            "欢迎收看AI早报。"
        ),
        spoken_text=normalize_with_ledger(
            f"各位观众早上好，今天是{run_date.month}月{run_date.day}日。欢迎收看AI早报。"
        ).spoken_text,
        caption_units=_caption_units_for_text(
            f"各位观众早上好，今天是{run_date.month}月{run_date.day}日。欢迎收看AI早报。"
        ),
    )
    overview_groups = _overview_groups(selection.items)
    overview_pages = _overview_pages(overview_groups, layout_mode="legacy")
    overview = ScriptSegment(
        segment_id="overview",
        kind="overview",
        title=f"{run_date.isoformat()} 资讯概览",
        category="overview",
        source_item_id=None,
        source_name=None,
        broadcast_text="首先来看今日资讯概览。",
        source_fragments=tuple(),
        screen_points=tuple(),
        layout_type="overview",
        screen_groups=overview_groups,
        screen_pages=overview_pages,
        minimum_duration_seconds=round(sum(float(page.get("duration_seconds") or 0.0) for page in overview_pages), 3),
        display_text="首先来看今日资讯概览。",
        spoken_text="首先来看今日资讯概览。",
        caption_units=_caption_units_for_text("首先来看今日资讯概览。"),
    )
    outro = ScriptSegment(
        segment_id="outro",
        kind="outro",
        title="明天见",
        category="结尾",
        source_item_id=None,
        source_name=None,
        broadcast_text="今天的AI资讯播送完毕。我们明天见。",
        source_fragments=tuple(),
        screen_points=tuple(),
        layout_type="outro",
        display_text="今天的AI资讯播送完毕。我们明天见。",
        spoken_text="今天的AI资讯播送完毕。我们明天见。",
        caption_units=_caption_units_for_text("今天的AI资讯播送完毕。我们明天见。"),
    )
    ordered = [intro, overview, *segments, outro]
    return {
        "version": "1.0",
        "show_name": show_name,
        "show_name_en": DEFAULT_SHOW_NAME_EN,
        "date": run_date.isoformat(),
        "mode": selection.mode,
        "title": f"{run_date.isoformat()} {show_name}",
        "opening": {"duration_seconds": 4.0, "style": "editorial-reveal", "hero_segment_id": overview.segment_id},
        "segments": [segment.to_dict() for segment in ordered],
        "source_item_ids": [item.item_id for item in selection.items],
        "category_counts": selection.category_counts,
        "selection_reason": selection.reason,
        "editorial_policy": "factual-only; source title and summary fragments; no independent analysis",
    }


def build_script_from_editorial_plan(
    selection: SelectionResult,
    *,
    run_date: date,
    editorial_input: Mapping[str, Any],
    editorial_plan: Mapping[str, Any],
    show_name: str = DEFAULT_SHOW_NAME,
) -> dict[str, Any]:
    """Build the Azure/OpenMontage script from a Codex-authored plan.

    The plan owns the editorial decisions (event grouping, narration beats and
    card copy); this function only normalizes its validated structure into the
    existing rendering contract.  No model-generated text is accepted before
    ``validate_editorial_plan`` has checked its source claims.
    """

    source_items = {item.item_id: item for item in selection.items}
    errors = validate_editorial_plan(editorial_plan, editorial_input, source_items)
    if errors:
        raise ValueError("editorial plan validation failed: " + "; ".join(errors[:8]))

    segments: list[ScriptSegment] = []
    used_progress_labels: set[str] = set()
    story_entries = list(enumerate(editorial_plan["stories"]))
    plan_version = str(editorial_plan.get("version") or "")
    if plan_version in {"5.0", "4.0"}:
        story_entries.sort(key=lambda pair: (int(pair[1].get("presentation_order") or 999999), pair[0]))
    else:
        story_entries.sort(key=lambda pair: (_category_rank(str(pair[1].get("category") or "other")), pair[0]))
    for presentation_index, (original_index, story) in enumerate(story_entries, 1):
        source_ids = tuple(str(value) for value in story["source_item_ids"])
        anchor = source_items[source_ids[0]]
        claims = tuple(
            {
                "claim_id": str(claim["id"]),
                "source_item_id": str(claim["source_item_id"]),
                "source_field": str(claim["source_field"]),
                "source_text": _clean(str(claim["source_text"])),
            }
            for claim in story["claims"]
        )
        narration = story["narration"]
        beats_list: list[dict[str, Any]] = []
        for raw_beat in narration["beats"]:
            beat = dict(raw_beat)
            display_beat = normalize_display_text(beat.get("text"))
            normalized = normalize_with_ledger(display_beat)
            beat["text"] = display_beat
            beat["spoken_text"] = normalized.spoken_text
            beats_list.append(beat)
        beats = tuple(beats_list)
        caption_units = tuple(dict(unit) for unit in (narration.get("caption_units") or []) if isinstance(unit, Mapping))
        if not caption_units:
            fallback_units: list[dict[str, Any]] = []
            for beat_index, beat in enumerate(beats, 1):
                beat_id = str(beat.get("beat_id") or f"beat-{beat_index:02d}")
                for unit_index, unit_text in enumerate(split_caption_units(beat.get("text")), 1):
                    normalized = normalize_with_ledger(unit_text)
                    fallback_units.append({
                        "unit_id": f"{beat_id}-caption-{unit_index:02d}",
                        "beat_id": beat_id,
                        "beat_type": str(beat.get("type") or ""),
                        "display_text": normalized.display_text,
                        "spoken_text": normalized.spoken_text,
                        "claim_ids": [str(value) for value in beat.get("claim_ids", []) if value],
                        "card_ids": [str(value) for value in beat.get("card_ids", []) if value],
                        "visual_asset_id": str(beat.get("visual_asset_id") or ""),
                    })
            caption_units = tuple(fallback_units)
        display_text = "".join(str(unit.get("display_text") or "") for unit in caption_units)
        spoken_text = "".join(str(unit.get("spoken_text") or "") for unit in caption_units)
        narration_display_text = display_text or normalize_display_text(narration.get("display_text"))
        narration_spoken_text = spoken_text or normalize_with_ledger(narration_display_text).spoken_text
        cards = tuple(
            {**dict(card), **{
                field: normalize_display_text(card.get(field))
                for field in ("label", "headline", "body", "metric")
                if card.get(field)
            }}
            for card in story["cards"]
        )
        screen_points = tuple(_clean(card.get("body")) for card in cards if _clean(card.get("body")))
        layout = dict(story.get("layout") or {})
        layout_type = str(layout.get("type") or "stack")
        plan = {
            "version": "2.0",
            "variant": layout_type,
            "density": str(layout.get("density") or "regular"),
            "category": str(story.get("category") or anchor.category or "other"),
            "story_kind": str(story.get("story_kind") or "single"),
            "group_label": _clean(story.get("group_label")),
            "cards_per_page": 4 if str(story.get("story_kind") or "single") == "brief_group" else 5,
            "card_count": len(cards),
            "cards": list(cards),
        }
        title = _clean(story.get("title") or anchor.title)
        segments.append(
            ScriptSegment(
                segment_id=f"story-{presentation_index:02d}",
                kind="news",
                title=title,
                category=str(story.get("category") or anchor.category or "other"),
                source_item_id=anchor.item_id,
                source_name=anchor.source_name,
                broadcast_text=narration_display_text,
                source_fragments=claims,
                screen_points=screen_points,
                layout_type=layout_type,
                progress_label=_clean(story.get("navigation_title")) or progress_label(title, used=used_progress_labels),
                visual_plan=plan,
                aihot_url=anchor.aihot_url,
                original_url=anchor.original_url,
                source_item_ids=source_ids,
                event_key=str(story.get("event_key") or ""),
                story_id=str(story.get("event_key") or ""),
                subject=_clean(story.get("subject")),
                navigation_title=_clean(story.get("navigation_title")) or title,
                presentation_order=int(story.get("presentation_order")) if str(story.get("presentation_order") or "").isdigit() else presentation_index,
                narration_beats=beats,
                cards=cards,
                display_text=narration_display_text,
                spoken_text=narration_spoken_text,
                caption_units=caption_units,
            )
        )

    intro = ScriptSegment(
        segment_id="intro",
        kind="intro",
        title=show_name,
        category="开场",
        source_item_id=None,
        source_name=None,
        broadcast_text=(
            f"各位观众早上好，今天是{run_date.month}月{run_date.day}日。"
            "欢迎收看AI早报。"
        ),
        source_fragments=tuple(),
        screen_points=tuple(),
        layout_type="intro",
        display_text=(
            f"各位观众早上好，今天是{run_date.month}月{run_date.day}日。"
            "欢迎收看AI早报。"
        ),
        spoken_text=normalize_with_ledger(
            f"各位观众早上好，今天是{run_date.month}月{run_date.day}日。欢迎收看AI早报。"
        ).spoken_text,
        caption_units=_caption_units_for_text(
            f"各位观众早上好，今天是{run_date.month}月{run_date.day}日。欢迎收看AI早报。"
        ),
    )
    if plan_version in {"5.0", "4.0"}:
        overview_items = [
            source_items[item_id]
            for _, story in story_entries
            if isinstance(story, Mapping)
            for item_id in [str(value) for value in story.get("source_item_ids") or []]
            if item_id in source_items
        ]
    else:
        overview_items = list(selection.items)
    overview_title_overrides: dict[str, str] = {}
    overview_text_overrides: dict[str, str] = {}
    overview_claim_overrides: dict[str, list[str]] = {}
    for _, story in story_entries:
        if not isinstance(story, Mapping):
            continue
        source_ids_for_story = [str(value) for value in story.get("source_item_ids") or []]
        if str(story.get("story_kind") or "single") == "brief_group":
            for item in story.get("overview_items") or []:
                if not isinstance(item, Mapping):
                    continue
                item_id = str(item.get("source_item_id") or "")
                if item_id:
                    overview_title_overrides[item_id] = _clean(item.get("title") or item.get("text"))
                    overview_text_overrides[item_id] = _clean(item.get("text"))
                    overview_claim_overrides[item_id] = [str(value) for value in item.get("claim_ids") or [] if value]
        elif source_ids_for_story:
            item_id = source_ids_for_story[0]
            if _clean(story.get("navigation_title")):
                overview_title_overrides[item_id] = _clean(story.get("navigation_title"))
            if _clean(story.get("overview_text")):
                overview_text_overrides[item_id] = _clean(story.get("overview_text"))
            overview_claim_overrides[item_id] = [str(value) for value in story.get("overview_claim_ids") or [] if value]
    overview_groups = _overview_groups(
        overview_items,
        title_overrides=overview_title_overrides,
        text_overrides=overview_text_overrides,
        claim_overrides=overview_claim_overrides,
        category_order=tuple(selection.policy.get("dimensions") or CATEGORY_ORDER),
        category_labels=EDITORIAL_DIMENSION_LABELS if selection.policy.get("dimensions") else CATEGORY_LABELS,
    )
    overview_pages = _overview_pages(
        overview_groups,
        layout_mode="content_height" if plan_version in {"5.0", "4.0"} else "legacy",
    )
    overview = ScriptSegment(
        segment_id="overview",
        kind="overview",
        title=f"{run_date.isoformat()} 资讯概览",
        category="overview",
        source_item_id=None,
        source_name=None,
        broadcast_text="首先来看今日资讯概览。",
        source_fragments=tuple(),
        screen_points=tuple(),
        layout_type="overview",
        screen_groups=overview_groups,
        screen_pages=overview_pages,
        minimum_duration_seconds=round(sum(float(page.get("duration_seconds") or 0.0) for page in overview_pages), 3),
        display_text="首先来看今日资讯概览。",
        spoken_text="首先来看今日资讯概览。",
        caption_units=_caption_units_for_text("首先来看今日资讯概览。"),
    )
    outro = ScriptSegment(
        segment_id="outro",
        kind="outro",
        title="明天见",
        category="结尾",
        source_item_id=None,
        source_name=None,
        broadcast_text="今天的AI资讯播送完毕。我们明天见。",
        source_fragments=tuple(),
        screen_points=tuple(),
        layout_type="outro",
        display_text="今天的AI资讯播送完毕。我们明天见。",
        spoken_text="今天的AI资讯播送完毕。我们明天见。",
        caption_units=_caption_units_for_text("今天的AI资讯播送完毕。我们明天见。"),
    )
    ordered = [intro, overview, *segments, outro]
    return {
        "version": "3.0",
        "show_name": show_name,
        "show_name_en": DEFAULT_SHOW_NAME_EN,
        "date": run_date.isoformat(),
        "mode": selection.mode,
        "title": f"{run_date.isoformat()} {show_name}",
        "opening": {"duration_seconds": 4.0, "style": "editorial-reveal", "hero_segment_id": overview.segment_id},
        "segments": [segment.to_dict() for segment in ordered],
        "source_item_ids": [item.item_id for item in selection.items],
        "category_counts": selection.category_counts,
        "selection_reason": selection.reason,
        "editorial_policy": "Codex-authored factual beats and cards; every generated field is linked to exact AIHOT evidence",
        "speech": {"version": "1.0", "canonical_text": "spoken_text", "provider_default": "azure"},
        "editorial": {
            "plan_version": editorial_plan.get("version"),
            "prompt_version": editorial_plan.get("prompt_version"),
            "input_sha256": editorial_plan.get("input_sha256"),
            "story_count": len(segments),
        },
        "pronunciation_ledger": build_pronunciation_ledger({"segments": [segment.to_dict() for segment in ordered]}),
    }


def _tokens(text: str) -> set[str]:
    canonical = normalize_display_text(text)
    number_tokens = {token.replace(" ", "") for token in _NUMBER_RE.findall(canonical)}
    ascii_tokens = {token.rstrip(".。") for token in _ASCII_TOKEN_RE.findall(canonical)}
    return number_tokens | ascii_tokens


def validate_script(
    script: Mapping[str, Any],
    source_items: Mapping[str, SourceItem],
    editorial_input: Mapping[str, Any] | None = None,
) -> list[str]:
    errors: list[str] = []
    plan_version = str((script.get("editorial") or {}).get("plan_version") or "")
    v5_script = plan_version == "5.0"
    segments = script.get("segments")
    if not isinstance(segments, list) or not segments:
        return ["script has no segments"]
    for raw in segments:
        if not isinstance(raw, Mapping):
            errors.append("script contains a non-object segment")
            continue
        if raw.get("kind") != "news":
            for unit_index, unit in enumerate(raw.get("caption_units") or []):
                if not isinstance(unit, Mapping):
                    errors.append(f"segment {raw.get('id')} caption unit {unit_index} is malformed")
                    continue
                unit_text = str(unit.get("display_text") or "").strip()
                if not re.search(r"[。！？!?；;]$", unit_text):
                    errors.append(f"segment {raw.get('id')} caption unit {unit_index} is not a complete sentence")
                if caption_visible_units(unit_text) > 28:
                    errors.append(f"segment {raw.get('id')} caption unit {unit_index} exceeds 28 visible units")
            continue
        item_id = str(raw.get("source_item_id") or "")
        item_ids = [str(value) for value in (raw.get("source_item_ids") or [item_id]) if str(value)]
        items = [source_items.get(value) for value in item_ids]
        if not item_ids or any(item is None for item in items):
            errors.append(f"segment {raw.get('id')} references unknown source item(s) {item_ids}")
            continue
        source_text_raw = "\n".join(
            "\n".join(part for part in (item.source_text(), detail_text_for_item(editorial_input or {}, item.item_id)) if part)
            for item in items
            if item is not None
        )
        source_text = re.sub(r"\s+", "", source_text_raw)
        fragments = raw.get("source_fragments") or []
        if not fragments:
            errors.append(f"segment {raw.get('id')} has no source fragments")
        for fragment in fragments:
            if not isinstance(fragment, Mapping) or not fragment.get("source_text"):
                errors.append(f"segment {raw.get('id')} has malformed source fragment")
                continue
            fragment_text = str(fragment["source_text"])
            fragment_item_id = str(fragment.get("source_item_id") or "")
            fragment_source = source_items.get(fragment_item_id) if fragment_item_id else None
            fragment_pool = (
                "\n".join(part for part in (fragment_source.source_text(), detail_text_for_item(editorial_input or {}, fragment_source.item_id)) if part)
                if fragment_source is not None
                else source_text_raw
            )
            if re.sub(r"\s+", "", fragment_text) not in re.sub(r"\s+", "", fragment_pool):
                errors.append(f"segment {raw.get('id')} contains a fragment not found in its source")
        display = str(raw.get("display_text") or raw.get("broadcast_text") or "")
        spoken = str(raw.get("spoken_text") or raw.get("broadcast_text") or "")
        if not display:
            errors.append(f"segment {raw.get('id')} has empty broadcast text")
        normalized_display = normalize_with_ledger(display).display_text
        if display != normalized_display:
            errors.append(f"segment {raw.get('id')} display_text is not canonical (remove grouped number separators)")
        caption_units = raw.get("caption_units") or []
        if caption_units:
            if not isinstance(caption_units, list):
                errors.append(f"segment {raw.get('id')} caption_units must be a list")
            else:
                unit_display = "".join(str(unit.get("display_text") or "") for unit in caption_units if isinstance(unit, Mapping))
                unit_spoken = "".join(str(unit.get("spoken_text") or "") for unit in caption_units if isinstance(unit, Mapping))
                if unit_display != display:
                    errors.append(f"segment {raw.get('id')} caption units do not concatenate to display_text")
                if unit_spoken != spoken:
                    errors.append(f"segment {raw.get('id')} caption units do not concatenate to spoken_text")
                for unit_index, unit in enumerate(caption_units):
                    if not isinstance(unit, Mapping):
                        errors.append(f"segment {raw.get('id')} caption unit {unit_index} is malformed")
                        continue
                    unit_text = str(unit.get("display_text") or "").strip()
                    if not v5_script and not re.search(r"[。！？!?；;]$", unit_text):
                        errors.append(f"segment {raw.get('id')} caption unit {unit_index} is not a complete sentence")
                    if caption_visible_units(unit_text) > 28:
                        errors.append(f"segment {raw.get('id')} caption unit {unit_index} exceeds 28 visible units")
                card_ids = {str(card.get("id")) for card in raw.get("cards") or [] if isinstance(card, Mapping) and card.get("id")}
                if raw.get("subject"):
                    if raw.get("subject") not in str(raw.get("display_text") or ""):
                        errors.append(f"segment {raw.get('id')} display_text does not name subject {raw.get('subject')!r}")
                    if not v5_script:
                        mapped_ids: list[str] = []
                        for unit in caption_units:
                            if isinstance(unit, Mapping):
                                mapped_ids.extend(str(value) for value in unit.get("card_ids") or [])
                        unknown = sorted(set(mapped_ids) - card_ids)
                        if unknown:
                            errors.append(f"segment {raw.get('id')} captions reference unknown card_ids: {unknown}")
                        missing = sorted(card_ids - set(mapped_ids))
                        if missing:
                            errors.append(f"segment {raw.get('id')} cards are not mapped to captions: {missing}")
        source_tokens = _tokens(source_text_raw)
        extra_tokens = _tokens(display) - source_tokens
        if extra_tokens:
            errors.append(f"segment {raw.get('id')} adds unsupported tokens: {sorted(extra_tokens)}")
        if raw.get("kind") == "news" and spoken:
            # Spoken rewrites may replace digits/abbreviations with Chinese
            # words, so grounding is checked on display text while this gate
            # checks only deterministic normalization and code hazards.
            spoken_errors = validate_spoken_text({"segments": [raw]})
            errors.extend(spoken_errors)
    return errors


def build_fact_ledger(script: Mapping[str, Any], source_items: Mapping[str, SourceItem]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for segment in script.get("segments", []):
        if segment.get("kind") != "news":
            continue
        item = source_items.get(str(segment.get("source_item_id")))
        if item is None:
            continue
        related_ids = [str(value) for value in (segment.get("source_item_ids") or [item.item_id]) if str(value)]
        related_items = [source_items.get(value) for value in related_ids]
        records.append(
            {
                "segment_id": segment["id"],
                "source_item_id": item.item_id,
                "source_item_ids": related_ids,
                "source_name": item.source_name,
                "source_urls": [
                    {"item_id": related.item_id, "aihot": related.aihot_url, "original": related.original_url}
                    for related in related_items
                    if related is not None
                ],
                "event_key": segment.get("event_key"),
                "source_fields": {"title": item.title, "summary": item.summary},
                "claims": list(segment.get("source_fragments", [])),
                "validation": "Codex-authored copy linked to exact AIHOT evidence; no independent analysis",
            }
        )
    return {"version": "1.0", "records": records}
