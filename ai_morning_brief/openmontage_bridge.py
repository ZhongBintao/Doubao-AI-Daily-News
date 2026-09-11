from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from .config import DEFAULT_AZURE_TTS_TEMPERATURE, DEFAULT_LOCALE, DEFAULT_RATE, DEFAULT_REGION, DEFAULT_VOICE, load_allowed_env
from .media import MediaError, concat_narration, make_audio_assets, media_duration, mix_audio, write_json, write_subtitles
from .speech import (
    GEMINI_DEFAULT_MODEL,
    GEMINI_DEFAULT_VOICE,
    AzureWordBoundaryProvider,
    GeminiTTSProvider,
    SpeechProviderError,
    proportional_word_timestamps,
    validate_word_boundaries,
)


class OpenMontageError(RuntimeError):
    """Raised when OpenMontage or Azure cannot complete a stage."""


_TRANSIENT_PROVIDER_MARKERS = (
    "ssleoferror",
    "connectionerror",
    "connecttimeout",
    "readtimeout",
    "timed out",
    "max retries exceeded",
    "temporarily unavailable",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
)


def _file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _retryable_provider_result(result: Any) -> bool:
    if result is None or getattr(result, "success", False):
        return False
    error = str(getattr(result, "error", "") or "").lower()
    return any(marker in error for marker in _TRANSIENT_PROVIDER_MARKERS)


def _execute_with_transient_retries(call: Any, *, attempts: int = 3) -> Any:
    """Retry only transport/server failures; never switch voice providers.

    A fresh ``trust_env=False`` session avoids an intermittent keep-alive/TLS
    EOF observed on the local proxy path while keeping the provider contract
    and credentials entirely inside the OpenMontage call.
    """

    import requests

    session = requests.Session()
    session.trust_env = False
    original_post = requests.post
    requests.post = session.post
    try:
        result = None
        for attempt in range(attempts):
            result = call()
            if not _retryable_provider_result(result) or attempt >= attempts - 1:
                return result
            time.sleep(1.5 * (attempt + 1))
        return result
    finally:
        requests.post = original_post
        session.close()


def _audio_events(script: Mapping[str, Any], durations: Mapping[str, float], assets: Mapping[str, Path]) -> list[dict[str, Any]]:
    """Place quiet whooshes at every story boundary and chimes at section changes."""

    events: list[dict[str, Any]] = []
    cursor = 0.0
    previous_category: str | None = None
    seen_news = False
    for segment in script.get("segments", []):
        segment_id = str(segment.get("id"))
        duration = max(0.0, float(durations.get(segment_id, 0.0)))
        kind = str(segment.get("kind") or "")
        if kind == "overview":
            events.append({"kind": "overview-entry", "path": str(assets["chime"]), "start": max(0.0, cursor - 0.10), "volume": 0.16})
        elif kind == "news":
            events.append({"kind": "story-boundary", "path": str(assets["whoosh"]), "start": max(0.0, cursor - 0.22), "volume": 0.34})
            category = str(segment.get("category") or "other")
            if seen_news and category != previous_category:
                events.append({"kind": "category-change", "path": str(assets["chime"]), "start": max(0.0, cursor - 0.06), "volume": 0.22})
            previous_category = category
            seen_news = True
        elif kind == "outro":
            events.append({"kind": "outro", "path": str(assets["chime"]), "start": max(0.0, cursor - 0.10), "volume": 0.20})
        cursor += duration
    return events


def _music_boundaries(script: Mapping[str, Any], durations: Mapping[str, float]) -> dict[str, float]:
    """Map the podcast opening/middle/ending semantics onto video scenes."""

    cursor = 0.0
    intro_end = 0.0
    overview_start = 0.0
    outro_start = 0.0
    total = 0.0
    for segment in script.get("segments", []):
        segment_id = str(segment.get("id"))
        duration = max(0.0, float(durations.get(segment_id, 0.0)))
        if segment.get("kind") == "overview":
            overview_start = cursor
        if segment.get("kind") == "intro":
            intro_end = cursor + duration
        if segment.get("kind") == "outro":
            outro_start = cursor
        cursor += duration
        total = cursor
    ending_start = max(overview_start, outro_start - 3.0)
    return {
        "intro_end": intro_end,
        "middle_start": overview_start,
        "ending_start": min(total, ending_start),
        "total_duration": total,
    }


def _subtitle_alignment_report(
    entries: list[Mapping[str, Any]],
    *,
    provider: str,
    aligned: bool,
) -> dict[str, Any]:
    """Describe the timing source actually used to build the subtitles."""

    provider_name = str(provider or "").strip().lower()
    segments: list[dict[str, Any]] = []
    for entry in entries:
        segment_id = str(entry.get("segment_id") or "")
        if not aligned:
            if provider_name == "gemini":
                mode = "gemini-proportional"
            elif provider_name == "doubao-voice-clone":
                mode = "doubao-voice-clone-proportional"
            elif provider_name == "doubao":
                mode = "doubao-proportional"
            else:
                mode = "proportional-fallback"
        else:
            mode = str(entry.get("alignment_provider") or "provider-word-timestamp")
        segments.append({
            "segment_id": segment_id,
            "mode": mode,
            "alignment_path": entry.get("alignment_path"),
        })
    modes = sorted({str(segment["mode"]) for segment in segments if segment.get("mode")})
    return {
        "requested": bool(aligned),
        "mode": modes[0] if len(modes) == 1 else "mixed" if modes else "none",
        "approximate": any(mode in {"gemini-proportional", "proportional-fallback", "doubao-proportional", "doubao-voice-clone-proportional"} for mode in modes),
        "proportional_fallback_segments": [segment["segment_id"] for segment in segments if segment["mode"] == "proportional-fallback"],
        "compatibility_segments": [segment["segment_id"] for segment in segments if segment["mode"] == "azure-stt-compat"],
        "segments": segments,
    }


def _pad_audio(path: Path, target_duration: float) -> None:
    """Pad a short spoken segment with silence for overview reading time."""

    current = media_duration(path)
    if target_duration <= current + 0.02:
        return
    temporary = path.with_name(f".{path.stem}-padded.wav")
    result = subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-i", str(path),
        "-af", f"apad=pad_dur={target_duration - current:.3f}", "-t", f"{target_duration:.3f}",
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "1", str(temporary),
    ], capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0 or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        raise OpenMontageError(f"could not pad audio segment {path.name}: {(result.stderr or '')[-400:]}")
    os.replace(temporary, path)


def _load_azure_environment(env_path: Path | None) -> dict[str, str]:
    values = load_allowed_env(env_path)
    for key, value in values.items():
        os.environ[key] = value
    if not os.environ.get("AZURE_SPEECH_REGION"):
        os.environ["AZURE_SPEECH_REGION"] = DEFAULT_REGION
    # Older local .env files sometimes store the STT endpoint under the TTS
    # name. Translate it without ever logging the endpoint or key value.
    endpoint = os.environ.get("AZURE_TTS_ENDPOINT", "").rstrip("/")
    if endpoint and ".api.cognitive.microsoft.com" in endpoint:
        os.environ["AZURE_SPEECH_ENDPOINT"] = endpoint
        os.environ.pop("AZURE_TTS_ENDPOINT", None)
    if not os.environ.get("AZURE_SPEECH_KEY"):
        raise OpenMontageError("AZURE_SPEECH_KEY is not configured")
    return values


def _load_gemini_environment(env_path: Path | None) -> dict[str, str]:
    """Load Google AI Studio/Gemini settings without exposing the key."""

    values = load_allowed_env(env_path)
    for key, value in values.items():
        os.environ[key] = value
    # ``load_allowed_env`` provides this alias for project .env files; repeat
    # the check for callers that exported the Google-facing name directly.
    if not os.environ.get("GEMINI_API_KEY") and os.environ.get("GOOGLE_AI_STUDIO_API_KEY"):
        os.environ["GEMINI_API_KEY"] = os.environ["GOOGLE_AI_STUDIO_API_KEY"]
    if not os.environ.get("GEMINI_API_KEY"):
        raise OpenMontageError("GOOGLE_AI_STUDIO_API_KEY is not configured")
    return values


def _synthesize_and_align_gemini(
    project_dir: Path,
    script: Mapping[str, Any],
    *,
    openmontage_root: Path,
    env_path: Path | None = None,
    align: bool = True,
) -> dict[str, Any]:
    """Render one explicitly requested Gemini TTS edition.

    Gemini supplies audio but no stable equivalent to Azure WordBoundary. The
    provider call is therefore recorded as ``gemini-proportional`` and the
    subtitles deliberately use the local deterministic proportional timing
    fallback. No Azure credentials or OpenMontage Azure tools are consulted.
    """

    _load_gemini_environment(env_path)
    if not openmontage_root.is_dir():
        raise OpenMontageError(f"OpenMontage directory not found: {openmontage_root}")
    provider = GeminiTTSProvider(
        model=os.environ.get("GEMINI_TTS_MODEL", GEMINI_DEFAULT_MODEL),
        voice=os.environ.get("GEMINI_TTS_VOICE", GEMINI_DEFAULT_VOICE),
    )
    audio_dir = project_dir / "assets" / "audio"
    alignment_dir = project_dir / "artifacts" / "alignments"
    audio_dir.mkdir(parents=True, exist_ok=True)
    alignment_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    durations: dict[str, float] = {}
    spoken_durations: dict[str, float] = {}
    for segment in script.get("segments", []):
        segment_id = str(segment["id"])
        display_text = str(segment.get("display_text") or segment.get("broadcast_text") or "").strip()
        spoken_text = str(segment.get("spoken_text") or display_text).strip()
        if not display_text or not spoken_text:
            raise OpenMontageError(f"empty narration text for {segment_id}")
        output_path = audio_dir / f"narration-{segment_id}.wav"
        speech_path = audio_dir / f".narration-{segment_id}-speech.wav"
        speech_path.unlink(missing_ok=True)
        try:
            result = provider.synthesize(spoken_text, speech_path)
        except SpeechProviderError as exc:
            raise OpenMontageError(f"Gemini TTS failed for {segment_id}: {exc}") from exc
        if not speech_path.is_file() or speech_path.stat().st_size == 0:
            raise OpenMontageError(f"Gemini TTS returned no audio for {segment_id}")
        speech_duration = media_duration(speech_path)
        spoken_durations[segment_id] = speech_duration
        minimum_duration = max(0.0, float(segment.get("minimum_duration_seconds") or 0.0))
        if minimum_duration > speech_duration + 0.02:
            _pad_audio(speech_path, minimum_duration)
        os.replace(speech_path, output_path)
        duration = media_duration(output_path)
        durations[segment_id] = duration
        alignment_path: Path | None = None
        word_timestamps: list[dict[str, Any]] = []
        if align:
            word_timestamps = proportional_word_timestamps(spoken_text, duration)
            alignment_path = alignment_dir / f"{segment_id}.json"
            write_json(alignment_path, {
                "version": "1.0",
                "provider": "gemini",
                "alignment_provider": provider.alignment_provider,
                "alignment_quality": "approximate",
                "language": DEFAULT_LOCALE,
                "duration_seconds": duration,
                "segments": [{"text": spoken_text, "start": 0.0, "end": duration}],
                "word_timestamps": word_timestamps,
                "source_audio": f"assets/audio/{output_path.name}",
                "script_section_id": segment_id,
                "canonical_text": spoken_text,
            })
        manifest.append({
            "segment_id": segment_id,
            "audio_path": f"assets/audio/{output_path.name}",
            "alignment_path": f"artifacts/alignments/{alignment_path.name}" if alignment_path else None,
            "provider": "gemini",
            "model": result.get("model", provider.model),
            "voice": result.get("voice", provider.voice),
            "locale": DEFAULT_LOCALE,
            "alignment_provider": provider.alignment_provider if align else None,
            "alignment_quality": "approximate" if align else "none",
            "word_count": len(word_timestamps),
            "spoken_duration_seconds": round(speech_duration, 3),
            "duration_seconds": round(duration, 3),
            "display_text": display_text,
            "spoken_text": spoken_text,
            "native_word_boundary": False,
        })
    segment_ids = [str(segment["id"]) for segment in script.get("segments", [])]
    narration = concat_narration(project_dir, segment_ids)
    audio_assets = make_audio_assets(project_dir, media_duration(narration))
    audio_events = _audio_events(script, durations, audio_assets)
    music_boundaries = _music_boundaries(script, durations)
    final_mix = mix_audio(project_dir, narration, audio_assets, audio_events, music_boundaries)
    # Gemini has no provider-native word boundaries, so captions stay on the
    # authored display text and use the deterministic fallback in media.py.
    subtitle_path, cues = write_subtitles(project_dir, script, durations, aligned=False, spoken_durations=spoken_durations)
    subtitle_alignment = _subtitle_alignment_report(manifest, provider="gemini", aligned=False)
    manifest_path = project_dir / "artifacts" / "google_audio_manifest.json"
    write_json(manifest_path, {
        "version": "1.0",
        "provider": "gemini",
        "model": provider.model,
        "voice": provider.voice,
        "locale": DEFAULT_LOCALE,
        "tts_settings": {"canonical_text": "spoken_text", "alignment_provider": provider.alignment_provider, "alignment_quality": "approximate"},
        "segments": manifest,
        "audio": {
            "narration_track": "assets/audio/narration-track.wav",
            "music": {"opening": "assets/music/opening.mp3", "middle": "assets/music/middle-loop.mp3", "ending": "assets/music/ending.mp3"},
            "transition_whoosh": "assets/audio/transition-whoosh.wav",
            "category_chime": "assets/audio/category-chime.wav",
            "final_mix": "assets/audio/final-mix.wav",
            "events": audio_events,
            "music_boundaries": music_boundaries,
            "music_report": "artifacts/background-music.json",
        },
        "spoken_durations": {key: round(value, 3) for key, value in spoken_durations.items()},
        "subtitle_path": "assets/subtitles/subtitles.srt",
        "subtitle_cue_count": len(cues),
        "subtitle_alignment": subtitle_alignment,
        "failure_policy": "stop on Gemini TTS failure; no automatic provider fallback",
        "native_word_boundary": False,
        "compatibility_fallback": "deterministic proportional subtitle timing",
        "no_secrets_in_artifacts": True,
    })
    return {
        "durations": durations,
        "final_mix": final_mix,
        "subtitle_path": subtitle_path,
        "manifest": manifest,
        "manifest_path": manifest_path,
        "subtitle_cues": cues,
        "music_boundaries": music_boundaries,
        "provider": "gemini",
        "alignment_provider": provider.alignment_provider,
        "subtitle_alignment": subtitle_alignment,
    }


def reuse_synthesized_audio(
    project_dir: Path,
    script: Mapping[str, Any],
    *,
    align: bool = True,
    speech_provider: str = "gemini",
) -> dict[str, Any]:
    """Reuse a completed provider pass while rebuilding mix/render stages.

    This is intentionally an explicit opt-in recovery path. It never calls a
    provider; it verifies that the frozen script's segment audio and manifest
    are complete, then rebuilds subtitles and the local mix from those stems.
    """

    provider_name = str(speech_provider or "").strip().lower()
    if provider_name == "gemini":
        manifest_name = "google_audio_manifest.json"
    elif provider_name in {"doubao", "doubao-voice-clone"}:
        manifest_name = "doubao_audio_manifest.json"
    else:
        manifest_name = "azure_audio_manifest.json"
    manifest_path = project_dir / "artifacts" / manifest_name
    if not manifest_path.is_file():
        raise OpenMontageError(f"cannot reuse audio; manifest is missing: {manifest_name}")
    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenMontageError(f"cannot read reusable audio manifest: {manifest_name}") from exc
    if not isinstance(manifest_data, Mapping) or str(manifest_data.get("provider")) != provider_name:
        raise OpenMontageError(f"reusable audio manifest provider mismatch: expected {provider_name}")
    tts_settings = manifest_data.get("tts_settings") if isinstance(manifest_data.get("tts_settings"), Mapping) else {}
    alignment_provider = tts_settings.get("alignment_provider")
    if not alignment_provider:
        alignment_provider = next(
            (str(entry.get("alignment_provider")) for entry in manifest_data.get("segments", [])
             if isinstance(entry, Mapping) and entry.get("alignment_provider")),
            None,
        )
    by_id = {str(entry.get("segment_id")): entry for entry in manifest_data.get("segments", []) if isinstance(entry, Mapping)}
    durations: dict[str, float] = {}
    spoken_durations: dict[str, float] = {}
    for segment in script.get("segments", []):
        segment_id = str(segment.get("id"))
        entry = by_id.get(segment_id)
        standard_audio_path = project_dir / "assets" / "audio" / f"narration-{segment_id}.wav"
        declared_audio_path = standard_audio_path
        if entry and entry.get("audio_path"):
            declared_audio_path = Path(str(entry["audio_path"]))
            if not declared_audio_path.is_absolute():
                declared_audio_path = project_dir / declared_audio_path
        audio_path = declared_audio_path.resolve()
        project_root = project_dir.resolve()
        if audio_path != project_root and project_root not in audio_path.parents:
            raise OpenMontageError(f"reusable audio path escapes the edition directory for {segment_id}")
        if entry is None or not audio_path.is_file() or audio_path.stat().st_size == 0:
            raise OpenMontageError(f"reusable audio is incomplete for {segment_id}")
        script_spoken = str(segment.get("spoken_text") or segment.get("display_text") or segment.get("broadcast_text") or "")
        manifest_spoken = str(entry.get("spoken_text") or "")
        if manifest_spoken and manifest_spoken != script_spoken:
            raise OpenMontageError(f"reusable audio manifest text is stale for {segment_id}")
        expected_hash = str(entry.get("sha256") or "")
        if expected_hash and _file_sha256(audio_path) != expected_hash:
            raise OpenMontageError(f"reusable audio hash mismatch for {segment_id}")
        # The media mixer intentionally consumes the canonical narration-<id>
        # paths. External Doubao handoffs may declare another in-run path, so
        # materialize a verified copy at the canonical path before mixing.
        if audio_path != standard_audio_path.resolve():
            standard_audio_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(audio_path, standard_audio_path)
            audio_path = standard_audio_path
        if align and provider_name not in {"gemini", "doubao", "doubao-voice-clone"}:
            alignment_path = project_dir / "artifacts" / "alignments" / f"{segment_id}.json"
            if not alignment_path.is_file() or alignment_path.stat().st_size == 0:
                raise OpenMontageError(f"reusable subtitle alignment is missing for {segment_id}: {alignment_path}")
        durations[segment_id] = media_duration(audio_path)
        spoken_durations[segment_id] = float(entry.get("spoken_duration_seconds") or durations[segment_id])
    segment_ids = [str(segment["id"]) for segment in script.get("segments", [])]
    narration = concat_narration(project_dir, segment_ids)
    audio_assets = make_audio_assets(project_dir, media_duration(narration))
    audio_events = _audio_events(script, durations, audio_assets)
    music_boundaries = _music_boundaries(script, durations)
    final_mix = mix_audio(project_dir, narration, audio_assets, audio_events, music_boundaries)
    # ASR-measured word timestamps for the captions: transcribe each narration
    # block (incremental, cached by audio hash) and write alignment ledgers.
    # This is best-effort by design — a block without a usable alignment
    # simply keeps deterministic proportional timing; nothing here can fail
    # or block the render.
    subtitle_alignment_entries = [dict(entry) for entry in manifest_data.get("segments", []) if isinstance(entry, Mapping)]
    transcription_summary: dict[str, Any] | None = None
    measured_ids: set[str] = set()
    if align:
        try:
            from .speech_qa import transcribe_narration

            transcription_summary = transcribe_narration(project_dir)
            measured_ids = set(transcription_summary.get("aligned_segments") or [])
        except (ImportError, FileNotFoundError, ValueError, RuntimeError) as exc:
            transcription_summary = {"error": str(exc)[:300]}
    for entry in subtitle_alignment_entries:
        if str(entry.get("segment_id") or "") in measured_ids:
            entry["alignment_provider"] = "asr-local"
    # Azure keeps its hard "alignment required" contract; doubao/gemini use
    # whatever measured alignments exist and fall back per block.
    azure_contract = provider_name not in {"gemini", "doubao", "doubao-voice-clone"}
    subtitle_aligned = bool(align) and (azure_contract or bool(measured_ids))
    subtitle_path, cues = write_subtitles(
        project_dir,
        script,
        durations,
        aligned=subtitle_aligned,
        spoken_durations=spoken_durations,
        proportional_fallback=not azure_contract,
    )
    subtitle_alignment = _subtitle_alignment_report(
        subtitle_alignment_entries,
        provider=provider_name,
        aligned=subtitle_aligned,
    )
    subtitle_alignment["speech_qa_status"] = (
        "measured" if measured_ids else ("transcription_unavailable" if transcription_summary else "not_run")
    )
    if transcription_summary:
        subtitle_alignment["transcription_engine"] = transcription_summary.get("engine")
        subtitle_alignment["transcription_failed_segments"] = list(transcription_summary.get("failed_segments") or [])
    return {
        "durations": durations,
        "final_mix": final_mix,
        "subtitle_path": subtitle_path,
        "manifest": list(manifest_data.get("segments") or []),
        "manifest_path": manifest_path,
        "subtitle_cues": cues,
        "music_boundaries": music_boundaries,
        "provider": provider_name,
        "alignment_provider": alignment_provider,
        "subtitle_alignment": subtitle_alignment,
    }


def synthesize_and_align(
    project_dir: Path,
    script: Mapping[str, Any],
    *,
    openmontage_root: Path,
    env_path: Path | None = None,
    align: bool = True,
    speech_provider: str = "azure",
) -> dict[str, Any]:
    speech_provider = str(speech_provider or "azure").strip().lower()
    if speech_provider == "gemini":
        return _synthesize_and_align_gemini(
            project_dir,
            script,
            openmontage_root=openmontage_root,
            env_path=env_path,
            align=align,
        )
    if speech_provider in {"doubao", "doubao-voice-clone"}:
        raise OpenMontageError(
            f"{speech_provider} provider cannot synthesize directly; use the doubao TTS "
            "adapter (or the daily orchestrator) to pre-synthesize audio via the "
            "in-conversation voice synthesis tool, then run pipeline run with "
            f"--reuse-audio --speech-provider {speech_provider}"
        )
    if speech_provider != "azure":
        raise OpenMontageError(f"unsupported speech provider: {speech_provider}")
    _load_azure_environment(env_path)
    if not openmontage_root.is_dir():
        raise OpenMontageError(f"OpenMontage directory not found: {openmontage_root}")
    sys.path.insert(0, str(openmontage_root))
    # Prefer the Speech SDK path because it returns canonical WordBoundary
    # offsets during synthesis. The REST tools remain an explicit compatibility
    # path for an existing OpenMontage checkout while the optional SDK is being
    # installed; this path is labelled in the manifest and still aligns the
    # authored text rather than accepting STT as the subtitle source.
    native_provider: AzureWordBoundaryProvider | None = None
    native_error: str | None = None
    try:
        native_provider = AzureWordBoundaryProvider(
            region=os.environ.get("AZURE_SPEECH_REGION", DEFAULT_REGION),
            voice=DEFAULT_VOICE,
            locale=DEFAULT_LOCALE,
            temperature=DEFAULT_AZURE_TTS_TEMPERATURE,
        )
    except SpeechProviderError as exc:
        native_error = str(exc)
    try:
        from tools.audio.azure_tts import AzureTTS
        from tools.analysis.azure_stt import AzureSpeechToText
    except Exception as exc:  # pragma: no cover - depends on local checkout
        if native_provider is None:
            raise OpenMontageError(f"could not import OpenMontage audio tools: {exc}") from exc
        AzureTTS = None  # type: ignore[assignment]
        AzureSpeechToText = None  # type: ignore[assignment]

    audio_dir = project_dir / "assets" / "audio"
    alignment_dir = project_dir / "artifacts" / "alignments"
    audio_dir.mkdir(parents=True, exist_ok=True)
    alignment_dir.mkdir(parents=True, exist_ok=True)
    tts = AzureTTS() if AzureTTS is not None else None
    stt = AzureSpeechToText() if AzureSpeechToText is not None else None
    manifest: list[dict[str, Any]] = []
    durations: dict[str, float] = {}
    spoken_durations: dict[str, float] = {}
    for segment in script.get("segments", []):
        segment_id = str(segment["id"])
        display_text = str(segment.get("display_text") or segment.get("broadcast_text") or "").strip()
        spoken_text = str(segment.get("spoken_text") or display_text).strip()
        if not display_text or not spoken_text:
            raise OpenMontageError(f"empty narration text for {segment_id}")
        output_path = audio_dir / f"narration-{segment_id}.wav"
        speech_path = audio_dir / f".narration-{segment_id}-speech.wav"
        boundary_words: list[dict[str, Any]] = []
        alignment_provider = "azure-word-boundary"
        native_failure: str | None = None
        if native_provider is not None:
            try:
                native_result = native_provider.synthesize(spoken_text, speech_path)
                boundary_words = [
                    {"word": str(boundary.get("text") or ""), "start": float(boundary.get("start_seconds", 0.0)), "end": float(boundary.get("end_seconds", 0.0)), "text_offset": int(boundary.get("text_offset", 0)), "word_length": int(boundary.get("word_length", 0))}
                    for boundary in validate_word_boundaries(spoken_text, native_result.get("word_boundaries"))
                ]
            except SpeechProviderError as exc:
                # A transient SDK failure must not discard a complete edition.
                # Fall back to the existing REST/STT adapter for this segment,
                # and expose the downgrade in the manifest for review.
                native_failure = str(exc)[:400]
                speech_path.unlink(missing_ok=True)
                alignment_provider = "azure-stt-compat"
        if native_provider is None or alignment_provider == "azure-stt-compat":
            if tts is None:
                raise OpenMontageError(f"Azure TTS unavailable for {segment_id}: {native_error or 'provider not installed'}")
            result = _execute_with_transient_retries(lambda: tts.execute({
                "text": spoken_text,
                "voice": DEFAULT_VOICE,
                "locale": DEFAULT_LOCALE,
                # DragonHD rejects expressive prosody; neutral values are kept
                # only for the REST compatibility adapter.
                "rate": "0%",
                "pitch": "0%",
                "output_format": "wav",
                "output_path": str(speech_path),
            }))
            if not result.success or not speech_path.is_file() or speech_path.stat().st_size == 0:
                raise OpenMontageError(f"Azure TTS failed for {segment_id}: {result.error}")
            alignment_provider = "azure-stt-compat"
        speech_duration = media_duration(speech_path)
        spoken_durations[segment_id] = speech_duration
        minimum_duration = max(0.0, float(segment.get("minimum_duration_seconds") or 0.0))
        if minimum_duration > speech_duration + 0.02:
            _pad_audio(speech_path, minimum_duration)
        os.replace(speech_path, output_path)
        duration = media_duration(output_path)
        durations[segment_id] = duration
        alignment_path: Path | None = None
        word_count = 0
        # Use STT only for segments that actually used the compatibility TTS
        # path.  A native provider can be configured globally while one
        # segment downgrades after a timeout/error; that segment still needs a
        # real compatibility alignment rather than an empty native ledger.
        if align and alignment_provider == "azure-stt-compat":
            if stt is None:
                raise OpenMontageError(f"Azure STT compatibility tool unavailable for {segment_id}")
            alignment_result = _execute_with_transient_retries(lambda: stt.execute({
                "input_path": str(output_path),
                "language": DEFAULT_LOCALE,
                "diarize": False,
                "profanity_filter": "None",
                "output_dir": str(alignment_dir),
            }))
            words = (alignment_result.data or {}).get("word_timestamps", []) if alignment_result.success else []
            if not alignment_result.success or not words:
                raise OpenMontageError(f"Azure word alignment failed for {segment_id}: {alignment_result.error}")
            word_count = len(words)
            alignment_path = alignment_dir / f"{segment_id}.json"
            write_json(alignment_path, {
                "version": "2.0",
                "provider": "azure",
                "alignment_provider": alignment_provider,
                "language": DEFAULT_LOCALE,
                "duration_seconds": alignment_result.data.get("duration_seconds"),
                "segments": alignment_result.data.get("segments", []),
                "word_timestamps": words,
                "source_audio": f"assets/audio/{output_path.name}",
                "script_section_id": segment_id,
                "canonical_text": spoken_text,
            })
        elif align and alignment_provider == "azure-word-boundary":
            word_count = len(boundary_words)
            alignment_path = alignment_dir / f"{segment_id}.json"
            write_json(alignment_path, {
                "version": "2.0",
                "provider": "azure",
                "alignment_provider": alignment_provider,
                "language": DEFAULT_LOCALE,
                "duration_seconds": duration,
                "segments": [{"text": spoken_text, "start": 0.0, "end": duration}],
                "word_timestamps": boundary_words,
                "word_boundaries": [
                    {"text_offset": word.get("text_offset"), "word_length": word.get("word_length"), "start_seconds": word.get("start"), "end_seconds": word.get("end"), "text": word.get("word")}
                    for word in boundary_words
                ],
                "source_audio": f"assets/audio/{output_path.name}",
                "script_section_id": segment_id,
                "canonical_text": spoken_text,
            })
        manifest.append({
            "segment_id": segment_id,
            "audio_path": f"assets/audio/{output_path.name}",
            "alignment_path": f"artifacts/alignments/{alignment_path.name}" if alignment_path else None,
            "provider": "azure",
            "alignment_provider": alignment_provider,
            "voice": DEFAULT_VOICE,
            "locale": DEFAULT_LOCALE,
            "region": os.environ.get("AZURE_SPEECH_REGION", DEFAULT_REGION),
            "word_count": word_count,
            "spoken_duration_seconds": round(speech_duration, 3),
            "duration_seconds": round(duration, 3),
            "temperature_parameter_sent": bool(native_provider is not None),
            "display_text": display_text,
            "spoken_text": spoken_text,
            # A configured native provider is not enough to claim native
            # alignment: an individual segment may have fallen back to the
            # REST/STT compatibility path after an SDK error.
            "native_word_boundary": alignment_provider == "azure-word-boundary",
            "native_failure": native_failure,
        })
    segment_ids = [str(segment["id"]) for segment in script.get("segments", [])]
    narration = concat_narration(project_dir, segment_ids)
    audio_assets = make_audio_assets(project_dir, media_duration(narration))
    audio_events = _audio_events(script, durations, audio_assets)
    music_boundaries = _music_boundaries(script, durations)
    final_mix = mix_audio(project_dir, narration, audio_assets, audio_events, music_boundaries)
    subtitle_path, cues = write_subtitles(project_dir, script, durations, aligned=align, spoken_durations=spoken_durations)
    subtitle_alignment = _subtitle_alignment_report(manifest, provider="azure", aligned=align)
    write_json(project_dir / "artifacts" / "azure_audio_manifest.json", {
        "version": "2.0",
        "provider": "azure",
        "voice": DEFAULT_VOICE,
        "locale": DEFAULT_LOCALE,
        "region": os.environ.get("AZURE_SPEECH_REGION", DEFAULT_REGION),
        "tts_settings": {"rate": "neutral", "pitch": "neutral", "temperature": DEFAULT_AZURE_TTS_TEMPERATURE, "temperature_parameter_sent": native_provider is not None, "canonical_text": "spoken_text"},
        "segments": manifest,
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
            "events": audio_events,
            "music_boundaries": music_boundaries,
            "music_report": "artifacts/background-music.json",
        },
        "spoken_durations": {key: round(value, 3) for key, value in spoken_durations.items()},
        "subtitle_path": "assets/subtitles/subtitles.srt",
        "subtitle_cue_count": len(cues),
        "subtitle_alignment": subtitle_alignment,
        "failure_policy": "stop on Azure failure; Gemini is benchmark-only until explicit approval",
        "native_word_boundary": any(bool(entry.get("native_word_boundary")) for entry in manifest),
        "compatibility_fallback": native_error if native_provider is None else None,
        "compatibility_segments": [entry["segment_id"] for entry in manifest if entry.get("alignment_provider") == "azure-stt-compat"],
        "no_secrets_in_artifacts": True,
    })
    return {"durations": durations, "final_mix": final_mix, "subtitle_path": subtitle_path, "manifest": manifest, "subtitle_cues": cues, "music_boundaries": music_boundaries, "provider": "azure", "alignment_provider": "azure-word-boundary" if native_provider is not None else "azure-stt-compat", "subtitle_alignment": subtitle_alignment}


def render_hyperframes(project_dir: Path, *, openmontage_root: Path, output_path: Path) -> dict[str, Any]:
    if not (project_dir / "hyperframes" / "index.html").is_file():
        raise OpenMontageError("HyperFrames workspace has no index.html")
    sys.path.insert(0, str(openmontage_root))
    try:
        from tools.video.hyperframes_compose import HyperFramesCompose
    except Exception as exc:  # pragma: no cover - depends on local checkout
        raise OpenMontageError(f"could not import OpenMontage HyperFrames tool: {exc}") from exc
    tool = HyperFramesCompose()
    result = tool.execute({
        "operation": "render_existing",
        "workspace_path": str(project_dir / "hyperframes"),
        "output_path": str(output_path),
        "quality": "standard",
        "fps": 30,
        "strict_check": False,
        "snapshots": True,
    })
    if not result.success:
        raise OpenMontageError(f"HyperFrames render failed: {result.error}")
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise OpenMontageError("HyperFrames reported success but output MP4 is missing")
    return result.data or {"output": str(output_path)}


def probe_video(path: Path) -> dict[str, Any]:
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration,size",
            "-show_entries", "stream=codec_type,codec_name,width,height,sample_rate,channels",
            "-of", "json", str(path),
        ], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OpenMontageError(f"ffprobe failed for {path}") from exc
    if result.returncode != 0:
        raise OpenMontageError(f"ffprobe failed: {(result.stderr or '')[-300:]}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenMontageError("ffprobe returned invalid JSON") from exc
    streams = data.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if not video or not audio:
        raise OpenMontageError("final MP4 must contain both video and audio streams")
    width, height = video.get("width"), video.get("height")
    if width != 1920 or height != 1080:
        raise OpenMontageError(f"final MP4 must be 1920x1080, got {width}x{height}")
    return data
