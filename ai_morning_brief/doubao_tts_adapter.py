"""Doubao in-conversation TTS adapter for the AI morning brief pipeline.

Supports two synthesis modes:

  voice-clone    (default) Use audio_to_audio_plus with a reference audio
                 file (example-audio.mp3) to clone the voice.  Cloud-computer
                 native production mode.

  text-to-audio  Use text_to_audio_plus with a fixed voice description.
                 Legacy compatibility mode.

The adapter operates in two phases when used standalone:

  prepare   Read the frozen narration plan and emit a synthesis checklist
            (segment id, spoken text, target WAV path).  The agent then calls
            the Doubao voice tool once per segment and saves each WAV to the
            path listed in the checklist.

  finalize  Verify that every segment audio file exists and is non-empty,
            normalise it to 48 kHz / 16-bit / mono WAV, read durations with
            ffprobe, and write ``doubao_audio_manifest.json`` in the same
            schema as the Gemini manifest so ``pipeline run --reuse-audio
            --speech-provider doubao[-voice-clone]`` can consume it.

When used by the daily orchestrator, ``generate_tts_manifest()`` and
``verify_and_finalize()`` are imported directly instead of going through the
CLI.

This module never calls a TTS provider itself.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from .config import (
    DEFAULT_LOCALE,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_TTS_MODE,
    DEFAULT_VOICE_CLONE_REFERENCE_AUDIO,
    DOUBAO_VOICE_CLONE_ALIGNMENT,
    DOUBAO_VOICE_CLONE_PROVIDER,
)
from .media import write_json

DOUBAO_MANIFEST_VERSION = "2.0"
DOUBAO_PROVIDER = "doubao"
DOUBAO_ALIGNMENT_PROVIDER = "doubao-proportional"
TARGET_SAMPLE_RATE = 48000
VALID_TTS_MODES = ("voice-clone", "text-to-audio")


def _run_dir_for_date(run_date: str, output_root: Path = DEFAULT_OUTPUT_ROOT) -> Path:
    return output_root / run_date


def _load_narration_plan(run_dir: Path) -> dict[str, Any]:
    plan_path = run_dir / "artifacts" / "narration_plan.json"
    if not plan_path.is_file():
        raise FileNotFoundError(
            f"narration_plan.json not found: {plan_path}\n"
            "Run 'pipeline prepare' and complete the editorial plan first, "
            "or generate the narration plan via the script builder."
        )
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping) or not isinstance(data.get("segments"), list):
        raise ValueError(f"invalid narration_plan.json at {plan_path}")
    return data


def _segment_entries(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for segment in plan.get("segments", []):
        if not isinstance(segment, Mapping):
            continue
        segment_id = str(segment.get("id") or "").strip()
        if not segment_id:
            continue
        spoken = str(segment.get("spoken_text") or segment.get("broadcast_text") or "").strip()
        display = str(segment.get("display_text") or spoken).strip()
        if not spoken:
            # Segments with no spoken text (e.g. pure visual overview) are
            # skipped; the renderer gives them minimum_duration from the plan.
            continue
        entries.append({
            "segment_id": segment_id,
            "display_text": display,
            "spoken_text": spoken,
            "kind": str(segment.get("kind") or ""),
            "minimum_duration_seconds": float(segment.get("minimum_duration_seconds") or 0.0),
        })
    return entries


def _audio_path(run_dir: Path, segment_id: str) -> Path:
    return run_dir / "assets" / "audio" / f"narration-{segment_id}.wav"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_declared_audio_path(run_dir: Path, declared_path: str | None, segment_id: str) -> Path:
    """Resolve a checklist path while keeping generated audio inside the run."""

    candidate = Path(str(declared_path or "")) if declared_path else _audio_path(run_dir, segment_id)
    if not candidate.is_absolute():
        candidate = run_dir / candidate
    resolved_run = run_dir.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_candidate != resolved_run and resolved_run not in resolved_candidate.parents:
        raise RuntimeError(f"audio output_path escapes the edition directory for {segment_id}")
    return resolved_candidate


def _load_tts_manifest(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "artifacts" / "tts_manifest.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read tts_manifest.json: {path}") from exc
    if not isinstance(value, Mapping):
        raise RuntimeError(f"invalid tts_manifest.json: {path}")
    return dict(value)


def _normalise_wav(source: Path, target: Path) -> None:
    """Convert any WAV input to 48 kHz / 16-bit / mono PCM."""
    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(source),
            "-ar", str(TARGET_SAMPLE_RATE), "-ac", "1",
            "-c:a", "pcm_s16le", str(target),
        ],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
        raise RuntimeError(f"ffmpeg normalisation failed for {source.name}: {(result.stderr or '')[-300:]}")


def _media_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path.name}")
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise RuntimeError(f"ffprobe returned invalid duration for {path.name}: {result.stdout!r}")


def _reference_audio_info(reference_path: Path | None = None) -> dict[str, Any]:
    """Collect metadata about the voice-clone reference audio."""
    path = reference_path or DEFAULT_VOICE_CLONE_REFERENCE_AUDIO
    info: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "sha256": None,
        "duration_seconds": None,
        "size_bytes": None,
    }
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        info["sha256"] = digest.hexdigest()
        info["size_bytes"] = path.stat().st_size
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            if result.returncode == 0:
                info["duration_seconds"] = round(float(result.stdout.strip()), 3)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return info


def _tts_mode_provider(tts_mode: str) -> str:
    return DOUBAO_VOICE_CLONE_PROVIDER if tts_mode == "voice-clone" else DOUBAO_PROVIDER


def _tts_mode_alignment(tts_mode: str) -> str:
    return DOUBAO_VOICE_CLONE_ALIGNMENT if tts_mode == "voice-clone" else DOUBAO_ALIGNMENT_PROVIDER


def _tts_mode_voice_label(tts_mode: str, reference_info: Mapping[str, Any] | None = None) -> str:
    if tts_mode == "voice-clone":
        ref_name = Path(str((reference_info or {}).get("path") or "")).name
        return f"voice-clone:{ref_name}"
    return "doubao-in-conversation"


def generate_tts_manifest(
    run_dir: Path,
    *,
    tts_mode: str = DEFAULT_TTS_MODE,
    reference_audio: Path | None = None,
) -> dict[str, Any]:
    """Generate the TTS synthesis manifest (orchestrator-facing importable API).

    Returns the manifest dict and writes it to
    ``artifacts/tts_manifest.json``.  In voice-clone mode the manifest includes
    reference-audio metadata and instructs the agent to use audio_to_audio_plus.
    """
    if tts_mode not in VALID_TTS_MODES:
        raise ValueError(f"invalid tts_mode: {tts_mode}; expected one of {VALID_TTS_MODES}")
    plan = _load_narration_plan(run_dir)
    entries = _segment_entries(plan)
    if not entries:
        raise RuntimeError("No segments with spoken text found in narration_plan.json.")

    audio_dir = run_dir / "assets" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    reference_info = _reference_audio_info(reference_audio) if tts_mode == "voice-clone" else None
    provider = _tts_mode_provider(tts_mode)
    alignment = _tts_mode_alignment(tts_mode)
    voice_label = _tts_mode_voice_label(tts_mode, reference_info)

    if tts_mode == "voice-clone":
        tool_name = "audio_to_audio_plus"
        instructions = (
            "For each segment below, call the Doubao audio_to_audio_plus tool with "
            "@音频1 = the reference audio file, then save the returned WAV to the exact "
            "output_path. Prompt: '用参考音频的音色、语速和朗读风格，清晰朗读以下文字，"
            "不增删字词，无背景音无杂音：{spoken_text}'. After all segments are done, "
            "re-run the daily orchestrator (or run 'doubao_tts_adapter finalize')."
        )
    else:
        tool_name = "text_to_audio_plus"
        instructions = (
            "For each segment below, call the Doubao in-conversation voice synthesis "
            "tool (text_to_audio_plus) with the spoken_text, then save the returned WAV "
            "to the exact output_path. Use a neutral Chinese news-reading voice at normal "
            "speed. Do not add or omit words. After all segments are done, run "
            "'doubao_tts_adapter finalize --date DATE'."
        )

    manifest = {
        "version": DOUBAO_MANIFEST_VERSION,
        "date": run_dir.name,
        "tts_mode": tts_mode,
        "provider": provider,
        "tool": tool_name,
        "alignment_provider": alignment,
        "voice": voice_label,
        "reference_audio": reference_info,
        "instructions": instructions,
        "segment_count": len(entries),
        "segments": [
            {
                "segment_id": entry["segment_id"],
                "kind": entry["kind"],
                "spoken_text": entry["spoken_text"],
                "output_path": str(_audio_path(run_dir, entry["segment_id"])),
                "char_count": len(entry["spoken_text"]),
                "minimum_duration_seconds": entry.get("minimum_duration_seconds", 0.0),
            }
            for entry in entries
        ],
    }

    manifest_path = run_dir / "artifacts" / "tts_manifest.json"
    write_json(manifest_path, manifest)
    return manifest


def verify_audio_complete(run_dir: Path) -> tuple[bool, list[str]]:
    """Check whether every segment's audio file exists and is non-empty.

    Returns (all_complete, missing_segment_ids).  Reads the segment list from
    tts_manifest.json if present, otherwise from narration_plan.json.
    """
    manifest_path = run_dir / "artifacts" / "tts_manifest.json"
    declared_paths: dict[str, Path] = {}
    if manifest_path.is_file():
        data = _load_tts_manifest(run_dir) or {}
        for entry in data.get("segments", []):
            if isinstance(entry, Mapping) and entry.get("segment_id"):
                segment_id = str(entry["segment_id"])
                declared_paths[segment_id] = _resolve_declared_audio_path(run_dir, entry.get("output_path"), segment_id)
        segment_ids = list(declared_paths)
    else:
        plan = _load_narration_plan(run_dir)
        segment_ids = [e["segment_id"] for e in _segment_entries(plan)]
    missing: list[str] = []
    for segment_id in segment_ids:
        path = declared_paths.get(segment_id) or _audio_path(run_dir, segment_id)
        if not path.is_file() or path.stat().st_size <= 0:
            missing.append(segment_id)
    return (len(missing) == 0, missing)


def verify_and_finalize(
    run_dir: Path,
    *,
    tts_mode: str = DEFAULT_TTS_MODE,
    reference_audio: Path | None = None,
) -> dict[str, Any]:
    """Verify all segment audio, normalise, and write the final manifest.

    Importable API for the daily orchestrator.  Equivalent to the CLI
    ``finalize`` command but raises on missing audio instead of returning 1.
    """
    if tts_mode not in VALID_TTS_MODES:
        raise ValueError(f"invalid tts_mode: {tts_mode}; expected one of {VALID_TTS_MODES}")
    plan = _load_narration_plan(run_dir)
    entries = _segment_entries(plan)
    if not entries:
        raise RuntimeError("No segments with spoken text found.")

    audio_dir = run_dir / "assets" / "audio"
    alignment_dir = run_dir / "artifacts" / "alignments"
    audio_dir.mkdir(parents=True, exist_ok=True)
    alignment_dir.mkdir(parents=True, exist_ok=True)

    provider = _tts_mode_provider(tts_mode)
    alignment = _tts_mode_alignment(tts_mode)
    reference_info = _reference_audio_info(reference_audio) if tts_mode == "voice-clone" else None
    voice_label = _tts_mode_voice_label(tts_mode, reference_info)

    manifest_segments: list[dict[str, Any]] = []
    durations: dict[str, float] = {}
    spoken_durations: dict[str, float] = {}
    tts_manifest = _load_tts_manifest(run_dir) or {}
    declared_entries = {
        str(item.get("segment_id")): item
        for item in tts_manifest.get("segments", [])
        if isinstance(item, Mapping) and item.get("segment_id")
    }

    for entry in entries:
        segment_id = entry["segment_id"]
        declared = declared_entries.get(segment_id) or {}
        if declared and str(declared.get("spoken_text") or "") != entry["spoken_text"]:
            raise RuntimeError(f"tts_manifest spoken_text is stale for segment {segment_id}; regenerate prepare")
        raw_path = _resolve_declared_audio_path(run_dir, declared.get("output_path"), segment_id)
        if not raw_path.is_file() or raw_path.stat().st_size <= 0:
            raise RuntimeError(f"audio file not found for segment {segment_id}: {raw_path}")

        # Normalise in-place via a temp file, then atomically replace.
        staging = raw_path.with_suffix(".normalised.wav")
        try:
            _normalise_wav(raw_path, staging)
            staging.replace(raw_path)
        finally:
            staging.unlink(missing_ok=True)

        duration = _media_duration(raw_path)
        minimum = float(entry.get("minimum_duration_seconds") or 0.0)
        if minimum > 0 and duration < minimum - 0.02:
            padded = raw_path.with_suffix(".padded.wav")
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error", "-i", str(raw_path),
                    "-af", f"apad=pad_dur={minimum - duration:.3f}",
                    "-t", f"{minimum:.3f}", "-c:a", "pcm_s16le",
                    "-ar", str(TARGET_SAMPLE_RATE), "-ac", "1", str(padded),
                ],
                capture_output=True, text=True, timeout=120, check=False,
            )
            if result.returncode == 0 and padded.is_file() and padded.stat().st_size > 0:
                padded.replace(raw_path)
            else:
                padded.unlink(missing_ok=True)
            duration = _media_duration(raw_path)

        durations[segment_id] = duration
        spoken_durations[segment_id] = duration

        manifest_segments.append({
            "segment_id": segment_id,
            "audio_path": raw_path.relative_to(run_dir).as_posix(),
            "provider": provider,
            "voice": voice_label,
            "locale": DEFAULT_LOCALE,
            "alignment_provider": alignment,
            "alignment_quality": "approximate",
            "word_count": 0,
            "spoken_duration_seconds": round(duration, 3),
            "duration_seconds": round(duration, 3),
            "display_text": entry["display_text"],
            "spoken_text": entry["spoken_text"],
            "native_word_boundary": False,
            "sha256": _file_sha256(raw_path),
        })

    manifest = {
        "version": DOUBAO_MANIFEST_VERSION,
        "provider": provider,
        "tts_mode": tts_mode,
        "voice": voice_label,
        "locale": DEFAULT_LOCALE,
        "reference_audio": reference_info,
        "tts_settings": {
            "canonical_text": "spoken_text",
            "alignment_provider": alignment,
            "alignment_quality": "approximate",
        },
        "segments": manifest_segments,
        "audio": {
            "narration_track": "assets/audio/narration-track.wav",
            "music": {
                "opening": "assets/music/opening.mp3",
                "middle": "assets/music/middle-loop.mp3",
                "ending": "assets/music/ending.mp3",
            },
            "transition_whoosh": "assets/audio/transition-whoosh.wav",
            "category_chime": "assets/audio/category-chime.wav",
            "final_mix": "assets/audio/final-mix.wav",
            "music_report": "artifacts/background-music.json",
        },
        "spoken_durations": {key: round(value, 3) for key, value in spoken_durations.items()},
        "subtitle_path": "assets/subtitles/subtitles.srt",
        "subtitle_alignment": {
            "requested": True,
            "mode": alignment,
            "approximate": True,
            "proportional_fallback_segments": [s["segment_id"] for s in manifest_segments],
            "compatibility_segments": [],
        },
        "failure_policy": "stop on missing segment audio; no automatic provider fallback",
        "native_word_boundary": False,
        "compatibility_fallback": "deterministic proportional subtitle timing",
        "no_secrets_in_artifacts": True,
    }

    manifest_path = run_dir / "artifacts" / "doubao_audio_manifest.json"
    write_json(manifest_path, manifest)
    return manifest


def cmd_prepare(args: argparse.Namespace) -> int:
    run_dir = _run_dir_for_date(args.date)
    tts_mode = getattr(args, "tts_mode", DEFAULT_TTS_MODE)
    reference = Path(args.reference_audio) if getattr(args, "reference_audio", None) else None
    try:
        manifest = generate_tts_manifest(run_dir, tts_mode=tts_mode, reference_audio=reference)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Prepared {manifest['segment_count']} segments for Doubao TTS synthesis (mode={tts_mode}).")
    print(f"Manifest: {run_dir / 'artifacts' / 'tts_manifest.json'}")
    if tts_mode == "voice-clone":
        ref = manifest.get("reference_audio") or {}
        print(f"Reference audio: {ref.get('path')} (exists={ref.get('exists')}, duration={ref.get('duration_seconds')}s)")
    print()
    for entry in manifest["segments"]:
        path = Path(entry["output_path"])
        status = "EXISTS" if path.is_file() else "TODO"
        print(f"  [{status}] {entry['segment_id']:12s} ({entry['char_count']:3d} chars) -> {path}")
    print()
    print("Next: synthesize each segment with the Doubao voice tool, then re-run the orchestrator or 'finalize'.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    run_dir = _run_dir_for_date(args.date)
    plan = _load_narration_plan(run_dir)
    entries = _segment_entries(plan)
    done = 0
    for entry in entries:
        path = _resolve_declared_audio_path(run_dir, entry.get("output_path"), entry["segment_id"])
        if path.is_file() and path.stat().st_size > 0:
            done += 1
            print(f"  [DONE] {entry['segment_id']:12s} ({path.stat().st_size // 1024} KB)")
        else:
            print(f"  [TODO] {entry['segment_id']:12s} ({len(entry['spoken_text'])} chars)")
    print(f"\n{done}/{len(entries)} segments complete.")
    return 0 if done == len(entries) else 1


def cmd_finalize(args: argparse.Namespace) -> int:
    run_dir = _run_dir_for_date(args.date)
    tts_mode = getattr(args, "tts_mode", DEFAULT_TTS_MODE)
    reference = Path(args.reference_audio) if getattr(args, "reference_audio", None) else None
    try:
        manifest = verify_and_finalize(run_dir, tts_mode=tts_mode, reference_audio=reference)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"  [ERROR] {exc}", file=sys.stderr)
        return 1

    total = sum(float(s.get("duration_seconds") or 0) for s in manifest["segments"])
    print(f"Finalized {len(manifest['segments'])} segments, total narration duration {total:.1f}s.")
    print(f"Manifest: {run_dir / 'artifacts' / 'doubao_audio_manifest.json'}")
    print()
    print("Next: run the pipeline with audio reuse:")
    print(f"  OpenMontage/.venv/bin/python -m ai_morning_brief.pipeline run \\")
    print(f"    --date {args.date} --force --reuse-source --reuse-audio \\")
    print(f"    --env-file .env --speech-provider {manifest['provider']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Doubao in-conversation TTS adapter: prepare checklist, verify audio, write manifest."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, handler, help_text in [
        ("prepare", cmd_prepare, "Read narration plan and emit a TTS synthesis manifest."),
        ("status", cmd_status, "Show which segments still need audio."),
        ("finalize", cmd_finalize, "Verify audio, normalise to 48k mono, write doubao_audio_manifest.json."),
    ]:
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--date", required=True, help="Edition date, YYYY-MM-DD")
        sub.add_argument("--tts-mode", choices=VALID_TTS_MODES, default=DEFAULT_TTS_MODE,
                         help="Synthesis mode: voice-clone (default, audio_to_audio_plus + reference) or text-to-audio (legacy)")
        sub.add_argument("--reference-audio", default=None,
                         help="Path to voice-clone reference audio (default: project example-audio.mp3)")
        sub.set_defaults(handler=handler)

    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
