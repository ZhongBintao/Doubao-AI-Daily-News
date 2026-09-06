from __future__ import annotations

"""Speech provider adapters and the gated Gemini shadow benchmark.

Azure remains the production provider by default. The native Speech SDK
adapter is used when the optional SDK is installed; the OpenMontage REST tools
remain an explicit compatibility fallback so an existing local checkout can
still be rerendered while the SDK is being provisioned. Gemini can be selected
explicitly for a one-off production edition, but is never an automatic
fallback.
"""

import base64
import hashlib
import json
import os
import random
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Mapping

from .config import DEFAULT_AZURE_TTS_TEMPERATURE, DEFAULT_LOCALE, DEFAULT_REGION, DEFAULT_VOICE
from .media import media_duration, write_json


TICKS_PER_SECOND = 10_000_000
GEMINI_DEFAULT_MODEL = "gemini-3.1-flash-tts-preview"
GEMINI_DEFAULT_VOICE = "Kore"
BENCHMARK_PHRASE_COUNT = 20
BENCHMARK_REGRESSION_PHRASES = (
    "模型采用 Q4_K_M 量化，文件大小是 17GB。",
    "实测生成速度约为 14 tokens/s。",
    "上下文窗口达到 262,144 token。",
    "这套模型有 27.3B 参数。",
    "服务面向 en-US、hi-IN 和 zh-CN。",
    "外部机构的门槛是 100 亿美元。",
    "Open ASR 的识别结果需要人工复核。",
    "版本从 GLM-5.3 更新到 Qwen3.8 27B。",
)
BENCHMARK_NUMERIC_ERROR_MAX = 0
BENCHMARK_TERM_ACCURACY_MIN = 0.95
BENCHMARK_NATURALNESS_DELTA_MIN = -0.25


class SpeechProviderError(RuntimeError):
    """Raised when a speech provider cannot produce an auditable result."""


def _safe_provider_error(exc: BaseException) -> str:
    """Keep provider diagnostics useful without copying keys or URLs."""

    message = str(exc)
    for key in ("GEMINI_API_KEY", "GOOGLE_AI_STUDIO_API_KEY", "AZURE_SPEECH_KEY", "AZURE_SPEECH_ENDPOINT", "AZURE_TTS_ENDPOINT"):
        value = os.environ.get(key)
        if value:
            message = message.replace(value, "<redacted>")
    message = re.sub(r"https?://[^\s]+", "<provider-url>", message)
    return message[:400]


def _ticks(value: Any) -> int:
    if hasattr(value, "total_seconds"):
        return round(float(value.total_seconds()) * TICKS_PER_SECOND)
    return int(value or 0)


def _normal_boundary_type(value: Any) -> str:
    return str(value).rsplit(".", 1)[-1].lower()


def _escape_with_offsets(text: str) -> tuple[str, list[int]]:
    import html

    pieces: list[str] = []
    offsets: list[int] = []
    for index, character in enumerate(text):
        escaped = html.escape(character)
        pieces.append(escaped)
        offsets.extend([index] * len(escaped))
    offsets.append(len(text))
    return "".join(pieces), offsets


def _build_plain_ssml(locale: str, voice: str, text: str, *, temperature: float = DEFAULT_AZURE_TTS_TEMPERATURE) -> tuple[str, int, list[int]]:
    """Build DragonHD-compatible SSML and map callback offsets to source text."""

    escaped, offsets = _escape_with_offsets(text)
    import html

    # DragonHD does not support prosody or express-as. Temperature is passed
    # only as the documented structured-voice parameter. The template default
    # is a measured 0.7 compromise between expressive variation and stability.
    parameter = f' parameters="temperature={temperature:g}"' if ":DragonHD" in voice else ""
    opening = (
        f'<speak version="1.0" xml:lang="{html.escape(locale, quote=True)}" '
        f'xmlns="http://www.w3.org/2001/10/synthesis"><voice name="{html.escape(voice, quote=True)}"{parameter}>'
    )
    return f"{opening}{escaped}</voice></speak>", len(opening), offsets


def _source_span(raw_offset: int, raw_length: int, content_start: int, offsets: list[int], text_length: int) -> tuple[int, int]:
    start = raw_offset - content_start
    end = start + raw_length
    if start < 0 or end < start or start >= len(offsets) or end >= len(offsets):
        raise SpeechProviderError("Azure WordBoundary text span is outside the approved spoken text")
    source_start = offsets[start]
    source_end = offsets[end]
    if source_end <= source_start or source_end > text_length:
        raise SpeechProviderError("Azure WordBoundary could not be mapped to the approved spoken text")
    return source_start, source_end


def validate_word_boundaries(text: str, boundaries: Any) -> list[dict[str, Any]]:
    if not isinstance(boundaries, list):
        raise SpeechProviderError("Azure did not return WordBoundary events")
    result: list[dict[str, Any]] = []
    previous = -1
    for boundary in boundaries:
        if not isinstance(boundary, Mapping) or _normal_boundary_type(boundary.get("boundary_type", "word")) != "word":
            continue
        offset = int(boundary.get("audio_offset_ticks", 0))
        duration = int(boundary.get("duration_ticks", 0))
        text_offset = int(boundary.get("text_offset", 0))
        word_length = int(boundary.get("word_length", 0))
        # Azure may emit punctuation/placeholder callbacks with zero duration;
        # they are not usable caption anchors and must not enter the ledger.
        if duration <= 0:
            continue
        if offset < previous or text_offset < 0 or word_length <= 0:
            raise SpeechProviderError("Azure WordBoundary offsets are invalid or non-monotonic")
        end = text_offset + word_length
        if end > len(text):
            raise SpeechProviderError("Azure WordBoundary exceeds the approved spoken text")
        value = text[text_offset:end]
        if not value.strip():
            continue
        result.append({
            "boundary_type": "word",
            "audio_offset_ticks": offset,
            "duration_ticks": duration,
            "text_offset": text_offset,
            "word_length": word_length,
            "text": value,
            "start_seconds": offset / TICKS_PER_SECOND,
            "end_seconds": (offset + duration) / TICKS_PER_SECOND,
        })
        previous = offset
    if not result:
        raise SpeechProviderError("Azure synthesis returned no usable WordBoundary events")
    return result


class AzureWordBoundaryProvider:
    """Native Azure Speech SDK adapter with canonical WordBoundary timing."""

    provider = "azure"
    alignment_provider = "azure-word-boundary"

    def __init__(self, *, region: str = DEFAULT_REGION, voice: str = DEFAULT_VOICE, locale: str = DEFAULT_LOCALE, temperature: float = DEFAULT_AZURE_TTS_TEMPERATURE):
        self.region = region
        self.voice = voice
        self.locale = locale
        self.temperature = float(temperature)
        try:
            import azure.cognitiveservices.speech as speechsdk
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise SpeechProviderError("Azure Speech SDK is not installed; install azure-cognitiveservices-speech") from exc
        self.sdk = speechsdk
        key = os.environ.get("AZURE_SPEECH_KEY")
        if not key:
            raise SpeechProviderError("AZURE_SPEECH_KEY is not configured")
        self.key = key

    @classmethod
    def available(cls) -> bool:
        try:
            import azure.cognitiveservices.speech  # noqa: F401
        except Exception:
            return False
        return bool(os.environ.get("AZURE_SPEECH_KEY"))

    def synthesize(self, text: str, output_path: Path) -> dict[str, Any]:
        if not text.strip():
            raise SpeechProviderError("cannot synthesize empty spoken text")
        sdk = self.sdk
        config = sdk.SpeechConfig(subscription=self.key, region=self.region)
        config.speech_synthesis_voice_name = self.voice
        property_id = getattr(sdk.PropertyId, "SpeechServiceResponse_RequestWordBoundary", None)
        if property_id is not None:
            config.set_property(property_id, "true")
        output_format = getattr(sdk.SpeechSynthesisOutputFormat, "Riff48Khz16BitMonoPcm", None)
        if output_format is None:
            output_format = getattr(sdk.SpeechSynthesisOutputFormat, "Riff24Khz16BitMonoPcm")
        config.set_speech_synthesis_output_format(output_format)
        ssml, content_start, offsets = _build_plain_ssml(self.locale, self.voice, text, temperature=self.temperature)
        synthesizer = sdk.SpeechSynthesizer(speech_config=config, audio_config=None)
        boundaries: list[dict[str, Any]] = []

        def received(event: Any) -> None:
            if _normal_boundary_type(getattr(event, "boundary_type", "")) != "word":
                return
            duration_ticks = _ticks(event.duration)
            if duration_ticks <= 0:
                return
            start, end = _source_span(int(event.text_offset), int(event.word_length), content_start, offsets, len(text))
            boundaries.append({
                "boundary_type": "word",
                "audio_offset_ticks": _ticks(event.audio_offset),
                "duration_ticks": duration_ticks,
                "text_offset": start,
                "word_length": end - start,
            })

        synthesizer.synthesis_word_boundary.connect(received)
        # The SDK's Future has no portable timeout parameter. Keep the
        # provider call bounded so one stalled region can downgrade this
        # segment to the labelled REST compatibility path instead of hanging
        # the entire unattended edition. The worker is daemonized because the
        # native SDK may finish its own socket cleanup later.
        holder: dict[str, Any] = {}

        def synthesize_worker() -> None:
            try:
                holder["result"] = synthesizer.speak_ssml_async(ssml).get()
            except Exception as exc:  # pragma: no cover - SDK/runtime dependent
                holder["error"] = exc

        worker = threading.Thread(target=synthesize_worker, name="azure-word-boundary", daemon=True)
        worker.start()
        timeout = max(5.0, float(os.environ.get("AI_MORNING_BRIEF_AZURE_NATIVE_TIMEOUT_SECONDS", "25")))
        worker.join(timeout=timeout)
        if worker.is_alive():
            raise SpeechProviderError(f"Azure synthesis timed out after {timeout:.0f}s")
        if "error" in holder:
            raise SpeechProviderError(f"Azure synthesis failed: {holder['error']}")
        result = holder.get("result")
        if result is None:
            raise SpeechProviderError("Azure synthesis returned no result")
        if result.reason != sdk.ResultReason.SynthesizingAudioCompleted:
            details = getattr(result, "error_details", "")
            raise SpeechProviderError(f"Azure synthesis failed{': ' + str(details) if details else ''}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(bytes(result.audio_data))
        validated = validate_word_boundaries(text, boundaries)
        return {"audio_path": str(output_path), "word_boundaries": validated, "alignment_provider": self.alignment_provider, "voice": self.voice, "temperature": self.temperature}


def _convert_audio_bytes_to_wav(data: bytes, mime_type: str, output_path: Path) -> None:
    if not data:
        raise SpeechProviderError("Gemini returned empty audio data")
    suffix = ".raw"
    if "wav" in mime_type.lower():
        suffix = ".wav"
    elif "l16" in mime_type.lower() or "pcm" in mime_type.lower():
        suffix = ".pcm"
    temporary = output_path.with_suffix(suffix)
    temporary.write_bytes(data)
    if suffix == ".wav":
        output_path.write_bytes(data)
        if temporary != output_path:
            temporary.unlink(missing_ok=True)
        return
    # Gemini's PCM payloads are normally 24 kHz, 16-bit, mono. Respect an
    # explicit sample-rate parameter when the API supplies one, then give the
    # rest of the pipeline a stable 48 kHz WAV.
    match = re.search(r"(?:rate|sample[-_]?rate)\s*=\s*(\d+)", mime_type, flags=re.IGNORECASE)
    input_rate = int(match.group(1)) if match else 24000
    result = subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", str(input_rate), "-ac", "1", "-i", str(temporary),
        "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(output_path),
    ], capture_output=True, text=True, timeout=120, check=False)
    temporary.unlink(missing_ok=True)
    if result.returncode != 0 or not output_path.is_file():
        raise SpeechProviderError(f"could not convert Gemini audio: {(result.stderr or '')[-300:]}")


class GeminiTTSProvider:
    """Gemini TTS adapter for blind comparison and explicit opt-in runs."""

    provider = "gemini"
    alignment_provider = "gemini-proportional"

    def __init__(self, *, model: str = GEMINI_DEFAULT_MODEL, voice: str = GEMINI_DEFAULT_VOICE):
        self.model = model
        self.voice = voice
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_STUDIO_API_KEY")
        if not key:
            raise SpeechProviderError("GOOGLE_AI_STUDIO_API_KEY is not configured")
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise SpeechProviderError("google-genai is not installed; install google-genai") from exc
        self._genai = genai
        self._types = types
        self.client = genai.Client(api_key=key)

    def _request_audio(self, text: str) -> tuple[bytes, str]:
        """Request audio, preferring the current Interactions API."""

        # Google is moving TTS examples from the legacy
        # ``models.generate_content`` surface to ``interactions.create``.
        # Prefer the documented Interactions response (``output_audio``), but
        # retain a narrow legacy adapter for older google-genai wheels.
        prompt = f"Read the following Mandarin news sentence clearly and exactly; do not add or omit words:\n{text}"
        response: Any
        interactions = getattr(self.client, "interactions", None)
        if interactions is not None and hasattr(interactions, "create"):
            response = interactions.create(
                model=self.model,
                input=prompt,
                response_format={"type": "audio"},
                generation_config={"speech_config": [{"voice": self.voice}]},
            )
            output_audio = getattr(response, "output_audio", None)
            raw = getattr(output_audio, "data", None) if output_audio is not None else None
            mime = str(getattr(output_audio, "mime_type", "audio/L16;rate=24000") or "audio/L16;rate=24000") if output_audio is not None else "audio/L16;rate=24000"
        else:
            # Legacy SDKs expose audio as an inline_data part on a generate_content
            # response. Keep this fallback only for benchmark compatibility.
            types = self._types
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice)
                        )
                    ),
                ),
            )
            part = None
            for candidate in getattr(response, "candidates", []) or []:
                content = getattr(candidate, "content", None)
                for item in getattr(content, "parts", []) or []:
                    if getattr(item, "inline_data", None) is not None:
                        part = item.inline_data
                        break
                if part is not None:
                    break
            raw = getattr(part, "data", None) if part is not None else None
            mime = str(getattr(part, "mime_type", "audio/L16;rate=24000") or "audio/L16;rate=24000") if part is not None else "audio/L16;rate=24000"
        if raw is None:
            raise SpeechProviderError("Gemini response did not contain inline audio")
        if isinstance(raw, str):
            raw = base64.b64decode(raw)
        return bytes(raw), mime

    def synthesize(self, text: str, output_path: Path) -> dict[str, Any]:
        """Synthesize one sentence with a bounded provider call."""

        if not text.strip():
            raise SpeechProviderError("cannot synthesize empty spoken text")
        holder: dict[str, Any] = {}

        def worker() -> None:
            try:
                holder["result"] = self._request_audio(text)
            except Exception as exc:  # pragma: no cover - provider/runtime dependent
                holder["error"] = exc

        thread = threading.Thread(target=worker, name="gemini-tts", daemon=True)
        thread.start()
        timeout = max(10.0, float(os.environ.get("AI_MORNING_BRIEF_GEMINI_TIMEOUT_SECONDS", "90")))
        thread.join(timeout=timeout)
        if thread.is_alive():
            raise SpeechProviderError(f"Gemini synthesis timed out after {timeout:.0f}s")
        if "error" in holder:
            raise SpeechProviderError(f"Gemini synthesis failed: {_safe_provider_error(holder['error'])}")
        raw, mime = holder.get("result") or (None, None)
        if raw is None:
            raise SpeechProviderError("Gemini response did not contain inline audio")
        _convert_audio_bytes_to_wav(bytes(raw), str(mime or "audio/L16;rate=24000"), output_path)
        return {
            "audio_path": str(output_path),
            "provider": self.provider,
            "model": self.model,
            "voice": self.voice,
            "mime_type": str(mime or ""),
            "alignment_provider": self.alignment_provider,
            "duration_seconds": round(media_duration(output_path), 3),
        }


def proportional_word_timestamps(text: str, duration: float) -> list[dict[str, Any]]:
    """Create an explicitly approximate, text-complete timing ledger.

    Gemini TTS returns audio but no stable WordBoundary contract. These
    timings are only an auditable alignment artifact; the caption writer uses
    its deterministic proportional fallback and never treats this as
    provider-native timing.
    """

    duration = max(0.05, float(duration))
    matches = list(re.finditer(r"\S+", text))
    if not matches:
        return []
    weights = [max(1, sum(not char.isspace() for char in match.group(0))) for match in matches]
    total = max(1, sum(weights))
    cursor = 0.0
    result: list[dict[str, Any]] = []
    for match, weight in zip(matches, weights):
        start = duration * cursor / total
        cursor += weight
        end = duration * cursor / total
        result.append({
            "word": match.group(0),
            "start": round(start, 6),
            "end": round(max(start + 0.04, end), 6),
            "text_offset": match.start(),
            "word_length": len(match.group(0)),
        })
    result[-1]["end"] = round(duration, 6)
    return result


def benchmark_phrases(script: Mapping[str, Any], *, count: int = BENCHMARK_PHRASE_COUNT) -> list[str]:
    candidates: list[str] = []
    for segment in script.get("segments", []):
        if not isinstance(segment, Mapping):
            continue
        if segment.get("kind") != "news":
            continue
        text = str(segment.get("spoken_text") or segment.get("display_text") or segment.get("broadcast_text") or "").strip()
        if text and text not in candidates:
            candidates.append(text)
    for phrase in BENCHMARK_REGRESSION_PHRASES:
        if phrase not in candidates:
            candidates.append(phrase)
    if not candidates:
        candidates.extend(BENCHMARK_REGRESSION_PHRASES)
    index = 0
    while len(candidates) < count:
        candidates.append(BENCHMARK_REGRESSION_PHRASES[index % len(BENCHMARK_REGRESSION_PHRASES)])
        index += 1
    return candidates[:count]


def _blind_labels(case_id: str, seed: str) -> tuple[str, str]:
    randomizer = random.Random(f"{seed}:{case_id}")
    labels = ["A", "B"]
    randomizer.shuffle(labels)
    return labels[0], labels[1]


def run_tts_benchmark(project_dir: Path, script: Mapping[str, Any], *, env_path: Path | None = None, live: bool = True) -> dict[str, Any]:
    """Run or prepare the 20-phrase blind Azure/Gemini shadow test."""

    benchmark_dir = project_dir / "artifacts" / "tts-benchmark"
    benchmark_dir.mkdir(parents=True, exist_ok=True)
    phrases = benchmark_phrases(script)
    date_seed = str(script.get("date") or project_dir.name)
    cases: list[dict[str, Any]] = []
    provider_map: dict[str, dict[str, str]] = {}
    for index, phrase in enumerate(phrases, 1):
        case_id = f"case-{index:02d}"
        a, b = _blind_labels(case_id, date_seed)
        provider_map[case_id] = {a: "azure", b: "gemini"}
        cases.append({"case_id": case_id, "text": phrase, "labels": [a, b], "status": "pending"})
    if len(cases) < BENCHMARK_PHRASE_COUNT:
        for index in range(len(cases) + 1, BENCHMARK_PHRASE_COUNT + 1):
            phrase = BENCHMARK_REGRESSION_PHRASES[(index - 1) % len(BENCHMARK_REGRESSION_PHRASES)]
            case_id = f"case-{index:02d}"
            a, b = _blind_labels(case_id, date_seed)
            provider_map[case_id] = {a: "azure", b: "gemini"}
            cases.append({"case_id": case_id, "text": phrase, "labels": [a, b], "status": "pending"})

    report: dict[str, Any] = {
        "version": "1.0",
        "status": "pending",
        "model": os.environ.get("GEMINI_TTS_MODEL", GEMINI_DEFAULT_MODEL),
        "voice": os.environ.get("GEMINI_TTS_VOICE", GEMINI_DEFAULT_VOICE),
        "case_count": len(cases),
        "cases": cases,
        "scoring_gate": {
            "critical_numeric_errors_max": BENCHMARK_NUMERIC_ERROR_MAX,
            "term_accuracy_min": BENCHMARK_TERM_ACCURACY_MIN,
            "naturalness_delta_min": BENCHMARK_NATURALNESS_DELTA_MIN,
            "approval_required_for_production_fallback": True,
        },
        "no_secrets_in_artifacts": True,
    }
    if not live:
        report["status"] = "prepared"
        write_json(benchmark_dir / "benchmark.json", report)
        write_json(benchmark_dir / "provider_map.private.json", {"sensitive": True, "mapping": provider_map})
        return report
    if not os.environ.get("GEMINI_API_KEY"):
        report["status"] = "awaiting_gemini_credentials"
        report["blocker"] = "GEMINI_API_KEY is not configured; Azure production is unchanged"
        write_json(benchmark_dir / "benchmark.json", report)
        write_json(benchmark_dir / "provider_map.private.json", {"sensitive": True, "mapping": provider_map})
        return report
    try:
        gemini = GeminiTTSProvider(model=report["model"], voice=report["voice"])
    except Exception as exc:  # benchmark availability must never block Azure
        report["status"] = "unavailable"
        report["blocker"] = _safe_provider_error(exc)
        write_json(benchmark_dir / "benchmark.json", report)
        write_json(benchmark_dir / "provider_map.private.json", {"sensitive": True, "mapping": provider_map})
        return report
    for case in cases:
        case_id = case["case_id"]
        gemini_path = benchmark_dir / f"{case_id}-gemini.wav"
        try:
            result = gemini.synthesize(case["text"], gemini_path)
            case["gemini"] = {"audio_path": f"artifacts/tts-benchmark/{gemini_path.name}", "duration_seconds": result.get("duration_seconds")}
            case["status"] = "ready_for_blind_scoring"
        except Exception as exc:  # benchmark failures are diagnostics, never a production fallback
            case["status"] = "failed"
            case["error"] = _safe_provider_error(exc)
    report["status"] = "ready_for_blind_scoring" if all(case["status"] == "ready_for_blind_scoring" for case in cases) else "partial"
    write_json(benchmark_dir / "benchmark.json", report)
    write_json(benchmark_dir / "provider_map.private.json", {"sensitive": True, "mapping": provider_map})
    return report


def evaluate_tts_scores(scores: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Evaluate human blind scores without deciding provider routing."""

    numeric_errors = sum(int(score.get("critical_numeric_errors", 0) or 0) for score in scores)
    term_total = sum(float(score.get("term_accuracy", 0.0) or 0.0) for score in scores)
    term_accuracy = term_total / len(scores) if scores else 0.0
    naturalness = sum(float(score.get("naturalness_delta", 0.0) or 0.0) for score in scores) / len(scores) if scores else -1.0
    passed = bool(scores) and numeric_errors <= BENCHMARK_NUMERIC_ERROR_MAX and term_accuracy >= BENCHMARK_TERM_ACCURACY_MIN and naturalness >= BENCHMARK_NATURALNESS_DELTA_MIN
    return {
        "status": "pass" if passed else "fail",
        "case_count": len(scores),
        "critical_numeric_errors": numeric_errors,
        "term_accuracy": round(term_accuracy, 4),
        "naturalness_delta": round(naturalness, 4),
        "production_fallback_approved": False,
        "approval_required": True,
    }
