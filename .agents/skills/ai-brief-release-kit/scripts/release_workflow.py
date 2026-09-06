#!/usr/bin/env python3
"""Deterministic preparation and assembly helpers for AI每日早报 release kits."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image


SKILL_DIR = Path(__file__).resolve().parents[1]
COVER_WORKFLOW_PATH = SKILL_DIR.parent / "ai-brief-cover-generator" / "scripts" / "cover_workflow.py"
# Keep numeric-leading model and storage tokens intact (for example ``27B``
# and ``17GB``) so source-token validation does not inspect a trailing letter
# as an unsupported standalone token.
ASCII_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9._-]*[A-Za-z])[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*(?![A-Za-z0-9])"
)
NUMBER_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?%?")


class ReleaseKitError(ValueError):
    """Raised when a release plan or package cannot pass its hard gates."""


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseKitError(f"cannot read JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseKitError(f"JSON root must be an object: {path}")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReleaseKitError(f"cannot hash file: {path}: {exc}") from exc
    return digest.hexdigest()


def cover_module() -> Any:
    if not COVER_WORKFLOW_PATH.is_file():
        raise ReleaseKitError(f"cover ranking helper is unavailable: {COVER_WORKFLOW_PATH}")
    spec = importlib.util.spec_from_file_location("ai_brief_cover_workflow", COVER_WORKFLOW_PATH)
    if spec is None or spec.loader is None:
        raise ReleaseKitError("could not load cover ranking helper")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def selected_items(editorial: dict[str, Any]) -> list[dict[str, Any]]:
    items = editorial.get("items")
    if not isinstance(items, list) or not items:
        raise ReleaseKitError("editorial input has no selected items")
    frozen_ids = editorial.get("selection", {}).get("item_ids") or []
    frozen_set = {str(value) for value in frozen_ids}
    selected = [
        item
        for item in items
        if isinstance(item, dict)
        and str(item.get("id") or "").strip()
        and ((frozen_set and str(item.get("id")) in frozen_set) or (not frozen_set and item.get("selected")))
    ]
    if not selected:
        raise ReleaseKitError("frozen edition has no selected items")
    return selected


def ranked_items(editorial: dict[str, Any]) -> list[dict[str, Any]]:
    module = cover_module()
    ranked = module.rank_items(editorial)
    if not ranked:
        raise ReleaseKitError("cover ranking returned no selected items")
    return ranked


def choose_leads(
    editorial: dict[str, Any],
    primary_item_id: str | None = None,
    secondary_item_id: str | None = None,
) -> list[dict[str, Any]]:
    ranked = ranked_items(editorial)
    by_id = {str(item["item_id"]): item for item in ranked}
    if primary_item_id:
        if primary_item_id not in by_id:
            raise ReleaseKitError(f"primary item is not selected: {primary_item_id}")
        ordered = [by_id[primary_item_id]] + [item for item in ranked if item["item_id"] != primary_item_id]
    else:
        ordered = ranked
    if secondary_item_id:
        if secondary_item_id not in by_id:
            raise ReleaseKitError(f"secondary item is not selected: {secondary_item_id}")
        if secondary_item_id == ordered[0]["item_id"]:
            raise ReleaseKitError("primary and secondary items must differ")
        ordered = [ordered[0], by_id[secondary_item_id]] + [
            item for item in ordered[1:] if item["item_id"] != secondary_item_id
        ]
    return ordered[:2]


def source_text(item: dict[str, Any]) -> str:
    return f"{item.get('title', '')}\n{item.get('summary', '')}".replace("％", "%")


def missing_source_tokens(copy_text: str, item: dict[str, Any]) -> list[str]:
    source = source_text(item)
    tokens = [*ASCII_TOKEN_RE.findall(copy_text), *NUMBER_TOKEN_RE.findall(copy_text)]
    missing: list[str] = []
    for token in tokens:
        if token[:1].isdigit():
            pattern = rf"(?<![\d.]){re.escape(token)}(?![\d.])"
        else:
            pattern = rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])"
        if not re.search(pattern, source, flags=re.IGNORECASE):
            missing.append(token)
    return sorted(set(missing), key=lambda value: (value.casefold(), value))


def validate_publication_copy(
    article_description: str,
    lead_items: list[dict[str, Any]],
    date: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ReleaseKitError("editorial input has no valid YYYY-MM-DD date")
    article_description = article_description.strip()
    if not article_description:
        raise ReleaseKitError("article description must be non-empty")
    if "\n" in article_description or "\r" in article_description:
        raise ReleaseKitError("article description must be single-line")
    if ";" in article_description:
        raise ReleaseKitError("use the full-width semicolon separator `；`")
    clauses = [part.strip() for part in article_description.split("；")]
    if len(clauses) not in (1, 2) or any(not part for part in clauses):
        raise ReleaseKitError("article description must contain one or two non-empty clauses")
    if len(clauses) > len(lead_items):
        raise ReleaseKitError("article description has more clauses than recorded lead items")
    clause_checks = []
    for clause, item in zip(clauses, lead_items):
        missing = missing_source_tokens(clause, find_source_item_from_ranked(item))
        if missing:
            raise ReleaseKitError(
                f"article description contains unsupported tokens for {item['item_id']}: {', '.join(missing)}"
            )
        clause_checks.append({"item_id": item["item_id"], "text": clause, "status": "pass", "missing_tokens": []})
    return {
        "status": "pass",
        "description_clauses": clause_checks,
    }


def find_source_item_from_ranked(ranked_item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": ranked_item["item_id"],
        "title": ranked_item.get("title", ""),
        "summary": ranked_item.get("summary", ""),
    }


def default_release_kit_dir(editorial_path: Path, date: str) -> Path:
    if editorial_path.parent.name == "artifacts" and editorial_path.parent.parent.name == date:
        return editorial_path.parent.parent / "release-kit"
    return Path("outputs") / date / "release-kit"


def build_release_plan(args: argparse.Namespace) -> dict[str, Any]:
    editorial_path = Path(args.editorial_input).resolve()
    editorial = read_json(editorial_path)
    date = str(editorial.get("date") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ReleaseKitError("editorial input has no valid YYYY-MM-DD date")
    input_sha256 = str(editorial.get("input_sha256") or "").strip()
    if not input_sha256:
        raise ReleaseKitError("editorial input is missing input_sha256")
    leads = choose_leads(editorial, args.primary_item_id, args.secondary_item_id)
    source_by_id = {str(item["id"]): item for item in selected_items(editorial)}
    title = f"AI每日早报{date}"
    article_description = args.description.strip()
    validation = validate_publication_copy(
        article_description,
        leads,
        date,
    )
    clauses = [part.strip() for part in article_description.split("；")]
    description_item_ids = [item["item_id"] for item in leads[: len(clauses)]]
    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else default_release_kit_dir(editorial_path, date).resolve()
    )
    plan_path = output_dir / "release_plan.json"
    if plan_path.exists() and not args.force:
        raise ReleaseKitError(f"release plan already exists; use --force to replace it: {plan_path}")
    selection_policy = (editorial.get("selection") or {}).get("policy") or {}
    selection_policy_sha256 = hashlib.sha256(
        json.dumps(selection_policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    plan = {
        "schema_version": 3,
        "status": "pass",
        "date": date,
        "input_sha256": input_sha256,
        "editorial_input": str(editorial_path),
        "ranked_items": [
            {
                "rank": item.get("rank"),
                "item_id": item["item_id"],
                "title": item.get("title", ""),
                "summary": item.get("summary", ""),
                "category": source_by_id.get(item["item_id"], {}).get("category"),
                "source": source_by_id.get(item["item_id"], {}).get("source", {}),
                "links": source_by_id.get(item["item_id"], {}).get("links", {}),
                "aihot_score": item.get("aihot_score"),
                "recognized_brands": item.get("recognized_brands", []),
                "selection_key": item.get("selection_key", []),
            }
            for item in leads
        ],
        "source_items": [
            {
                "item_id": str(item.get("id") or ""),
                "title": str(item.get("title") or ""),
                "category": item.get("category"),
                "score": item.get("score"),
                "selection_meta": item.get("selection_meta") or {},
                "links": item.get("links") or {},
            }
            for item in selected_items(editorial)
        ],
        "selection_policy": selection_policy,
        "selection_policy_sha256": selection_policy_sha256,
        "cover_story_item_id": leads[0]["item_id"],
        "cover_manifest_path": str(output_dir / "covers" / "cover_manifest.json"),
        "description_item_ids": description_item_ids,
        "publish_copy": {
            "title": title,
            "description": article_description,
        },
        "validation": validation,
        "pipeline_integrity": {
            "main_pipeline_modified": False,
            "publishing_performed": False,
        },
    }
    write_json_atomic(plan_path, plan)
    plan["plan_path"] = str(plan_path)
    return plan


def package_root_from_plan(plan_path: Path) -> Path:
    return plan_path.resolve().parent / "video-publish-package"


def run_dir_from_plan(plan: dict[str, Any], plan_path: Path) -> Path:
    editorial_path = Path(str(plan.get("editorial_input") or "")).resolve()
    if editorial_path.parent.name == "artifacts":
        return editorial_path.parent.parent
    return plan_path.resolve().parent.parent


def require_success_reports(run_dir: Path, plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    run_report = read_json(run_dir / "run_report.json")
    quality_report = read_json(run_dir / "artifacts" / "quality_report.json")
    if run_report.get("status") != "success":
        raise ReleaseKitError("run_report.json is not successful")
    if quality_report.get("status") != "pass":
        raise ReleaseKitError("quality_report.json is not pass")
    expected_date = str(plan.get("date") or "")
    expected_hash = str(plan.get("input_sha256") or "")
    if run_dir.name != expected_date:
        raise ReleaseKitError("run directory date does not match release plan")
    report_hash = str(
        (((run_report.get("details") or {}).get("script") or {}).get("editorial") or {}).get("input_sha256") or ""
    )
    if report_hash != expected_hash:
        raise ReleaseKitError("run report input_sha256 does not match release plan")
    if str(quality_report.get("date") or "") != expected_date:
        raise ReleaseKitError("quality report date does not match release plan")
    if str(quality_report.get("input_sha256") or "") != expected_hash:
        raise ReleaseKitError("quality report input_sha256 does not match release plan")
    return run_report, quality_report


def verify_image(path: Path) -> tuple[int, int]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ReleaseKitError(f"cover is missing or empty: {path}")
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return image.size
    except (OSError, ValueError) as exc:
        raise ReleaseKitError(f"cover is corrupt: {path}: {exc}") from exc


def publish_copy_markdown(plan: dict[str, Any]) -> str:
    copy = plan["publish_copy"]
    date = plan["date"]
    source_rows = []
    for item in plan.get("source_items") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").replace("|", "\\|")
        category = str(item.get("category") or "未分类").replace("|", "\\|")
        score = item.get("score")
        score_text = "未提供" if score is None or score == "" else str(score)
        links = item.get("links") or {}
        aihot = str(links.get("aihot") or "")
        original = str(links.get("original") or "")
        aihot_link = f"[AIHOT]({aihot})" if aihot else "未提供"
        original_link = f"[原文]({original})" if original else "未提供"
        source_rows.append(f"| {title} | {category} | {score_text} | {aihot_link} | {original_link} |")
    source_appendix = ""
    if source_rows:
        source_appendix = (
            "\n## 本期来源与评分\n\n"
            "评分为 AIHOT 原始字段，仅用于审计；本期选题采用维度内相对排名，不使用跨维度固定分数线。\n\n"
            "| 资讯 | 维度 | AIHOT评分 | AIHOT站内链接 | 原文链接 |\n"
            "|---|---|---:|---|---|\n"
            + "\n".join(source_rows)
            + "\n"
        )
    return (
        f"# AI每日早报 {date}\n\n"
        f"标题：{copy['title']}\n\n"
        f"文章简介：{copy['description']}\n"
        + source_appendix
    )


def file_record(path: Path, package_dir: Path, media_type: str, dimensions: list[int] | None = None) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(package_dir)),
        "media_type": media_type,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        **({"dimensions": dimensions} if dimensions else {}),
    }


def finalize_package(args: argparse.Namespace) -> dict[str, Any]:
    plan_path = Path(args.release_plan).resolve()
    plan = read_json(plan_path)
    if plan.get("status") != "pass" or plan.get("validation", {}).get("status") != "pass":
        raise ReleaseKitError("release plan has not passed validation")
    run_dir = Path(args.run_dir).resolve() if args.run_dir else run_dir_from_plan(plan, plan_path)
    run_report, quality_report = require_success_reports(run_dir, plan)
    video_path = Path(args.video).resolve() if args.video else run_dir / "renders" / f"ai-daily-news-{plan['date']}.mp4"
    if not video_path.is_file() or video_path.stat().st_size <= 0:
        raise ReleaseKitError(f"final MP4 is missing or empty: {video_path}")
    cover_manifest_path = (
        Path(args.cover_manifest).resolve()
        if args.cover_manifest
        else Path(
            str(plan.get("cover_manifest_path") or (run_dir / "release-kit" / "covers" / "cover_manifest.json"))
        ).resolve()
    )
    cover_manifest = read_json(cover_manifest_path)
    try:
        cover_schema_version = int(cover_manifest.get("schema_version") or 1)
    except (TypeError, ValueError) as exc:
        raise ReleaseKitError("cover manifest schema_version is invalid") from exc
    if cover_schema_version < 3 or cover_schema_version > 5:
        raise ReleaseKitError("cover manifest schema_version 3, 4, or 5 is required")
    expected_cover_status = "complete_unreviewed" if cover_schema_version == 5 else "pass"
    if cover_manifest.get("status") != expected_cover_status:
        raise ReleaseKitError(f"cover manifest status must be {expected_cover_status}")
    if cover_manifest.get("input_sha256") != plan.get("input_sha256"):
        raise ReleaseKitError("cover input_sha256 does not match release plan")
    selected_item = (cover_manifest.get("selected_item") or {}).get("id")
    if selected_item != plan.get("cover_story_item_id"):
        raise ReleaseKitError("cover story item does not match release plan")
    results = cover_manifest.get("results")
    if not isinstance(results, dict) or not results:
        raise ReleaseKitError("cover manifest has no generated results")
    if cover_schema_version in (3, 4):
        family_review = cover_manifest.get("family_review") or {}
        if family_review.get("status") != "pass":
            raise ReleaseKitError("cover family consistency review is not pass")
        required_family_fields = ("palette_and_color_system", "typography_mood_and_hierarchy", "overall_editorial_style", "brand_treatment")
        if any(family_review.get(field) is not True for field in required_family_fields):
            raise ReleaseKitError("cover family high-level consistency fields are incomplete")
        if int(cover_manifest.get("formal_cover_count") or 0) != len(results):
            raise ReleaseKitError("formal cover count does not match approved results")
        anchor_ratio = str((cover_manifest.get("anchor") or {}).get("ratio") or "")
        if anchor_ratio not in results:
            raise ReleaseKitError("cover family anchor is missing from approved results")
        if any(
            not isinstance(result, dict)
            or result.get("approval_status") != "approved"
            for result in results.values()
        ):
            raise ReleaseKitError("cover results include a non-approved candidate")
        if cover_schema_version == 4:
            required_brand = ((cover_manifest.get("brand_policy") or {}).get("required_brand") or {})
            if required_brand.get("name") and required_brand.get("asset"):
                if any(
                    not isinstance(result.get("brand_render"), dict)
                    or result["brand_render"].get("status") != "pass"
                    or not result["brand_render"].get("visible")
                    for result in results.values()
                ):
                    raise ReleaseKitError("cover results are missing a passing official Logo composition receipt")
    cover_manifest_sha256 = sha256_file(cover_manifest_path)
    video_sha256 = sha256_file(video_path)

    parent = Path(args.output_dir).resolve().parent if args.output_dir else package_root_from_plan(plan_path).parent
    package_dir = Path(args.output_dir).resolve() if args.output_dir else package_root_from_plan(plan_path)
    if package_dir.exists() and not args.force:
        existing_manifest_path = package_dir / "package.json"
        if existing_manifest_path.is_file():
            existing_manifest = read_json(existing_manifest_path)
            if (
                existing_manifest.get("status") == "pass"
                and existing_manifest.get("input_sha256") == plan.get("input_sha256")
                and existing_manifest.get("date") == plan.get("date")
                and existing_manifest.get("cover_story_item_id") == plan.get("cover_story_item_id")
                and existing_manifest.get("description_item_ids") == plan.get("description_item_ids")
                and existing_manifest.get("publish_copy") == plan.get("publish_copy")
                and (existing_manifest.get("source_reports") or {}).get("cover_manifest_sha256") == cover_manifest_sha256
                and (existing_manifest.get("source_reports") or {}).get("video_sha256") == video_sha256
            ):
                return {**existing_manifest, "package_dir": str(package_dir), "reused": True}
        raise ReleaseKitError(f"package already exists; use --force to replace it: {package_dir}")
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".video-publish-package-", dir=parent))
    try:
        (stage / "covers").mkdir(parents=True, exist_ok=True)
        (stage / "videos").mkdir(parents=True, exist_ok=True)
        copy_path = stage / "publish-copy.md"
        copy_path.write_text(publish_copy_markdown(plan), encoding="utf-8")
        records = [file_record(copy_path, stage, "text/markdown")]

        for ratio, result in sorted(results.items()):
            source_field = "generated_file" if cover_schema_version == 5 else "normalized_file"
            source = Path(str(result.get(source_field) or ""))
            if not source.is_absolute():
                source = cover_manifest_path.parent / source
            source = source.resolve()
            if not source.is_file() or source.stat().st_size <= 0:
                raise ReleaseKitError(f"cover is missing or empty: {source}")
            destination = stage / "covers" / f"{str(ratio).replace(':', 'x')}.png"
            shutil.copy2(source, destination)
            if cover_schema_version == 5:
                records.append(file_record(destination, stage, "image/png"))
            else:
                dimensions = verify_image(destination)
                records.append(file_record(destination, stage, "image/png", list(dimensions)))

        video_destination = stage / "videos" / video_path.name
        shutil.copy2(video_path, video_destination)
        records.append(file_record(video_destination, stage, "video/mp4"))
        package_manifest = {
            "schema_version": 2,
            "status": "pass",
            "date": plan["date"],
            "input_sha256": plan["input_sha256"],
            "cover_story_item_id": plan["cover_story_item_id"],
            "description_item_ids": plan["description_item_ids"],
            "publish_copy": plan["publish_copy"],
            "source_items": plan.get("source_items", []),
            "selection_policy": plan.get("selection_policy", {}),
            "selection_policy_sha256": plan.get("selection_policy_sha256"),
            "files": records,
            "source_reports": {
                "run_status": run_report.get("status"),
                "quality_status": quality_report.get("status"),
                "cover_manifest": str(cover_manifest_path),
                "cover_manifest_sha256": cover_manifest_sha256,
                "cover_family_id": cover_manifest.get("family_id"),
                "cover_schema_version": cover_schema_version,
                "cover_generation_mode": cover_manifest.get("generation_mode"),
                "video_sha256": video_sha256,
            },
            "publishing_performed": False,
        }
        write_json_atomic(stage / "package.json", package_manifest)
        if package_dir.exists():
            backup = parent / f".{package_dir.name}.backup-{os.getpid()}"
            if backup.exists():
                shutil.rmtree(backup)
            package_dir.replace(backup)
            try:
                stage.replace(package_dir)
            except Exception:
                backup.replace(package_dir)
                raise
            shutil.rmtree(backup)
        else:
            stage.replace(package_dir)
        return {**package_manifest, "package_dir": str(package_dir)}
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise


def update_video_package(args: argparse.Namespace) -> dict[str, Any]:
    """Replace only the video in an existing package.

    This is an explicit compatibility path for a re-rendered video whose
    source snapshot is newer than a deliberately frozen cover and publication
    copy.  The normal ``finalize`` command remains strict and still requires
    every input hash to match.  Here, the existing package is treated as the
    authority for covers, copy, and their audit metadata; only the video bytes
    and the corresponding file/hash records are changed.
    """

    package_dir = Path(args.package_dir).resolve()
    run_dir = Path(args.run_dir).resolve()
    video_path = Path(args.video).resolve()
    package_manifest_path = package_dir / "package.json"
    if not package_dir.is_dir() or not package_manifest_path.is_file():
        raise ReleaseKitError(f"existing package is missing: {package_dir}")
    if not video_path.is_file() or video_path.stat().st_size <= 0:
        raise ReleaseKitError(f"replacement MP4 is missing or empty: {video_path}")

    run_report = read_json(run_dir / "run_report.json")
    quality_report = read_json(run_dir / "artifacts" / "quality_report.json")
    if run_report.get("status") != "success":
        raise ReleaseKitError("run_report.json is not successful")
    if quality_report.get("status") != "pass":
        raise ReleaseKitError("quality_report.json is not pass")
    package = read_json(package_manifest_path)
    if package.get("status") != "pass":
        raise ReleaseKitError("existing package is not a passing package")
    if str(package.get("date") or "") != run_dir.name:
        raise ReleaseKitError("run directory date does not match existing package")

    records = package.get("files")
    if not isinstance(records, list) or not records:
        raise ReleaseKitError("existing package has no file records")
    video_records = [
        record
        for record in records
        if isinstance(record, dict)
        and record.get("media_type") == "video/mp4"
        and str(record.get("path") or "") == f"videos/{video_path.name}"
    ]
    if len(video_records) != 1:
        raise ReleaseKitError(
            f"existing package must contain exactly one video record at videos/{video_path.name}"
        )
    video_record = video_records[0]

    # Validate every untouched record before staging.  This makes the
    # video-only operation fail closed if the package was modified manually.
    preserved_paths: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            raise ReleaseKitError("existing package contains an invalid file record")
        relative = str(record.get("path") or "")
        if not relative:
            raise ReleaseKitError("existing package contains a file record without a path")
        source = package_dir / relative
        if not source.is_file() or source.stat().st_size <= 0:
            raise ReleaseKitError(f"existing package file is missing or empty: {source}")
        if record.get("sha256") != sha256_file(source):
            raise ReleaseKitError(f"existing package file hash does not match: {source}")
        if source.resolve() != (package_dir / f"videos/{video_path.name}").resolve():
            preserved_paths.append(relative)

    replacement_hash = sha256_file(video_path)
    parent = package_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".video-publish-package-update-", dir=parent))
    try:
        shutil.copytree(package_dir, stage, dirs_exist_ok=True)
        staged_video = stage / "videos" / video_path.name
        shutil.copy2(video_path, staged_video)
        staged_record = {
            **video_record,
            "bytes": staged_video.stat().st_size,
            "sha256": sha256_file(staged_video),
        }
        updated_records = [
            staged_record
            if isinstance(record, dict)
            and record.get("media_type") == "video/mp4"
            and str(record.get("path") or "") == f"videos/{video_path.name}"
            else record
            for record in records
        ]
        updated_package = {**package, "files": updated_records}
        source_reports = dict(package.get("source_reports") or {})
        source_reports["video_sha256"] = replacement_hash
        updated_package["source_reports"] = source_reports
        write_json_atomic(stage / "package.json", updated_package)

        # The staged copy must preserve every non-video byte exactly before
        # the directory swap becomes visible.
        for relative in preserved_paths:
            original = package_dir / relative
            staged = stage / relative
            if sha256_file(original) != sha256_file(staged) or original.stat().st_size != staged.stat().st_size:
                raise ReleaseKitError(f"video-only update changed preserved file: {relative}")

        backup = parent / f".{package_dir.name}.backup-{os.getpid()}"
        if backup.exists():
            shutil.rmtree(backup)
        package_dir.replace(backup)
        try:
            stage.replace(package_dir)
        except Exception:
            backup.replace(package_dir)
            raise
        shutil.rmtree(backup)
        return {
            "status": "pass",
            "package_dir": str(package_dir),
            "updated": "video-only",
            "video_path": str(package_dir / "videos" / video_path.name),
            "video_sha256": replacement_hash,
            "preserved_files": preserved_paths,
            "run_status": run_report.get("status"),
            "quality_status": quality_report.get("status"),
        }
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="freeze source-grounded publication copy")
    prepare.add_argument("--editorial-input", required=True)
    prepare.add_argument("--description", required=True, help="source-grounded article description")
    prepare.add_argument("--primary-item-id")
    prepare.add_argument("--secondary-item-id")
    prepare.add_argument("--output-dir")
    prepare.add_argument("--force", action="store_true")
    finalize = subparsers.add_parser("finalize", help="assemble a verified private publication package")
    finalize.add_argument("--release-plan", required=True)
    finalize.add_argument("--run-dir")
    finalize.add_argument("--video")
    finalize.add_argument("--cover-manifest")
    finalize.add_argument("--output-dir")
    finalize.add_argument("--force", action="store_true")
    update_video = subparsers.add_parser(
        "update-video",
        help="replace only the video in an existing package while preserving covers and copy",
    )
    update_video.add_argument("--package-dir", required=True)
    update_video.add_argument("--run-dir", required=True)
    update_video.add_argument("--video", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = build_release_plan(args)
        elif args.command == "finalize":
            result = finalize_package(args)
        else:
            result = update_video_package(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ReleaseKitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
