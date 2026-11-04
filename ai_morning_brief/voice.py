"""v6 per-sentence narration audio.

Every narration unit (one sentence) is one audio file. Two providers:

* ``doubao`` — agent handoff, one call per sentence (see the voice skill):
  ``text-to-audio`` (default) uses ``text_to_audio_plus`` with the fixed voice
  profile; ``voice-clone`` uses ``audio_to_audio_plus`` with ``example-audio.mp3``.
  Doubao gets the raw sentence (it reads English and numerals itself); the
  text-to-audio voice is sped up by ``DOUBAO_TEMPO`` (pitch preserved) at
  finalize. Timing inside a sentence is proportional (sentences are short).
* ``edge``   — automatic Microsoft Edge neural TTS (no key). Provides native
  word boundaries. Also the automatic fallback when no agent is available.

``finalize`` converts every unit to 48 kHz mono WAV, trims leading/trailing
silence exactly, and writes per-unit word timings to ``alignments/``.
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import subprocess
import time
from array import array
from pathlib import Path
from typing import Any, Mapping

from . import config as C

SAMPLE_RATE = 48000
DOUBAO_PROMPT = "用参考音频的音色、语速和朗读风格，清晰、有节奏地朗读以下文字，语气自然有精神，不增删字词，无背景音无杂音：{spoken_text}"


class VoiceError(RuntimeError):
    pass


def unit_audio_path(run_dir: Path, unit_id: str) -> Path:
    return run_dir / "assets" / "audio" / "units" / f"{unit_id}.wav"


def raw_audio_path(run_dir: Path, unit_id: str, suffix: str = ".mp3") -> Path:
    return run_dir / "assets" / "audio" / "raw" / f"{unit_id}{suffix}"


def spoken_for(unit: Mapping[str, Any], provider: str) -> str:
    """Text sent to the TTS: Doubao reads the raw sentence; edge gets the normalised spoken text."""

    return str(unit["display_text"] if provider == "doubao" else unit["spoken_text"])


def doubao_mode(mode: str | None = None) -> str:
    mode = mode or C.DOUBAO_TTS_MODE
    if mode not in C.DOUBAO_TTS_MODES:
        raise VoiceError(f"unknown Doubao TTS mode {mode!r}; choose one of {C.DOUBAO_TTS_MODES}")
    return mode


def _doubao_block(mode: str, reference: Path) -> dict[str, Any]:
    if mode == "voice-clone":
        return {
            "mode": mode, "tool": "audio_to_audio_plus", "reference_audio": str(reference), "prompt_template": DOUBAO_PROMPT,
            "tempo": C.DOUBAO_TEMPO[mode],
            "instructions": "逐条处理 units：用 audio_to_audio_plus，@音频1 = reference_audio，prompt = prompt_template（把 {spoken_text} 换成该条 spoken_text），保存到该条 output_path。每条只读一句，不要合并。",
        }
    return {
        "mode": mode, "tool": "text_to_audio_plus", "voice": C.DOUBAO_TTS_VOICE_PROFILE, "request_template": C.DOUBAO_TTS_REQUEST,
        "tempo": C.DOUBAO_TEMPO[mode],
        "instructions": ("逐条处理 units：调用 text_to_audio_plus，请求 = request_template（{voice} 换成 voice 字段的固定音色描述，逐字使用；"
                         "{spoken_text} 换成该条 spoken_text 原文），保存到该条 output_path。spoken_text 里的英文和阿拉伯数字（GPU、RSA-260、552B）"
                         "照原样读，不拆单字母、不念成中文数字、不增删字词。每条只读一句，不要合并；语速由程序统一加速，不要在请求里要求语速。"),
    }


def build_manifest(run_dir: Path, final_plan: Mapping[str, Any], provider: str, *, mode: str | None = None) -> dict[str, Any]:
    if provider not in C.TTS_PROVIDERS:
        raise VoiceError(f"unknown TTS provider {provider!r}; choose one of {C.TTS_PROVIDERS}")
    reference = C.REPO_ROOT / "example-audio.mp3"
    mode = doubao_mode(mode) if provider == "doubao" else None
    units = []
    for unit in final_plan.get("units") or []:
        units.append({
            "unit_id": unit["unit_id"],
            "scene_id": unit["scene_id"],
            "spoken_text": spoken_for(unit, provider),
            "display_text": unit["display_text"],
            "output_path": str(raw_audio_path(run_dir, unit["unit_id"], ".wav" if provider == "doubao" else ".mp3")),
        })
    path = run_dir / "artifacts" / "tts_manifest.json"
    _drop_stale_audio(run_dir, path, units)
    manifest = {
        "version": C.V6_VERSION,
        "provider": provider,
        "units": units,
        "doubao": _doubao_block(mode, reference) if provider == "doubao" else None,
        "edge": {"voice": C.EDGE_TTS_VOICE, "rate": C.EDGE_TTS_RATE} if provider == "edge" else None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _drop_stale_audio(run_dir: Path, manifest_path: Path, units: list[dict[str, Any]]) -> None:
    """Delete audio whose sentence changed, so an edited plan never keeps its old voice-over.

    Output paths are keyed by unit id (u01, u02 ...); when a sentence is rewritten
    the old file would otherwise count as already synthesized.
    """

    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = {}
    old = {str(u.get("output_path")): str(u.get("spoken_text")) for u in previous.get("units") or []}
    for unit in units:
        out = Path(unit["output_path"])
        if out.is_file() and old.get(str(out)) != unit["spoken_text"]:
            out.unlink(missing_ok=True)
            (run_dir / "artifacts" / "alignments" / f"{unit['unit_id']}.raw.json").unlink(missing_ok=True)


def missing_units(manifest: Mapping[str, Any]) -> list[str]:
    return [u["unit_id"] for u in manifest.get("units") or [] if not Path(u["output_path"]).is_file() or Path(u["output_path"]).stat().st_size == 0]


# ---------------------------------------------------------------------------
# edge provider
# ---------------------------------------------------------------------------
def _edge_ssl_context() -> ssl.SSLContext | None:
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        path = os.environ.get(name)
        if path and Path(path).is_file():
            return ssl.create_default_context(cafile=path)
    return None


async def _edge_one(text: str, out_path: Path, voice: str, rate: str) -> list[dict[str, Any]]:
    import edge_tts  # optional dependency, installed by bootstrap
    import edge_tts.communicate as communicate

    context = _edge_ssl_context()
    if context is not None:
        communicate._SSL_CTX = context  # route through a proxy CA when the host requires one
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None
    comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary", proxy=proxy)
    words: list[dict[str, Any]] = []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as handle:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                handle.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append({"text": chunk["text"], "start": chunk["offset"] / 1e7, "end": (chunk["offset"] + chunk["duration"]) / 1e7})
    if out_path.stat().st_size == 0:
        raise VoiceError("edge-tts returned no audio")
    return words


def synthesize_edge(run_dir: Path, manifest: Mapping[str, Any], *, attempts: int = 3) -> list[str]:
    """Synthesize every missing unit with edge-tts. Returns unit ids that failed."""

    voice = (manifest.get("edge") or {}).get("voice") or C.EDGE_TTS_VOICE
    rate = (manifest.get("edge") or {}).get("rate") or C.EDGE_TTS_RATE
    failed: list[str] = []
    for unit in manifest.get("units") or []:
        out = Path(unit["output_path"])
        words_path = run_dir / "artifacts" / "alignments" / f"{unit['unit_id']}.raw.json"
        if out.is_file() and out.stat().st_size > 0 and words_path.is_file():
            try:
                done = json.loads(words_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                done = {}
            if done.get("text", unit["spoken_text"]) == unit["spoken_text"] and done.get("voice") == voice and done.get("rate") == rate:
                continue
        for attempt in range(1, attempts + 1):
            try:
                words = asyncio.run(_edge_one(unit["spoken_text"], out, voice, rate))
                words_path.parent.mkdir(parents=True, exist_ok=True)
                words_path.write_text(json.dumps({"provider": "edge", "voice": voice, "rate": rate, "text": unit["spoken_text"], "words": words}, ensure_ascii=False), encoding="utf-8")
                break
            except Exception as exc:  # network / service errors
                out.unlink(missing_ok=True)
                if attempt == attempts:
                    failed.append(unit["unit_id"])
                else:
                    time.sleep(2 * attempt)
    return failed


# ---------------------------------------------------------------------------
# normalize + trim + align
# ---------------------------------------------------------------------------
def decode_mono(path: Path) -> array:
    result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
                            capture_output=True, timeout=120, check=False)
    if result.returncode != 0:
        raise VoiceError(f"cannot decode {path.name}: {result.stderr.decode(errors='replace')[-300:]}")
    samples = array("h")
    samples.frombytes(result.stdout[: len(result.stdout) // 2 * 2])
    return samples


def write_wav(path: Path, samples: array, *, channels: int = 1) -> None:
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(samples.tobytes())


def _voiced_bounds(samples: array, *, threshold_db: float = -42.0, window: int = 480) -> tuple[int, int]:
    limit = 32768 * (10 ** (threshold_db / 20))
    n = len(samples)
    first, last = None, None
    for start in range(0, n, window):
        block = samples[start:start + window]
        if block and max(abs(min(block)), abs(max(block))) >= limit:
            first = start
            break
    for start in range((n // window) * window, -1, -window):
        block = samples[start:start + window]
        if block and max(abs(min(block)), abs(max(block))) >= limit:
            last = min(n, start + window)
            break
    if first is None or last is None or last <= first:
        return 0, n
    pad = int(0.04 * SAMPLE_RATE)
    return max(0, first - pad), min(n, last + pad)


def _proportional_words(text: str, duration: float) -> list[dict[str, Any]]:
    chars = [ch for ch in text if not ch.isspace()]
    if not chars:
        return []
    step = duration / len(chars)
    return [{"text": ch, "start": round(i * step, 4), "end": round((i + 1) * step, 4)} for i, ch in enumerate(chars)]


def _apply_tempo(path: Path, tempo: float) -> None:
    """Speed one sentence up in place, pitch preserved (ffmpeg atempo)."""

    tmp = path.with_suffix(".tempo.wav")
    result = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-filter:a", f"atempo={tempo:g}", "-ar", str(SAMPLE_RATE), "-ac", "1",
                             "-c:a", "pcm_s16le", str(tmp)], capture_output=True, timeout=120, check=False)
    if result.returncode != 0 or not tmp.is_file():
        raise VoiceError(f"atempo failed for {path.name}: {result.stderr.decode(errors='replace')[-200:]}")
    tmp.replace(path)


def wav_seconds(path: Path) -> float:
    return len(decode_mono(path)) / SAMPLE_RATE


def finalize(run_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize, trim and align every unit; write artifacts/voice_manifest.json."""

    missing = missing_units(manifest)
    if missing:
        raise VoiceError("missing narration audio for units: " + ", ".join(missing))
    tempo = float((manifest.get("doubao") or {}).get("tempo") or 1.0) if manifest.get("provider") == "doubao" else 1.0
    entries = []
    for unit in manifest.get("units") or []:
        samples = decode_mono(Path(unit["output_path"]))
        start, end = _voiced_bounds(samples)
        trimmed = samples[start:end]
        if len(trimmed) < int(0.2 * SAMPLE_RATE):
            raise VoiceError(f"unit {unit['unit_id']} audio is too short after trimming")
        out = unit_audio_path(run_dir, unit["unit_id"])
        write_wav(out, trimmed)
        if abs(tempo - 1.0) > 1e-3:
            _apply_tempo(out, tempo)
        duration = wav_seconds(out)
        offset = start / SAMPLE_RATE
        raw_words_path = run_dir / "artifacts" / "alignments" / f"{unit['unit_id']}.raw.json"
        if raw_words_path.is_file() and abs(tempo - 1.0) <= 1e-3:
            raw = json.loads(raw_words_path.read_text(encoding="utf-8"))
            words = [{"text": w["text"], "start": round(max(0.0, w["start"] - offset), 4), "end": round(min(duration, max(0.0, w["end"] - offset)), 4)}
                     for w in raw.get("words") or []]
            alignment = "native-word-boundary"
        else:
            words = _proportional_words(unit["spoken_text"], duration)
            alignment = "proportional"
        align_path = run_dir / "artifacts" / "alignments" / f"{unit['unit_id']}.json"
        align_path.parent.mkdir(parents=True, exist_ok=True)
        align_path.write_text(json.dumps({"unit_id": unit["unit_id"], "alignment": alignment, "duration": duration, "words": words}, ensure_ascii=False), encoding="utf-8")
        entries.append({"unit_id": unit["unit_id"], "scene_id": unit["scene_id"], "audio_path": str(out.relative_to(run_dir)),
                        "duration": round(duration, 4), "alignment": alignment, "trimmed_lead_seconds": round(offset, 4),
                        "spoken_text": unit["spoken_text"], "display_text": unit["display_text"]})
    result = {"version": C.V6_VERSION, "provider": manifest.get("provider"), "doubao_mode": (manifest.get("doubao") or {}).get("mode"),
              "tempo": tempo, "units": entries,
              "total_speech_seconds": round(sum(e["duration"] for e in entries), 3)}
    (run_dir / "artifacts" / "voice_manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
