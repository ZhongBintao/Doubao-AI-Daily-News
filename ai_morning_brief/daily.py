"""Cloud-computer native daily orchestrator for AI每日早报.

Single entry point that runs the entire pipeline end-to-end with minimal
agent intervention.  State is tracked in ``outputs/YYYY-MM-DD/run_state.json``;
the default behaviour is idempotent resume — re-running continues from the
first incomplete stage.

Stages
------
  bootstrap    environment self-check + git pull                 (automatic)
  fetch        AIHOT fetch + selection + freeze                  (automatic)
  editorial    [AGENT] write editorial_plan.json                 (handoff)
  script_tts   generate narration_plan + tts_manifest            (automatic)
  voice_clone  [AGENT] synthesize segments via audio_to_audio_plus (handoff)
  render       audio finalize + mix + subtitles + HyperFrames    (automatic)
  cover        [AGENT] generate 3 covers via image_edit          (handoff)
  release      release copy + package assembly                    (automatic)
  report       final summary                                      (automatic)

Usage
-----
  python -m ai_morning_brief.daily --date 2026-09-06
  python -m ai_morning_brief.daily --date 2026-09-06 --force
  python -m ai_morning_brief.daily --date 2026-09-06 --status
  python -m ai_morning_brief.daily --date 2026-09-06 --skip cover,release
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .config import (
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_OPENMONTAGE_ROOT,
    DEFAULT_TTS_MODE,
    DEFAULT_VOICE_CLONE_REFERENCE_AUDIO,
    DOUBAO_VOICE_CLONE_PROVIDER,
    REPO_ROOT,
    env_path,
)
from .media import write_json

DAILY_VERSION = "1.0.0"
TIMEZONE = "Asia/Shanghai"
EXIT_AGENT_HANDOFF = 10
EXIT_FAILURE = 2

# Stage order — do not reorder without updating the dependency logic.
STAGES: tuple[str, ...] = (
    "bootstrap",
    "fetch",
    "editorial",
    "script_tts",
    "voice_clone",
    "render",
    "cover",
    "release",
    "report",
)

AGENT_STAGES = frozenset({"editorial", "voice_clone", "cover"})

STAGE_LABELS: dict[str, str] = {
    "bootstrap": "环境自检与代码更新",
    "fetch": "AIHOT 素材冻结与选题",
    "editorial": "编辑写稿",
    "script_tts": "旁白脚本与 TTS 清单生成",
    "voice_clone": "语音克隆合成",
    "render": "音频混音与视频渲染",
    "cover": "封面生成",
    "release": "发布文案与整合包",
    "report": "完成汇总",
}


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))


def _state_path(run_dir: Path) -> Path:
    return run_dir / "run_state.json"


def load_state(run_dir: Path) -> dict[str, Any]:
    path = _state_path(run_dir)
    if not path.is_file():
        return {
            "version": DAILY_VERSION,
            "date": run_dir.name,
            "created_at": _now().isoformat(),
            "stages": {name: {"status": "pending", "started_at": None, "finished_at": None, "error": None, "artifacts": {}} for name in STAGES},
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    # Backfill any stages added after the state file was created.
    for name in STAGES:
        data.setdefault("stages", {}).setdefault(name, {"status": "pending", "started_at": None, "finished_at": None, "error": None, "artifacts": {}})
    return data


def save_state(run_dir: Path, state: Mapping[str, Any]) -> None:
    write_json(_state_path(run_dir), dict(state))


def _set_stage(state: dict[str, Any], name: str, **fields: Any) -> None:
    stage = state["stages"].setdefault(name, {"status": "pending"})
    stage.update(fields)


def _stage_status(state: Mapping[str, Any], name: str) -> str:
    return str(state.get("stages", {}).get(name, {}).get("status", "pending"))


def _first_incomplete(state: Mapping[str, Any], skip: frozenset[str]) -> str | None:
    for name in STAGES:
        if name in skip:
            continue
        if _stage_status(state, name) != "done":
            return name
    return None


# ---------------------------------------------------------------------------
# Handoff printing
# ---------------------------------------------------------------------------

def _print_handoff(stage: str, run_dir: Path, instructions: str) -> None:
    bar = "=" * 64
    print(f"\n{bar}")
    print(f"  AGENT HANDOFF: {stage} — {STAGE_LABELS.get(stage, stage)}")
    print(f"{bar}")
    print(f"  工作目录: {run_dir}")
    print()
    print(instructions.rstrip())
    print()
    print(f"  完成后重新运行:")
    print(f"    python -m ai_morning_brief.daily --date {run_dir.name}")
    print(f"{bar}\n")


# ---------------------------------------------------------------------------
# Stage: bootstrap
# ---------------------------------------------------------------------------

def run_bootstrap(run_dir: Path, state: dict[str, Any]) -> None:
    """Git pull + environment self-check.  Idempotent and non-destructive."""
    artifacts: dict[str, Any] = {}

    # 1. Git pull (best-effort; failure does not block the run).
    if (REPO_ROOT / ".git").is_dir():
        try:
            result = subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60, check=False,
            )
            artifacts["git_pull"] = {"returncode": result.returncode, "stdout": (result.stdout or "")[-500:], "stderr": (result.stderr or "")[-500:]}
        except (OSError, subprocess.TimeoutExpired) as exc:
            artifacts["git_pull"] = {"error": str(exc)[:300]}
    else:
        artifacts["git_pull"] = {"skipped": "no .git directory"}

    # 2. Verify / repair the local environment.
    local_env = REPO_ROOT / ".local" / "env.sh"
    venv_python = REPO_ROOT / "OpenMontage" / ".venv" / "bin" / "python"
    needs_bootstrap = not local_env.is_file() or not venv_python.is_file()
    if needs_bootstrap:
        bootstrap_script = REPO_ROOT / "scripts" / "bootstrap_env.sh"
        if bootstrap_script.is_file():
            result = subprocess.run(
                ["bash", str(bootstrap_script)],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600, check=False,
            )
            artifacts["bootstrap_env"] = {"returncode": result.returncode, "stdout_tail": (result.stdout or "")[-800:], "stderr_tail": (result.stderr or "")[-500:]}
            if result.returncode != 0:
                raise RuntimeError(f"bootstrap_env.sh failed (exit {result.returncode}): {(result.stderr or '')[-300:]}")
        else:
            raise RuntimeError(f"bootstrap script not found: {bootstrap_script}")
    else:
        artifacts["bootstrap_env"] = {"skipped": "environment already present"}

    # 3. Inject project-local bin paths into the current process environment
    #    so subsequent subprocess calls find ffmpeg / node without needing
    #    the caller to source .local/env.sh manually.
    local_bin = REPO_ROOT / ".local" / "bin"
    node_bin = REPO_ROOT / ".local" / "node" / "bin"
    extra_paths = [str(p) for p in (local_bin, node_bin) if p.is_dir()]
    if extra_paths:
        os.environ["PATH"] = ":".join(extra_paths) + ":" + os.environ.get("PATH", "")
    artifacts["path_injected"] = extra_paths

    # 4. Disk space sanity check (warning only).
    try:
        usage = shutil.disk_usage(str(run_dir))
        free_gb = usage.free / (1024 ** 3)
        artifacts["disk_free_gb"] = round(free_gb, 2)
        if free_gb < 0.5:
            print(f"  [WARN] 磁盘剩余空间不足: {free_gb:.2f} GB", file=sys.stderr)
    except OSError:
        pass

    # 5. Verify reference audio exists.
    ref = DEFAULT_VOICE_CLONE_REFERENCE_AUDIO
    artifacts["reference_audio"] = {"path": str(ref), "exists": ref.is_file()}

    _set_stage(state, "bootstrap", status="done", finished_at=_now().isoformat(), artifacts=artifacts)


# ---------------------------------------------------------------------------
# Stage: fetch
# ---------------------------------------------------------------------------

def run_fetch(run_dir: Path, state: dict[str, Any], run_date: date) -> None:
    """Run pipeline prepare to freeze AIHOT source and generate editorial input."""
    from .pipeline import run_pipeline

    report = run_pipeline(
        run_date=run_date,
        output_root=run_dir.parent,
        openmontage_root=DEFAULT_OPENMONTAGE_ROOT,
        env_file=env_path(),
        prepare_only=True,
        source_visual_mode="off",
    )
    status = str(report.get("status", ""))
    if status not in {"prepared", "success"}:
        raise RuntimeError(f"pipeline prepare failed: status={status}, error={report.get('error')}")

    selection = (report.get("details") or {}).get("selection") or {}
    selected_count = int(selection.get("selected_count", 0))
    if str(selection.get("status") or "") == "failure":
        raise RuntimeError(f"AIHOT 选题失败: {selection.get('reason') or 'unknown selection failure'}")

    artifacts = {
        "selected_count": selected_count,
        "eligible_count": int(selection.get("eligible_count", 0)),
        "editorial_input": str(run_dir / "artifacts" / "editorial_input.json"),
        "writing_request": str(run_dir / "artifacts" / "writing_request.json"),
        "editorial_draft": str(run_dir / "artifacts" / "editorial_draft.json"),
    }
    _set_stage(state, "fetch", status="done", finished_at=_now().isoformat(), artifacts=artifacts)


# ---------------------------------------------------------------------------
# Stage: editorial (AGENT HANDOFF)
# ---------------------------------------------------------------------------

def run_editorial_handoff(run_dir: Path, state: dict[str, Any]) -> bool:
    """Generate editorial_task.json and print handoff instructions.

    Returns True if the agent needs to take over (handoff printed), False if
    editorial_plan.json already exists and is valid.
    """
    plan_path = run_dir / "artifacts" / "editorial_plan.json"
    if plan_path.is_file() and plan_path.stat().st_size > 0:
        # Already written; mark done and let script_tts pick it up.
        _set_stage(state, "editorial", status="done", finished_at=_now().isoformat(),
                    artifacts={"editorial_plan": str(plan_path)})
        return False

    # Build the consolidated editorial task file.
    artifacts_dir = run_dir / "artifacts"
    editorial_input = _read_json(artifacts_dir / "editorial_input.json")
    writing_request = _read_json(artifacts_dir / "writing_request.json")
    editorial_draft = _read_json(artifacts_dir / "editorial_draft.json")

    task = {
        "version": "1.0",
        "date": run_dir.name,
        "task": "按照 v5 规范生成 editorial_plan.json",
        "input_files": {
            "editorial_input": "artifacts/editorial_input.json",
            "writing_request": "artifacts/writing_request.json",
            "editorial_draft": "artifacts/editorial_draft.json (结构参考，不要照抄)",
        },
        "output_file": "artifacts/editorial_plan.json",
        "editorial_input": editorial_input,
        "writing_request": writing_request,
        "editorial_draft": editorial_draft,
        "validation": {
            "plan_version": "5.0",
            "writer_version": "4.1",
            "required_story_fields": ["subject", "navigation_title", "overview_text", "overview_claim_ids", "presentation_order"],
            "required_beat_fields": ["beat_id"],
            "rules": [
                "每条故事只能出现一次，绑定对应 source item 和 claim",
                "overview_text 必须引用 title 之外的 summary/detail claim",
                "beat 必须解释具体事件与事实证据，不设字数上限",
                "卡片数量由有效 claim 决定，每张卡有稳定 id/subject",
                "严禁添加来源没有的数字、因果、预测、建议、品牌名",
                "不把 score、URL、AIHOT 链接写进视频文案或卡片",
            ],
            "validate_command": (
                "OpenMontage/.venv/bin/python -c \""
                "import json,sys; from pathlib import Path; "
                "from ai_morning_brief.editorial import build_editorial_quality_report, load_editorial_plan; "
                "from ai_morning_brief.models import SourceItem; "
                f"d='{run_dir.name}'; ad=Path('outputs')/d/'artifacts'; "
                "plan=load_editorial_plan(ad/'editorial_plan.json'); "
                "ei=json.loads((ad/'editorial_input.json').read_text(encoding='utf-8')); "
                "si={str(i.get('id') or i.get('item_id')):SourceItem.from_mapping(i) for i in ei.get('items',[]) if isinstance(i,dict)}; "
                "r=build_editorial_quality_report(plan,ei,si); "
                "json.dump(r,(ad/'editorial_quality_report.json').open('w',encoding='utf-8'),ensure_ascii=False,indent=2); "
                "print('Status:',r.get('status')); "
                "[print(' -',e) for e in (r.get('errors') or [])[:10]]; "
                "sys.exit(0 if r.get('status')=='pass' else 1)\""
            ),
        },
    }
    write_json(artifacts_dir / "editorial_task.json", task)

    instructions = (
        "  请完成编辑写稿：\n"
        "  1. 读取任务文件: artifacts/editorial_task.json\n"
        "     （包含冻结资讯 editorial_input、写稿请求 writing_request、草稿 editorial_draft、校验标准）\n"
        "  2. 按 v5 规范写入: artifacts/editorial_plan.json\n"
        "     - 计划版本 5.0，writer.version 4.1\n"
        "     - 每条故事有 subject / navigation_title / overview_text / overview_claim_ids / presentation_order\n"
        "     - 每个 beat 有稳定 beat_id，内容完整解释事件\n"
        "     - 严禁添加来源没有的数字、因果、预测、建议\n"
        "  3. 运行校验命令（在任务文件的 validation.validate_command 中）\n"
        "     确保 editorial_quality_report.json 的 status = pass\n"
        "     不通过则修改 editorial_plan.json 后重新校验，最多重试 2 次"
    )
    _print_handoff("editorial", run_dir, instructions)
    return True


# ---------------------------------------------------------------------------
# Stage: script_tts
# ---------------------------------------------------------------------------

def run_script_tts(run_dir: Path, state: dict[str, Any], run_date: date) -> None:
    """Generate narration_plan.json and tts_manifest.json."""
    import json as _json
    from datetime import date as _date

    from .editorial import load_editorial_plan
    from .models import SourceItem
    from .script import build_script_from_editorial_plan, validate_script
    from .writing import finalize_editorial_plan
    from .doubao_tts_adapter import generate_tts_manifest

    artifacts_dir = run_dir / "artifacts"

    # 1. Load and finalize editorial plan.
    editorial_input = _json.loads((artifacts_dir / "editorial_input.json").read_text(encoding="utf-8"))
    plan = load_editorial_plan(artifacts_dir / "editorial_plan.json")
    plan, _ = finalize_editorial_plan(plan)
    write_json(artifacts_dir / "editorial_plan_final.json", plan)

    # 2. Build selection object (lightweight shim matching the pipeline's shape).
    source_items: dict[str, SourceItem] = {}
    for item in editorial_input.get("items", []):
        if not isinstance(item, Mapping):
            continue
        item_id = str(item.get("id") or item.get("item_id") or "").strip()
        if item_id:
            source_items[item_id] = SourceItem.from_mapping(item)
    selection_data = editorial_input.get("selection") or {}
    selection_items = tuple(source_items[item_id] for item_id in selection_data.get("item_ids", []) if item_id in source_items)

    class _Selection:
        def __init__(self) -> None:
            self.items = selection_items
            # Current editorial_input uses ``mode``.  ``status`` is accepted
            # only as a compatibility fallback for older dated snapshots;
            # mixing the two names here previously caused a KeyError at the
            # script_tts handoff even though prepare had succeeded.
            self.mode = str(selection_data.get("mode") or selection_data.get("status") or "failure")
            self.category_counts = selection_data.get("category_counts", {})
            self.reason = selection_data.get("reason", "")
            self.eligible_count = selection_data.get("eligible_count", 0)
            self.policy = selection_data.get("policy", {})
            self.provenance = selection_data.get("provenance", {})
            self.selection_metadata: dict[str, Any] = {}

    # 3. Build narration script.
    script = build_script_from_editorial_plan(
        _Selection(),
        run_date=run_date,
        editorial_input=editorial_input,
        editorial_plan=plan,
    )
    errors = validate_script(script, source_items, editorial_input)
    if errors:
        raise RuntimeError("script validation failed: " + "; ".join(errors[:5]))
    write_json(artifacts_dir / "narration_plan.json", script)

    # 4. Generate TTS manifest (voice-clone mode by default).
    tts_manifest = generate_tts_manifest(run_dir, tts_mode=DEFAULT_TTS_MODE)

    _set_stage(state, "script_tts", status="done", finished_at=_now().isoformat(), artifacts={
        "narration_plan": str(artifacts_dir / "narration_plan.json"),
        "tts_manifest": str(artifacts_dir / "tts_manifest.json"),
        "segment_count": len(script.get("segments", [])),
        "tts_segment_count": tts_manifest.get("segment_count", 0),
    })


# ---------------------------------------------------------------------------
# Stage: voice_clone (AGENT HANDOFF)
# ---------------------------------------------------------------------------

def run_voice_clone_handoff(run_dir: Path, state: dict[str, Any]) -> bool:
    """Print handoff instructions for agent-driven voice cloning.

    Returns True if handoff is needed, False if all audio is already complete.
    """
    from .doubao_tts_adapter import verify_audio_complete

    all_complete, missing = verify_audio_complete(run_dir)
    if all_complete:
        _set_stage(state, "voice_clone", status="done", finished_at=_now().isoformat(),
                    artifacts={"segments": "all complete"})
        return False

    manifest_path = run_dir / "artifacts" / "tts_manifest.json"
    manifest = _read_json(manifest_path) or {}
    segments = manifest.get("segments", [])
    ref = manifest.get("reference_audio") or {}
    ref_path = str(ref.get("path") or DEFAULT_VOICE_CLONE_REFERENCE_AUDIO)

    seg_list = "\n".join(
        f"    {i+1}. {s['segment_id']:12s} ({s['char_count']:3d}字) -> {s['output_path']}"
        for i, s in enumerate(segments)
    )
    missing_str = ", ".join(missing) if missing else "（首次合成）"

    instructions = (
        f"  请完成语音克隆合成（共 {len(segments)} 段，缺失: {missing_str}）：\n"
        f"  参考音频: {ref_path}\n"
        f"  工具: audio_to_audio_plus\n"
        f"  对每一段执行：\n"
        f"    1. 调用 audio_to_audio_plus，@音频1 = {ref_path}\n"
        f"    2. prompt: \"用参考音频的音色、语速和朗读风格，清晰朗读以下文字，不增删字词，无背景音无杂音：{{spoken_text}}\"\n"
        f"    3. 将返回的音频保存到 output_path\n"
        f"  段落清单：\n{seg_list}\n"
        f"  注意: 必须严格使用 spoken_text 原文，不增删字词；每段单独合成"
    )
    _print_handoff("voice_clone", run_dir, instructions)
    return True


# ---------------------------------------------------------------------------
# Stage: render
# ---------------------------------------------------------------------------

def run_render(run_dir: Path, state: dict[str, Any], run_date: date) -> None:
    """Verify audio, finalize manifest, then run full pipeline render."""
    from .doubao_tts_adapter import verify_audio_complete, verify_and_finalize
    from .pipeline import run_pipeline

    # 1. Verify all segment audio exists.
    all_complete, missing = verify_audio_complete(run_dir)
    if not all_complete:
        # Reset voice_clone stage so resume goes back to the handoff.
        _set_stage(state, "voice_clone", status="pending", error=f"missing segments: {', '.join(missing)}")
        _set_stage(state, "render", status="pending")
        save_state(run_dir, state)
        raise RuntimeError(
            f"音频未完成，缺失 {len(missing)} 段: {', '.join(missing[:10])}。"
            f"请回到 voice_clone 阶段补合成后重新运行。"
        )

    # 2. Finalize audio (normalize + manifest).
    verify_and_finalize(run_dir, tts_mode=DEFAULT_TTS_MODE)

    # 3. Run full pipeline render with audio reuse.
    report = run_pipeline(
        run_date=run_date,
        output_root=run_dir.parent,
        openmontage_root=DEFAULT_OPENMONTAGE_ROOT,
        env_file=env_path(),
        prepare_only=False,
        force=True,
        reuse_source=True,
        reuse_audio=True,
        speech_provider=DOUBAO_VOICE_CLONE_PROVIDER,
        source_visual_mode="off",
    )
    status = str(report.get("status", ""))
    if status != "success":
        failed_stage = (report.get("details") or {}).get("preflight", {}).get("failed_stage") or report.get("failed_stage")
        raise RuntimeError(f"pipeline render failed: status={status}, stage={failed_stage}, error={report.get('error')}")

    output_path = run_dir / "renders" / f"ai-daily-news-{run_date.isoformat()}.mp4"
    quality = _read_json(run_dir / "artifacts" / "quality_report.json") or {}
    duration = float(quality.get("duration_seconds", 0))

    _set_stage(state, "render", status="done", finished_at=_now().isoformat(), artifacts={
        "output_video": str(output_path),
        "duration_seconds": round(duration, 2),
        "quality_status": quality.get("status"),
        "size_mb": round(output_path.stat().st_size / (1024 * 1024), 2) if output_path.is_file() else 0,
    })


# ---------------------------------------------------------------------------
# Stage: cover (AGENT HANDOFF)
# ---------------------------------------------------------------------------

def run_cover_handoff(run_dir: Path, state: dict[str, Any]) -> bool:
    """Generate cover task and print handoff instructions.

    Returns True if handoff is needed, False if all 3 covers already exist.
    """
    covers_dir = run_dir / "release-kit" / "covers"
    if _is_no_news_edition(run_dir):
        covers_dir.mkdir(parents=True, exist_ok=True)
        write_json(covers_dir / "cover_task.json", {
            "version": "1.0",
            "date": run_dir.name,
            "status": "skipped_no_news",
            "reason": "零讯短报没有来源新闻，不生成来源绑定封面",
            "expected_files": [],
        })
        _set_stage(
            state,
            "cover",
            status="done",
            finished_at=_now().isoformat(),
            artifacts={"skipped": "no-news edition", "cover_task": str(covers_dir / "cover_task.json")},
        )
        return False
    expected = ["16x9.png", "3x4.png", "9x16.png"]
    existing = [f for f in expected if (covers_dir / f).is_file() and (covers_dir / f).stat().st_size > 0]
    if len(existing) == 3:
        # release_workflow requires a schema-5 cover manifest. The handoff
        # only produces PNGs, so materialize the manifest before release.
        _ensure_cover_manifest(covers_dir)
        _set_stage(state, "cover", status="done", finished_at=_now().isoformat(),
                    artifacts={
                        "covers": [str(covers_dir / f) for f in expected],
                        "cover_manifest": str(covers_dir / "cover_manifest.json"),
                    })
        return False

    # Try to generate cover_request.json via cover_workflow.py prepare.
    cover_script = REPO_ROOT / "skills" / "ai-brief-cover-generator" / "scripts" / "cover_workflow.py"
    editorial_input_path = run_dir / "artifacts" / "editorial_input.json"
    cover_request_path = covers_dir / "cover_request.json"

    if cover_script.is_file() and editorial_input_path.is_file() and not cover_request_path.is_file():
        covers_dir.mkdir(parents=True, exist_ok=True)
        # Use the first selected item as the cover story.
        ei = _read_json(editorial_input_path) or {}
        items = ei.get("items", [])
        first_item = items[0] if items else {}
        item_id = str(first_item.get("id") or first_item.get("item_id") or "")
        headline = str(first_item.get("title", "AI 每日早报"))[:80]
        subheadline = str(first_item.get("summary", ""))[:120]
        visual_brief = f"基于资讯: {headline}"

        cmd = [
            sys.executable, str(cover_script), "prepare",
            "--editorial-input", str(editorial_input_path),
            "--item-id", item_id,
            "--headline", headline,
            "--subheadline", subheadline,
            "--visual-brief", visual_brief,
            "--image-provider", "seedream",
            "--force",
        ]
        result = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60, check=False)
        if result.returncode != 0:
            print(f"  [WARN] cover_workflow.py prepare failed: {(result.stderr or '')[-300:]}", file=sys.stderr)

    # Build cover task for the agent.
    cover_request = _read_json(cover_request_path) if cover_request_path.is_file() else {}
    ratios = cover_request.get("ratios") or cover_request.get("ratio_requests") or {}

    task = {
        "version": "1.0",
        "date": run_dir.name,
        "output_dir": str(covers_dir),
        "expected_files": expected,
        "cover_request": str(cover_request_path) if cover_request_path.is_file() else None,
        "reference_images": [
            str(REPO_ROOT / "skills" / "ai-brief-cover-generator" / "assets" / "references" / "cover-style-system-16x9.png"),
        ],
        "ratios": {
            "16:9": {"width": 1920, "height": 1080, "output": "16x9.png", "prompt": _extract_prompt(ratios, "16:9")},
            "3:4": {"width": 1080, "height": 1440, "output": "3x4.png", "prompt": _extract_prompt(ratios, "3:4")},
            "9:16": {"width": 1080, "height": 1920, "output": "9x16.png", "prompt": _extract_prompt(ratios, "9:16")},
        },
        "instructions": (
            "使用 image_edit 工具（不是 image_gen），按顺序生成 16:9 → 3:4 → 9:16 三张封面。"
            "每张参考图包含 cover-style-system-16x9.png（风格系统参考）。"
            "生成后将图片保存到 output_dir 下对应文件名。"
            "16:9 生成后，3:4 和 9:16 可额外参考已生成的 16x9.png。"
        ),
    }
    write_json(covers_dir / "cover_task.json", task)

    missing_covers = [f for f in expected if not (covers_dir / f).is_file()]
    instructions = (
        f"  请生成封面（缺失: {', '.join(missing_covers)}）：\n"
        f"  1. 读取任务文件: release-kit/covers/cover_task.json\n"
        f"  2. 使用 image_edit 工具，参考 cover-style-system-16x9.png\n"
        f"  3. 按顺序生成 16:9 (1920x1080) → 3:4 (1080x1440) → 9:16 (1080x1920)\n"
        f"  4. 保存到 release-kit/covers/ 下对应文件名 (16x9.png / 3x4.png / 9x16.png)\n"
        f"  5. 每张封面的 prompt 在 cover_task.json 的 ratios 字段中"
    )
    _print_handoff("cover", run_dir, instructions)
    return True


def _ensure_cover_manifest(covers_dir: Path) -> None:
    """Create the schema-5 cover manifest after the three images exist."""

    manifest = covers_dir / "cover_manifest.json"
    if manifest.is_file() and manifest.stat().st_size > 0:
        return
    cover_script = REPO_ROOT / "skills" / "ai-brief-cover-generator" / "scripts" / "cover_workflow.py"
    request = covers_dir / "cover_request.json"
    if not (cover_script.is_file() and request.is_file()):
        print("  [WARN] cannot build cover manifest: cover_workflow.py or cover_request.json missing", file=sys.stderr)
        return
    cmd = [
        sys.executable, str(cover_script), "record",
        "--request", str(request),
        "--image", f"16:9={covers_dir / '16x9.png'}",
        "--image", f"3:4={covers_dir / '3x4.png'}",
        "--image", f"9:16={covers_dir / '9x16.png'}",
        "--force",
    ]
    result = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0:
        print(f"  [WARN] cover_workflow record failed: {(result.stderr or '')[-300:]}", file=sys.stderr)


def _extract_prompt(ratios: Any, key: str) -> str:
    """Extract the seedream_prompt from a cover request ratios structure."""
    if not isinstance(ratios, Mapping):
        return ""
    entry = ratios.get(key) or ratios.get(key.replace(":", "")) or {}
    if isinstance(entry, Mapping):
        return str(entry.get("seedream_prompt") or entry.get("prompt") or "")
    return ""


# ---------------------------------------------------------------------------
# Stage: release
# ---------------------------------------------------------------------------

def run_release(run_dir: Path, state: dict[str, Any], run_date: date) -> None:
    """Generate release copy and assemble the publish package."""
    if _is_no_news_edition(run_dir):
        release_dir = run_dir / "release-kit"
        release_dir.mkdir(parents=True, exist_ok=True)
        write_json(release_dir / "release_status.json", {
            "version": "1.0",
            "date": run_date.isoformat(),
            "status": "skipped_no_news",
            "reason": "零讯短报没有来源新闻，不生成来源绑定发布包",
        })
        _set_stage(
            state,
            "release",
            status="done",
            finished_at=_now().isoformat(),
            artifacts={"skipped": "no-news edition", "release_status": str(release_dir / "release_status.json")},
        )
        return
    release_script = REPO_ROOT / "skills" / "ai-brief-release-kit" / "scripts" / "release_workflow.py"
    if not release_script.is_file():
        print("  [WARN] release_workflow.py not found, skipping release stage", file=sys.stderr)
        _set_stage(state, "release", status="done", finished_at=_now().isoformat(),
                    artifacts={"skipped": "release script not found"})
        return

    editorial_input_path = run_dir / "artifacts" / "editorial_input.json"
    release_dir = run_dir / "release-kit"
    release_plan_path = release_dir / "release_plan.json"
    cover_manifest_path = release_dir / "covers" / "cover_manifest.json"
    video_path = run_dir / "renders" / f"ai-daily-news-{run_date.isoformat()}.mp4"

    # 1. Prepare release plan (generate description from editorial input).
    if not release_plan_path.is_file():
        ei = _read_json(editorial_input_path) or {}
        items = ei.get("items", [])
        # Build a concise description from the top 1-2 items.
        desc_parts = []
        for item in items[:2]:
            title = str(item.get("title", "")).strip()
            if title:
                desc_parts.append(title)
        description = "；".join(desc_parts) if desc_parts else f"AI每日早报{run_date.isoformat()}"
        primary_item_id = str(items[0].get("id") or items[0].get("item_id") or "") if items else ""

        cmd = [
            sys.executable, str(release_script), "prepare",
            "--editorial-input", str(editorial_input_path),
            "--description", description,
            "--primary-item-id", primary_item_id,
            "--output-dir", str(release_dir),
            "--force",
        ]
        result = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"release_workflow prepare failed: {(result.stderr or '')[-300:]}")

    # 2. Finalize package.
    cmd = [
        sys.executable, str(release_script), "finalize",
        "--release-plan", str(release_plan_path),
        "--run-dir", str(run_dir),
        "--video", str(video_path),
        "--cover-manifest", str(cover_manifest_path),
        "--output-dir", str(release_dir),
        "--force",
    ]
    result = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"release_workflow finalize failed: {(result.stderr or '')[-300:]}")

    package_dir = release_dir / "video-publish-package"
    _set_stage(state, "release", status="done", finished_at=_now().isoformat(), artifacts={
        "release_plan": str(release_plan_path),
        "package_dir": str(package_dir),
        "package_exists": package_dir.is_dir(),
    })


# ---------------------------------------------------------------------------
# Stage: report
# ---------------------------------------------------------------------------

def run_report(run_dir: Path, state: dict[str, Any], run_date: date) -> None:
    """Print final summary of all deliverables."""
    video_path = run_dir / "renders" / f"ai-daily-news-{run_date.isoformat()}.mp4"
    covers_dir = run_dir / "release-kit" / "covers"
    package_dir = run_dir / "release-kit" / "video-publish-package"
    quality = _read_json(run_dir / "artifacts" / "quality_report.json") or {}

    duration = float(quality.get("duration_seconds", 0))
    mins, secs = divmod(int(duration), 60)
    size_mb = round(video_path.stat().st_size / (1024 * 1024), 2) if video_path.is_file() else 0

    selection = _read_json(run_dir / "artifacts" / "selection_report.json") or {}
    selected_count = int(selection.get("selected_count", 0))

    cover_files = []
    for name in ("16x9.png", "3x4.png", "9x16.png"):
        p = covers_dir / name
        if p.is_file():
            cover_files.append(str(p))

    print()
    print("=" * 64)
    print(f"  AI 每日早报 {run_date.isoformat()} 生成完成")
    print("=" * 64)
    print(f"  视频: {video_path}")
    print(f"    分辨率: 1920x1080 | 时长: {mins}分{secs}秒 | 大小: {size_mb} MB")
    print(f"  资讯条数: {selected_count} 条")
    print(f"  语音: 豆包语音克隆 (audio_to_audio_plus + example-audio.mp3)")
    print(f"  字幕: 比例估算 (文字 100% 来自写稿文案)")
    if cover_files:
        print(f"  封面 ({len(cover_files)}/3):")
        for cf in cover_files:
            print(f"    {cf}")
    else:
        print(f"  封面: 未生成")
    if package_dir.is_dir():
        print(f"  发布文案包: {package_dir}")
    else:
        print(f"  发布文案包: 未生成")
    print(f"  完整产出目录: {run_dir}")
    print("=" * 64)
    print("  发布前请人工审听和审阅视频内容。")
    print("=" * 64)

    _set_stage(state, "report", status="done", finished_at=_now().isoformat(), artifacts={
        "video": str(video_path),
        "duration_seconds": round(duration, 2),
        "size_mb": size_mb,
        "covers": cover_files,
        "package_dir": str(package_dir) if package_dir.is_dir() else None,
    })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _is_no_news_edition(run_dir: Path) -> bool:
    editorial = _read_json(run_dir / "artifacts" / "editorial_input.json") or {}
    return str((editorial.get("selection") or {}).get("mode") or "") == "no-news"


# ---------------------------------------------------------------------------
# Main orchestration loop
# ---------------------------------------------------------------------------

STAGE_RUNNERS: dict[str, Callable[..., Any]] = {
    "bootstrap": run_bootstrap,
    "fetch": run_fetch,
    "script_tts": run_script_tts,
    "render": run_render,
    "release": run_release,
    "report": run_report,
}

AGENT_HANDOFF_RUNNERS: dict[str, Callable[..., bool]] = {
    "editorial": run_editorial_handoff,
    "voice_clone": run_voice_clone_handoff,
    "cover": run_cover_handoff,
}


def run_daily(
    *,
    run_date: date,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    force: bool = False,
    status_only: bool = False,
    skip: Sequence[str] = (),
    from_stage: str | None = None,
    to_stage: str | None = None,
) -> int:
    run_dir = output_root / run_date.isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)

    skip_set = frozenset(skip)

    # --force: clear state and start fresh.
    if force:
        state_path = _state_path(run_dir)
        if state_path.is_file():
            state_path.unlink()

    state = load_state(run_dir)

    # --status: print progress and exit.
    if status_only:
        _print_status(run_dir, state)
        return 0

    # Determine stage range.
    start_idx = STAGES.index(from_stage) if from_stage else 0
    end_idx = STAGES.index(to_stage) if to_stage else len(STAGES) - 1
    if from_stage:
        # Reset from_stage and everything after so they re-run.
        for name in STAGES[start_idx:]:
            _set_stage(state, name, status="pending", error=None)

    # Run stages in order.
    for name in STAGES[start_idx:end_idx + 1]:
        if name in skip_set:
            if _stage_status(state, name) != "done":
                _set_stage(state, name, status="skipped", finished_at=_now().isoformat())
            print(f"  [SKIP] {name} ({STAGE_LABELS.get(name, name)})")
            continue

        current_status = _stage_status(state, name)
        if current_status == "done":
            print(f"  [DONE] {name} ({STAGE_LABELS.get(name, name)}) — 已完成，跳过")
            continue

        print(f"\n  >>> {name} ({STAGE_LABELS.get(name, name)})")
        _set_stage(state, name, status="running", started_at=_now().isoformat(), error=None)
        save_state(run_dir, state)

        try:
            if name in AGENT_HANDOFF_RUNNERS:
                needs_handoff = AGENT_HANDOFF_RUNNERS[name](run_dir, state)
                save_state(run_dir, state)
                if needs_handoff:
                    # Agent needs to take over; exit and wait for resume.
                    return EXIT_AGENT_HANDOFF
                # Already complete; stage marked done inside the handoff function.
            else:
                runner = STAGE_RUNNERS[name]
                # Runners that need run_date get it; bootstrap doesn't. Fetch
                # and render derive their output root from run_dir so the
                # caller's --output-root is honored consistently.
                if name == "bootstrap":
                    runner(run_dir, state)
                else:
                    runner(run_dir, state, run_date)
                save_state(run_dir, state)
                print(f"  [OK] {name} 完成")
        except Exception as exc:
            _set_stage(state, name, status="failed", finished_at=_now().isoformat(), error=str(exc)[:500])
            save_state(run_dir, state)
            print(f"\n  [FAILED] {name}: {exc}", file=sys.stderr)
            print(f"  状态已保存到 {_state_path(run_dir)}", file=sys.stderr)
            print(f"  修复后重新运行: python -m ai_morning_brief.daily --date {run_date.isoformat()}", file=sys.stderr)
            return EXIT_FAILURE

    # All stages done.
    save_state(run_dir, state)
    return 0


def _print_status(run_dir: Path, state: Mapping[str, Any]) -> None:
    print(f"\n  AI 每日早报 {run_dir.name} — 运行状态")
    print(f"  状态文件: {_state_path(run_dir)}")
    print()
    for name in STAGES:
        stage = state.get("stages", {}).get(name, {})
        status = stage.get("status", "pending")
        label = STAGE_LABELS.get(name, name)
        icon = {"done": "✓", "running": "▶", "failed": "✗", "skipped": "○", "pending": "·"}.get(status, "?")
        error = stage.get("error")
        line = f"  {icon} {name:14s} {status:10s} {label}"
        if error:
            line += f" — {error[:80]}"
        print(line)
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AI每日早报 · 云电脑原生编排器（单入口，自动断点续跑）",
    )
    parser.add_argument("--date", dest="run_date", help="期号日期 YYYY-MM-DD；默认 Asia/Shanghai 今天")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--force", action="store_true", help="清除状态，从头重跑")
    parser.add_argument("--status", action="store_true", help="仅打印当前进度，不执行")
    parser.add_argument("--skip", type=str, default="", help="跳过的阶段，逗号分隔（如 cover,release）")
    parser.add_argument("--from", dest="from_stage", choices=STAGES, default=None, help="从指定阶段开始（重置该阶段及之后）")
    parser.add_argument("--to", dest="to_stage", choices=STAGES, default=None, help="执行到指定阶段为止")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.run_date:
        try:
            run_date = date.fromisoformat(args.run_date)
        except ValueError:
            print("--date must be YYYY-MM-DD", file=sys.stderr)
            return EXIT_FAILURE
    else:
        run_date = _now().date()

    skip = [s.strip() for s in args.skip.split(",") if s.strip()] if args.skip else []

    return run_daily(
        run_date=run_date,
        output_root=args.output_root.expanduser().resolve(),
        force=args.force,
        status_only=args.status,
        skip=skip,
        from_stage=args.from_stage,
        to_stage=args.to_stage,
    )


if __name__ == "__main__":
    raise SystemExit(main())
