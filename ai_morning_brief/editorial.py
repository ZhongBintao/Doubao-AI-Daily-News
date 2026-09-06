from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import SelectionResult, SourceItem
from .source_detail import detail_text_for_item
from .writing import CAPTION_MAX_VISIBLE_UNITS, caption_visible_units, normalize_display_text, split_caption_sentences


EDITORIAL_INPUT_VERSION = "2.0"
EDITORIAL_PLAN_VERSION = "5.0"
EDITORIAL_PROMPT_VERSION = "codex-editorial-v5.1"
EDITORIAL_MAX_STORIES = 8
EDITORIAL_MIN_STORIES = 3
NARRATION_MIN_CHARS = 72
# A story is allowed to use as many source-grounded beats as are needed for a
# complete explanation.  The old 72–150 character bound was a pacing shortcut
# and is retained only as a named legacy value for v2/v3 fixture validation.
NARRATION_MAX_CHARS = 150
CARD_MIN_COUNT = 2
CARD_MAX_COUNT = 4
# Dense cards are intentionally stricter than the legacy two-card fallback.
# These thresholds keep the high-density production template useful without
# forcing filler when a source genuinely contains little text.
DENSE_CARD_MIN_COUNT = 3
DENSE_CARD_BODY_MIN_CHARS = 35
DENSE_CARD_BODY_MAX_CHARS = 90
DENSE_CARD_COVERAGE_RATIO = 0.80
DENSE_CARD_COVERAGE_CAP = 180
ALLOWED_LAYOUTS = {
    "hero-metric",
    "timeline",
    "impact-path",
    "feature-matrix",
    "comparison",
    "research-evidence",
    "action-path",
    "stack",
}
ALLOWED_BEATS = {"hook", "fact", "evidence", "context", "result", "impact", "action", "limitation"}
ALLOWED_CARD_ROLES = {"lead", "support", "metric", "timeline", "impact", "action", "evidence"}
MAX_V5_VISUALS_PER_STORY = 2
ALLOWED_CARD_SPANS = {1, 2, 3, 4, 5, 6}
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9.+#/-]*")
_NUMBER_RE = re.compile(r"(?<![A-Za-z])\d+(?:[.,]\d+)?(?:\s?[万亿千万百十%％倍个名家月日年美元港元欧元元桶/日]+)?", re.UNICODE)


class EditorialPlanError(ValueError):
    """Raised when a Codex-authored editorial plan is unsafe to render."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def document_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("\u3000", " ").strip())


def _richness_source_text(item: SourceItem) -> str:
    """Return source text used for card-density checks.

    URLs in an AIHOT summary are links, not useful spoken/card copy. They are
    excluded from the length baseline while all title/summary facts remain
    available to the editor.
    """

    summary = re.sub(r"https?://\S+", "", item.summary or "")
    return _clean(summary)


def _normalise_inline_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _source_item_for_input(item: SourceItem, selection_metadata: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    # ``reason`` is intentionally excluded: it is an editorial signal from
    # AIHOT, not a source fact that may be narrated or shown on a card.
    result = {
        "id": item.item_id,
        "title": item.title,
        "summary": item.summary,
        "originalTitle": item.original_title,
        "category": item.category,
        "source": {"name": item.source_name},
        "links": {"aihot": item.aihot_url, "original": item.original_url},
        "publishedAt": item.published_at,
        "discoveredAt": item.discovered_at,
        "score": item.score,
        "selected": item.selected,
    }
    metadata = dict((selection_metadata or {}).get(item.item_id) or {})
    if metadata:
        result["selection_meta"] = metadata
    return result


def build_editorial_input(
    response_url: str,
    selection: SelectionResult,
    *,
    run_date: date,
    etag: str | None = None,
    source_details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "version": EDITORIAL_INPUT_VERSION,
        "prompt_version": EDITORIAL_PROMPT_VERSION,
        "date": run_date.isoformat(),
        "source": {"url": response_url, "etag": etag, "window": "24h", "mode": "selected"},
        "selection": {
            "mode": selection.mode,
            "eligible_count": selection.eligible_count,
            "selected_count": len(selection.items),
            "item_ids": [item.item_id for item in selection.items],
            "policy": dict(selection.policy),
            "policy_sha256": document_sha256(dict(selection.policy)),
        },
        "items": [_source_item_for_input(item, selection.selection_metadata) for item in selection.items],
    }
    if source_details:
        body["source_details"] = json.loads(json.dumps(source_details, ensure_ascii=False))
    body["input_sha256"] = document_sha256(body)
    return body


def write_editorial_input(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _normalised_source_text(item: SourceItem, editorial_input: Mapping[str, Any] | None = None) -> str:
    detail = detail_text_for_item(editorial_input or {}, item.item_id)
    return re.sub(r"\s+", "", "\n".join(part for part in (item.source_text(), detail) if part))


def _source_field_text(item: SourceItem, field: str, editorial_input: Mapping[str, Any] | None = None) -> str:
    if field == "detail":
        return detail_text_for_item(editorial_input or {}, item.item_id)
    return {
        "title": item.title,
        "summary": item.summary or "",
        "originalTitle": item.original_title or "",
    }.get(field, "")


def _contains_source_text(item: SourceItem, text: str, editorial_input: Mapping[str, Any] | None = None, field: str | None = None) -> bool:
    candidate = re.sub(r"\s+", "", _clean(text))
    source = _source_field_text(item, field, editorial_input) if field else _normalised_source_text(item, editorial_input)
    return bool(candidate) and candidate in re.sub(r"\s+", "", source)


def _source_copy_violation(item: SourceItem, text: str, editorial_input: Mapping[str, Any] | None = None) -> bool:
    """Detect verbatim source copy while allowing short factual tokens.

    The writer may retain names and numbers, but narration/card prose must be
    rewritten into a clear spoken sentence. Exact source fields/sentences are
    rejected at any length; a long contiguous source span is also rejected.
    """

    candidate = re.sub(r"[\s，。！？!?；;：:,.、\"“”‘’()（）\[\]【】]", "", normalize_display_text(text)).casefold()
    if not candidate:
        return False
    source_parts = [item.title, item.original_title or ""]
    source_parts.extend(re.split(r"(?<=[。！？!?；;])", item.summary or ""))
    detail = detail_text_for_item(editorial_input or {}, item.item_id)
    source_parts.extend(re.split(r"(?<=[。！？!?；;])", detail))
    for part in source_parts:
        normalized = re.sub(r"[\s，。！？!?；;：:,.、\"“”‘’()（）\[\]【】]", "", normalize_display_text(part)).casefold()
        if normalized and candidate == normalized:
            return True
        if len(candidate) >= 18 and candidate in normalized:
            return True
    return False


def _validate_writer_header(plan: Mapping[str, Any], errors: list[str]) -> None:
    writer = plan.get("writer")
    if not isinstance(writer, Mapping):
        errors.append("editorial plan is missing writer skill metadata")
        return
    if str(writer.get("skill") or "ai-brief-editorial-writer") != "ai-brief-editorial-writer":
        errors.append("editorial plan writer.skill must be ai-brief-editorial-writer")
    if str(writer.get("version") or "") not in {"2.0", "3.0", "4.0", "4.1"}:
        errors.append("editorial plan writer.version is missing or stale")
    if str(writer.get("status") or "") not in {"approved", "finalized"}:
        errors.append("editorial plan writer.status must be approved or finalized; draft scaffolds cannot render")
    if str(writer.get("source") or "").casefold() == "deterministic-scaffold":
        errors.append("deterministic editorial scaffold cannot be rendered; rewrite it with the editorial writer skill")


def _tokens(text: str) -> set[str]:
    canonical = normalize_display_text(text)
    ascii_tokens = {token.rstrip(".。") for token in _ASCII_TOKEN_RE.findall(canonical)}
    number_tokens = {token.replace(" ", "") for token in _NUMBER_RE.findall(canonical)}
    return ascii_tokens | number_tokens


def _generated_texts(story: Mapping[str, Any]) -> Iterable[tuple[str, str, list[str]]]:
    title_claims = [str(value) for value in story.get("title_claim_ids", []) if value]
    yield "title", _clean(story.get("title")), title_claims
    if story.get("subject"):
        yield "subject", _clean(story.get("subject")), title_claims
    if story.get("navigation_title"):
        yield "navigation_title", _clean(story.get("navigation_title")), title_claims
    if story.get("overview_text"):
        yield "overview_text", _clean(story.get("overview_text")), [str(value) for value in story.get("overview_claim_ids", []) if value]
    narration = story.get("narration")
    if isinstance(narration, Mapping):
        for index, beat in enumerate(narration.get("beats") or []):
            if isinstance(beat, Mapping):
                yield f"narration.beats[{index}]", _clean(beat.get("text")), [str(value) for value in beat.get("claim_ids", []) if value]
    for index, card in enumerate(story.get("cards") or []):
        if not isinstance(card, Mapping):
            continue
        claim_ids = [str(value) for value in card.get("claim_ids", []) if value]
        if card.get("subject"):
            yield f"cards[{index}].subject", _clean(card.get("subject")), claim_ids
        for field in ("label", "headline", "body", "metric"):
            if card.get(field):
                yield f"cards[{index}].{field}", _clean(card.get(field)), claim_ids


def _story_source_ids(story: Mapping[str, Any]) -> list[str]:
    values = story.get("source_item_ids")
    if isinstance(values, list):
        return [str(value) for value in values if str(value)]
    legacy = story.get("source_item_id")
    return [str(legacy)] if legacy else []


def validate_editorial_plan(plan: Mapping[str, Any], editorial_input: Mapping[str, Any], source_items: Mapping[str, SourceItem]) -> list[str]:
    errors: list[str] = []
    legacy_plan = plan.get("version") == "2.0"
    strict_plan = plan.get("version") == EDITORIAL_PLAN_VERSION
    v4_plan = plan.get("version") == "4.0"
    v3_plan = plan.get("version") == "3.0"
    semantic_plan = strict_plan or v4_plan
    if plan.get("version") not in {EDITORIAL_PLAN_VERSION, "4.0", "3.0", "2.0"}:
        errors.append(f"unsupported editorial plan version: {plan.get('version')!r}")
    input_hash = str(editorial_input.get("input_sha256") or "")
    if not input_hash or plan.get("input_sha256") != input_hash:
        errors.append("editorial plan input_sha256 does not match frozen editorial input")
    if plan.get("prompt_version") not in {EDITORIAL_PROMPT_VERSION, "codex-editorial-v5", "codex-editorial-v4", "codex-editorial-v3"}:
        errors.append("editorial plan prompt_version is missing or stale")
    if not legacy_plan:
        _validate_writer_header(plan, errors)
    stories = plan.get("stories")
    if not isinstance(stories, list):
        return errors + ["editorial plan has no stories list"]
    minimum_scenes = 1 if strict_plan else EDITORIAL_MIN_STORIES
    if not minimum_scenes <= len(stories) <= EDITORIAL_MAX_STORIES:
        errors.append(f"editorial plan must contain {minimum_scenes}-{EDITORIAL_MAX_STORIES} stories")

    selected_ids = [str(value) for value in editorial_input.get("selection", {}).get("item_ids", [])]
    selected_set = set(selected_ids)
    selection_meta_by_id = {
        str(item.get("id")): item.get("selection_meta") or {}
        for item in editorial_input.get("items") or []
        if isinstance(item, Mapping) and item.get("id")
    }
    covered_ids: list[str] = []
    event_keys: set[str] = set()
    claim_ids_global: set[str] = set()
    for story_index, story in enumerate(stories):
        prefix = f"stories[{story_index}]"
        if not isinstance(story, Mapping):
            errors.append(f"{prefix} is not an object")
            continue
        source_ids = _story_source_ids(story)
        if not source_ids:
            errors.append(f"{prefix} has no source_item_ids")
        if len(set(source_ids)) != len(source_ids):
            errors.append(f"{prefix} repeats a source_item_id")
        for item_id in source_ids:
            if item_id not in source_items:
                errors.append(f"{prefix} references unknown source item {item_id}")
            covered_ids.append(item_id)
        story_kind = _clean(story.get("story_kind") or "single")
        if story_kind not in {"single", "brief_group"}:
            errors.append(f"{prefix}.story_kind must be single or brief_group")
        if strict_plan and story_kind == "brief_group":
            if not 2 <= len(source_ids) <= 4:
                errors.append(f"{prefix} brief_group must contain 2-4 source items")
            categories = {source_items[item_id].category for item_id in source_ids if item_id in source_items}
            if len(categories) > 1:
                errors.append(f"{prefix} brief_group source items must share one dimension")
            if not _clean(story.get("group_label")):
                errors.append(f"{prefix} brief_group has no group_label")
            if story.get("cards_per_page") not in {None, 4}:
                errors.append(f"{prefix} brief_group cards_per_page must be 4")
            if any(str(selection_meta_by_id.get(item_id, {}).get("rank")) == "1" for item_id in source_ids):
                errors.append(f"{prefix} dimension leaders must remain standalone")
        event_key = _clean(story.get("event_key"))
        if not event_key:
            errors.append(f"{prefix} has no event_key for duplicate-event protection")
        elif event_key in event_keys:
            errors.append(f"{prefix} reuses event_key {event_key!r}; merge duplicate event sources")
        else:
            event_keys.add(event_key)
        category = _clean(story.get("category"))
        if not category:
            errors.append(f"{prefix} has no category")
        title = _clean(story.get("title"))
        if not title:
            errors.append(f"{prefix} has no title")
        if semantic_plan:
            subject = _clean(story.get("subject"))
            navigation_title = _clean(story.get("navigation_title"))
            presentation_order = story.get("presentation_order")
            if not subject:
                errors.append(f"{prefix} has no explicit subject")
            elif story_kind != "brief_group" and not any(
                _contains_source_text(source_items[item_id], subject, editorial_input)
                for item_id in source_ids
                if item_id in source_items
            ):
                errors.append(f"{prefix}.subject is not present in its source")
            if not navigation_title:
                errors.append(f"{prefix} has no navigation_title")
            elif subject and re.sub(r"\s+", "", subject).casefold() not in re.sub(r"\s+", "", navigation_title).casefold():
                errors.append(f"{prefix}.navigation_title must name the subject")
            elif strict_plan:
                remainder = _normalise_inline_text(navigation_title).replace(_normalise_inline_text(subject), "", 1).strip("：:，,。！？!? -—")
                if not remainder or re.fullmatch(r"(?:事件|解读|引争议|争议|动态|最新动态|安全|进展|消息|变化)", remainder):
                    errors.append(f"{prefix}.navigation_title must state a concrete subject change")
                if "…" in navigation_title or "..." in navigation_title:
                    errors.append(f"{prefix}.navigation_title must be complete and cannot use ellipsis")
            if isinstance(presentation_order, bool) or not isinstance(presentation_order, int) or presentation_order < 1:
                errors.append(f"{prefix}.presentation_order must be a positive integer")
            if strict_plan:
                overview_text = _clean(story.get("overview_text"))
                overview_claim_ids = story.get("overview_claim_ids")
                if not overview_text:
                    errors.append(f"{prefix} has no overview_text")
                elif subject and story_kind != "brief_group" and _normalise_inline_text(subject) not in _normalise_inline_text(overview_text):
                    errors.append(f"{prefix}.overview_text must name the subject")
                elif _normalise_inline_text(overview_text) == _normalise_inline_text(navigation_title):
                    errors.append(f"{prefix}.overview_text cannot reuse navigation_title")
                if not isinstance(overview_claim_ids, list) or not overview_claim_ids:
                    errors.append(f"{prefix}.overview_claim_ids must reference summary/detail evidence")
                if story_kind == "brief_group":
                    overview_items = story.get("overview_items")
                    overview_ids = {
                        str(value.get("source_item_id"))
                        for value in overview_items or []
                        if isinstance(value, Mapping)
                    }
                    if not isinstance(overview_items, list) or overview_ids != set(source_ids):
                        errors.append(f"{prefix}.overview_items must cover every grouped source item")
                    else:
                        for overview_index, overview_item in enumerate(overview_items):
                            if not _clean(overview_item.get("text")) or not overview_item.get("claim_ids"):
                                errors.append(f"{prefix}.overview_items[{overview_index}] needs text and claim_ids")

        narration = story.get("narration")
        beats = narration.get("beats") if isinstance(narration, Mapping) else None
        minimum_beats = 2 if strict_plan else 3
        if strict_plan and story_kind == "brief_group":
            minimum_beats = max(2, len(source_ids))
        if not isinstance(beats, list) or len(beats) < minimum_beats:
            errors.append(f"{prefix} narration must contain at least {minimum_beats} beats")
        else:
            beat_types = [str(beat.get("type")) for beat in beats if isinstance(beat, Mapping)]
            if strict_plan:
                if not {"fact", "evidence", "context", "result"}.intersection(beat_types):
                    errors.append(f"{prefix} narration must include a source-supported factual beat")
            else:
                if not {"hook", "fact"}.issubset(set(beat_types)):
                    errors.append(f"{prefix} narration must include hook and fact beats")
                if not {"impact", "action"}.intersection(beat_types):
                    errors.append(f"{prefix} narration must include an impact or source-supported action beat")
            narration_chars = sum(len(_clean(beat.get("text"))) for beat in beats if isinstance(beat, Mapping))
            if not legacy_plan and not strict_plan and not NARRATION_MIN_CHARS <= narration_chars <= NARRATION_MAX_CHARS:
                errors.append(f"{prefix} narration length {narration_chars} is outside {NARRATION_MIN_CHARS}-{NARRATION_MAX_CHARS}")
            if len({_clean(beat.get("text")) for beat in beats if isinstance(beat, Mapping)}) < minimum_beats:
                errors.append(f"{prefix} narration repeats beat text")
            beat_ids_seen: set[str] = set()
            visual_beat_indexes: list[int] = []
            visual_asset_ids: list[str] = []
            for beat_index, beat in enumerate(beats):
                if legacy_plan:
                    continue
                beat_prefix = f"{prefix}.narration.beats[{beat_index}]"
                if not isinstance(beat, Mapping):
                    errors.append(f"{beat_prefix} is not an object")
                    continue
                beat_text = normalize_display_text(beat.get("text"))
                if not beat_text or not re.search(r"[。！？!?；;]$", beat_text):
                    errors.append(f"{beat_prefix} must end in terminal punctuation")
                beat_type = _clean(beat.get("type"))
                if beat_type not in ALLOWED_BEATS:
                    errors.append(f"{beat_prefix} has unsupported type {beat_type!r}")
                if v4_plan or v3_plan:
                    if len(split_caption_sentences(beat_text)) != 1:
                        errors.append(f"{beat_prefix} must contain exactly one sentence")
                    if caption_visible_units(beat_text) > CAPTION_MAX_VISIBLE_UNITS:
                        errors.append(f"{beat_prefix} exceeds single-line caption capacity of {CAPTION_MAX_VISIBLE_UNITS}")
                    card_ids = beat.get("card_ids")
                    if not isinstance(card_ids, list) or not card_ids or any(not _clean(value) for value in card_ids):
                        errors.append(f"{beat_prefix} must map to one or more card_ids")
                if strict_plan:
                    beat_id = _clean(beat.get("beat_id"))
                    if not beat_id:
                        errors.append(f"{beat_prefix} has no stable beat_id")
                    elif beat_id in beat_ids_seen:
                        errors.append(f"{beat_prefix} repeats beat_id {beat_id!r}")
                    else:
                        beat_ids_seen.add(beat_id)
                    visual_asset_id = _clean(beat.get("visual_asset_id"))
                    if visual_asset_id:
                        visual_beat_indexes.append(beat_index)
                        visual_asset_ids.append(visual_asset_id)
            if strict_plan:
                if visual_beat_indexes and visual_beat_indexes[0] == 0:
                    errors.append(f"{prefix} source visual beats must follow a card-first beat")
                if len(visual_asset_ids) != len(set(visual_asset_ids)):
                    errors.append(f"{prefix} source visual asset IDs must be unique")
                visual_limit = MAX_V5_VISUALS_PER_STORY
                try:
                    from .screenshots import source_kind
                    if source_ids and all(source_kind(source_items[item_id].original_url) == "x" for item_id in source_ids if item_id in source_items):
                        visual_limit = 1
                except ImportError:
                    pass
                if len(visual_asset_ids) > visual_limit:
                    errors.append(f"{prefix} may use at most {visual_limit} source visual beat(s) for its source kind")
        layout = story.get("layout") if isinstance(story.get("layout"), Mapping) else {}
        layout_density = _clean(layout.get("density")) or "regular"
        cards = story.get("cards")
        valid_card_count = isinstance(cards, list) and ((len(cards) >= 1) if strict_plan else (CARD_MIN_COUNT <= len(cards) <= CARD_MAX_COUNT))
        if not valid_card_count:
            errors.append(f"{prefix} cards must contain at least 1 card" if strict_plan else f"{prefix} cards must contain {CARD_MIN_COUNT}-{CARD_MAX_COUNT} cards")
        else:
            if not strict_plan and layout_density == "dense" and len(cards) < DENSE_CARD_MIN_COUNT:
                errors.append(f"{prefix} dense layout must contain {DENSE_CARD_MIN_COUNT}-{CARD_MAX_COUNT} cards")
            roles: set[str] = set()
            bodies: set[str] = set()
            body_lengths: list[int] = []
            card_ids_seen: set[str] = set()
            for card_index, card in enumerate(cards):
                card_prefix = f"{prefix}.cards[{card_index}]"
                if not isinstance(card, Mapping):
                    errors.append(f"{card_prefix} is not an object")
                    continue
                if semantic_plan:
                    card_id = _clean(card.get("id"))
                    if not card_id:
                        errors.append(f"{card_prefix} has no id")
                    elif card_id in card_ids_seen:
                        errors.append(f"{card_prefix} repeats id {card_id!r}")
                    else:
                        card_ids_seen.add(card_id)
                    if not _clean(card.get("subject")):
                        errors.append(f"{card_prefix} has no explicit subject")
                    if strict_plan and story_kind == "brief_group":
                        if _clean(card.get("source_item_id")) not in source_ids:
                            errors.append(f"{card_prefix}.source_item_id must identify one grouped source item")
                role = _clean(card.get("role"))
                if role not in ALLOWED_CARD_ROLES:
                    errors.append(f"{card_prefix} has unsupported role {role!r}")
                roles.add(role)
                body = _clean(card.get("body"))
                if not body:
                    errors.append(f"{card_prefix} has empty body")
                body_lengths.append(len(body))
                normal_body = re.sub(r"\s+", "", body)
                if normal_body in bodies:
                    errors.append(f"{card_prefix} repeats another card body")
                bodies.add(normal_body)
                span = card.get("span", 2)
                if isinstance(span, bool) or not isinstance(span, int) or span not in ALLOWED_CARD_SPANS:
                    errors.append(f"{card_prefix} has invalid span")
                metric = _clean(card.get("metric"))
                if metric:
                    metric_normal = _normalise_inline_text(metric)
                    body_normal = _normalise_inline_text(body)
                    headline_normal = _normalise_inline_text(card.get("headline"))
                    label_normal = _normalise_inline_text(card.get("label"))
                    if body_normal.count(metric_normal) != 1:
                        errors.append(f"{card_prefix}.metric must occur exactly once in the card body")
                    if metric_normal in headline_normal or metric_normal in label_normal:
                        errors.append(f"{card_prefix}.metric must not repeat in the label or headline")
            if not strict_plan and layout_density == "dense" and body_lengths:
                source_lengths = [
                    len(_richness_source_text(source_items[item_id]))
                    for item_id in source_ids
                    if item_id in source_items and _richness_source_text(source_items[item_id])
                ]
                if source_lengths:
                    target = min(DENSE_CARD_COVERAGE_CAP, math.ceil(max(source_lengths) * DENSE_CARD_COVERAGE_RATIO))
                    body_total = sum(body_lengths)
                    if body_total < target:
                        errors.append(f"{prefix} dense card bodies cover {body_total} chars; need at least {target}")
                for card_index, body_length in enumerate(body_lengths):
                    if not DENSE_CARD_BODY_MIN_CHARS <= body_length <= DENSE_CARD_BODY_MAX_CHARS:
                        errors.append(
                            f"{prefix}.cards[{card_index}].body length {body_length} is outside "
                            f"{DENSE_CARD_BODY_MIN_CHARS}-{DENSE_CARD_BODY_MAX_CHARS}"
                        )
                expected_span = 3 if len(cards) == 4 else 2 if len(cards) == 3 else None
                if expected_span is not None:
                    for card_index, card in enumerate(cards):
                        if isinstance(card, Mapping) and card.get("span", 2) != expected_span:
                            errors.append(f"{prefix}.cards[{card_index}] dense layout requires span {expected_span}")
            if not strict_plan and len(roles) < 2:
                errors.append(f"{prefix} cards need at least two distinct information roles")
            if v4_plan and isinstance(beats, list):
                mapped_ids: list[str] = []
                for beat in beats:
                    if isinstance(beat, Mapping):
                        mapped_ids.extend(_clean(value) for value in beat.get("card_ids") or [])
                unknown_cards = sorted(set(mapped_ids) - card_ids_seen)
                if unknown_cards:
                    errors.append(f"{prefix} beats reference unknown card_ids: {unknown_cards}")
                missing_cards = sorted(card_ids_seen - set(mapped_ids))
                if missing_cards:
                    errors.append(f"{prefix} cards are never spoken: {missing_cards}")
            if strict_plan and story_kind == "brief_group":
                card_source_ids = [_clean(card.get("source_item_id")) for card in cards if isinstance(card, Mapping)]
                if len(cards) != len(source_ids) or set(card_source_ids) != set(source_ids) or len(card_source_ids) != len(set(card_source_ids)):
                    errors.append(f"{prefix} brief_group requires exactly one card per source item")
                mapped_ids = [
                    _clean(value)
                    for beat in beats or []
                    if isinstance(beat, Mapping)
                    for value in beat.get("card_ids") or []
                ]
                if set(mapped_ids) != card_ids_seen:
                    errors.append(f"{prefix} brief_group beats must map every card")
                if any(
                    len([str(value) for value in beat.get("card_ids") or [] if _clean(value)]) != 1
                    for beat in beats or []
                    if isinstance(beat, Mapping)
                ):
                    errors.append(f"{prefix} brief_group requires one beat per card")

        claims = story.get("claims")
        claims_by_id: dict[str, Mapping[str, Any]] = {}
        if not isinstance(claims, list) or not claims:
            errors.append(f"{prefix} has no source claims")
        else:
            for claim_index, claim in enumerate(claims):
                claim_prefix = f"{prefix}.claims[{claim_index}]"
                if not isinstance(claim, Mapping):
                    errors.append(f"{claim_prefix} is not an object")
                    continue
                claim_id = _clean(claim.get("id"))
                claim_item_id = _clean(claim.get("source_item_id"))
                field = _clean(claim.get("source_field"))
                text = _clean(claim.get("source_text"))
                if not claim_id or claim_id in claim_ids_global:
                    errors.append(f"{claim_prefix} has a missing or duplicate id")
                else:
                    claim_ids_global.add(claim_id)
                    claims_by_id[claim_id] = claim
                if claim_item_id not in source_ids:
                    errors.append(f"{claim_prefix} source_item_id is not part of its story")
                item = source_items.get(claim_item_id)
                if item is None or field not in {"title", "summary", "originalTitle", "detail"} or not text:
                    errors.append(f"{claim_prefix} has invalid source evidence")
                elif field == "detail" and not detail_text_for_item(editorial_input, claim_item_id):
                    errors.append(f"{claim_prefix} references detail without a frozen source detail snapshot")
                elif not _contains_source_text(item, text, editorial_input, field):
                    errors.append(f"{claim_prefix} source_text is not an exact fragment of the source")

            if strict_plan:
                overview_refs = [str(value) for value in story.get("overview_claim_ids") or [] if value]
                evidence_fields = {
                    _clean(claims_by_id[claim_id].get("source_field"))
                    for claim_id in overview_refs
                    if claim_id in claims_by_id
                }
                if not evidence_fields.intersection({"summary", "detail"}):
                    errors.append(f"{prefix}.overview_claim_ids must include summary or detail evidence")
                if story_kind == "brief_group":
                    for overview_index, overview_item in enumerate(story.get("overview_items") or []):
                        if not isinstance(overview_item, Mapping):
                            continue
                        overview_item_id = _clean(overview_item.get("source_item_id"))
                        overview_text = _clean(overview_item.get("text"))
                        overview_refs = [_clean(value) for value in overview_item.get("claim_ids") or [] if _clean(value)]
                        if any(
                            ref not in claims_by_id or _clean(claims_by_id[ref].get("source_item_id")) != overview_item_id
                            for ref in overview_refs
                        ):
                            errors.append(f"{prefix}.overview_items[{overview_index}] has invalid claim_ids")
                        if overview_item_id in source_items:
                            extra_tokens = _tokens(overview_text) - _tokens(
                                " ".join(part for part in (source_items[overview_item_id].source_text(), detail_text_for_item(editorial_input, overview_item_id)) if part)
                            )
                            if extra_tokens:
                                errors.append(f"{prefix}.overview_items[{overview_index}] adds unsupported tokens: {sorted(extra_tokens)}")

        source_text = " ".join(
            " ".join(part for part in (source_items[item_id].source_text(), detail_text_for_item(editorial_input, item_id)) if part)
            for item_id in source_ids
            if item_id in source_items
        )
        source_tokens = _tokens(source_text)
        for generated_path, generated_text, refs in _generated_texts(story):
            if not generated_text:
                errors.append(f"{prefix}.{generated_path} is empty")
                continue
            if generated_text != normalize_display_text(generated_text):
                errors.append(f"{prefix}.{generated_path} uses non-canonical grouped number separators")
            if not refs:
                errors.append(f"{prefix}.{generated_path} has no claim_ids")
            unknown_refs = [ref for ref in refs if ref not in claims_by_id]
            if unknown_refs:
                errors.append(f"{prefix}.{generated_path} references unknown claims: {unknown_refs}")
            extra_tokens = _tokens(generated_text) - source_tokens
            if extra_tokens:
                errors.append(f"{prefix}.{generated_path} adds unsupported tokens: {sorted(extra_tokens)}")
            for item_id in source_ids:
                if legacy_plan:
                    break
                item = source_items.get(item_id)
                if item is not None and _source_copy_violation(item, generated_text, editorial_input):
                    errors.append(f"{prefix}.{generated_path} copies AIHOT source prose; rewrite it in the editorial writer skill")
                    break

    if len(covered_ids) != len(set(covered_ids)):
        errors.append("a source item is assigned to more than one story; merge duplicate events")
    if set(covered_ids) != selected_set:
        missing = sorted(selected_set - set(covered_ids))
        extra = sorted(set(covered_ids) - selected_set)
        if missing:
            errors.append(f"editorial plan dropped selected source items: {missing}")
        if extra:
            errors.append(f"editorial plan added unselected source items: {extra}")
    if semantic_plan:
        orders = [story.get("presentation_order") for story in stories if isinstance(story, Mapping)]
        if len(orders) != len(set(orders)) or set(orders) != set(range(1, len(stories) + 1)):
            errors.append("editorial plan presentation_order must be a complete 1..N sequence")
    if strict_plan:
        navigation_units = sum(caption_visible_units(_clean(story.get("navigation_title"))) for story in stories if isinstance(story, Mapping))
        if navigation_units > 100:
            errors.append("editorial plan navigation titles cannot fit the single top rail at minimum font size")
    return errors


def load_editorial_plan(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EditorialPlanError(f"could not read editorial plan: {path}") from exc
    if not isinstance(value, dict):
        raise EditorialPlanError("editorial plan must be a JSON object")
    return value


def build_editorial_quality_report(
    plan: Mapping[str, Any],
    editorial_input: Mapping[str, Any],
    source_items: Mapping[str, SourceItem],
) -> dict[str, Any]:
    """Persistable pre-TTS quality gates for the authored plan."""

    errors = validate_editorial_plan(plan, editorial_input, source_items)
    story_reports: list[dict[str, Any]] = []
    for index, story in enumerate(plan.get("stories") or [], 1):
        if not isinstance(story, Mapping):
            continue
        beats = (story.get("narration") or {}).get("beats") if isinstance(story.get("narration"), Mapping) else []
        units = []
        for beat in beats or []:
            if isinstance(beat, Mapping):
                text = normalize_display_text(beat.get("text"))
                units.append({"type": beat.get("type"), "visible_units": caption_visible_units(text), "complete_sentence": bool(re.search(r"[。！？!?；;]$", text))})
        story_reports.append({"story_index": index, "caption_unit_count": len(units), "caption_units": units})
    return {
        "version": "1.0",
        "status": "pass" if not errors else "fail",
        "gates": {
            "writer_skill_approved": not any("writer.status" in error or "deterministic editorial scaffold" in error for error in errors),
            "source_copy_rejected": not any("copies AIHOT source prose" in error for error in errors),
            "caption_ready": not any("caption" in error or "sentence" in error for error in errors),
            "display_numbers_canonical": not any("unsupported tokens" in error for error in errors),
        },
        "errors": errors,
        "stories": story_reports,
        "pre_tts_gate": "pass" if not errors else "blocked",
    }
