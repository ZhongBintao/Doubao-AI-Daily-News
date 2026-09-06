"""Doubao in-conversation TTS adapter for the AI morning brief pipeline.

The Doubao voice synthesis tool is invoked interactively by the agent, not
from Python.  This script therefore operates in two phases:

  prepare   Read the frozen narration plan and emit a synthesis checklist
            (segment id, spoken text, target WAV path).  The agent then calls
            the Doubao voice tool once per segment and saves each WAV to the
            path listed in the checklist.

  finalize  Verify that every segment audio file exists and is non-empty,
            normalise it to 48 kHz / 16-bit / mono WAV, read durations with
            ffprobe, and write ``doubao_audio_manifest.json`` in the same
            schema as the Gemini manifest so ``pipeline run --reuse-audio
            --speech-provider doubao`` can consume it.

  status    Show which segments still lack a completed audio file.

This module never calls a TTS provider itself.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from .config import DEFAULT_LOCALE, DEFAULT_OUTPUT_ROOT
from .media import write_json

DOUBAO_MANIFEST_VERSION = "1.0"
DOUBAO_PROVIDER = "doubao"
DOUBAO_ALIGNMENT_PROVIDER = "doubao-proportional"
TARGET_SAMPLE_RATE = 48000


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


def cmd_prepare(args: argparse.Namespace) -> int:
    run_dir = _run_dir_for_date(args.date)
    plan = _load_narration_plan(run_dir)
    entries = _segment_entries(plan)
    if not entries:
        print("No segments with spoken text found in narration_plan.json.", file=sys.stderr)
        return 1

    audio_dir = run_dir / "assets" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    checklist = {
        "version": "1.0",
        "date": args.date,
        "provider": DOUBAO_PROVIDER,
        "instructions": (
            "For each segment below, call the Doubao in-conversation voice synthesis "
            "tool with the spoken_text, then save the returned WAV to the exact "
            "output_path. Use a neutral Chinese news-reading voice at normal speed. "
            "Do not add or omit words. After all segments are done, run "
            "'doubao_tts_adapter finalize --date DATE'."
        ),
        "segment_count": len(entries),
        "segments": [
            {
                "segment_id": entry["segment_id"],
                "kind": entry["kind"],
                "spoken_text": entry["spoken_text"],
                "output_path": str(_audio_path(run_dir, entry["segment_id"])),
                "char_count": len(entry["spoken_text"]),
            }
            for entry in entries
        ],
    }

    checklist_path = run_dir / "artifacts" / "doubao_tts_checklist.json"
    write_json(checklist_path, checklist)
    print(f"Prepared {len(entries)} segments for Doubao TTS synthesis.")
    print(f"Checklist: {checklist_path}")
    print()
    for entry in entries:
        path = _audio_path(run_dir, entry["segment_id"])
        status = "EXISTS" if path.is_file() else "TODO"
        print(f"  [{status}] {entry['segment_id']:12s} ({len(entry['spoken_text']):3d} chars) -> {path}")
    print()
    print("Next: synthesize each segment with the Doubao voice tool, then run 'finalize'.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    run_dir = _run_dir_for_date(args.date)
    plan = _load_narration_plan(run_dir)
    entries = _segment_entries(plan)
    done = 0
    for entry in entries:
        path = _audio_path(run_dir, entry["segment_id"])
        if path.is_file() and path.stat().st_size > 0:
            done += 1
            print(f"  [DONE] {entry['segment_id']:12s} ({path.stat().st_size // 1024} KB)")
        else:
            print(f"  [TODO] {entry['segment_id']:12s} ({len(entry['spoken_text'])} chars)")
    print(f"\n{done}/{len(entries)} segments complete.")
    return 0 if done == len(entries) else 1


def cmd_finalize(args: argparse.Namespace) -> int:
    run_dir = _run_dir_for_date(args.date)
    plan = _load_narration_plan(run_dir)
    entries = _segment_entries(plan)
    if not entries:
        print("No segments with spoken text found.", file=sys.stderr)
        return 1

    audio_dir = run_dir / "assets" / "audio"
    alignment_dir = run_dir / "artifacts" / "alignments"
    audio_dir.mkdir(parents=True, exist_ok=True)
    alignment_dir.mkdir(parents=True, exist_ok=True)

    manifest_segments: list[dict[str, Any]] = []
    durations: dict[str, float] = {}
    spoken_durations: dict[str, float] = {}

    for entry in entries:
        segment_id = entry["segment_id"]
        raw_path = _audio_path(run_dir, segment_id)
        if not raw_path.is_file() or raw_path.stat().st_size <= 0:
            print(f"  [MISSING] {segment_id}: audio file not found at {raw_path}", file=sys.stderr)
            return 1

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
            # Pad with trailing silence so the renderer's fixed-duration
            # scenes (e.g. the 5-second overview page) are satisfied.
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
                print(f"  [PAD] {segment_id}: padded {duration:.2f}s -> {minimum:.2f}s")
            else:
                print(f"  [WARN] {segment_id}: padding failed ({(result.stderr or '')[-200:]})", file=sys.stderr)
            padded.unlink(missing_ok=True)
            duration = _media_duration(raw_path)

        durations[segment_id] = duration
        spoken_durations[segment_id] = duration

        manifest_segments.append({
            "segment_id": segment_id,
            "audio_path": f"assets/audio/{raw_path.name}",
            "provider": DOUBAO_PROVIDER,
            "voice": "doubao-in-conversation",
            "locale": DEFAULT_LOCALE,
            "alignment_provider": DOUBAO_ALIGNMENT_PROVIDER,
            "alignment_quality": "approximate",
            "word_count": 0,
            "spoken_duration_seconds": round(duration, 3),
            "duration_seconds": round(duration, 3),
            "display_text": entry["display_text"],
            "spoken_text": entry["spoken_text"],
            "native_word_boundary": False,
        })

    # Build the manifest in the same shape as the Gemini manifest so the
    # reuse_synthesized_audio bridge can consume it without provider-specific
    # branches beyond the manifest filename and alignment flag.
    manifest = {
        "version": DOUBAO_MANIFEST_VERSION,
        "provider": DOUBAO_PROVIDER,
        "voice": "doubao-in-conversation",
        "locale": DEFAULT_LOCALE,
        "tts_settings": {
            "canonical_text": "spoken_text",
            "alignment_provider": DOUBAO_ALIGNMENT_PROVIDER,
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
            "mode": "doubao-proportional",
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

    total = sum(durations.values())
    print(f"Finalized {len(manifest_segments)} segments, total narration duration {total:.1f}s.")
    print(f"Manifest: {manifest_path}")
    print()
    print("Next: run the pipeline with audio reuse:")
    print(f"  OpenMontage/.venv/bin/python -m ai_morning_brief.pipeline run \\")
    print(f"    --date {args.date} --force --reuse-source --reuse-audio \\")
    print(f"    --env-file .env --speech-provider doubao")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Doubao in-conversation TTS adapter: prepare checklist, verify audio, write manifest."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, handler, help_text in [
        ("prepare", cmd_prepare, "Read narration plan and emit a TTS synthesis checklist."),
        ("status", cmd_status, "Show which segments still need audio."),
        ("finalize", cmd_finalize, "Verify audio, normalise to 48k mono, write doubao_audio_manifest.json."),
    ]:
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--date", required=True, help="Edition date, YYYY-MM-DD")
        sub.set_defaults(handler=handler)

    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
