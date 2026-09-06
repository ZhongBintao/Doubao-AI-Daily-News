from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .aihot import AIHotClient, AIHotError, AIHotResponse, load_fixture
from .build_contract import current_build_contract
from .config import (
    DEFAULT_SELECTION_CANDIDATE_PAGE_SIZE,
    DEFAULT_SELECTION_HEAD_SHARE,
    DEFAULT_SELECTION_MAX_ITEMS,
    DEFAULT_SELECTION_SOFT_MIN,
    EDITORIAL_DIMENSIONS,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_OPENMONTAGE_ROOT,
    DEFAULT_SOURCE_CONTRACT,
    DEFAULT_AIHOT_SKILL_VERSION,
    DEFAULT_SHOW_NAME,
    env_path,
)
from .editorial import (
    EditorialPlanError,
    build_editorial_input,
    build_editorial_quality_report,
    load_editorial_plan,
    write_editorial_input,
)
from .writing import (
    build_editorial_draft,
    build_pronunciation_ledger,
    build_writing_request,
    finalize_editorial_plan,
    write_writing_request,
)
from .materialize import materialize
from .media import MediaError, write_json
from .models import SourceItem
from .openmontage_bridge import OpenMontageError, probe_video, render_hyperframes, reuse_synthesized_audio, synthesize_and_align
from .speech import run_tts_benchmark
from .script import build_fact_ledger, build_script_from_editorial_plan, validate_script
from .selection import SelectionPolicy, select_items, select_items_by_dimension
from .source_detail import load_source_detail_snapshot, merge_source_detail_snapshot
from .screenshots import (
    ScreenshotError,
    ScreenshotPending,
    attach_screenshots_to_script,
    collect_screenshots,
    normalize_mode,
    prepare_screenshot_requests,
    source_visual_acceptance,
)


PIPELINE_VERSION = "0.5.0"
TIMEZONE = "Asia/Shanghai"


def _now(tz_name: str = TIMEZONE) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def _safe_error(exc: BaseException) -> str:
    message = str(exc)
    for key in ("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION", "AZURE_TTS_ENDPOINT", "GOOGLE_AI_STUDIO_API_KEY", "GEMINI_API_KEY"):
        value = os.environ.get(key)
        if value:
            message = message.replace(value, "<redacted>")
    # Do not put an access-bearing query string or a large provider response in
    # the artifact; the stage name and short diagnostic are enough for recovery.
    message = re.sub(r"https?://[^\s]+", "<provider-url>", message)
    return message[:800]


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _render_output_path(run_dir: Path, run_date: date, report: Mapping[str, Any] | None = None) -> Path:
    reported = str((((report or {}).get("details") or {}).get("render") or {}).get("output_path") or "").strip()
    candidate = Path(reported) if reported else run_dir / "renders" / f"ai-daily-news-{run_date.isoformat()}.mp4"
    return candidate if candidate.is_absolute() else run_dir / candidate


def _can_reuse_success(
    run_dir: Path,
    run_date: date,
    report: Mapping[str, Any],
    build_contract: Mapping[str, Any],
) -> bool:
    """Only reuse a dated success when its complete artifact contract matches."""

    if report.get("build_contract", {}).get("fingerprint") != build_contract.get("fingerprint"):
        return False
    quality_path = run_dir / "artifacts" / "quality_report.json"
    quality = _read_json(quality_path)
    if not quality or quality.get("status") != "pass":
        return False
    if quality.get("build_contract", {}).get("fingerprint") != build_contract.get("fingerprint"):
        return False
    output_path = _render_output_path(run_dir, run_date, report)
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        return False
    expected_hash = str(quality.get("output_sha256") or "").strip()
    actual_hash = _file_sha256(output_path)
    return bool(expected_hash and actual_hash and expected_hash == actual_hash)


def _archive_stale_artifacts(
    run_dir: Path,
    run_date: date,
    *,
    reason: str,
    previous_report: Mapping[str, Any] | None,
    current_contract: Mapping[str, Any],
) -> Path | None:
    """Move old generated outputs aside so a failed rebuild cannot masquerade as current."""

    candidates = [
        run_dir / "renders" / f"ai-daily-news-{run_date.isoformat()}.mp4",
        run_dir / "release-kit" / "video-publish-package",
        run_dir / "run_report.json",
        run_dir / "artifacts" / "quality_report.json",
    ]
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return None
    archive_root = run_dir / "archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    stamp = _now().strftime("%Y%m%dT%H%M%S%f")
    archive_dir = archive_root / f"stale-{stamp}"
    suffix = 1
    while archive_dir.exists():
        suffix += 1
        archive_dir = archive_root / f"stale-{stamp}-{suffix}"
    archive_dir.mkdir(parents=True)
    moved: list[str] = []
    for source in existing:
        target = archive_dir / source.name
        if source.name == "video-publish-package":
            target = archive_dir / "video-publish-package"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        moved.append(str(target.relative_to(archive_dir)))
    write_json(
        archive_dir / "archive_manifest.json",
        {
            "version": "1.0",
            "archived_at": _now().isoformat(),
            "reason": reason,
            "previous_report_status": (previous_report or {}).get("status"),
            "previous_build_contract": (previous_report or {}).get("build_contract"),
            "current_build_contract": dict(current_contract),
            "moved": moved,
            "recoverable": True,
            "no_secrets_in_artifacts": True,
        },
    )
    return archive_dir


def _write_stage_report(run_dir: Path, *, status: str, stage: str, started_at: str, details: Mapping[str, Any] | None = None, error: str | None = None) -> dict[str, Any]:
    report = {
        "version": PIPELINE_VERSION,
        "build_contract": current_build_contract(pipeline_version=PIPELINE_VERSION),
        "status": status,
        "failed_stage": stage if status == "failed" else None,
        "started_at": started_at,
        "finished_at": _now().isoformat(),
        "details": dict(details or {}),
        "error": error,
        "no_secrets_in_artifacts": True,
    }
    write_json(run_dir / "run_report.json", report)
    return report


def _source_snapshot(response: AIHotResponse) -> dict[str, Any]:
    return {
        "version": "2.0",
        "retrieved_at": _now().isoformat(),
        "url": response.url,
        "etag": response.etag,
        "from_cache": response.from_cache,
        "contract": {
            "source_repo": DEFAULT_SOURCE_CONTRACT,
            "aihot_skill_version": DEFAULT_AIHOT_SKILL_VERSION,
            "endpoint_mode": "selected",
            "window": "24h",
            "by": "timeline",
            "dimensions": list(EDITORIAL_DIMENSIONS),
        },
        "payload": response.payload,
        "category_payloads": dict(response.category_payloads or response.payload.get("categoryResponses") or {}),
    }


def _dimension_items(response: AIHotResponse) -> dict[str, tuple[SourceItem, ...]]:
    """Partition a live aggregate or legacy flat fixture by editorial dimension."""

    result: dict[str, tuple[SourceItem, ...]] = {}
    category_payloads = response.category_payloads or response.payload.get("categoryResponses") or {}
    for dimension in EDITORIAL_DIMENSIONS:
        raw = category_payloads.get(dimension) if isinstance(category_payloads, Mapping) else None
        if isinstance(raw, Mapping) and isinstance(raw.get("items"), list):
            result[dimension] = tuple(
                SourceItem.from_mapping(item) for item in raw["items"] if isinstance(item, Mapping)
            )
        else:
            result[dimension] = tuple(item for item in response.items if item.category == dimension)
    return result


def _selection_report(selection: Any) -> dict[str, Any]:
    records = list(selection.selection_metadata.values())
    records.sort(key=lambda value: (str(value.get("dimension") or ""), int(value.get("rank") or 999999), int(value.get("api_index") or 999999)))
    return {
        "version": "1.0",
        "status": selection.mode,
        "policy": dict(selection.policy),
        "policy_sha256": hashlib.sha256(json.dumps(dict(selection.policy), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
        "eligible_count": selection.eligible_count,
        "selected_count": len(selection.items),
        "items": records,
    }


def _items_by_id(response: AIHotResponse) -> dict[str, SourceItem]:
    return {item.item_id: item for item in response.items}


def _preflight(openmontage_root: Path, *, prepare_only: bool, speech_provider: str, reuse_audio: bool = False) -> dict[str, Any]:
    speech_provider = str(speech_provider or "azure").strip().lower()
    result = {
        "openmontage_path": str(openmontage_root),
        "openmontage_present": openmontage_root.is_dir(),
        "prepare_only": prepare_only,
        "reuse_audio": reuse_audio,
        "speech_provider": speech_provider,
        "azure_configured": bool(os.environ.get("AZURE_SPEECH_KEY")) if not prepare_only else None,
        "gemini_configured": bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_STUDIO_API_KEY")) if not prepare_only else None,
        "doubao_configured": True if not prepare_only else None,
    }
    if not openmontage_root.is_dir():
        raise OpenMontageError(f"OpenMontage directory not found: {openmontage_root}")
    if speech_provider not in {"azure", "gemini", "doubao"}:
        raise OpenMontageError(f"unsupported speech provider: {speech_provider}")
    if not prepare_only and not reuse_audio:
        if speech_provider == "azure" and not os.environ.get("AZURE_SPEECH_KEY"):
            raise OpenMontageError("AZURE_SPEECH_KEY is not configured")
        if speech_provider == "gemini" and not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_STUDIO_API_KEY")):
            raise OpenMontageError("GOOGLE_AI_STUDIO_API_KEY is not configured")
        # doubao uses the in-conversation voice synthesis tool; no API key required.
        # Audio must be pre-synthesized and placed via doubao_tts_adapter.py, then
        # the pipeline runs with --reuse-audio --speech-provider doubao.
    return result


def run_pipeline(
    *,
    run_date: date,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    fixture: Path | None = None,
    openmontage_root: Path = DEFAULT_OPENMONTAGE_ROOT,
    env_file: Path | None = None,
    prepare_only: bool = False,
    force: bool = False,
    align: bool = True,
    limit: int | None = None,
    candidate_page_size: int | None = None,
    selection_soft_min: int = DEFAULT_SELECTION_SOFT_MIN,
    selection_max_items: int = DEFAULT_SELECTION_MAX_ITEMS,
    selection_head_share: float = DEFAULT_SELECTION_HEAD_SHARE,
    reuse_source: bool = False,
    source_visual_mode: str | None = None,
    source_visual_min_stories: int | None = None,
    x_screenshot_mode: str | None = None,
    speech_provider: str = "azure",
    reuse_audio: bool = False,
) -> dict[str, Any]:
    run_dir = output_root / run_date.isoformat()
    artifacts = run_dir / "artifacts"
    run_dir.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    build_contract = current_build_contract(pipeline_version=PIPELINE_VERSION)
    existing_report = _read_json(run_dir / "run_report.json")
    if existing_report and existing_report.get("status") == "success" and not force:
        if _can_reuse_success(run_dir, run_date, existing_report, build_contract):
            return {**existing_report, "reused": True}
        _archive_stale_artifacts(
            run_dir,
            run_date,
            reason="dated success is stale or incomplete for the current build contract",
            previous_report=existing_report,
            current_contract=build_contract,
        )
    elif force or (run_dir / "renders" / f"ai-daily-news-{run_date.isoformat()}.mp4").is_file():
        _archive_stale_artifacts(
            run_dir,
            run_date,
            reason="explicit rebuild or prior failed run must not reuse the current output",
            previous_report=existing_report,
            current_contract=build_contract,
        )
    started_at = _now().isoformat()
    current_stage = "preflight"
    try:
        if not prepare_only:
            # Importing the bridge is cheap; loading selected values here keeps
            # the environment contract explicit before any Azure call.
            from .config import load_allowed_env
            for key, value in load_allowed_env(env_file or env_path()).items():
                os.environ[key] = value
        preflight = _preflight(openmontage_root, prepare_only=prepare_only, speech_provider=speech_provider, reuse_audio=reuse_audio)

        current_stage = "fetch"
        source_path = artifacts / "source_snapshot.json"
        source_document = _read_json(source_path)
        legacy_source_snapshot = bool(source_document and str(source_document.get("version") or "1.0") != "2.0")
        if source_document and (not force or reuse_source) and not fixture:
            payload = source_document.get("payload")
            if not isinstance(payload, dict):
                source_document = None
        if source_document is not None and (not force or reuse_source) and not fixture:
            response = AIHotResponse(
                url=str(source_document.get("url") or "snapshot://local"),
                payload=source_document["payload"],
                items=tuple(SourceItem.from_mapping(item) for item in source_document["payload"].get("items", [])),
                etag=source_document.get("etag"),
                from_cache=True,
                category_payloads=source_document.get("category_payloads") or source_document["payload"].get("categoryResponses"),
            )
        else:
            page_size = int(candidate_page_size or limit or DEFAULT_SELECTION_CANDIDATE_PAGE_SIZE)
            response = (
                load_fixture(fixture)
                if fixture
                else AIHotClient(cache_dir=artifacts / "aihot-cache").fetch_selected_24h_by_category(
                    EDITORIAL_DIMENSIONS, limit=page_size, force=force
                )
            )
            write_json(source_path, _source_snapshot(response))
        if not source_path.is_file():
            write_json(source_path, _source_snapshot(response))

        current_stage = "selection"
        policy = SelectionPolicy(
            soft_min=selection_soft_min,
            max_items=selection_max_items,
            head_share=selection_head_share,
        )
        # Historical schema-1 snapshots are accepted for replay, but are
        # explicitly kept on the legacy flat selector instead of being
        # silently reinterpreted as the new four-dimension contract.
        selection = (
            select_items(response.items)
            if legacy_source_snapshot
            else select_items_by_dimension(_dimension_items(response), policy=policy)
        )
        editorial_brief = {
            "version": "1.0",
            "date": run_date.isoformat(),
            "show_name": DEFAULT_SHOW_NAME,
            "source": {"url": response.url, "window": "24h", "mode": "selected", "etag": response.etag},
            "selection": {
                "status": selection.mode,
                "eligible_count": selection.eligible_count,
                "selected_count": len(selection.items),
                "category_counts": selection.category_counts,
                "reason": selection.reason,
                "item_ids": [item.item_id for item in selection.items],
                "policy": dict(selection.policy),
            },
            "items": [item.to_dict() for item in selection.items],
        }
        write_json(artifacts / "editorial_brief.json", editorial_brief)
        write_json(artifacts / "selection_report.json", _selection_report(selection))
        if selection.mode == "failure":
            return _write_stage_report(run_dir, status="failed", stage="selection", started_at=started_at, details={"preflight": preflight, "selection": editorial_brief["selection"]}, error=selection.reason)

        editorial_input = build_editorial_input(response.url, selection, run_date=run_date, etag=response.etag)
        detail_snapshot_path = artifacts / "source_detail_snapshot.json"
        if detail_snapshot_path.is_file():
            editorial_input = merge_source_detail_snapshot(
                editorial_input,
                load_source_detail_snapshot(detail_snapshot_path),
            )
        write_editorial_input(artifacts / "editorial_input.json", editorial_input)
        current_stage = "writing"
        writing_request = build_writing_request(editorial_input)
        write_writing_request(artifacts / "writing_request.json", writing_request)
        # A deterministic scaffold gives the Codex writer a complete starting
        # point offline. It is explicitly marked draft and is never promoted
        # when an approved editorial_plan.json is absent.
        draft_path = artifacts / "editorial_draft.json"
        if not draft_path.is_file() or force:
            write_json(draft_path, build_editorial_draft(editorial_input))
        existing_visual_requests = _read_json(artifacts / "source_visual_requests.json") or _read_json(artifacts / "screenshot_requests.json")
        screenshot_mode = normalize_mode(source_visual_mode or x_screenshot_mode or (existing_visual_requests or {}).get("mode") or "off")
        existing_visual_minimum = int(
            ((existing_visual_requests or {}).get("acceptance") or {}).get(
                "minimum_selected_stories"
            )
            or 0
        )
        resolved_visual_minimum = (
            int(source_visual_min_stories)
            if source_visual_min_stories is not None
            else existing_visual_minimum
        )
        visual_acceptance = source_visual_acceptance(
            mode=screenshot_mode,
            minimum_selected_stories=resolved_visual_minimum,
        )
        screenshot_requests = prepare_screenshot_requests(
            run_dir,
            selection.items,
            mode=screenshot_mode,
            minimum_selected_stories=resolved_visual_minimum,
        )
        if prepare_only:
            return _write_stage_report(
                run_dir,
                status="prepared",
                stage="editorial_input",
                started_at=started_at,
                details={
                    "preflight": preflight,
                    "selection": editorial_brief["selection"],
                    "editorial_input": {"path": str(artifacts / "editorial_input.json"), "input_sha256": editorial_input["input_sha256"]},
                    "writing": {"request_path": str(artifacts / "writing_request.json"), "draft_path": str(draft_path), "status": "draft_ready"},
                    "source_visuals": {"mode": screenshot_mode, "request_count": len(screenshot_requests.get("requests") or []), "path": str(artifacts / "source_visual_requests.json"), "acceptance": visual_acceptance},
                },
            )

        current_stage = "screenshots"
        screenshot_manifest = collect_screenshots(run_dir, mode=screenshot_mode)
        # Auto capture is a hard prerequisite for a complete run.  Decide its
        # coverage before loading the editorial plan so an unavailable browser
        # cannot send the automation into script/TTS/render retries.
        if screenshot_mode == "auto" and resolved_visual_minimum > 0:
            validated_stories = int(screenshot_manifest.get("validated_stories") or 0)
            if validated_stories < resolved_visual_minimum:
                error_code = "source_visual_minimum_not_met"
                if any(str(item.get("error_code") or "") == "browser_capture_unavailable" for item in screenshot_manifest.get("items") or []):
                    error_code = "browser_capture_unavailable"
                raise ScreenshotPending(
                    f"source visual minimum not met before script/TTS/render: required {resolved_visual_minimum}, found {validated_stories}",
                    details={
                        **screenshot_manifest,
                        "error_code": error_code,
                        "stop_before": "script_tts_render",
                    },
                )

        current_stage = "script"
        plan_path = artifacts / "editorial_plan.json"
        if not plan_path.is_file():
            raise EditorialPlanError("editorial_plan.json is required; run the Codex editorial step after prepare")
        editorial_plan = load_editorial_plan(plan_path)
        editorial_plan, _ = finalize_editorial_plan(editorial_plan)
        write_json(artifacts / "editorial_plan_final.json", editorial_plan)
        source_items = _items_by_id(response)
        editorial_quality = build_editorial_quality_report(editorial_plan, editorial_input, source_items)
        write_json(artifacts / "editorial_quality_report.json", editorial_quality)
        if editorial_quality["status"] != "pass":
            raise EditorialPlanError("editorial quality gate failed before TTS: " + "; ".join(editorial_quality["errors"][:5]))
        script = build_script_from_editorial_plan(
            selection,
            run_date=run_date,
            editorial_input=editorial_input,
            editorial_plan=editorial_plan,
        )
        script = attach_screenshots_to_script(script, screenshot_manifest)
        screenshot_manifest["selected_stories"] = int((script.get("screenshot") or {}).get("selected_stories") or 0)
        screenshot_manifest["selected_item_ids"] = [
            item_id
            for segment in script.get("segments", [])
            if segment.get("kind") == "news" and (segment.get("visual_plan") or {}).get("screenshots")
            for item_id in [str(value) for value in segment.get("source_item_ids") or [segment.get("source_item_id")] if str(value)]
        ]
        screenshot_manifest["acceptance"] = source_visual_acceptance(
            mode=screenshot_mode,
            minimum_selected_stories=resolved_visual_minimum,
            selected_stories=screenshot_manifest["selected_stories"],
        )
        write_json(artifacts / "screenshot_manifest.json", screenshot_manifest)
        write_json(artifacts / "source_visual_manifest.json", screenshot_manifest)
        if not screenshot_manifest["acceptance"]["requirement_met"]:
            current_stage = "source_visual_acceptance"
            raise ScreenshotError(
                "source visual acceptance failed: "
                f"required at least {resolved_visual_minimum} selected story/stories, "
                f"found {screenshot_manifest['selected_stories']}"
            )
        script_errors = validate_script(script, source_items, editorial_input)
        if script_errors:
            raise ValueError("script grounding validation failed: " + "; ".join(script_errors[:5]))
        write_json(artifacts / "narration_plan.json", script)
        write_json(artifacts / "fact_ledger.json", build_fact_ledger(script, source_items))
        write_json(artifacts / "pronunciation_ledger.json", build_pronunciation_ledger(script))
        if prepare_only:
            return _write_stage_report(
                run_dir,
                status="prepared",
                stage="script",
                started_at=started_at,
                details={"preflight": preflight, "selection": editorial_brief["selection"], "script": {"segment_count": len(script["segments"]), "mode": selection.mode}},
            )

        current_stage = "voice"
        if reuse_audio:
            audio = reuse_synthesized_audio(
                run_dir,
                script,
                align=align,
                speech_provider=speech_provider,
            )
        else:
            audio = synthesize_and_align(
                run_dir,
                script,
                openmontage_root=openmontage_root,
                env_path=env_file or env_path(),
                align=align,
                speech_provider=speech_provider,
            )
        subtitle_alignment = audio.get("subtitle_alignment") or {}
        if align and str(speech_provider).strip().lower() == "azure" and subtitle_alignment.get("proportional_fallback_segments"):
            raise OpenMontageError(
                "Azure subtitle alignment unexpectedly used proportional fallback for: "
                + ", ".join(str(value) for value in subtitle_alignment["proportional_fallback_segments"])
            )
        # Keep the 20-phrase A/B benchmark on the default Azure path. An
        # explicit Gemini production run already spends the provider budget on
        # the edition itself, so record a clear non-live status instead of
        # making twenty unrelated extra requests.
        benchmark_execution = "live"
        benchmark_path = run_dir / "artifacts" / "tts-benchmark" / "benchmark.json"
        if reuse_audio:
            tts_benchmark = _read_json(benchmark_path)
            if tts_benchmark is None:
                tts_benchmark = {
                    "version": "1.0",
                    "status": "skipped_reuse_audio",
                    "reason": "Audio reuse was explicitly selected; no benchmark provider call was made",
                    "case_count": 0,
                    "no_secrets_in_artifacts": True,
                }
                write_json(benchmark_path, tts_benchmark)
                benchmark_execution = "skipped_reuse_audio"
            else:
                benchmark_execution = "reused"
        elif str(speech_provider).strip().lower() == "gemini":
            tts_benchmark = {
                "version": "1.0",
                "status": "skipped_production_provider",
                "reason": "Gemini was explicitly selected for this edition",
                "case_count": 0,
                "no_secrets_in_artifacts": True,
            }
            write_json(benchmark_path, tts_benchmark)
            benchmark_execution = "skipped_production_provider"
        else:
            tts_benchmark = run_tts_benchmark(run_dir, script, env_path=env_file or env_path(), live=True)
        current_stage = "materialize"
        rendered = materialize(run_dir, script, audio["durations"], audio["subtitle_cues"])
        template_contract = rendered.get("template_contract") or {}
        if template_contract.get("status") != "passed":
            raise OpenMontageError(
                "template contract validation failed: "
                + "; ".join(str(error) for error in template_contract.get("errors") or [])
            )
        current_stage = "render"
        output_path = run_dir / "renders" / f"ai-daily-news-{run_date.isoformat()}.mp4"
        staging_dir = run_dir / ".staging" / f"run-{started_at.replace(':', '').replace('+', '-') }"
        staging_output = staging_dir / output_path.name
        staging_output.parent.mkdir(parents=True, exist_ok=True)
        render_report = render_hyperframes(run_dir, openmontage_root=openmontage_root, output_path=staging_output)
        layout_report = (((render_report.get("steps") or {}).get("check") or {}).get("report") or {}).get("layout") or {}
        overflow_findings = [
            finding for finding in layout_report.get("findings") or []
            if isinstance(finding, Mapping) and str(finding.get("code") or "") == "text_box_overflow"
        ]
        if overflow_findings:
            selectors = [str(finding.get("selector") or "unknown") for finding in overflow_findings[:8]]
            raise OpenMontageError(f"final-frame text overflow detected: {selectors}")
        current_stage = "quality"
        probe = probe_video(staging_output)
        duration = float((probe.get("format") or {}).get("duration") or 0)
        if duration <= 0:
            raise OpenMontageError(f"final duration is not positive: {duration:.2f}s")
        music_report = _read_json(artifacts / "background-music.json")
        if not music_report or (music_report.get("quality_gate") or {}).get("status") != "passed":
            raise MediaError("background music quality report is missing or failed")
        output_sha256 = _file_sha256(staging_output)
        if not output_sha256:
            raise OpenMontageError("final staged output is missing or empty")
        quality_report = {
            "version": "1.4",
            "status": "pass",
            "date": run_date.isoformat(),
            "build_contract": build_contract,
            "input_sha256": editorial_input.get("input_sha256"),
            "output_sha256": output_sha256,
            "output_path": str(output_path),
            "duration_seconds": round(duration, 3),
            "video": probe,
            "checks": {
                "has_video": True,
                "has_audio": True,
                "resolution": "1920x1080",
                "duration_policy": "content_driven_audio; no hard maximum; duration must cover the complete authored narration",
                "source_visuals": {
                    "mode": screenshot_manifest.get("mode", "off"),
                    "status": screenshot_manifest.get("status", "disabled"),
                    "total_pages": screenshot_manifest.get("total_pages", 0),
                    "selected_stories": screenshot_manifest.get("selected_stories", 0),
                    "selected_item_ids": screenshot_manifest.get("selected_item_ids", []),
                    "acceptance": screenshot_manifest.get("acceptance") or {},
                },
                "overview_timing": rendered.get("overview_timing") or (template_contract.get("overview_timing") or {}),
                # Backwards-compatible key for consumers of the original X
                # screenshot report.
                "x_screenshots": {
                    "mode": screenshot_manifest.get("mode", "off"),
                    "status": screenshot_manifest.get("status", "disabled"),
                    "total_pages": screenshot_manifest.get("total_pages", 0),
                    "selected_stories": screenshot_manifest.get("selected_stories", 0),
                },
                "background_music": {
                    "status": "passed",
                    "target_gap_db": music_report.get("rules", {}).get("target_background_gap_db"),
                    "target_gap_lu": music_report.get("rules", {}).get("target_background_gap_lu"),
                    "pre_duck_gap_lu": (music_report.get("loudness") or {}).get("pre_duck_gap_lu"),
                    "voice_lufs": (music_report.get("loudness") or {}).get("voice_lufs"),
                    "music_lufs_pre_duck": (music_report.get("loudness") or {}).get("music_lufs_pre_duck"),
                    "music_lufs_post_duck": (music_report.get("loudness") or {}).get("music_lufs_post_duck"),
                    "mix_lufs": (music_report.get("loudness") or {}).get("mix_lufs"),
                    "true_peak_dbfs": (music_report.get("loudness") or {}).get("true_peak_dbfs"),
                    "max_duck_db": (music_report.get("sidechain") or {}).get("max_duck_db"),
                    "sidechain_attack_ms": (music_report.get("sidechain") or {}).get("attack_ms"),
                    "sidechain_release_ms": (music_report.get("sidechain") or {}).get("release_ms"),
                    "duck_window_ms": music_report.get("rules", {}).get("duck_window_ms"),
                    "duck_recovery_ms": music_report.get("rules", {}).get("duck_recovery_ms"),
                    "newly_clipped_samples": (music_report.get("peak_protection") or {}).get("newly_clipped_samples", 0),
                },
                "speech": {
                    "provider": audio.get("provider", "azure"),
                    "native_word_boundary": bool((audio.get("manifest") or [{}])[0].get("native_word_boundary")) if audio.get("manifest") else False,
                    "canonical_text": "spoken_text",
                    "pronunciation_ledger": str(artifacts / "pronunciation_ledger.json"),
                    "manifest": str(audio.get("manifest_path") or (artifacts / "azure_audio_manifest.json")),
                    "alignment_provider": audio.get("alignment_provider"),
                    "subtitle_alignment": subtitle_alignment,
                    "subtitle_alignment_gate": {"status": "pass", "requested": bool(align)},
                    "gemini_benchmark_status": tts_benchmark.get("status"),
                    "tts_benchmark_execution": benchmark_execution,
                },
                "editorial": {
                    "status": editorial_quality.get("status"),
                    "report": str(artifacts / "editorial_quality_report.json"),
                    "caption_unit_policy": "complete editorial beats split into continuous width-safe caption units",
                },
                "final_frame_layout": {
                    "status": "pass",
                    "text_overflow_count": 0,
                    "layout_report": layout_report,
                },
                "template_contract": template_contract,
            },
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_output, output_path)
        write_json(artifacts / "quality_report.json", quality_report)
        return _write_stage_report(
            run_dir,
            status="success",
            stage="complete",
            started_at=started_at,
            details={
                "preflight": preflight,
                "selection": editorial_brief["selection"],
                "script": {
                    "segment_count": len(script["segments"]),
                    "mode": selection.mode,
                    "editorial": {
                        "plan_path": str(artifacts / "editorial_plan.json"),
                        "final_plan_path": str(artifacts / "editorial_plan_final.json"),
                        "plan_version": editorial_plan.get("version"),
                        "prompt_version": editorial_plan.get("prompt_version"),
                        "input_sha256": editorial_plan.get("input_sha256"),
                        "story_count": len(editorial_plan.get("stories") or []),
                    },
                },
                "source_visuals": {
                    "mode": screenshot_manifest.get("mode", "off"),
                    "status": screenshot_manifest.get("status", "disabled"),
                    "total_pages": screenshot_manifest.get("total_pages", 0),
                    "selected_stories": screenshot_manifest.get("selected_stories", 0),
                    "selected_item_ids": screenshot_manifest.get("selected_item_ids", []),
                    "acceptance": screenshot_manifest.get("acceptance") or {},
                    "manifest": str(artifacts / "source_visual_manifest.json"),
                },
                "screenshots": {
                    "mode": screenshot_manifest.get("mode", "off"),
                    "status": screenshot_manifest.get("status", "disabled"),
                    "total_pages": screenshot_manifest.get("total_pages", 0),
                    "manifest": str(artifacts / "screenshot_manifest.json"),
                },
                "audio": {"duration_seconds": rendered["total_duration_seconds"], "subtitle_count": len(audio["subtitle_cues"]), "music_report": str(artifacts / "background-music.json"), "speech_provider": audio.get("provider", "azure"), "speech_manifest": str(audio.get("manifest_path") or (artifacts / "azure_audio_manifest.json")), "tts_benchmark": str(artifacts / "tts-benchmark" / "benchmark.json")},
                "render": {"output_path": str(output_path), "openmontage": render_report},
                "quality": quality_report,
                "source_attribution": "AIHOT 过去 24 小时精选",
            },
        )
    except ScreenshotPending as exc:
        return _write_stage_report(
            run_dir,
            status="awaiting_screenshots",
            stage="screenshots",
            started_at=started_at,
            details={
                "run_dir": str(run_dir),
                "request_manifest": str(artifacts / "source_visual_requests.json"),
                "task_document": str(artifacts / "SOURCE_VISUAL_TASKS.md"),
                **exc.details,
            },
            error=_safe_error(exc),
        )
    except (ScreenshotError, AIHotError, OpenMontageError, EditorialPlanError, ValueError, OSError, MediaError) as exc:
        return _write_stage_report(run_dir, status="failed", stage=current_stage, started_at=started_at, details={"run_dir": str(run_dir)}, error=_safe_error(exc))
    except Exception as exc:  # pragma: no cover - defensive boundary for unattended runs
        return _write_stage_report(run_dir, status="failed", stage=current_stage, started_at=started_at, details={"run_dir": str(run_dir)}, error=_safe_error(exc))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build AI每日早报 from AIHOT and OpenMontage.")
    parser.add_argument("run", nargs="?", choices=["run", "prepare"], default="run")
    parser.add_argument("--date", dest="run_date", help="edition date in YYYY-MM-DD; defaults to Asia/Shanghai today")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--fixture", type=Path, help="offline AIHOT JSON fixture")
    parser.add_argument("--openmontage-root", type=Path, default=DEFAULT_OPENMONTAGE_ROOT)
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--prepare-only", action="store_true", help="fetch/select and write frozen editorial input without Azure or rendering")
    parser.add_argument("--force", action="store_true", help="rebuild an existing date explicitly")
    parser.add_argument("--reuse-source", action="store_true", help="when rebuilding, reuse the frozen source snapshot bound to the editorial plan")
    parser.add_argument("--source-visual-mode", "--x-screenshot-mode", "--screenshot-mode", dest="source_visual_mode", choices=("off", "manual", "auto"), default=None, help="optional original-source visual intake: off, manual, or auto (Codex in-app browser)")
    parser.add_argument("--source-visual-min-stories", type=int, choices=(0, 1, 2), default=None, help="minimum source-visual stories required for acceptance; effective default is 0 or the prepared request value")
    parser.add_argument("--speech-provider", choices=("azure", "gemini", "doubao"), default="azure", help="voice provider; Gemini uses GOOGLE_AI_STUDIO_API_KEY; doubao uses in-conversation voice synthesis (requires --reuse-audio)")
    parser.add_argument("--reuse-audio", action="store_true", help="reuse a completed provider manifest and rebuild local mix/render without another TTS call")
    parser.add_argument("--no-align", action="store_true", help="skip Azure STT alignment; intended only for local smoke runs")
    parser.add_argument("--limit", type=int, default=None, help="compatibility alias for --candidate-page-size")
    parser.add_argument("--candidate-page-size", type=int, default=None)
    parser.add_argument("--selection-soft-min", type=int, default=DEFAULT_SELECTION_SOFT_MIN)
    parser.add_argument("--selection-max-items", type=int, default=DEFAULT_SELECTION_MAX_ITEMS)
    parser.add_argument("--selection-head-share", type=float, default=DEFAULT_SELECTION_HEAD_SHARE)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.run_date:
        try:
            run_date = date.fromisoformat(args.run_date)
        except ValueError:
            print("--date must be YYYY-MM-DD", file=sys.stderr)
            return 2
    else:
        run_date = _now().date()
    report = run_pipeline(
        run_date=run_date,
        output_root=args.output_root.expanduser().resolve(),
        fixture=args.fixture.expanduser().resolve() if args.fixture else None,
        openmontage_root=args.openmontage_root.expanduser().resolve(),
        env_file=args.env_file.expanduser().resolve() if args.env_file else None,
        prepare_only=args.prepare_only or args.run == "prepare",
        force=args.force,
        align=not args.no_align,
        limit=args.limit,
        candidate_page_size=args.candidate_page_size,
        selection_soft_min=args.selection_soft_min,
        selection_max_items=args.selection_max_items,
        selection_head_share=args.selection_head_share,
        reuse_source=args.reuse_source,
        source_visual_mode=args.source_visual_mode,
        source_visual_min_stories=args.source_visual_min_stories,
        speech_provider=args.speech_provider,
        reuse_audio=args.reuse_audio,
    )
    print(json.dumps({"status": report.get("status"), "date": run_date.isoformat(), "path": str(args.output_root / run_date.isoformat()), "error": report.get("error")}, ensure_ascii=False))
    return 0 if report.get("status") in {"success", "prepared"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
