#!/usr/bin/env python3
"""Prepare and record native full-cover GPT Image outputs for AI每日早报.

This helper never calls a model or the network. It freezes source-grounded
copy, reference-image roles, and one complete-cover prompt per ratio, then
records the first generated files without image decoding or post-processing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 5
PROMPT_VERSION = "ai-brief-cover-full-image-v5"
MODEL_NAME = "Codex built-in GPT Image"
SKILL_DIR = Path(__file__).resolve().parents[1]
REGISTRY_PATH = SKILL_DIR / "references" / "logo_manifest.json"
STYLE_REFERENCE_PATH = SKILL_DIR / "assets" / "references" / "cover-style-system-16x9.png"
MAX_EDITION_BRANDS = 6

STANDARD_RATIOS: dict[str, dict[str, Any]] = {
    "16:9": {
        "size": (1920, 1080),
        "platforms": ["Bilibili 横版封面", "横屏视频", "网页或资讯卡片"],
    },
    "3:4": {
        "size": (1080, 1440),
        "platforms": ["小红书首图", "微信公众号内嵌海报", "竖版资讯卡"],
    },
    "9:16": {
        "size": (1080, 1920),
        "platforms": ["抖音竖屏封面", "快手竖屏封面", "视频号竖屏封面"],
    },
}
DEFAULT_COVER_RATIOS = ("16:9", "3:4", "9:16")

ACTION_TERMS = (
    "起诉", "发布", "接管", "增长", "降低", "突破", "攻破", "训练",
    "索赔", "侵权", "开源", "收购", "融资", "文明", "agent", "model",
)
NUMERIC_RE = re.compile(
    r"\d+(?:\.\d+)?(?:%|％|万|亿|倍|美元|万美元|亿美元|元|GB|TB)?",
    re.IGNORECASE,
)


class CoverWorkflowError(ValueError):
    """Raised when a cover request violates the frozen source contract."""


@dataclass(frozen=True)
class RatioSpec:
    ratio: str
    width: int
    height: int
    slug: str
    platforms: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ratio": self.ratio,
            "requested_size": [self.width, self.height],
            "slug": self.slug,
            "platform_suggestions": list(self.platforms),
        }


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoverWorkflowError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CoverWorkflowError(f"expected a JSON object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def selected_items(editorial: dict[str, Any]) -> list[dict[str, Any]]:
    items = editorial.get("items")
    if not isinstance(items, list):
        raise CoverWorkflowError("editorial_input.json is missing items")
    frozen_ids = editorial.get("selection", {}).get("item_ids") or []
    frozen_set = {str(value) for value in frozen_ids}
    selected = [
        item
        for item in items
        if isinstance(item, dict)
        and (
            (frozen_set and str(item.get("id")) in frozen_set)
            or (not frozen_set and bool(item.get("selected")))
        )
    ]
    if not selected:
        raise CoverWorkflowError("frozen edition has no selected items")
    return selected


def _prepare_registry_entry(entry: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    value = dict(entry)
    relative = value.get("asset")
    value["resolved_asset"] = str((base_dir / str(relative)).resolve()) if relative else None
    return value


def registry_entries() -> list[dict[str, Any]]:
    entries = read_json(REGISTRY_PATH).get("entries")
    if not isinstance(entries, list):
        raise CoverWorkflowError("Logo registry is missing entries")
    return [
        _prepare_registry_entry(entry, SKILL_DIR)
        for entry in entries
        if isinstance(entry, dict)
    ]


def extra_logo_entries(manifest_path: Path | None) -> list[dict[str, Any]]:
    if manifest_path is None:
        return []
    raw_entries = read_json(manifest_path).get("entries")
    if not isinstance(raw_entries, list):
        raise CoverWorkflowError("extra Logo manifest is missing entries")
    prepared: list[dict[str, Any]] = []
    for raw in raw_entries:
        if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
            raise CoverWorkflowError("each extra Logo entry requires a name")
        value = _prepare_registry_entry(raw, manifest_path.parent)
        value["aliases"] = [str(alias) for alias in (value.get("aliases") or [value["name"]])]
        asset_text = value.get("resolved_asset")
        if asset_text:
            asset = Path(str(asset_text))
            if not asset.is_file() or asset.stat().st_size <= 0:
                raise CoverWorkflowError(f"extra Logo reference is missing or empty: {asset}")
            expected_hash = str(value.get("sha256") or "")
            if not expected_hash or sha256_file(asset) != expected_hash:
                raise CoverWorkflowError(f"extra Logo reference hash is missing or incorrect: {asset}")
            if not value.get("source_page_url"):
                raise CoverWorkflowError(f"extra Logo requires an official source URL: {value['name']}")
        else:
            value.setdefault("fallback_status", "plain_text_only")
        prepared.append(value)
    return prepared


def brand_matches(text: str, entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    lines = [line.casefold() for line in text.splitlines()] or [text.casefold()]
    matches: list[tuple[int, int, dict[str, Any]]] = []
    seen: set[str] = set()
    for registry_index, entry in enumerate(entries):
        aliases = [str(value).strip() for value in entry.get("aliases", [])]
        key = str(entry.get("name") or "").casefold()
        line_indexes = [
            line_index
            for line_index, line in enumerate(lines)
            if any(alias and alias.casefold() in line for alias in aliases)
        ]
        if key and key not in seen and line_indexes:
            matches.append((min(line_indexes), registry_index, dict(entry)))
            seen.add(key)
    matches.sort(key=lambda value: (value[0], value[1]))
    return [value[2] for value in matches]


def rank_items(editorial: dict[str, Any]) -> list[dict[str, Any]]:
    entries = registry_entries()
    ranked: list[dict[str, Any]] = []
    for index, item in enumerate(selected_items(editorial)):
        text = f"{item.get('title', '')}\n{item.get('summary', '')}"
        brands = brand_matches(text, entries)
        numeric_anchors = len(NUMERIC_RE.findall(text))
        lowered = text.casefold()
        visual_anchors = sum(1 for term in ACTION_TERMS if term in lowered)
        try:
            impact = float(item.get("score") or 0)
        except (TypeError, ValueError):
            impact = 0.0
        sort_key = (impact, len(brands), numeric_anchors + visual_anchors, -index)
        ranked.append({
            "item_id": str(item.get("id") or ""),
            "frozen_index": index,
            "title": str(item.get("title") or ""),
            "summary": str(item.get("summary") or ""),
            "aihot_score": impact,
            "recognized_brands": [entry["name"] for entry in brands],
            "numeric_anchors": numeric_anchors,
            "visual_anchors": visual_anchors,
            "selection_key": list(sort_key),
        })
    ranked.sort(key=lambda value: tuple(value["selection_key"]), reverse=True)
    for rank, value in enumerate(ranked, start=1):
        value["rank"] = rank
    return ranked


def parse_ratio(value: str) -> RatioSpec:
    raw = value.strip()
    ratio_text, separator, size_text = raw.partition("=")
    match = re.fullmatch(r"(\d+):(\d+)", ratio_text)
    if not match:
        raise CoverWorkflowError(f"invalid ratio {value!r}; expected W:H")
    ratio_w, ratio_h = int(match.group(1)), int(match.group(2))
    if ratio_w <= 0 or ratio_h <= 0:
        raise CoverWorkflowError(f"ratio values must be positive: {value!r}")
    standard = STANDARD_RATIOS.get(ratio_text)
    if standard and not separator:
        width, height = standard["size"]
        platforms = tuple(standard["platforms"])
    else:
        if not separator:
            raise CoverWorkflowError(
                f"custom ratio {ratio_text} requires exact dimensions, e.g. {ratio_text}=1200x1500"
            )
        size_match = re.fullmatch(r"(\d+)[xX](\d+)", size_text)
        if not size_match:
            raise CoverWorkflowError(f"invalid target size in {value!r}; expected WIDTHxHEIGHT")
        width, height = int(size_match.group(1)), int(size_match.group(2))
        expected = ratio_w / ratio_h
        actual = width / height
        if width <= 0 or height <= 0 or abs(actual - expected) / expected > 0.01:
            raise CoverWorkflowError(f"target dimensions {width}x{height} do not match ratio {ratio_text}")
        platforms = ("用户指定的自定义比例用途",)
    return RatioSpec(ratio_text, width, height, ratio_text.replace(":", "x"), platforms)


def validate_copy(headline: str, subheadline: str, item: dict[str, Any]) -> None:
    if not headline.strip() or not subheadline.strip():
        raise CoverWorkflowError("headline and subheadline must both be non-empty")
    source = f"{item.get('title', '')}\n{item.get('summary', '')}".replace("％", "%")
    copy_text = f"{headline}\n{subheadline}".replace("％", "%")
    unsupported = [token for token in NUMERIC_RE.findall(copy_text) if token not in source]
    if unsupported:
        raise CoverWorkflowError(
            "cover copy contains numeric claims absent from the selected source: "
            + ", ".join(unsupported)
        )


def find_item(editorial: dict[str, Any], item_id: str | None) -> dict[str, Any]:
    chosen_id = item_id or rank_items(editorial)[0]["item_id"]
    for item in selected_items(editorial):
        if str(item.get("id")) == chosen_id:
            return item
    raise CoverWorkflowError(f"item {chosen_id!r} is not selected in the frozen edition")


def presentation_item_ids(editorial_path: Path, editorial: dict[str, Any]) -> list[str]:
    """Read story array order from the final editorial plan when available."""
    selected_ids = [str(item.get("id") or "") for item in selected_items(editorial)]
    candidates = (
        editorial_path.parent / "editorial_plan_final.json",
        editorial_path.parent / "editorial_plan.json",
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        plan = read_json(candidate)
        if str(plan.get("input_sha256") or "") not in {"", str(editorial.get("input_sha256") or "")}:
            continue
        ordered: list[str] = []
        for story in plan.get("stories") or []:
            if not isinstance(story, dict):
                continue
            for item_id in story.get("source_item_ids") or []:
                item_id = str(item_id)
                if item_id in selected_ids and item_id not in ordered:
                    ordered.append(item_id)
        if ordered:
            return ordered + [item_id for item_id in selected_ids if item_id not in ordered]
    return selected_ids


def select_edition_brands(
    editorial: dict[str, Any],
    headliner_item_id: str,
    presentation_order: Iterable[str],
    additional_names: Iterable[str] = (),
    extra_entries: Iterable[dict[str, Any]] = (),
    *,
    maximum: int = MAX_EDITION_BRANDS,
) -> list[dict[str, Any]]:
    """Select only source-present brands: headliner first, then video order."""
    entries = [*registry_entries(), *extra_entries]
    items = {str(item.get("id") or ""): item for item in selected_items(editorial)}
    all_source_text = "\n".join(
        f"{item.get('title', '')}\n{item.get('summary', '')}" for item in items.values()
    ).casefold()
    clean_additional_names = [str(value).strip() for value in additional_names if str(value).strip()]
    unsupported_names = [name for name in clean_additional_names if name.casefold() not in all_source_text]
    if unsupported_names:
        raise CoverWorkflowError(
            "additional brand is absent from the frozen selected sources: "
            + ", ".join(unsupported_names)
        )
    entry_names = {str(entry.get("name") or "").casefold() for entry in entries}
    for name in clean_additional_names:
        if name.casefold() not in entry_names:
            entries.append({
                "name": name,
                "aliases": [name],
                "source_page_url": None,
                "source_asset_url": None,
                "asset": None,
                "resolved_asset": None,
                "sha256": None,
                "fallback_status": "exact_text_identity_only",
            })
            entry_names.add(name.casefold())
    ordered_ids = [str(headliner_item_id)] + [
        str(item_id) for item_id in presentation_order if str(item_id) != str(headliner_item_id)
    ]
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item_id in ordered_ids:
        item = items.get(item_id)
        if item is None:
            continue
        text = f"{item.get('title', '')}\n{item.get('summary', '')}"
        for entry in brand_matches(text, entries):
            key = str(entry.get("name") or "").casefold()
            if key and key not in seen:
                ordered.append(entry)
                seen.add(key)
                if len(ordered) >= maximum:
                    return ordered

    return ordered


def related_brands(
    editorial: dict[str, Any],
    additional_names: Iterable[str],
    extra_entries: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Compatibility helper returning the schema-5 edition brand ordering."""
    headliner = rank_items(editorial)[0]["item_id"]
    frozen_order = [str(item.get("id") or "") for item in selected_items(editorial)]
    return select_edition_brands(
        editorial, headliner, frozen_order, additional_names, extra_entries
    )


def _reference_record(path: Path, role: str, *, brand: str | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "role": role,
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
    }
    if brand:
        value["brand"] = brand
    return value


def brand_reference_inputs(brands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    for brand in brands:
        asset_text = brand.get("resolved_asset")
        if not asset_text:
            continue
        asset = Path(str(asset_text))
        if asset.is_file() and asset.stat().st_size > 0:
            inputs.append(_reference_record(asset, "official_brand_identity_reference", brand=str(brand["name"])))
    return inputs


def build_prompt(
    *,
    date_label: str,
    headline: str,
    subheadline: str,
    ratio: RatioSpec,
    brands: list[dict[str, Any]],
    visual_brief: str,
    generation_role: str = "anchor",
    anchor_ratio: str | None = None,
) -> str:
    if generation_role not in {"anchor", "adaptation"}:
        raise CoverWorkflowError(f"unsupported generation role: {generation_role}")
    if generation_role == "adaptation" and not anchor_ratio:
        raise CoverWorkflowError("adaptation prompts require an anchor ratio")
    brand_names = "\n".join(f"- {value['name']}" for value in brands) or "- 本期无可核实品牌"
    adaptation_input = ""
    if generation_role == "adaptation":
        adaptation_input = (
            f"\n- Same-edition {anchor_ratio} cover: inherit only this edition's visual concept "
            f"and family character, then fully recompose it for {ratio.ratio}; do not crop, stretch, or programmatically adapt it."
        )
    return f"""Use case: ads-marketing
Asset type: AI每日早报完整平面封面，{ratio.ratio}

Primary request:
一次性生成可直接发布的最终封面。文字排版、品牌标识、新闻插画、
信息图元素、层级、间距和整体构图必须统一生成；不会进行任何后期
排版、Logo叠加、裁切或修正。

Input images:
- Image 1：主动视觉系统参考。只学习其编辑型信息图语言、层级、
  配色、字体气质、品牌胶囊、插画质感和信息密度；忽略并禁止复制
  其中的日期、标题、数字、新闻事实和品牌名单。{adaptation_input}
- Official brand identity references：只用于忠实呈现下列本期品牌；
  不得把身份参考当作构图模板，也不得混合或变形品牌。

Text — verbatim, each exactly once:
"AI每日早报"
"{date_label}"
"{headline}"
"{subheadline}"

Current-edition brands:
{brand_names}

News visual brief:
{visual_brief.strip()}

Visual system:
暖象牙色明亮底色；黑色超粗主标题；橙红色重点词和圆角信息条；
深青色与橙色的编辑型信息图插画；轻微立体材质和柔和阴影；
栏目名和日期形成次级层级；主标题具有压倒性视觉权重；
新闻隐喻、趋势箭头、数据路径、设备或模型符号与品牌区域形成
一个完整叙事。品牌可使用右侧或环绕式胶囊，但不规定固定坐标，
并按比例重新组织。

Constraints:
生成完整成品，不留待排版空白；准确生成全部指定文字；
不得添加其他标题、数字、公司、事实或伪文字；
不得复制参考图中的旧内容；不得发明、混合或变形品牌；
无播放器、播放量、时长、弹幕、平台界面、手机框、二维码、
水印或签名。"""


def choose_anchor_ratio(ratios: list[RatioSpec]) -> str:
    if not ratios:
        raise CoverWorkflowError("cannot choose an anchor without ratios")
    return "16:9" if any(value.ratio == "16:9" for value in ratios) else ratios[0].ratio


def default_output_dir(editorial_path: Path, date: str) -> Path:
    if editorial_path.parent.name == "artifacts" and editorial_path.parent.parent.name == date:
        return editorial_path.parent.parent / "release-kit" / "covers"
    return Path("outputs") / date / "release-kit" / "covers"


def _parse_assignment(value: str, *, name: str) -> tuple[str, str]:
    key, separator, path_text = value.partition("=")
    if not separator or not key.strip() or not path_text.strip():
        raise CoverWorkflowError(f"invalid {name} assignment {value!r}; expected RATIO=PATH")
    return key.strip(), path_text.strip()


def prepare_request(args: argparse.Namespace) -> dict[str, Any]:
    editorial_path = Path(args.editorial_input).resolve()
    editorial = read_json(editorial_path)
    date = str(editorial.get("date") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise CoverWorkflowError("editorial input has no valid YYYY-MM-DD date")
    item = find_item(editorial, args.item_id)
    validate_copy(args.headline, args.subheadline, item)
    visual_brief = str(args.visual_brief or "").strip()
    if not visual_brief:
        raise CoverWorkflowError("news visual brief must be non-empty")
    validate_copy(args.headline, f"{args.subheadline}\n{visual_brief}", item)

    specs: list[RatioSpec] = []
    seen: set[str] = set()
    for raw in (getattr(args, "ratio", None) or list(DEFAULT_COVER_RATIOS)):
        spec = parse_ratio(raw)
        if spec.ratio in seen:
            raise CoverWorkflowError(f"duplicate ratio: {spec.ratio}")
        specs.append(spec)
        seen.add(spec.ratio)
    anchor_ratio = choose_anchor_ratio(specs)
    specs.sort(key=lambda value: value.ratio != anchor_ratio)

    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else default_output_dir(editorial_path, date).resolve()
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    request_path = output_dir / "cover_request.json"
    if request_path.exists() and not args.force:
        raise CoverWorkflowError(f"request already exists; use --force to replace it: {request_path}")
    if not STYLE_REFERENCE_PATH.is_file() or STYLE_REFERENCE_PATH.stat().st_size <= 0:
        raise CoverWorkflowError(f"active cover style reference is missing or empty: {STYLE_REFERENCE_PATH}")

    previous = read_json(request_path) if request_path.is_file() else None
    revision = int((previous or {}).get("family_revision") or 0) + 1
    family_id = f"{date}-family-{revision:02d}"
    if previous and args.force:
        history = output_dir / "source" / str(previous.get("family_id") or f"{date}-family-{revision - 1:02d}")
        history.mkdir(parents=True, exist_ok=True)
        shutil.copy2(request_path, history / "cover_request.json")
        previous_manifest = output_dir / "cover_manifest.json"
        if previous_manifest.is_file():
            shutil.copy2(previous_manifest, history / "cover_manifest.json")

    extra_manifest = Path(args.extra_logo_manifest).resolve() if args.extra_logo_manifest else None
    order = presentation_item_ids(editorial_path, editorial)
    brands = select_edition_brands(
        editorial,
        str(item.get("id") or ""),
        order,
        args.brand or [],
        extra_logo_entries(extra_manifest),
    )
    style_reference = _reference_record(
        STYLE_REFERENCE_PATH, "active_editorial_visual_system_reference"
    )
    style_reference["content_policy"] = "learn_style_only_ignore_all_embedded_facts_copy_and_brands"
    logo_references = brand_reference_inputs(brands)
    date_label = date.replace("-", ".")
    anchor_output = output_dir / f"{anchor_ratio.replace(':', 'x')}.png"
    ratio_values: list[dict[str, Any]] = []
    for spec in specs:
        role = "anchor" if spec.ratio == anchor_ratio else "adaptation"
        references = [dict(style_reference)]
        if role == "adaptation":
            references.append({
                "role": "same_edition_anchor_reference",
                "ratio": anchor_ratio,
                "path": str(anchor_output),
                "availability": "required_after_anchor_generation",
            })
        references.extend(dict(value) for value in logo_references)
        value = spec.as_dict()
        value.update({
            "generation_order": len(ratio_values) + 1,
            "generation_role": role,
            "generation_request_count": 1,
            "style_reference_ratio": None if role == "anchor" else anchor_ratio,
            "reference_inputs": references,
            "prompt": build_prompt(
                date_label=date_label,
                headline=args.headline.strip(),
                subheadline=args.subheadline.strip(),
                ratio=spec,
                brands=brands,
                visual_brief=visual_brief,
                generation_role=role,
                anchor_ratio=anchor_ratio,
            ),
        })
        ratio_values.append(value)

    request = {
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "model": MODEL_NAME,
        "date": date,
        "input_sha256": str(editorial.get("input_sha256") or ""),
        "editorial_input": str(editorial_path),
        "output_dir": str(output_dir),
        "family_id": family_id,
        "family_revision": revision,
        "generation_mode": "full_cover_imagegen",
        "post_processing": False,
        "generation_policy": {
            "strategy": "first_result_direct",
            "one_request_per_ratio": True,
            "attempts_per_ratio": 1,
            "retries": 0,
            "human_confirmation": False,
            "image_review": False,
            "programmatic_visual_validation": False,
        },
        "selected_item": {
            "id": str(item.get("id") or ""),
            "title": str(item.get("title") or ""),
            "summary": str(item.get("summary") or ""),
            "score": item.get("score"),
            "links": item.get("links") or {},
        },
        "copy": {
            "series": "AI每日早报",
            "date_label": date_label,
            "headline": args.headline.strip(),
            "subheadline": args.subheadline.strip(),
        },
        "news_visual_brief": visual_brief,
        "presentation_item_ids": order,
        "brands": brands,
        "brand_selection": {
            "scope": "all_selected_stories",
            "ordering": "headliner_then_video_presentation_order",
            "maximum": MAX_EDITION_BRANDS,
            "count": len(brands),
            "no_fill_minimum": True,
            "identity_references_only": True,
        },
        "style_reference": style_reference,
        "anchor_ratio": anchor_ratio,
        "ratios": ratio_values,
    }
    write_json(request_path, request)
    return {**request, "request_path": str(request_path)}


def record_outputs(args: argparse.Namespace) -> dict[str, Any]:
    request_path = Path(args.request).resolve()
    request = read_json(request_path)
    if int(request.get("schema_version") or 0) != SCHEMA_VERSION:
        raise CoverWorkflowError("record requires a schema_version 5 cover request")
    output_dir = Path(str(request.get("output_dir") or request_path.parent)).resolve()
    manifest_path = output_dir / "cover_manifest.json"
    if manifest_path.exists() and not args.force:
        raise CoverWorkflowError(f"cover manifest already exists; use --force to replace it: {manifest_path}")

    assignments: dict[str, Path] = {}
    for raw in args.image:
        ratio, path_text = _parse_assignment(raw, name="image")
        if ratio in assignments:
            raise CoverWorkflowError(f"duplicate image assignment: {ratio}")
        source = Path(path_text).expanduser().resolve()
        if not source.is_file() or source.stat().st_size <= 0:
            raise CoverWorkflowError(f"generated cover is missing or empty: {source}")
        assignments[ratio] = source

    ratio_requests = request.get("ratios") or []
    requested = [str(value.get("ratio") or "") for value in ratio_requests if isinstance(value, dict)]
    if set(assignments) != set(requested) or len(assignments) != len(requested):
        missing = sorted(set(requested) - set(assignments))
        extra = sorted(set(assignments) - set(requested))
        raise CoverWorkflowError(f"image assignments must exactly match requested ratios; missing={missing}, extra={extra}")

    output_dir.mkdir(parents=True, exist_ok=True)
    for value in ratio_requests:
        ratio = str(value["ratio"])
        destination = output_dir / f"{value['slug']}.png"
        if destination.exists() and not args.force and assignments[ratio] != destination:
            raise CoverWorkflowError(f"formal cover already exists; use --force to replace it: {destination}")

    results: dict[str, dict[str, Any]] = {}
    for value in ratio_requests:
        ratio = str(value["ratio"])
        source = assignments[ratio]
        destination = output_dir / f"{value['slug']}.png"
        source_hash = sha256_file(source)
        if source != destination:
            temporary = destination.with_name(f".{destination.name}.tmp")
            shutil.copyfile(source, temporary)
            temporary.replace(destination)
        generated_hash = sha256_file(destination)
        if generated_hash != source_hash:
            raise CoverWorkflowError(f"byte-for-byte copy integrity failed for {ratio}")
        results[ratio] = {
            "generation_status": "succeeded",
            "generation_role": value.get("generation_role"),
            "attempts": 1,
            "requested_ratio": ratio,
            "requested_size": value.get("requested_size"),
            "source_file": str(source),
            "generated_file": str(destination),
            "bytes": destination.stat().st_size,
            "sha256": generated_hash,
            "source_sha256": source_hash,
            "post_processing": False,
        }

    anchor_ratio = str(request.get("anchor_ratio") or requested[0])
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete_unreviewed",
        "generation_mode": "full_cover_imagegen",
        "post_processing": False,
        "attempts": 1,
        "date": request.get("date"),
        "input_sha256": request.get("input_sha256"),
        "prompt_version": request.get("prompt_version") or PROMPT_VERSION,
        "model": request.get("model") or MODEL_NAME,
        "family_id": request.get("family_id"),
        "family_revision": request.get("family_revision"),
        "selected_item": request.get("selected_item"),
        "copy": request.get("copy"),
        "news_visual_brief": request.get("news_visual_brief"),
        "brands": request.get("brands") or [],
        "style_reference": request.get("style_reference"),
        "anchor": {
            "ratio": anchor_ratio,
            "generated_file": results[anchor_ratio]["generated_file"],
            "sha256": results[anchor_ratio]["sha256"],
        },
        "formal_cover_count": len(results),
        "results": results,
        "review_policy": "none_first_result_direct",
        "publishing_performed": False,
    }
    write_json(manifest_path, manifest)
    return {**manifest, "manifest_path": str(manifest_path)}


def validate_assets() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for entry in registry_entries():
        asset_text = entry.get("resolved_asset")
        if not asset_text:
            continue
        asset = Path(str(asset_text))
        actual = sha256_file(asset) if asset.is_file() and asset.stat().st_size > 0 else None
        checks.append({
            "name": entry.get("name"),
            "path": str(asset),
            "exists": bool(actual),
            "sha256_matches": bool(actual and actual == entry.get("sha256")),
        })
    style_actual = (
        sha256_file(STYLE_REFERENCE_PATH)
        if STYLE_REFERENCE_PATH.is_file() and STYLE_REFERENCE_PATH.stat().st_size > 0
        else None
    )
    checks.append({
        "name": "active cover style system",
        "path": str(STYLE_REFERENCE_PATH),
        "exists": bool(style_actual),
        "sha256": style_actual,
    })
    return {
        "status": "pass" if all(value["exists"] and value.get("sha256_matches", True) for value in checks) else "fail",
        "checks": checks,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    rank_parser = subparsers.add_parser("rank", help="rank selected frozen items")
    rank_parser.add_argument("--editorial-input", required=True)

    prepare_parser = subparsers.add_parser("prepare", help="freeze schema-5 full-cover prompts")
    prepare_parser.add_argument("--editorial-input", required=True)
    prepare_parser.add_argument("--item-id")
    prepare_parser.add_argument("--headline", required=True)
    prepare_parser.add_argument("--subheadline", required=True)
    prepare_parser.add_argument("--visual-brief", required=True)
    prepare_parser.add_argument("--ratio", action="append", help="defaults to 16:9, 3:4, and 9:16")
    prepare_parser.add_argument("--brand", action="append")
    prepare_parser.add_argument("--extra-logo-manifest")
    prepare_parser.add_argument("--output-dir")
    prepare_parser.add_argument("--force", action="store_true")

    record_parser = subparsers.add_parser("record", help="record first GPT Image files without post-processing")
    record_parser.add_argument("--request", required=True)
    record_parser.add_argument("--image", action="append", required=True, help="RATIO=PATH")
    record_parser.add_argument("--force", action="store_true")

    subparsers.add_parser("validate-assets", help="verify bundled reference-file provenance")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "rank":
            result: Any = rank_items(read_json(Path(args.editorial_input).resolve()))
        elif args.command == "prepare":
            result = prepare_request(args)
        elif args.command == "record":
            result = record_outputs(args)
        else:
            result = validate_assets()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not isinstance(result, dict) or result.get("status") != "fail" else 1
    except CoverWorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
