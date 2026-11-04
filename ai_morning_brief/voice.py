"""v6 narration audio: one audio file per sentence, few TTS requests.

Every narration unit (one sentence) ends up as one audio file, so pictures,
card highlights and captions follow the voice sentence by sentence. Two providers:

* ``doubao`` — agent handoff (see the voice skill): ``text-to-audio`` (default)
  uses ``text_to_audio_plus`` with the fixed voice profile; ``voice-clone`` uses
  ``audio_to_audio_plus`` with ``example-audio.mp3``. Doubao rate-limits many
  small calls, so consecutive sentences are read in one request (a *batch*,
  whole stories where possible, <= ``DOUBAO_BATCH_MAX_CHARS``) and
  :func:`prepare_doubao` cuts each answer back into sentences at its pauses.
  A batch that cannot be cut cleanly is asked again one sentence per request
  (that batch only). Doubao gets the raw sentence (it reads English and
  numerals itself); the text-to-audio voice is sped up by ``DOUBAO_TEMPO``
  (pitch preserved) at finalize. Timing inside a sentence is proportional.
* ``edge``   — automatic Microsoft Edge neural TTS (no key), one call per
  sentence with native word boundaries. Also the automatic fallback.

``finalize`` converts every unit to 48 kHz mono WAV, trims leading/trailing
silence exactly, and writes per-unit word timings to ``alignments/``.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
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
    wait = C.DOUBAO_RATE_LIMIT_WAIT_SECONDS
    common = (f"逐条处理 todo（只有还缺的音频）：把该条 request 原样发给工具，保存到该条 output_path。"
              f"一次只发一条，不要并发；遇到“合成太频繁”等限流报错，等 {wait} 秒再重试同一条（最多 3 次）。"
              "一条 todo 是一整段（几句话连读），不要拆开、不要合并、不增删字词；英文和阿拉伯数字（GPU、RSA-260、552B）照原样读。"
              "程序会自动把整段切回单句。")
    if mode == "voice-clone":
        return {
            "mode": mode, "tool": "audio_to_audio_plus", "reference_audio": str(reference), "prompt_template": DOUBAO_PROMPT,
            "tempo": C.DOUBAO_TEMPO[mode],
            "instructions": "用 audio_to_audio_plus，@音频1 = reference_audio，prompt = 该条 request。" + common,
        }
    return {
        "mode": mode, "tool": "text_to_audio_plus", "voice": C.DOUBAO_TTS_VOICE_PROFILE, "request_template": C.DOUBAO_TTS_REQUEST,
        "tempo": C.DOUBAO_TEMPO[mode],
        "instructions": "用 text_to_audio_plus，请求 = 该条 request（已按 request_template 填好固定音色描述和原文）。" + common
                        + "语速由程序统一加速，不要在请求里要求语速。",
    }


def _visible_len(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def _doubao_request(block: Mapping[str, Any], text: str) -> str:
    if block.get("tool") == "audio_to_audio_plus":
        return str(block["prompt_template"]).replace("{spoken_text}", text)
    return str(block["request_template"]).replace("{voice}", str(block["voice"])).replace("{spoken_text}", text)


def plan_batches(units: list[Mapping[str, Any]], *, max_chars: int | None = None, min_tail: int | None = None) -> list[list[str]]:
    """Group consecutive sentences into Doubao requests: whole scenes packed up to ``max_chars``.

    A scene longer than the cap is split at sentence boundaries into balanced parts;
    a tail shorter than ``min_tail`` joins the previous batch.
    """

    max_chars = max_chars or C.DOUBAO_BATCH_MAX_CHARS
    min_tail = C.DOUBAO_BATCH_MIN_TAIL if min_tail is None else min_tail
    size = {u["unit_id"]: _visible_len(str(u["spoken_text"])) for u in units}
    scenes: list[list[str]] = []
    last_scene = object()
    for unit in units:
        if unit["scene_id"] != last_scene:
            scenes.append([])
            last_scene = unit["scene_id"]
        scenes[-1].append(unit["unit_id"])
    pieces: list[list[str]] = []
    for scene in scenes:
        total = sum(size[u] for u in scene)
        if total <= max_chars:
            pieces.append(scene)
            continue
        target = total / math.ceil(total / max_chars)
        part: list[str] = []
        for uid in scene:
            if part and (sum(size[u] for u in part) + size[uid] > max_chars or sum(size[u] for u in part) >= target):
                pieces.append(part)
                part = []
            part.append(uid)
        pieces.append(part)
    batches: list[list[str]] = []
    for piece in pieces:
        if batches and sum(size[u] for u in batches[-1]) + sum(size[u] for u in piece) <= max_chars:
            batches[-1].extend(piece)
        else:
            batches.append(list(piece))
    if len(batches) > 1 and sum(size[u] for u in batches[-1]) < min_tail:
        batches[-2].extend(batches.pop())
    return batches


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
    previous = _read_json(path)
    _drop_stale_audio(run_dir, previous, units)
    block = _doubao_block(mode, reference) if provider == "doubao" else None
    batches: list[dict[str, Any]] = []
    if block is not None:
        # Sentences that already have audio (an earlier, interrupted run) are not asked again.
        pending = [u for u in units if not _has_audio(u["output_path"])]
        old_fallback = {b.get("spoken_text") for b in previous.get("batches") or [] if b.get("per_sentence")}
        for index, ids in enumerate(plan_batches(pending), start=1):
            batches.append(_make_batch(block, run_dir, f"b{index:02d}", [u for u in pending if u["unit_id"] in ids], old_fallback))
    _drop_stale_audio(run_dir, previous, [], batches)
    manifest = {
        "version": C.V6_VERSION,
        "provider": provider,
        "units": units,
        "doubao": block,
        "batches": batches if block is not None else None,
        "todo": [],
        "edge": {"voice": C.EDGE_TTS_VOICE, "rate": C.EDGE_TTS_RATE} if provider == "edge" else None,
    }
    if block is not None:
        manifest["todo"] = doubao_todo(manifest)
    _write_manifest(path, manifest)
    return manifest


def _make_batch(block: Mapping[str, Any], run_dir: Path, batch_id: str, units: list[Mapping[str, Any]], per_sentence_texts: set[str] | None = None) -> dict[str, Any]:
    text = "".join(str(u["spoken_text"]) for u in units)
    return {"batch_id": batch_id, "unit_ids": [u["unit_id"] for u in units], "chars": _visible_len(text), "spoken_text": text,
            "request": _doubao_request(block, text), "output_path": str(raw_audio_path(run_dir, batch_id, ".wav")),
            "per_sentence": text in (per_sentence_texts or set()) and len(units) > 1}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _drop_stale_audio(run_dir: Path, previous: Mapping[str, Any], units: list[dict[str, Any]], batches: list[dict[str, Any]] | None = None) -> None:
    """Delete audio whose text changed, so an edited plan never keeps its old voice-over.

    Output paths are keyed by id (u01 ..., b01 ...); when a sentence is rewritten
    (or a batch is regrouped) the old file would otherwise count as already synthesized.
    """

    old = {str(u.get("output_path")): str(u.get("spoken_text")) for u in list(previous.get("units") or []) + list(previous.get("batches") or [])}
    for unit in units:
        out = Path(unit["output_path"])
        if out.is_file() and old.get(str(out)) != unit["spoken_text"]:
            out.unlink(missing_ok=True)
            (run_dir / "artifacts" / "alignments" / f"{unit['unit_id']}.raw.json").unlink(missing_ok=True)
    for batch in batches or []:
        out = Path(batch["output_path"])
        if out.is_file() and old.get(str(out)) != batch["spoken_text"]:
            out.unlink(missing_ok=True)


def _has_audio(path: str | Path) -> bool:
    path = Path(path)
    return path.is_file() and path.stat().st_size > 0


def missing_units(manifest: Mapping[str, Any]) -> list[str]:
    return [u["unit_id"] for u in manifest.get("units") or [] if not _has_audio(u["output_path"])]


def doubao_todo(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The Doubao requests still needed: one per batch, or one per sentence for a batch that could not be cut."""

    block = manifest.get("doubao") or {}
    by_id = {u["unit_id"]: u for u in manifest.get("units") or []}
    todo: list[dict[str, Any]] = []
    for batch in manifest.get("batches") or []:
        missing = [uid for uid in batch["unit_ids"] if not _has_audio(by_id[uid]["output_path"])]
        if not missing or batch.get("replaced_by"):
            continue
        if batch.get("per_sentence"):
            for uid in missing:
                unit = by_id[uid]
                todo.append({"id": uid, "kind": "sentence", "spoken_text": unit["spoken_text"],
                             "request": _doubao_request(block, unit["spoken_text"]), "output_path": unit["output_path"]})
        elif not _has_audio(batch["output_path"]):
            todo.append({"id": batch["batch_id"], "kind": "batch", "sentences": len(batch["unit_ids"]), "chars": batch["chars"],
                         "spoken_text": batch["spoken_text"], "request": batch["request"], "output_path": batch["output_path"]})
    return todo


def prepare_doubao(run_dir: Path, manifest_path: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Cut every delivered batch back into sentences; return the requests still needed (and save them as ``todo``).

    A batch whose cut does not pass the checks is marked ``per_sentence``: its
    sentences are then asked one request each (only that batch).
    """

    by_id = {u["unit_id"]: u for u in manifest.get("units") or []}
    block = manifest.get("doubao") or {}
    batches: list[dict[str, Any]] = []
    for batch in manifest.get("batches") or []:
        units = [by_id[uid] for uid in batch["unit_ids"]]
        if batch.get("per_sentence") or all(_has_audio(u["output_path"]) for u in units) or not _has_audio(batch["output_path"]):
            batches.append(batch)
            continue
        samples = decode_mono(Path(batch["output_path"]))
        cut = split_batch(samples, [u["spoken_text"] for u in units])
        if cut is None:
            batch["split"] = {"status": "failed"}
            if batch["chars"] > C.DOUBAO_BATCH_FALLBACK_CHARS and len(units) > 1:
                # Too long to cut cleanly: ask again in a few smaller batches (still not one request per sentence).
                groups = plan_batches(units, max_chars=C.DOUBAO_BATCH_FALLBACK_CHARS)
                batch["replaced_by"] = [f"{batch['batch_id']}-{i}" for i in range(1, len(groups) + 1)]
                batches.append(batch)
                for sub_id, ids in zip(batch["replaced_by"], groups):
                    batches.append(_make_batch(block, run_dir, sub_id, [by_id[uid] for uid in ids]))
            else:
                batch["per_sentence"] = True
                batches.append(batch)
            continue
        for unit, (start, end) in zip(units, cut["ranges"]):
            write_wav(Path(unit["output_path"]), samples[start:end])
        batch["split"] = {"status": "ok", "method": cut["method"], "cuts_seconds": [round(r[1] / SAMPLE_RATE, 3) for r in cut["ranges"][:-1]],
                          "speed_ratios": cut["ratios"]}
        batches.append(batch)
    manifest["batches"] = batches
    manifest["todo"] = doubao_todo(manifest)
    _write_manifest(manifest_path, manifest)
    return manifest["todo"]


# ---------------------------------------------------------------------------
# cutting one batch back into sentences
# ---------------------------------------------------------------------------
FRAME = SAMPLE_RATE // 100   # 10 ms analysis frames
SPLIT_MIN_PAUSE = (0.12, 0.06)   # strict pass, then a looser pass
SPLIT_SPEED_RANGE = (0.55, 1.8)  # each sentence's seconds-per-character vs the batch average
SPLIT_GOOD_PAUSE = 0.45          # a pause this long counts as a full sentence break
SPLIT_PAUSE_WEIGHT = 0.3         # how much a long pause is preferred over a plausible length


def _frame_levels(samples: array) -> list[float]:
    levels = []
    for start in range(0, len(samples), FRAME):
        block = samples[start:start + FRAME]
        peak = max(max(block), -min(block)) if block else 0
        levels.append(20 * math.log10(max(peak, 1) / 32768))
    return levels


_LATIN = re.compile(r"[A-Za-z]+")
_DIGITS = re.compile(r"\d+(?:\.\d+)?")


def _sentence_weight(text: str) -> float:
    """Rough spoken length in Chinese-syllable units, used to place the expected sentence breaks.

    A Chinese character is one syllable; an English word takes about one syllable per two
    letters (Anthropic ~ 4, GPT ~ 3 letters spelled); a number is read digit by digit or as
    a short Chinese numeral (~1 per digit); "%" adds "百分之"; a comma adds a short pause.
    """

    weight = 0.0
    for word in _LATIN.findall(text):
        weight += len(word) if word.isupper() and len(word) <= 4 else max(1.0, len(word) / 2)
    for number in _DIGITS.findall(text):
        weight += len(number.replace(".", "")) + (1 if "." in number else 0)
    rest = _DIGITS.sub("", _LATIN.sub("", text))
    weight += sum(1 for ch in rest if "\u4e00" <= ch <= "\u9fff")
    weight += 3 * text.count("%") + 1.5 * sum(text.count(mark) for mark in "，、；：,;:")
    return max(weight, 1.0)


def _pauses(levels: list[float], threshold: float, first: int, last: int, min_frames: int) -> list[tuple[int, int]]:
    runs, start = [], None
    for i in range(first, last + 1):
        quiet = levels[i] < threshold
        if quiet and start is None:
            start = i
        elif not quiet and start is not None:
            if i - start >= min_frames:
                runs.append((start, i))
            start = None
    return runs


def _choose_cuts(candidates: list[tuple[float, float]], weights: list[float], start: float, end: float) -> list[int] | None:
    """Pick one pause per sentence break by dynamic programming over the sentence *lengths*.

    Every sentence is scored on its own (its duration against its share of the recording,
    on a log scale) plus a bonus for cutting at a long pause. Scoring lengths instead of
    absolute positions keeps small estimation errors from piling up over a long
    recording, so one request can hold a whole edition.
    """

    import bisect

    k, m = len(weights) - 1, len(candidates)
    if m < k:
        return None
    rate = (end - start) / sum(weights)
    expected = [w * rate for w in weights]
    mids = [c[0] for c in candidates]
    inf = float("inf")

    def length_cost(index: int, a: float, b: float) -> float:
        if b - a <= 0.05:
            return inf
        x = math.log((b - a) / expected[index])
        return x * x

    def pause_cost(j: int) -> float:
        return SPLIT_PAUSE_WEIGHT * (1 - min(candidates[j][1] / SPLIT_GOOD_PAUSE, 1.0))

    table = [[inf] * m for _ in range(k)]
    back = [[-1] * m for _ in range(k)]
    for j in range(m):
        table[0][j] = length_cost(0, start, mids[j]) + pause_cost(j)
    for b in range(1, k):
        for j in range(m):
            # Only previous breaks that leave sentence b a plausible length.
            lo = bisect.bisect_left(mids, mids[j] - expected[b] * 3.0)
            hi = bisect.bisect_right(mids, mids[j] - expected[b] * 0.3)
            best, best_i = inf, -1
            for i in range(lo, min(hi, j)):
                if table[b - 1][i] < inf:
                    value = table[b - 1][i] + length_cost(b, mids[i], mids[j])
                    if value < best:
                        best, best_i = value, i
            if best_i >= 0:
                table[b][j] = best + pause_cost(j)
                back[b][j] = best_i
    finals = [table[k - 1][j] + length_cost(k, mids[j], end) for j in range(m)]
    j = min(range(m), key=lambda x: finals[x])
    if finals[j] == inf:
        return None
    chosen = [j]
    for b in range(k - 1, 0, -1):
        j = back[b][j]
        chosen.append(j)
    return chosen[::-1]


def split_batch(samples: array, texts: list[str]) -> dict[str, Any] | None:
    """Cut one batch recording into ``len(texts)`` sentences at its pauses.

    Returns sample ranges per sentence (cut in the middle of each chosen pause)
    or ``None`` when no cut passes the checks (every sentence must keep a
    plausible speaking speed).
    """

    n = len(samples)
    if len(texts) <= 1:
        return {"ranges": [(0, n)], "method": "single", "ratios": [1.0]}
    levels = _frame_levels(samples)
    ordered = sorted(levels)
    speech = ordered[int(len(ordered) * 0.9)]
    floor = ordered[int(len(ordered) * 0.1)]
    threshold = max(speech - 35.0, min(floor + 12.0, speech - 20.0))
    voiced = [i for i, level in enumerate(levels) if level >= threshold]
    if not voiced:
        return None
    first, last = voiced[0], voiced[-1]
    weights = [_sentence_weight(t) for t in texts]
    start_s = first * FRAME / SAMPLE_RATE
    end_s = (last + 1) * FRAME / SAMPLE_RATE
    for method, min_pause in (("pause", SPLIT_MIN_PAUSE[0]), ("short-pause", SPLIT_MIN_PAUSE[1])):
        runs = _pauses(levels, threshold, first, last, max(1, round(min_pause * 100)))
        candidates = [((a + b) / 2 * FRAME / SAMPLE_RATE, (b - a) * FRAME / SAMPLE_RATE) for a, b in runs]
        chosen = _choose_cuts(candidates, weights, start_s, end_s)
        if chosen is None:
            continue
        cuts = [min(n, int(candidates[j][0] * SAMPLE_RATE)) for j in chosen]
        ranges = list(zip([0] + cuts, cuts + [n]))
        ratios = _speed_ratios(ranges, weights, first * FRAME, min(n, (last + 1) * FRAME))
        if all(SPLIT_SPEED_RANGE[0] <= r <= SPLIT_SPEED_RANGE[1] for r in ratios):
            return {"ranges": ranges, "method": method, "ratios": ratios}
    return None


def _speed_ratios(ranges: list[tuple[int, int]], weights: list[float], voiced_start: int, voiced_end: int) -> list[float]:
    total = (voiced_end - voiced_start) / sum(weights)
    out = []
    for (start, end), weight in zip(ranges, weights):
        length = min(end, voiced_end) - max(start, voiced_start)
        out.append(round(max(length, 0) / weight / total, 3))
    return out


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
              "doubao_requests": _request_summary(manifest),
              "total_speech_seconds": round(sum(e["duration"] for e in entries), 3)}
    (run_dir / "artifacts" / "voice_manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _request_summary(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    batches = manifest.get("batches") or []
    if not batches and manifest.get("provider") != "doubao":
        return None
    requests = sum(len(b["unit_ids"]) if b.get("per_sentence") else 1 for b in batches)
    return {"requests": requests, "batches": [b["batch_id"] for b in batches],
            "smaller_batches_after_failed_cut": [b["batch_id"] for b in batches if b.get("replaced_by")],
            "per_sentence_batches": [b["batch_id"] for b in batches if b.get("per_sentence")],
            "splits": {b["batch_id"]: b.get("split") for b in batches}}
