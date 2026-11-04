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
import re
import ssl
import subprocess
import time
from array import array
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from . import config as C

SAMPLE_RATE = 48000
DOUBAO_PROMPT = "用参考音频的音色、语速和朗读风格，清晰、有节奏地朗读以下文字，语气自然有精神，不增删字词，无背景音无杂音：{spoken_text}"


class VoiceError(RuntimeError):
    pass


def unit_audio_path(run_dir: Path, unit_id: str) -> Path:
    return run_dir / "assets" / "audio" / "units" / f"{unit_id}.wav"


def raw_audio_path(run_dir: Path, unit_id: str, suffix: str = ".mp3") -> Path:
    return run_dir / "assets" / "audio" / "raw" / f"{unit_id}{suffix}"


def chunk_raw_path(run_dir: Path, chunk_id: str, suffix: str = ".wav") -> Path:
    return run_dir / "assets" / "audio" / "raw" / f"{chunk_id}{suffix}"


def chunked_synthesis(provider: str, mode: str | None) -> bool:
    """Only Doubao text-to-audio uses chunked synthesis; voice-clone and edge stay per-unit."""

    return bool(C.DOUBAO_CHUNKED) and provider == "doubao" and mode == "text-to-audio"


def plan_chunks(units: list[Mapping[str, Any]], max_chars: int, block_of: Mapping[str, str] | None = None) -> list[list[Mapping[str, Any]]]:
    """Group ordered units into long chunks.

    Units are first grouped by their scene block (each headline story is its own block; the hook merges
    into the first story; all flashes and the outro share one block). A block that exceeds the char budget
    is then hard-split. This keeps the three headline stories separate and the short flash run continuous.
    """

    block_of = block_of or {}
    chunks: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    current_chars = 0
    for unit in units:
        size = len(str(unit["display_text"]))
        new_block = bool(current and block_of.get(unit["scene_id"]) != block_of.get(current[-1]["scene_id"]))
        if current and (new_block or current_chars + size > max_chars):
            chunks.append(current)
            current, current_chars = [], 0
        current.append(unit)
        current_chars += size
    if current:
        chunks.append(current)
    return chunks


def spoken_for(unit: Mapping[str, Any], provider: str) -> str:
    """Text sent to the TTS: Doubao reads the raw sentence; edge gets the normalised spoken text."""

    return str(unit["display_text"] if provider == "doubao" else unit["spoken_text"])


def doubao_mode(mode: str | None = None) -> str:
    mode = mode or C.DOUBAO_TTS_MODE
    if mode not in C.DOUBAO_TTS_MODES:
        raise VoiceError(f"unknown Doubao TTS mode {mode!r}; choose one of {C.DOUBAO_TTS_MODES}")
    return mode


def _doubao_block(mode: str, reference: Path, chunked: bool = False) -> dict[str, Any]:
    if mode == "voice-clone":
        return {
            "mode": mode, "tool": "audio_to_audio_plus", "reference_audio": str(reference), "prompt_template": DOUBAO_PROMPT,
            "tempo": C.DOUBAO_TEMPO[mode],
            "instructions": "逐条处理 units：用 audio_to_audio_plus，@音频1 = reference_audio，prompt = prompt_template（把 {spoken_text} 换成该条 spoken_text），保存到该条 output_path。每条只读一句，不要合并。",
        }
    base = {
        "mode": mode, "tool": "text_to_audio_plus", "voice": C.DOUBAO_TTS_VOICE_PROFILE, "request_template": C.DOUBAO_TTS_REQUEST,
        "tempo": C.DOUBAO_TEMPO[mode], "chunked": chunked,
    }
    if chunked:
        base["instructions"] = (
            "逐组处理 chunks：对每个 chunk 调用一次 text_to_audio_plus，请求 = request_template（{voice} 换成 voice 字段的固定音色描述，逐字使用；"
            "{spoken_text} 换成该 chunk 的 text 全文，即其中所有句子原样拼接），保存到该 chunk 的 output_path。英文和阿拉伯数字照原样自然读出，"
            "不拆单字母、不念成中文数字、不增删字词；不要在请求里要求语速，也不要拆成每句调用。")
    else:
        base["instructions"] = (
            "逐条处理 units：调用 text_to_audio_plus，请求 = request_template（{voice} 换成 voice 字段的固定音色描述，逐字使用；"
            "{spoken_text} 换成该条 spoken_text 原文），保存到该条 output_path。英文和阿拉伯数字照原样读，不拆单字母、不念成中文数字、"
            "不增删字词。每条只读一句，不要合并。")
    return base


def build_manifest(run_dir: Path, final_plan: Mapping[str, Any], provider: str, *, mode: str | None = None) -> dict[str, Any]:
    if provider not in C.TTS_PROVIDERS:
        raise VoiceError(f"unknown TTS provider {provider!r}; choose one of {C.TTS_PROVIDERS}")
    reference = C.REPO_ROOT / "example-audio.mp3"
    mode = doubao_mode(mode) if provider == "doubao" else None
    chunked = chunked_synthesis(provider, mode)
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
    chunks_meta: list[dict[str, Any]] | None = None
    if chunked:
        story_scenes = [s["scene_id"] for s in final_plan.get("scenes") or [] if s.get("kind") == "story"]
        first_story = story_scenes[0] if story_scenes else None
        block_of: dict[str, str] = {}
        for scene in final_plan.get("scenes") or []:
            kind, sid = scene.get("kind"), scene["scene_id"]
            if kind == "story":
                block_of[sid] = sid
            elif kind == "hook":
                block_of[sid] = first_story or sid
            else:  # flash and outro share one block
                block_of[sid] = "__flash__"
        chunks_meta = []
        for index, group in enumerate(plan_chunks(units, C.DOUBAO_CHUNK_MAX_CHARS, block_of), 1):
            chunk_id = f"c{index:02d}"
            chunks_meta.append({
                "chunk_id": chunk_id,
                "unit_ids": [u["unit_id"] for u in group],
                "text": "".join(str(u["spoken_text"]) for u in group),
                "output_path": str(chunk_raw_path(run_dir, chunk_id)),
            })
        _drop_stale_chunks(run_dir, path, units, chunks_meta)
    else:
        _drop_stale_audio(run_dir, path, units)
    manifest = {
        "version": C.V6_VERSION,
        "provider": provider,
        "chunked": chunked,
        "units": units,
        "chunks": chunks_meta,
        "doubao": _doubao_block(mode, reference, chunked) if provider == "doubao" else None,
        "edge": {"voice": C.EDGE_TTS_VOICE, "rate": C.EDGE_TTS_RATE} if provider == "edge" else None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _drop_stale_chunks(run_dir: Path, manifest_path: Path, units: list[dict[str, Any]],
                       chunks: list[dict[str, Any]]) -> None:
    """Delete chunk audio whose content changed, and clean up raw chunks that no longer exist."""

    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = {}
    old_text = {c.get("chunk_id"): c.get("text") for c in previous.get("chunks") or []}
    keep_paths = {c["output_path"] for c in chunks}
    for chunk in chunks:
        out = Path(chunk["output_path"])
        if out.is_file() and old_text.get(chunk["chunk_id"]) != chunk["text"]:
            out.unlink(missing_ok=True)
    for raw in (run_dir / "assets" / "audio" / "raw").glob("c*.wav"):
        if str(raw) not in keep_paths:
            raw.unlink(missing_ok=True)
    # per-unit alignments are regenerated from chunks, so stale native alignment files are invalid
    for unit in units:
        (run_dir / "artifacts" / "alignments" / f"{unit['unit_id']}.raw.json").unlink(missing_ok=True)


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


def missing_chunks(manifest: Mapping[str, Any]) -> list[str]:
    return [c["chunk_id"] for c in manifest.get("chunks") or []
            if not Path(c["output_path"]).is_file() or Path(c["output_path"]).stat().st_size == 0]


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
    import edge_tts
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


def watermark_cut(samples: array, sr: int, *, rms_thr: float = 300.0, frame_ms: int = 10,
                  min_gap_s: float = 0.10, gap_ratio: float = 1.5, max_search_s: float = 4.0) -> int | None:
    """Sample index where a fixed leading voice tag (e.g. "由豆包 AI 生成") ends, or None.

    The tag is a fixed recording whose internal pauses are short. The silence right after the tag is
    noticeably longer, so the boundary is the first gap that is both at least ``min_gap_s`` and
    ``gap_ratio`` times the longest gap seen inside the tag. This stays correct whether the post-tag
    padding is 0.13s or 0.3s. Fail-safe: no qualifying gap or an implausibly late cut returns None.
    """

    window = int(frame_ms / 1000 * sr)
    floor = int(min_gap_s / (frame_ms / 1000))
    frames = len(samples) // window

    def frame_rms(i: int) -> float:
        block = samples[i * window:(i + 1) * window]
        return (sum(x * x for x in block) / len(block)) ** 0.5

    onset = next((i for i in range(frames) if frame_rms(i) >= rms_thr), None)
    if onset is None:
        return None
    run = 0
    internal_max = 0
    for i in range(onset, frames):
        if frame_rms(i) < rms_thr:
            run += 1
            continue
        if run:
            if run >= floor and run >= gap_ratio * internal_max:
                cut_frame = i - run
                if cut_frame * frame_ms / 1000 > max_search_s:
                    return None
                return cut_frame * window
            internal_max = max(internal_max, run)
            run = 0
    if run and run >= floor and run >= gap_ratio * internal_max:  # tag ends at end of file
        cut_frame = frames - run
        if cut_frame * frame_ms / 1000 <= max_search_s:
            return cut_frame * window
    return None


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


# ---------------------------------------------------------------------------
# chunked synthesis: word tokenizer + DP boundary alignment
# ---------------------------------------------------------------------------
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[.\-#/+%][A-Za-z0-9]+)*|[\u4e00-\u9fff]")


def _word_tokens(text: str) -> list[tuple[str, int]]:
    """Speakable tokens as (token, weight): one English/number run or one CJK character each."""

    tokens = []
    for match in _WORD_RE.finditer(text):
        token = match.group(0)
        weight = len(token) if re.search(r"[A-Za-z0-9]", token) else 1
        tokens.append((token, weight))
    return tokens


def word_alignment(text: str, duration: float) -> list[dict[str, Any]]:
    """Word-level timings inside one unit, distributed by token weight (no native timestamps available)."""

    tokens = _word_tokens(text)
    total = sum(weight for _, weight in tokens)
    if not tokens or total == 0:
        return []
    words = []
    cursor = 0
    for token, weight in tokens:
        start = cursor / total * duration
        cursor += weight
        words.append({"text": token, "start": round(start, 4), "end": round(cursor / total * duration, 4)})
    return words


def _gap_midpoints(samples: array, sr: int, *, rms_thr: float = 300.0, frame_ms: int = 10,
                   min_gap_s: float) -> list[float]:
    """Times (seconds) of the midpoint of every silence run at least min_gap_s long."""

    window = int(frame_ms / 1000 * sr)
    need = int(min_gap_s / (frame_ms / 1000))
    frames = len(samples) // window
    mids: list[float] = []
    run = 0
    for i in range(frames + 1):
        silent = False
        if i < frames:
            block = samples[i * window:(i + 1) * window]
            silent = (sum(x * x for x in block) / len(block)) ** 0.5 < rms_thr
        if silent:
            run += 1
            continue
        if run >= need:
            mid = (i - run / 2) * frame_ms / 1000
            mids.append(mid)
        run = 0
    return mids


def _dp_boundaries(weights: list[int], duration: float, gaps: list[float], min_unit_s: float) -> list[float] | None:
    """Place N-1 unit boundaries at real pauses via DP; proportional char timing is the prior.

    Returns ordered boundary times, or None if no valid ordered assignment exists (caller falls back to
    pure proportional boundaries).
    """

    n_units = len(weights)
    if n_units < 2:
        return []
    cum = np.cumsum(weights)
    total = float(cum[-1])
    expected = [float(cum[k] / total) * duration for k in range(n_units - 1)]  # boundary after unit k
    k_count = n_units - 1
    m_count = len(gaps)
    if m_count < k_count:
        return None
    inf = float("inf")
    dp = np.full((k_count, m_count), inf)
    prev = np.full((k_count, m_count), -1, dtype=int)
    for m in range(m_count):
        if gaps[m] >= min_unit_s and duration - gaps[m] >= min_unit_s:
            dp[0][m] = (gaps[m] - expected[0]) ** 2
    for k in range(1, k_count):
        for m in range(m_count):
            best, best_n = inf, -1
            for n in range(m):
                if dp[k - 1][n] < inf and gaps[m] - gaps[n] >= min_unit_s:
                    if dp[k - 1][n] < best:
                        best, best_n = dp[k - 1][n], n
            if best_n >= 0 and duration - gaps[m] >= min_unit_s:
                dp[k][m] = best + (gaps[m] - expected[k]) ** 2
                prev[k][m] = best_n
    last = int(np.argmin(dp[k_count - 1]))
    if dp[k_count - 1][last] == inf:
        return None
    chosen: list[float] = []
    k, m = k_count - 1, last
    while k >= 0:
        chosen.append(gaps[m])
        m = prev[k][m]
        k -= 1
    chosen.reverse()
    return chosen


def _proportional_boundaries(weights: list[int], duration: float) -> list[float]:
    cum = np.cumsum(weights)
    total = float(cum[-1])
    return [float(cum[k] / total) * duration for k in range(len(weights) - 1)]


def finalize_chunked(run_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Turn each synthesized chunk back into per-unit WAVs and per-unit word alignments.

    Per chunk: strip the leading watermark, trim outer silence, place unit boundaries at real pauses
    (DP, proportional prior; pure proportional fallback), slice the audio, and write word-level timings.
    The output voice_manifest keeps the same per-unit shape the timeline and mix already consume.
    """

    missing = missing_chunks(manifest)
    if missing:
        raise VoiceError("missing narration chunks: " + ", ".join(missing))
    units_by_id = {u["unit_id"]: u for u in manifest.get("units") or []}
    entries: list[dict[str, Any]] = []
    stripped_count = 0
    snapped_chunks = 0
    for chunk in manifest.get("chunks") or []:
        chunk_id = chunk["chunk_id"]
        unit_ids = chunk["unit_ids"]
        samples = decode_mono(Path(chunk["output_path"]))
        wm = watermark_cut(samples, SAMPLE_RATE)
        if wm:
            samples = samples[wm:]
            stripped_count += 1
        lead, tail = _voiced_bounds(samples)
        body = samples[lead:tail]
        body_duration = len(body) / SAMPLE_RATE
        weights = [sum(w for _, w in _word_tokens(units_by_id[uid]["display_text"])) or 1 for uid in unit_ids]
        gaps = _gap_midpoints(body, SAMPLE_RATE, min_gap_s=C.DOUBAO_CHUNK_MIN_GAP_S)
        boundaries = _dp_boundaries(weights, body_duration, gaps, C.DOUBAO_CHUNK_MIN_UNIT_S)
        if boundaries is None:
            boundaries = _proportional_boundaries(weights, body_duration)
        else:
            snapped_chunks += 1
        cuts = [0] + [min(len(body), max(0, int(round(t * SAMPLE_RATE)))) for t in boundaries] + [len(body)]
        for index, uid in enumerate(unit_ids):
            seg = body[cuts[index]:cuts[index + 1]]
            if len(seg) < int(0.1 * SAMPLE_RATE):
                raise VoiceError(f"unit {uid} audio is too short after chunk alignment")
            out = unit_audio_path(run_dir, uid)
            write_wav(out, seg)
            duration = wav_seconds(out)
            words = word_alignment(units_by_id[uid]["display_text"], duration)
            align_path = run_dir / "artifacts" / "alignments" / f"{uid}.json"
            align_path.parent.mkdir(parents=True, exist_ok=True)
            align_path.write_text(json.dumps({"unit_id": uid, "alignment": "word-level", "duration": duration,
                                              "words": words}, ensure_ascii=False), encoding="utf-8")
            meta = units_by_id[uid]
            entries.append({"unit_id": uid, "scene_id": meta["scene_id"], "audio_path": str(out.relative_to(run_dir)),
                            "duration": round(duration, 4), "alignment": "word-level",
                            "trimmed_lead_seconds": round(lead / SAMPLE_RATE, 4), "watermark_stripped": bool(wm),
                            "spoken_text": meta["spoken_text"], "display_text": meta["display_text"]})
    entries.sort(key=lambda e: e["unit_id"])
    result = {"version": C.V6_VERSION, "provider": manifest.get("provider"),
              "doubao_mode": (manifest.get("doubao") or {}).get("mode"), "tempo": 1.0,
              "chunked": True, "chunks": len(manifest.get("chunks") or []), "boundaries_snapped_chunks": snapped_chunks,
              "watermark_stripped_units": stripped_count, "units": entries,
              "total_speech_seconds": round(sum(e["duration"] for e in entries), 3)}
    (run_dir / "artifacts" / "voice_manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def finalize(run_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize, trim and align every unit; write artifacts/voice_manifest.json."""

    if manifest.get("chunked"):
        return finalize_chunked(run_dir, manifest)
    missing = missing_units(manifest)
    if missing:
        raise VoiceError("missing narration audio for units: " + ", ".join(missing))
    tempo = float((manifest.get("doubao") or {}).get("tempo") or 1.0) if manifest.get("provider") == "doubao" else 1.0
    entries = []
    stripped_count = 0
    for unit in manifest.get("units") or []:
        samples = decode_mono(Path(unit["output_path"]))
        wm_cut = watermark_cut(samples, SAMPLE_RATE)
        if wm_cut:
            samples = samples[wm_cut:]
            stripped_count += 1
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
                        "watermark_stripped": bool(wm_cut),
                        "spoken_text": unit["spoken_text"], "display_text": unit["display_text"]})
    result = {"version": C.V6_VERSION, "provider": manifest.get("provider"), "doubao_mode": (manifest.get("doubao") or {}).get("mode"),
              "tempo": tempo, "watermark_stripped_units": stripped_count, "units": entries,
              "total_speech_seconds": round(sum(e["duration"] for e in entries), 3)}
    (run_dir / "artifacts" / "voice_manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
