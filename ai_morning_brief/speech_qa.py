"""Narration transcription for ASR-timed captions.

Single responsibility: transcribe every narration block with a local whisper
model and write word-level timestamps to ``artifacts/alignments/<id>.json``
so the caption builder can time the authored display text against when each
word was actually spoken, instead of proportionally splitting the audio.

There is no quality gate here: transcription failures never block rendering.
A block without a usable alignment simply falls back to the deterministic
proportional timing used before ASR timestamps existed (see
``media.write_subtitles``).

ASR engines, probed in order (override with ``AI_BRIEF_ASR_ENGINE``):
``mlx-whisper`` (Apple Silicon) -> ``faster-whisper`` -> ``whisper.cpp`` CLI.
``AI_BRIEF_ASR_ENGINE=none`` disables transcription entirely.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .media import media_duration, write_json

TRANSCRIPTION_VERSION = "1.0"
DEFAULT_WHISPER_MODEL = "small"
DEFAULT_MLX_WHISPER_MODEL = "mlx-community/whisper-small--int8"

ASR_ALIGNMENT_PROVIDER = "asr-local"


_KNOWN_ENGINES = ("mlx-whisper", "faster-whisper", "whisper-cpp", "none")


def detect_engine() -> str | None:
    """Pick the best available local ASR engine; None when nothing works."""

    forced = os.environ.get("AI_BRIEF_ASR_ENGINE", "").strip().lower()
    if forced:
        return forced if forced in _KNOWN_ENGINES else None
    try:
        import mlx_whisper  # noqa: F401
        return "mlx-whisper"
    except Exception:
        pass
    try:
        import faster_whisper  # noqa: F401
        return "faster-whisper"
    except Exception:
        pass
    for name in ("whisper-cli", "whisper-cpp"):
        if shutil.which(name):
            return "whisper-cpp"
    return None


def _transcribe_mlx_whisper(audio_path: Path, model: str) -> dict[str, Any]:
    import mlx_whisper

    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=model,
        word_timestamps=True,
        language="zh",
    )
    words: list[dict[str, Any]] = []
    for segment in result.get("segments", []) or []:
        for word in segment.get("words", []) or []:
            try:
                words.append({
                    "word": str(word.get("word", "")).strip(),
                    "start": round(float(word.get("start", 0.0)), 3),
                    "end": round(float(word.get("end", 0.0)), 3),
                })
            except (TypeError, ValueError):
                continue
    return {"text": str(result.get("text", "")).strip(), "words": [w for w in words if w["word"]]}


def _transcribe_faster_whisper(audio_path: Path, model: str) -> dict[str, Any]:
    from faster_whisper import WhisperModel

    whisper = WhisperModel(model, device="auto", compute_type="auto")
    segments, _info = whisper.transcribe(str(audio_path), language="zh", word_timestamps=True)
    text_parts: list[str] = []
    words: list[dict[str, Any]] = []
    for segment in segments:
        if segment.text:
            text_parts.append(segment.text.strip())
        for word in segment.words or []:
            token = str(word.word or "").strip()
            if not token:
                continue
            words.append({"word": token, "start": round(float(word.start), 3), "end": round(float(word.end), 3)})
    return {"text": " ".join(part for part in text_parts if part).strip(), "words": words}


def _transcribe_whisper_cpp(audio_path: Path, model: str) -> dict[str, Any]:
    binary = shutil.which("whisper-cli") or shutil.which("whisper-cpp")
    if not binary:
        raise RuntimeError("whisper.cpp CLI not found")
    if not model or not Path(model).is_file():
        raise RuntimeError(
            "whisper.cpp requires a local ggml model file; set AI_BRIEF_WHISPER_CPP_MODEL"
        )
    with tempfile.TemporaryDirectory(prefix="narration-transcribe-") as temp_dir:
        prefix = Path(temp_dir) / "transcript"
        result = subprocess.run(
            [binary, "-m", model, "-l", "zh", "-oj", "-of", str(prefix), str(audio_path)],
            capture_output=True, text=True, timeout=600, check=False,
        )
        output = prefix.with_suffix(".json")
        if result.returncode != 0 or not output.is_file():
            raise RuntimeError(f"whisper.cpp failed: {(result.stderr or '')[-300:]}")
        data = json.loads(output.read_text(encoding="utf-8"))
    words: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for segment in data.get("transcription", []) or []:
        text_parts.append(str(segment.get("text", "")).strip())
        for token in segment.get("tokens", []) or []:
            token_text = str(token.get("text", ""))
            stripped = token_text.strip()
            if not stripped or stripped.startswith("[") or stripped.startswith("<"):
                continue
            offsets = token.get("offsets") or {}
            try:
                words.append({
                    "word": stripped,
                    "start": round(int(offsets.get("from", 0)) / 1000, 3),
                    "end": round(int(offsets.get("to", 0)) / 1000, 3),
                })
            except (TypeError, ValueError):
                continue
    return {"text": "".join(text_parts).strip(), "words": words}


def transcribe(audio_path: Path, *, engine: str | None = None, model: str | None = None) -> dict[str, Any]:
    """Transcribe one WAV with the selected local engine.

    Returns ``{"text", "words", "engine"}``.  Raises RuntimeError with a
    short, secret-free message when the engine cannot run.
    """

    engine = engine or detect_engine() or ""
    model = model or os.environ.get("AI_BRIEF_ASR_MODEL") or ""
    if engine == "mlx-whisper":
        return {**_transcribe_mlx_whisper(audio_path, model or DEFAULT_MLX_WHISPER_MODEL), "engine": engine}
    if engine == "faster-whisper":
        return {**_transcribe_faster_whisper(audio_path, model or DEFAULT_WHISPER_MODEL), "engine": engine}
    if engine == "whisper-cpp":
        cpp_model = model or os.environ.get("AI_BRIEF_WHISPER_CPP_MODEL", "")
        return {**_transcribe_whisper_cpp(audio_path, cpp_model), "engine": engine}
    raise RuntimeError(
        "no local ASR engine available; install mlx-whisper or faster-whisper "
        "(pip install mlx-whisper / faster-whisper), or set AI_BRIEF_ASR_ENGINE"
    )


# ---------------------------------------------------------------------------
# Narration plan plumbing
# ---------------------------------------------------------------------------

def _load_narration_plan(run_dir: Path) -> dict[str, Any]:
    plan_path = run_dir / "artifacts" / "narration_plan.json"
    if not plan_path.is_file():
        raise FileNotFoundError(f"narration_plan.json not found: {plan_path}")
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping) or not isinstance(data.get("segments"), list):
        raise ValueError(f"invalid narration_plan.json at {plan_path}")
    return dict(data)


def _segment_entries(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for segment in plan.get("segments", []):
        if not isinstance(segment, Mapping):
            continue
        segment_id = str(segment.get("id") or "").strip()
        display = str(segment.get("display_text") or segment.get("broadcast_text") or "").strip()
        if not segment_id or not display:
            continue
        entries.append({
            "segment_id": segment_id,
            "kind": str(segment.get("kind") or ""),
            "display_text": display,
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


def write_asr_alignment(
    run_dir: Path,
    segment_id: str,
    *,
    transcript: Mapping[str, Any],
    audio_relative_path: str,
    duration: float,
    audio_sha256: str,
) -> Path:
    """Write the whisper word timestamps as the segment's alignment ledger.

    The file follows the standard alignment schema used by
    ``media.write_subtitles`` and carries the raw transcript so the caption
    builder can match display-text spans against real word times.
    """

    alignment_dir = run_dir / "artifacts" / "alignments"
    alignment_dir.mkdir(parents=True, exist_ok=True)
    alignment_path = alignment_dir / f"{segment_id}.json"
    words = [dict(word) for word in transcript.get("words", []) if isinstance(word, Mapping)]
    write_json(alignment_path, {
        "version": "2.0",
        "provider": "asr-local",
        "alignment_provider": ASR_ALIGNMENT_PROVIDER,
        "alignment_quality": "measured",
        "language": "zh",
        "duration_seconds": round(float(duration), 3),
        "segments": [{"text": str(transcript.get("text", "")), "start": 0.0, "end": round(float(duration), 3)}],
        "word_timestamps": words,
        "asr_transcript": str(transcript.get("text", "")),
        "asr_engine": str(transcript.get("engine", "")),
        "source_audio": audio_relative_path,
        "audio_sha256": audio_sha256,
        "script_section_id": segment_id,
        "canonical_text": "display_text",
        "no_secrets_in_artifacts": True,
    })
    return alignment_path


def _alignment_is_current(run_dir: Path, segment_id: str, audio_sha256: str) -> bool:
    """True when a cached alignment for the same audio already exists."""

    alignment_path = run_dir / "artifacts" / "alignments" / f"{segment_id}.json"
    if not alignment_path.is_file():
        return False
    try:
        data = json.loads(alignment_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(data, Mapping)
        and data.get("alignment_provider") == ASR_ALIGNMENT_PROVIDER
        and data.get("audio_sha256") == audio_sha256
        and bool(data.get("word_timestamps"))
    )


def transcribe_narration(run_dir: Path, *, engine: str | None = None, model: str | None = None, force: bool = False) -> dict[str, Any]:
    """Transcribe every narration block and write word-level alignments.

    Incremental: a block whose cached alignment matches the current audio
    hash is reused.  Blocks that cannot be transcribed (no engine, engine
    error, missing audio) are reported and simply keep no alignment — the
    caption builder falls back to proportional timing for them.  This
    function never raises for per-segment failures.
    """

    plan = _load_narration_plan(run_dir)
    entries = _segment_entries(plan)
    resolved_engine = engine or detect_engine()
    resolved_model = model or os.environ.get("AI_BRIEF_ASR_MODEL") or (
        DEFAULT_MLX_WHISPER_MODEL if resolved_engine == "mlx-whisper" else DEFAULT_WHISPER_MODEL
    )

    summary: dict[str, Any] = {
        "version": TRANSCRIPTION_VERSION,
        "engine": resolved_engine,
        "model": resolved_model,
        "aligned_segments": [],
        "skipped_segments": [],
        "failed_segments": [],
        "segments": [],
    }

    for entry in entries:
        segment_id = entry["segment_id"]
        audio_path = _audio_path(run_dir, segment_id)
        record: dict[str, Any] = {"segment_id": segment_id, "kind": entry["kind"], "status": "pending"}
        if not audio_path.is_file() or audio_path.stat().st_size <= 0:
            record["status"] = "missing_audio"
            record["reason"] = f"narration audio not found: {audio_path.name}"
            summary["segments"].append(record)
            summary["failed_segments"].append(segment_id)
            continue
        audio_hash = _file_sha256(audio_path)
        record["audio_sha256"] = audio_hash

        if not force and _alignment_is_current(run_dir, segment_id, audio_hash):
            record["status"] = "reused"
            summary["segments"].append(record)
            summary["aligned_segments"].append(segment_id)
            summary["skipped_segments"].append(segment_id)
            continue

        if resolved_engine in (None, "none"):
            record["status"] = "unavailable"
            record["reason"] = "no local ASR engine available; proportional caption timing will be used"
            summary["segments"].append(record)
            summary["failed_segments"].append(segment_id)
            continue

        try:
            transcript = transcribe(audio_path, engine=resolved_engine, model=resolved_model)
            duration = media_duration(audio_path)
        except Exception as exc:  # noqa: BLE001 - engine failures vary; degrade, never block
            record["status"] = "failed"
            record["reason"] = f"transcription failed: {str(exc)[:300]}"
            summary["segments"].append(record)
            summary["failed_segments"].append(segment_id)
            continue

        write_asr_alignment(
            run_dir,
            segment_id,
            transcript=transcript,
            audio_relative_path=f"assets/audio/{audio_path.name}",
            duration=duration,
            audio_sha256=audio_hash,
        )
        record["status"] = "transcribed"
        record["word_count"] = len(transcript.get("words", []))
        summary["segments"].append(record)
        summary["aligned_segments"].append(segment_id)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Transcribe narration blocks with a local ASR engine for caption timing."
    )
    parser.add_argument("--date", required=True, help="Edition date, YYYY-MM-DD")
    parser.add_argument("--output-root", default=None, help="Override the outputs root")
    parser.add_argument("--engine", default=None, choices=list(_KNOWN_ENGINES), help="Force an ASR engine")
    parser.add_argument("--model", default=None, help="ASR model name or ggml model path")
    parser.add_argument("--force", action="store_true", help="Re-transcribe even cached audio")
    args = parser.parse_args(argv)

    from .config import DEFAULT_OUTPUT_ROOT

    output_root = Path(args.output_root) if args.output_root else DEFAULT_OUTPUT_ROOT
    run_dir = output_root / args.date
    if not run_dir.is_dir():
        print(f"run directory not found: {run_dir}", file=sys.stderr)
        return 2
    try:
        summary = transcribe_narration(run_dir, engine=args.engine, model=args.model, force=args.force)
    except (FileNotFoundError, ValueError) as exc:
        print(f"  [ERROR] {exc}", file=sys.stderr)
        return 2
    print(f"Transcription engine: {summary.get('engine')} (model={summary.get('model')})")
    for item in summary.get("segments", []):
        marker = {"transcribed": "[OK]", "reused": "[CACHE]", "unavailable": "[SKIP]", "failed": "[FAIL]", "missing_audio": "[MISS]"}.get(str(item.get("status")), "[????]")
        detail = f" words={item['word_count']}" if "word_count" in item else f" {item.get('reason') or ''}"
        print(f"  {marker} {str(item.get('segment_id')):12s}{detail}")
    aligned = summary.get("aligned_segments") or []
    failed = summary.get("failed_segments") or []
    print(f"aligned: {len(aligned)} segment(s); fallback to proportional timing: {len(failed)} segment(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
