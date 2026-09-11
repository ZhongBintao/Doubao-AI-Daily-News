"""Post-synthesis speech quality gate driven by a local ASR model.

The gate closes the loop that deterministic text normalization used to open:
instead of mangling TTS input (``GPU`` -> ``G P U``), the pipeline now feeds
the raw authored text to the voice-clone provider and then *listens* to the
result with a local whisper model.  For every narration block it transcribes
the rendered WAV and checks the reading for:

* skipped content / misread facts (missing numerals, acronyms, model codes)
* stutters (immediately repeated spans, the ``story-07`` failure mode)
* hallucinated additions and empty/unintelligible readings

Retry contract (one voice block, max 3 synthesis attempts):

* attempt 1 — raw display text (spoken == display since normalization v3.0)
* attempt 2 — raw text again (cloning is non-deterministic; a fresh take
  frequently fixes a bad reading without changing any copy)
* attempt 3 — optional one-off colloquial override supplied by the writing
  agent at ``artifacts/speech_qa/colloquial_overrides.json`` (segment_id ->
  text).  The override changes only what the voice model reads; the on-screen
  ``display_text`` and captions stay byte-identical.

Passed blocks are reused; only failed blocks re-enter the synthesis handoff.
Every attempt keeps its transcript, word timestamps, verdict and audio hash
under ``artifacts/speech_qa/`` for auditing.  A block that still fails after
3 attempts marks the edition ``blocked`` and the renderer refuses to run.

ASR engines, probed in order (override with ``AI_BRIEF_ASR_ENGINE``):
``mlx-whisper`` (Apple Silicon) -> ``faster-whisper`` -> ``whisper.cpp`` CLI.
``AI_BRIEF_ASR_ENGINE=none`` disables the gate entirely; the report is then
``unavailable`` and rendering proceeds with proportional caption timing.
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
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .asr_match import compare_texts, extract_key_tokens, normalize_for_match
from .media import media_duration, write_json

SPEECH_QA_VERSION = "1.1"
TIMEZONE = "Asia/Shanghai"
MAX_ATTEMPTS = 3
MIN_SIMILARITY = 0.72
DEFAULT_WHISPER_MODEL = "small"
DEFAULT_MLX_WHISPER_MODEL = "mlx-community/whisper-small--int8"

QA_DIRNAME = "speech_qa"
STATE_FILENAME = "speech_qa_state.json"
REPORT_FILENAME = "speech_qa_report.json"
OVERRIDES_FILENAME = "colloquial_overrides.json"

ASR_ALIGNMENT_PROVIDER = "asr-local"


class SpeechQABlocked(RuntimeError):
    """Raised when a voice block still fails the ASR gate after MAX_ATTEMPTS."""


def _now() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).isoformat()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_dir(run_dir: Path) -> Path:
    return run_dir / "artifacts" / QA_DIRNAME


def _attempt_dir(run_dir: Path, segment_id: str) -> Path:
    return _artifact_dir(run_dir) / "attempts" / segment_id


def _state_path(run_dir: Path) -> Path:
    return _artifact_dir(run_dir) / STATE_FILENAME


def _overrides_path(run_dir: Path) -> Path:
    return _artifact_dir(run_dir) / OVERRIDES_FILENAME


# ---------------------------------------------------------------------------
# ASR engines
# ---------------------------------------------------------------------------

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
    with tempfile.TemporaryDirectory(prefix="speech-qa-") as temp_dir:
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

    Returns ``{"text", "words", "engine", "model"}``.  Raises RuntimeError
    with a short, secret-free message when the engine cannot run.
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
# Narration plan / manifest plumbing
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
        spoken = str(segment.get("spoken_text") or display).strip()
        if not segment_id or not display:
            continue
        entries.append({
            "segment_id": segment_id,
            "kind": str(segment.get("kind") or ""),
            "display_text": display,
            "spoken_text": spoken,
        })
    return entries


def _audio_path(run_dir: Path, segment_id: str) -> Path:
    return run_dir / "assets" / "audio" / f"narration-{segment_id}.wav"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _load_overrides(run_dir: Path) -> dict[str, str]:
    data = _load_json(_overrides_path(run_dir))
    return {str(key): str(value).strip() for key, value in data.items() if str(value).strip()}


def _update_tts_manifest_qa(run_dir: Path, qa_by_segment: Mapping[str, Mapping[str, Any]]) -> None:
    """Mirror the QA status onto the synthesis checklist for the voice agent."""

    manifest_path = run_dir / "artifacts" / "tts_manifest.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(manifest, Mapping):
        return
    segments = manifest.get("segments")
    if not isinstance(segments, list):
        return
    changed = False
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        segment_id = str(segment.get("segment_id") or "")
        qa = qa_by_segment.get(segment_id)
        if qa is not None and segment.get("qa") != dict(qa):
            segment["qa"] = dict(qa)
            changed = True
    if changed:
        write_json(manifest_path, manifest)


# ---------------------------------------------------------------------------
# Alignment output (real timestamps for captions)
# ---------------------------------------------------------------------------

def write_asr_alignment(
    run_dir: Path,
    segment_id: str,
    *,
    transcript: Mapping[str, Any],
    audio_relative_path: str,
    duration: float,
) -> Path:
    """Write the whisper word timestamps as the segment's alignment ledger.

    The file follows the standard alignment schema used by ``media.write_subtitles``
    but is tagged ``alignment_provider: asr-local`` and carries the raw
    transcript so the caption builder can match display text spans against
    real word times instead of proportionally splitting the audio.
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
        "asr_model": str(transcript.get("model", "")),
        "source_audio": audio_relative_path,
        "script_section_id": segment_id,
        "canonical_text": "display_text",
        "no_secrets_in_artifacts": True,
    })
    return alignment_path


# ---------------------------------------------------------------------------
# QA run
# ---------------------------------------------------------------------------

def run_speech_qa(
    run_dir: Path,
    *,
    engine: str | None = None,
    model: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run (or incrementally re-run) the ASR gate for every narration block.

    Incremental contract: a block whose WAV hash matches the last *passed*
    QA attempt is reused; anything else — first run, resynthesized audio, or
    ``--force`` — is transcribed again.  Attempt counters live in
    ``speech_qa_state.json`` so repeated invocations accumulate honestly.
    """

    plan = _load_narration_plan(run_dir)
    entries = _segment_entries(plan)
    state = _load_json(_state_path(run_dir))
    overrides = _load_overrides(run_dir)
    resolved_engine = engine or detect_engine()
    resolved_model = model or os.environ.get("AI_BRIEF_ASR_MODEL") or (
        DEFAULT_MLX_WHISPER_MODEL if resolved_engine == "mlx-whisper" else DEFAULT_WHISPER_MODEL
    )
    # Judgement rules evolve (e.g. the two-tier verdict policy): states
    # written by an older policy are re-judged instead of being honoured, so
    # a stricter old run cannot permanently block otherwise-correct audio.
    previous_report = _load_json(run_dir / "artifacts" / REPORT_FILENAME)
    state_reset = str(previous_report.get("version") or "") != SPEECH_QA_VERSION

    records: list[dict[str, Any]] = []
    manifest_qa: dict[str, dict[str, Any]] = {}
    resynthesis_queue: list[str] = []
    blocked_segments: list[str] = []
    engine_used: str | None = None

    for entry in entries:
        segment_id = entry["segment_id"]
        display_text = entry["display_text"]
        audio_path = _audio_path(run_dir, segment_id)
        segment_state = dict(state.get(segment_id) or {})
        if not isinstance(segment_state, Mapping):
            segment_state = {}

        record: dict[str, Any] = {
            "segment_id": segment_id,
            "kind": entry["kind"],
            "expected_text": display_text,
            "attempt": int(segment_state.get("attempt") or 1),
            "status": "pending",
            "audio_sha256": None,
            "spoken_override": None,
        }
        if not audio_path.is_file() or audio_path.stat().st_size <= 0:
            record["status"] = "missing_audio"
            record["reason"] = f"narration audio not found: {audio_path.name}"
            records.append(record)
            blocked_segments.append(segment_id)
            continue
        audio_hash = _file_sha256(audio_path)
        record["audio_sha256"] = audio_hash
        if state_reset:
            segment_state.pop("pending_attempt", None)
            if segment_state.get("status") in {"needs_resynthesis", "blocked"}:
                # Old-policy failure: re-judge with the current rules without
                # burning an extra attempt on the same audio.
                segment_state["status"] = "pending_rejudge"

        # Reuse a previously passed verdict for unchanged audio.
        if (
            not force
            and segment_state.get("status") == "passed"
            and segment_state.get("audio_sha256") == audio_hash
        ):
            record.update({
                "status": "passed",
                "verdict": segment_state.get("verdict") or "pass",
                "reason": "reused passed QA for unchanged audio",
                "similarity": segment_state.get("similarity"),
                "transcript": segment_state.get("transcript"),
                "engine": segment_state.get("engine"),
            })
            engine_used = engine_used or segment_state.get("engine")
            records.append(record)
            continue

        # A failed block whose audio has not changed is still waiting for the
        # voice agent to resynthesize: keep its state verbatim instead of
        # burning another attempt on the same WAV.
        if (
            not force
            and segment_state.get("status") in {"needs_resynthesis", "blocked"}
            and segment_state.get("audio_sha256") == audio_hash
        ):
            record.update({
                "status": str(segment_state.get("status")),
                "verdict": segment_state.get("verdict"),
                "reason": "audio unchanged since the failed attempt; waiting for resynthesis",
                "similarity": segment_state.get("similarity"),
                "transcript": segment_state.get("transcript"),
                "spoken_override": (overrides.get(segment_id) if segment_state.get("pending_attempt") == MAX_ATTEMPTS else None),
            })
            if record["status"] == "blocked":
                blocked_segments.append(segment_id)
            else:
                resynthesis_queue.append(segment_id)
            records.append(record)
            continue

        if resolved_engine in (None, "none"):
            record["status"] = "unavailable"
            record["reason"] = "no local ASR engine available; gate disabled"
            records.append(record)
            continue

        # A resynthesis happened (or the QA was forced): this pass counts as
        # the next synthesis attempt against the retry contract.
        pending = int(segment_state.get("pending_attempt") or 0)
        attempt = pending if pending else int(segment_state.get("attempt") or 1)
        record["attempt"] = attempt

        spoken_input = display_text
        if attempt >= MAX_ATTEMPTS:
            override = overrides.get(segment_id)
            if override:
                spoken_input = override
                record["spoken_override"] = override
            else:
                record["reason_note"] = "no colloquial override provided for the final attempt; raw text retried"

        try:
            transcript = transcribe(audio_path, engine=resolved_engine, model=resolved_model)
        except Exception as exc:  # noqa: BLE001 - engine failures vary widely; keep them short and secret-free
            record["status"] = "unavailable"
            record["reason"] = f"ASR failed: {str(exc)[:300]}"
            records.append(record)
            continue
        engine_used = engine_used or transcript.get("engine")

        verdict = compare_texts(
            spoken_input,
            str(transcript.get("text", "")),
            minimum_similarity=MIN_SIMILARITY,
            extra_expected_tokens=extract_key_tokens(display_text) if spoken_input != display_text else (),
        )
        record.update({
            "verdict": verdict["verdict"],
            "reason": verdict["reason"],
            "similarity": verdict["similarity"],
            "missing_tokens": verdict["missing_tokens"],
            "repetition": verdict["repetition"],
            "extra_content": verdict["extra_content"],
            "warnings": [str(item) for item in verdict.get("warnings", [])],
            "transcript": str(transcript.get("text", "")),
            "engine": transcript.get("engine"),
        })

        try:
            duration = media_duration(audio_path)
        except Exception as exc:  # noqa: BLE001 - surfaced in the report
            duration = 0.0
            record["duration_probe_error"] = str(exc)[:200]

        attempt_record = {
            **record,
            "expected_spoken_text": spoken_input,
            "attempted_at": _now(),
            "transcript_words": [dict(word) for word in transcript.get("words", []) if isinstance(word, Mapping)],
            "audio_duration_seconds": round(duration, 3),
            "no_secrets_in_artifacts": True,
        }
        write_json(
            _attempt_dir(run_dir, segment_id) / f"attempt-{attempt:02d}.json",
            attempt_record,
        )

        if verdict["verdict"] == "pass":
            record["status"] = "passed"
            state[segment_id] = {
                "status": "passed",
                "attempt": attempt,
                "audio_sha256": audio_hash,
                "verdict": verdict["verdict"],
                "reason": verdict["reason"],
                "similarity": verdict["similarity"],
                "transcript": str(transcript.get("text", "")),
                "engine": transcript.get("engine"),
                "pending_attempt": None,
                "updated_at": _now(),
            }
        else:
            next_attempt = attempt + 1
            if next_attempt > MAX_ATTEMPTS:
                record["status"] = "blocked"
                state[segment_id] = {
                    "status": "blocked",
                    "attempt": attempt,
                    "audio_sha256": audio_hash,
                    "verdict": verdict["verdict"],
                    "reason": verdict["reason"],
                    "pending_attempt": None,
                    "updated_at": _now(),
                }
                blocked_segments.append(segment_id)
            else:
                record["status"] = "needs_resynthesis"
                state[segment_id] = {
                    "status": "needs_resynthesis",
                    "attempt": attempt,
                    "audio_sha256": audio_hash,
                    "verdict": verdict["verdict"],
                    "reason": verdict["reason"],
                    "similarity": verdict["similarity"],
                    "transcript": str(transcript.get("text", "")),
                    "pending_attempt": next_attempt,
                    "next_attempt_uses_override": next_attempt >= MAX_ATTEMPTS,
                    "override_available": overrides.get(segment_id) is not None,
                    "updated_at": _now(),
                }
                resynthesis_queue.append(segment_id)

        # Real timestamps for the caption builder are written for every
        # transcribed block, pass or fail (a failed block blocks rendering
        # anyway, so its alignment file is purely diagnostic).
        try:
            if duration > 0:
                write_asr_alignment(
                    run_dir,
                    segment_id,
                    transcript=transcript,
                    audio_relative_path=f"assets/audio/{audio_path.name}",
                    duration=duration,
                )
        except Exception as exc:  # noqa: BLE001 - diagnostic only
            record["alignment_write_error"] = str(exc)[:200]

        manifest_qa[segment_id] = {
            "attempt": record["attempt"],
            "status": record["status"],
            "verdict": record.get("verdict"),
            "spoken_override": record.get("spoken_override"),
            "checked_at": _now(),
        }
        records.append(record)

    if resolved_engine in (None, "none"):
        status = "unavailable"
    elif blocked_segments:
        status = "blocked"
    elif resynthesis_queue:
        status = "failed"
    else:
        status = "passed"

    report = {
        "version": SPEECH_QA_VERSION,
        "status": status,
        "engine": engine_used or resolved_engine,
        "model": resolved_model,
        "max_attempts": MAX_ATTEMPTS,
        "minimum_similarity": MIN_SIMILARITY,
        "hard_floor_similarity": 0.45,
        "judgement_policy": {
            "hard_failures": ["empty_transcript", "missing_numeric_tokens", "repetition_of_authored_text", "major_omission", "low_similarity_below_hard_floor"],
            "accepted_with_warnings": ["unmatched_english_terms", "asr_hallucination_repetition", "moderate_similarity", "extra_content"],
            "rationale": "small ASR models mishear mixed-language technical terms; only high-confidence structural faults of the reading itself trigger resynthesis",
        },
        "generated_at": _now(),
        "segment_count": len(records),
        "segments": records,
        "resynthesis_queue": resynthesis_queue,
        "blocked_segments": blocked_segments,
        "overrides_file": str(_overrides_path(run_dir)),
        "no_secrets_in_artifacts": True,
    }
    write_json(_state_path(run_dir), state)
    write_json(run_dir / "artifacts" / REPORT_FILENAME, report)
    _update_tts_manifest_qa(run_dir, manifest_qa)
    return report


def ensure_speech_qa(run_dir: Path) -> dict[str, Any]:
    """Render-time gate: reuse a passed report or run the QA now.

    Raises :class:`SpeechQABlocked` when a voice block exhausted its retry
    budget, which aborts the render before any video is produced.
    """

    report = _load_json(run_dir / "artifacts" / REPORT_FILENAME)
    if report.get("status") == "passed":
        return report
    fresh = run_speech_qa(run_dir)
    if fresh.get("status") == "blocked":
        details = ", ".join(
            f"{item['segment_id']}({item.get('verdict')}: {str(item.get('reason'))[:80]})"
            for item in fresh.get("segments", [])
            if item.get("status") == "blocked"
        )
        raise SpeechQABlocked(
            "speech QA blocked the render; voice blocks still failing after "
            f"{MAX_ATTEMPTS} attempts: {details}. See "
            f"{run_dir / 'artifacts' / REPORT_FILENAME}"
        )
    return fresh


def print_handoff(report: Mapping[str, Any], run_dir: Path) -> None:
    """Print the resynthesis handoff for the voice-clone agent."""

    queue = list(report.get("resynthesis_queue") or [])
    if not queue:
        return
    overrides = _load_overrides(run_dir)
    details = {str(item.get("segment_id")): item for item in report.get("segments", []) if isinstance(item, Mapping)}
    print()
    print("=== 语音重合成清单（speech QA 未通过） ===")
    for segment_id in queue:
        item = details.get(segment_id) or {}
        attempt = int(item.get("attempt") or 1)
        next_attempt = min(attempt + 1, MAX_ATTEMPTS)
        audio = _audio_path(run_dir, segment_id)
        print(f"\n  [{segment_id}] attempt {next_attempt}/{MAX_ATTEMPTS}  ->  {audio}")
        print(f"    失败原因: {item.get('verdict')}: {item.get('reason')}")
        print(f"    ASR 听到的内容: {str(item.get('transcript') or '')[:120]}")
        if next_attempt >= MAX_ATTEMPTS:
            override = overrides.get(segment_id)
            if override:
                print(f"    本次朗读文本（口语改写，屏幕字幕不变）:\n      {override}")
            else:
                print("    本次朗读文本: 原文重试（可在 artifacts/speech_qa/colloquial_overrides.json "
                      f"为 {segment_id} 提供一次性口语改写）")
        else:
            print("    本次朗读文本: 原文重试")
    print("\n  完成后重新运行流水线；QA 将只复核上述段落。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local-ASR speech quality gate for AI每日早报 narration."
    )
    parser.add_argument("--date", required=True, help="Edition date, YYYY-MM-DD")
    parser.add_argument("--output-root", default=None, help="Override the outputs root")
    parser.add_argument("--engine", default=None, choices=list(_KNOWN_ENGINES), help="Force an ASR engine")
    parser.add_argument("--model", default=None, help="ASR model name or ggml model path")
    parser.add_argument("--force", action="store_true", help="Re-transcribe even unchanged audio")
    args = parser.parse_args(argv)

    from .config import DEFAULT_OUTPUT_ROOT

    output_root = Path(args.output_root) if args.output_root else DEFAULT_OUTPUT_ROOT
    run_dir = output_root / args.date
    if not run_dir.is_dir():
        print(f"run directory not found: {run_dir}", file=sys.stderr)
        return 2
    try:
        report = run_speech_qa(run_dir, engine=args.engine, model=args.model, force=args.force)
    except (FileNotFoundError, ValueError) as exc:
        print(f"  [ERROR] {exc}", file=sys.stderr)
        return 2
    print(f"Speech QA status: {report['status']} (engine={report.get('engine')}, model={report.get('model')})")
    for item in report.get("segments", []):
        marker = {"passed": "[PASS]", "needs_resynthesis": "[REDO]", "blocked": "[BLOCK]", "missing_audio": "[MISS]", "unavailable": "[SKIP]"}.get(str(item.get("status")), "[????]")
        similarity = item.get("similarity")
        similarity_text = f" sim={similarity:.3f}" if isinstance(similarity, (int, float)) else ""
        print(f"  {marker} {str(item.get('segment_id')):12s} attempt {item.get('attempt')}/{MAX_ATTEMPTS}{similarity_text}  {item.get('reason') or ''}")
    print(f"Report: {run_dir / 'artifacts' / REPORT_FILENAME}")
    print_handoff(report, run_dir)
    if report["status"] == "blocked":
        return 3
    if report["status"] == "failed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
