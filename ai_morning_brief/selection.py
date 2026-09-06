from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from .config import (
    DEFAULT_SELECTION_HEAD_SHARE,
    DEFAULT_SELECTION_MAX_ITEMS,
    DEFAULT_SELECTION_SOFT_MIN,
    EDITORIAL_DIMENSION_LABELS,
    EDITORIAL_DIMENSIONS,
)
from .models import SelectionResult, SourceItem


@dataclass(frozen=True)
class SelectionPolicy:
    """Adaptive policy for a frozen, four-dimension AIHOT candidate pool."""

    dimensions: tuple[str, ...] = EDITORIAL_DIMENSIONS
    soft_min: int = DEFAULT_SELECTION_SOFT_MIN
    max_items: int = DEFAULT_SELECTION_MAX_ITEMS
    minimum_items: int = 3
    head_share: float = DEFAULT_SELECTION_HEAD_SHARE

    def __post_init__(self) -> None:
        if not self.dimensions:
            raise ValueError("selection policy needs at least one dimension")
        if self.minimum_items < 1 or self.soft_min < self.minimum_items:
            raise ValueError("selection minimums are invalid")
        if self.max_items < self.soft_min:
            raise ValueError("selection max_items must be >= soft_min")
        if not 0 < self.head_share <= 1:
            raise ValueError("selection head_share must be in (0, 1]")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["dimensions"] = list(self.dimensions)
        return value


def _numeric_score(item: SourceItem) -> float | None:
    if item.score is None or item.score == "":
        return None
    try:
        return float(item.score)
    except (TypeError, ValueError):
        return None


def _head_count(candidate_count: int, head_share: float) -> int:
    if candidate_count <= 2:
        return candidate_count
    return min(4, max(2, math.ceil(candidate_count * head_share)))


def _ranked_candidates(items: Iterable[SourceItem], dimension: str) -> list[tuple[SourceItem, int]]:
    unique: list[tuple[SourceItem, int]] = []
    seen: set[str] = set()
    for api_index, item in enumerate(items):
        if item.category != dimension or not item.selected or not item.title.strip() or item.item_id in seen:
            continue
        seen.add(item.item_id)
        unique.append((item, api_index))
    # Score is compared only within this dimension. Missing scores remain
    # eligible and retain API order after scored candidates.
    unique.sort(
        key=lambda pair: (
            0 if _numeric_score(pair[0]) is not None else 1,
            -(_numeric_score(pair[0]) or 0),
            pair[1],
        )
    )
    return unique


def select_items_by_dimension(
    candidates_by_dimension: Mapping[str, Iterable[SourceItem]],
    *,
    policy: SelectionPolicy | None = None,
) -> SelectionResult:
    """Select relative leaders and retain an auditable decision record.

    The result contains raw scores and links for audit/release use. Those
    fields are deliberately not used as video-card content.
    """

    policy = policy or SelectionPolicy()
    ranked: dict[str, list[tuple[SourceItem, int]]] = {}
    records: dict[str, dict[str, Any]] = {}
    all_unique: dict[str, SourceItem] = {}
    for dimension in policy.dimensions:
        dimension_items = _ranked_candidates(candidates_by_dimension.get(dimension, ()), dimension)
        ranked[dimension] = dimension_items
        count = len(dimension_items)
        head = _head_count(count, policy.head_share)
        for rank, (item, api_index) in enumerate(dimension_items, 1):
            score = _numeric_score(item)
            percentile = 1.0 if count == 1 else round(1 - ((rank - 1) / count), 6)
            records[item.item_id] = {
                "item_id": item.item_id,
                "dimension": dimension,
                "dimension_label": EDITORIAL_DIMENSION_LABELS.get(dimension, dimension),
                "api_index": api_index,
                "rank": rank,
                "candidate_count": count,
                "rank_percentile": percentile,
                "tier": "head" if rank <= head else "reserve",
                "score": item.score,
                "score_numeric": score,
                "selected": False,
                "status": "candidate",
                "reason": None,
                "links": {"aihot": item.aihot_url, "original": item.original_url},
            }
            all_unique[item.item_id] = item

    selected_ids: list[str] = []

    def add(item: SourceItem, reason: str) -> None:
        if item.item_id in selected_ids or len(selected_ids) >= policy.max_items:
            return
        selected_ids.append(item.item_id)
        record = records[item.item_id]
        record["selected"] = True
        record["status"] = "selected"
        record["reason"] = reason

    # First guarantee every non-empty dimension's leader, then interleave
    # relative-head candidates by rank to avoid one busy dimension dominating.
    for dimension in policy.dimensions:
        if ranked[dimension]:
            add(ranked[dimension][0][0], "dimension_leader")
    max_head_rank = max((_head_count(len(value), policy.head_share) for value in ranked.values()), default=0)
    for rank in range(2, max_head_rank + 1):
        for dimension in policy.dimensions:
            values = ranked[dimension]
            if rank <= _head_count(len(values), policy.head_share):
                add(values[rank - 1][0], "relative_head")

    # A sparse day may not have enough head candidates. Fill from the reserve
    # in the same round-robin order until the soft target or hard cap.
    if len(selected_ids) < policy.soft_min:
        max_rank = max((len(value) for value in ranked.values()), default=0)
        for rank in range(1, max_rank + 1):
            for dimension in policy.dimensions:
                values = ranked[dimension]
                if rank <= len(values):
                    add(values[rank - 1][0], "reserve_fill")
                    if len(selected_ids) >= policy.soft_min:
                        break
            if len(selected_ids) >= policy.soft_min:
                break

    selected = tuple(all_unique[item_id] for item_id in selected_ids)
    counts = Counter(item.category or "other" for item in selected)
    if len(selected) < policy.minimum_items:
        mode = "failure"
        reason = f"only {len(selected)} eligible AIHOT items; at least {policy.minimum_items} required"
    elif len(selected) < policy.soft_min:
        mode = "short"
        reason = f"low-volume day with {len(selected)} relative leaders/reserve items"
    else:
        mode = "normal"
        reason = None
    for record in records.values():
        if not record["selected"]:
            record["status"] = "rejected"
            record["reason"] = "outside_relative_head_and_reserve_capacity"
    return SelectionResult(
        items=selected,
        mode=mode,
        category_counts=dict(sorted(counts.items())),
        eligible_count=len(all_unique),
        reason=reason,
        selection_metadata=records,
        policy=policy.to_dict(),
    )


def select_items(
    items: Iterable[SourceItem],
    *,
    max_items: int = 8,
    minimum_items: int = 3,
    max_per_category: int = 2,
) -> SelectionResult:
    """Legacy flat-fixture selector kept for v1-v4 tests and replays."""

    unique: list[SourceItem] = []
    seen: set[str] = set()
    for item in items:
        if not item.selected or not item.title.strip() or item.item_id in seen:
            continue
        seen.add(item.item_id)
        unique.append(item)

    selected: list[SourceItem] = []
    counts: Counter[str] = Counter()
    for item in unique:
        category = item.category or "other"
        if counts[category] >= max_per_category:
            continue
        selected.append(item)
        counts[category] += 1
        if len(selected) >= max_items:
            break
    if len(selected) < max_items:
        selected_ids = {item.item_id for item in selected}
        for item in unique:
            if item.item_id in selected_ids:
                continue
            selected.append(item)
            counts[item.category or "other"] += 1
            if len(selected) >= max_items:
                break

    if len(selected) < minimum_items:
        mode = "failure"
        reason = f"only {len(selected)} eligible AIHOT items; at least {minimum_items} required"
    elif len(selected) < 6:
        mode = "short"
        reason = f"low-volume day with {len(selected)} eligible AIHOT items"
    else:
        mode = "normal"
        reason = None
    return SelectionResult(
        items=tuple(selected),
        mode=mode,
        category_counts=dict(sorted(counts.items())),
        eligible_count=len(unique),
        reason=reason,
    )
