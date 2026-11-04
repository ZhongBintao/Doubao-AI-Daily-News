"""v6 narration audio: one audio file per sentence, read in a few long takes.

Every narration unit (one sentence) ends up as one audio file, so pictures,
card highlights and captions follow the voice sentence by sentence. Two providers:

* ``doubao`` — agent handoff (see the voice skill): ``text_to_audio_plus`` with
  the fixed voice profile, natural speed. One long take keeps timbre and energy
  steadier than stitched short ones, so the edition is read in as few *takes* as
  possible (:func:`plan_takes`): split only between stories, balanced, about
  ``DOUBAO_TAKE_TARGET_SECONDS`` each and never over ``DOUBAO_TAKE_MAX_SECONDS``.
  Every Doubao answer starts with a spoken tag ("内容由豆包AI生成") that
  :func:`watermark_cut` removes; :func:`prepare_doubao` then cuts the take back
  into sentences at its pauses (:func:`split_batch`). A take that cannot be cut,
  or came back truncated, is asked again in two halves, down to single sentences.
  Cut pieces keep their natural pauses, so the timeline can lay a take back down
  exactly as it was read.
* ``edge`` — automatic Microsoft Edge neural TTS (no key), one call per
  sentence with native word boundaries. Also the automatic fallback.

``finalize`` converts every unit to 48 kHz mono WAV and writes per-unit word
timings to ``alignments/``. Edge sentences are trimmed; pieces of a Doubao take
are kept whole and report the silence they carry (``lead`` / ``tail``).
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import ssl
import statistics
import subprocess
import time
from array import array
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from . import config as C

SAMPLE_RATE = 48000


class VoiceError(RuntimeError):
    pass


def unit_audio_path(run_dir: Path, unit_id: str) -> Path:
    return run_dir / "assets" / "audio" / "units" / f"{unit_id}.wav"


def raw_audio_path(run_dir: Path, unit_id: str, suffix: str = ".mp3") -> Path:
    return run_dir / "assets" / "audio" / "raw" / f"{unit_id}{suffix}"


def spoken_for(unit: Mapping[str, Any], provider: str) -> str:
    """Text sent to the TTS: Doubao reads the raw sentence; edge gets the normalised spoken text."""

    return str(unit["display_text"] if provider == "doubao" else unit["spoken_text"])


# ---------------------------------------------------------------------------
# spoken length and speaking rate
# ---------------------------------------------------------------------------
_LATIN = re.compile(r"[A-Za-z]+")
_DIGITS = re.compile(r"\d+(?:\.\d+)?")


def sentence_weight(text: str) -> float:
    """Rough spoken length in Chinese-syllable units.

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
    weight += sum(1 for ch in rest if "一" <= ch <= "鿿")
    weight += 3 * text.count("%") + 1.5 * sum(text.count(mark) for mark in "，、；：,;:")
    return max(weight, 1.0)


_sentence_weight = sentence_weight   # older name


def rate_state_path(state_dir: Path) -> Path:
    return state_dir / "doubao_speech_rate.json"


def load_speech_rate(state_dir: Path | None) -> float:
    """Median measured Doubao rate (weight units per second, pauses included) or the default."""

    if state_dir is None:
        return C.DOUBAO_SPEECH_RATE_DEFAULT
    rates = [float(m["rate"]) for m in _read_json(rate_state_path(state_dir)).get("measurements") or []
             if isinstance(m, Mapping) and C.DOUBAO_RATE_BOUNDS[0] <= float(m.get("rate") or 0) <= C.DOUBAO_RATE_BOUNDS[1]]
    return round(statistics.median(rates[-C.DOUBAO_RATE_HISTORY:]), 3) if rates else C.DOUBAO_SPEECH_RATE_DEFAULT


def record_speech_rate(state_dir: Path, run_date: str, measurements: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep the measured rate of every finished take (one entry per run date + take, newest last)."""

    path = rate_state_path(state_dir)
    known = [m for m in _read_json(path).get("measurements") or [] if isinstance(m, Mapping)]
    fresh = [{"date": run_date, "take": m["take"], "rate": m["rate"], "seconds": m.get("seconds")} for m in measurements
             if C.DOUBAO_RATE_BOUNDS[0] <= float(m["rate"]) <= C.DOUBAO_RATE_BOUNDS[1]]
    keys = {(m["date"], m["take"]) for m in fresh}
    merged = [m for m in known if (m.get("date"), m.get("take")) not in keys] + fresh
    merged = merged[-max(C.DOUBAO_RATE_HISTORY * 5, 50):]
    _write_manifest(path, {"unit": "sentence-weight per second, natural pauses included", "measurements": merged})
    return merged


# ---------------------------------------------------------------------------
# planning the takes
# ---------------------------------------------------------------------------
def story_groups(units: Sequence[Mapping[str, Any]], kind_of: Mapping[str, str] | None = None) -> list[list[Mapping[str, Any]]]:
    """Consecutive units grouped by story: the hook joins the story after it, the outro the story before it."""

    kind_of = kind_of or {}
    groups: list[list[Mapping[str, Any]]] = []
    last = object()
    for unit in units:
        if unit["scene_id"] != last:
            groups.append([])
            last = unit["scene_id"]
        groups[-1].append(unit)

    def kind(group: list[Mapping[str, Any]]) -> str:
        scene = group[0]["scene_id"]
        return kind_of.get(scene) or ("hook" if scene == "hook" else "outro" if scene == "outro" else "story")

    merged: list[list[Mapping[str, Any]]] = []
    carry: list[Mapping[str, Any]] = []
    for group in groups:
        if kind(group) == "hook":
            carry.extend(group)
        elif kind(group) == "outro" and merged:
            merged[-1].extend(carry + group)
            carry = []
        else:
            merged.append(carry + group)
            carry = []
    if carry:
        merged.append(carry)
    return merged


def _balanced_parts(weights: list[float], k: int) -> list[int]:
    """Split a sequence into k contiguous parts minimising the largest part; returns the part sizes."""

    n = len(weights)
    k = max(1, min(k, n))
    prefix = [0.0]
    for w in weights:
        prefix.append(prefix[-1] + w)
    inf = float("inf")
    best = [[inf] * (n + 1) for _ in range(k + 1)]
    back = [[0] * (n + 1) for _ in range(k + 1)]
    best[0][0] = 0.0
    for parts in range(1, k + 1):
        for end in range(1, n + 1):
            for start in range(parts - 1, end):
                value = max(best[parts - 1][start], prefix[end] - prefix[start])
                if value < best[parts][end]:
                    best[parts][end], back[parts][end] = value, start
    sizes, end = [], n
    for parts in range(k, 0, -1):
        start = back[parts][end]
        sizes.append(end - start)
        end = start
    return sizes[::-1]


def plan_takes(units: Sequence[Mapping[str, Any]], *, rate: float | None = None, kind_of: Mapping[str, str] | None = None,
               target: float | None = None, max_seconds: float | None = None, safety: float | None = None) -> list[list[str]]:
    """Group the edition into as few Doubao takes as possible, split only between stories.

    The number of takes is ceil(estimate / target); stories are then spread over them so the
    longest take is as short as possible. A take whose estimate x safety would pass the engine
    limit gets one more take; a single story longer than the limit is split between sentences.
    """

    rate = rate or C.DOUBAO_SPEECH_RATE_DEFAULT
    target = target or C.DOUBAO_TAKE_TARGET_SECONDS
    max_seconds = max_seconds or C.DOUBAO_TAKE_MAX_SECONDS
    safety = safety or C.DOUBAO_TAKE_SAFETY
    limit = max_seconds / safety - C.DOUBAO_WATERMARK_SECONDS   # seconds of text one take may hold

    def seconds(group: Sequence[Mapping[str, Any]]) -> float:
        return sum(sentence_weight(str(u["spoken_text"])) for u in group) / rate

    groups: list[list[Mapping[str, Any]]] = []
    for group in story_groups(units, kind_of):
        if seconds(group) <= limit or len(group) == 1:
            groups.append(group)
            continue
        weights = [seconds([u]) for u in group]
        parts = math.ceil(sum(weights) / limit)
        while True:
            sizes = _balanced_parts(weights, parts)
            pieces, at = [], 0
            for size in sizes:
                pieces.append(group[at:at + size])
                at += size
            if all(seconds(p) <= limit for p in pieces) or parts >= len(group):
                break
            parts += 1
        groups.extend(pieces)
    if not groups:
        return []
    weights = [seconds(g) for g in groups]
    k = max(1, math.ceil(sum(weights) / target))
    while True:
        sizes = _balanced_parts(weights, k)
        takes, at = [], 0
        for size in sizes:
            takes.append([u["unit_id"] for g in groups[at:at + size] for u in g])
            at += size
        longest = max(sum(weights[i] for i in range(sum(sizes[:j]), sum(sizes[:j + 1]))) for j in range(len(sizes)))
        if longest <= limit or k >= len(groups):
            return takes
        k += 1


def split_in_two(unit_ids: Sequence[str], scene_of: Mapping[str, str], weight_of: Mapping[str, float]) -> list[list[str]]:
    """Halve a take for a retry: at the story boundary nearest the middle, else at the middle sentence."""

    ids = list(unit_ids)
    if len(ids) < 2:
        return [ids]
    total = sum(weight_of[u] for u in ids)
    boundaries = [i for i in range(1, len(ids)) if scene_of[ids[i]] != scene_of[ids[i - 1]]] or list(range(1, len(ids)))
    best = min(boundaries, key=lambda i: abs(sum(weight_of[u] for u in ids[:i]) - total / 2))
    return [ids[:best], ids[best:]]


# ---------------------------------------------------------------------------
# manifest and handoff
# ---------------------------------------------------------------------------
def _doubao_block(rate: float) -> dict[str, Any]:
    wait = C.DOUBAO_RATE_LIMIT_WAIT_SECONDS
    return {
        "tool": C.DOUBAO_TTS_TOOL, "voice": C.DOUBAO_TTS_VOICE_PROFILE, "request_template": C.DOUBAO_TTS_REQUEST,
        "speech_rate": rate, "take_target_seconds": C.DOUBAO_TAKE_TARGET_SECONDS, "take_max_seconds": C.DOUBAO_TAKE_MAX_SECONDS,
        "instructions": (f"逐条处理 todo（只有还缺的音频）：用 {C.DOUBAO_TTS_TOOL}，把该条 request 原样发给工具（已填好固定音色描述和原文，"
                         "不要改写、不要加语速要求），把返回的音频原样保存到该条 output_path。"
                         f"一次只发一条，不要并发；遇到“合成太频繁”等限流报错，等 {wait} 秒再重试同一条（最多 3 次）。"
                         "一条 todo 是一整段（几条新闻连读，最长约 2 分钟），不要拆开、不要合并、不增删字词；"
                         "英文和阿拉伯数字（GPU、RSA-260、552B）照原样读。开头的“内容由豆包AI生成”和整段切回单句都由程序自动处理。"),
    }


def _doubao_request(block: Mapping[str, Any], text: str) -> str:
    return str(block["request_template"]).replace("{voice}", str(block["voice"])).replace("{spoken_text}", text)


def _take_text(units: Sequence[Mapping[str, Any]]) -> str:
    out = ""
    for i, unit in enumerate(units):
        if i and unit["scene_id"] != units[i - 1]["scene_id"]:
            out += C.DOUBAO_STORY_BREAK
        out += str(unit["spoken_text"])
    return out


def _make_take(block: Mapping[str, Any], run_dir: Path, take_id: str, units: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    text = _take_text(units)
    weight = sum(sentence_weight(str(u["spoken_text"])) for u in units)
    return {"take_id": take_id, "unit_ids": [u["unit_id"] for u in units], "spoken_text": text, "weight": round(weight, 1),
            "estimated_seconds": round(weight / float(block["speech_rate"]) + C.DOUBAO_WATERMARK_SECONDS, 1),
            "request": _doubao_request(block, text), "output_path": str(raw_audio_path(run_dir, take_id, ".wav"))}


def build_manifest(run_dir: Path, final_plan: Mapping[str, Any], provider: str, *, speech_rate: float | None = None,
                   state_dir: Path | None = None) -> dict[str, Any]:
    if provider not in C.TTS_PROVIDERS:
        raise VoiceError(f"unknown TTS provider {provider!r}; choose one of {C.TTS_PROVIDERS}")
    path = run_dir / "artifacts" / "tts_manifest.json"
    previous = _read_json(path)
    old_units = {str(u.get("unit_id")): u for u in previous.get("units") or []}
    units = []
    for unit in final_plan.get("units") or []:
        entry = {
            "unit_id": unit["unit_id"],
            "scene_id": unit["scene_id"],
            "spoken_text": spoken_for(unit, provider),
            "display_text": unit["display_text"],
            "output_path": str(raw_audio_path(run_dir, unit["unit_id"], ".wav" if provider == "doubao" else ".mp3")),
        }
        old = old_units.get(entry["unit_id"]) or {}
        if old.get("take_id") and old.get("spoken_text") == entry["spoken_text"] and old.get("output_path") == entry["output_path"]:
            entry["take_id"] = old["take_id"]
        units.append(entry)
    _drop_stale_audio(run_dir, previous, units)
    block = None
    takes: list[dict[str, Any]] = []
    if provider == "doubao":
        rate = speech_rate or load_speech_rate(state_dir if state_dir is not None else run_dir.parent / "_state")
        block = _doubao_block(rate)
        kind_of = {s["scene_id"]: s.get("kind", "story") for s in final_plan.get("scenes") or []}
        # Sentences that already have audio (an earlier, interrupted run) are not asked again.
        pending = [u for u in units if not _has_audio(u["output_path"])]
        for u in pending:
            u.pop("take_id", None)
        for index, ids in enumerate(plan_takes(pending, rate=rate, kind_of=kind_of), start=1):
            takes.append(_make_take(block, run_dir, f"t{index:02d}", [u for u in pending if u["unit_id"] in ids]))
        _drop_stale_audio(run_dir, previous, [], takes)
    manifest = {
        "version": C.V6_VERSION,
        "provider": provider,
        "units": units,
        "doubao": block,
        "takes": takes if block is not None else None,
        "todo": [],
        "edge": {"voice": C.EDGE_TTS_VOICE, "rate": C.EDGE_TTS_RATE} if provider == "edge" else None,
    }
    if block is not None:
        manifest["todo"] = doubao_todo(manifest)
    _write_manifest(path, manifest)
    return manifest


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _drop_stale_audio(run_dir: Path, previous: Mapping[str, Any], units: list[dict[str, Any]], takes: list[dict[str, Any]] | None = None) -> None:
    """Delete audio whose text changed, so an edited plan never keeps its old voice-over.

    Output paths are keyed by id (u01 ..., t01 ...); when a sentence is rewritten
    (or a take is regrouped) the old file would otherwise count as already synthesized.
    """

    old = {str(u.get("output_path")): str(u.get("spoken_text"))
           for u in list(previous.get("units") or []) + list(previous.get("takes") or previous.get("batches") or [])}
    for unit in units:
        out = Path(unit["output_path"])
        if out.is_file() and old.get(str(out)) != unit["spoken_text"]:
            out.unlink(missing_ok=True)
            unit.pop("take_id", None)
            (run_dir / "artifacts" / "alignments" / f"{unit['unit_id']}.raw.json").unlink(missing_ok=True)
    for take in takes or []:
        out = Path(take["output_path"])
        if out.is_file() and old.get(str(out)) != take["spoken_text"]:
            out.unlink(missing_ok=True)


def _has_audio(path: str | Path) -> bool:
    path = Path(path)
    return path.is_file() and path.stat().st_size > 0


def missing_units(manifest: Mapping[str, Any]) -> list[str]:
    return [u["unit_id"] for u in manifest.get("units") or [] if not _has_audio(u["output_path"])]


def doubao_todo(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The Doubao requests still needed: one per take whose audio has not been delivered."""

    by_id = {u["unit_id"]: u for u in manifest.get("units") or []}
    todo: list[dict[str, Any]] = []
    for take in manifest.get("takes") or []:
        if take.get("replaced_by") or all(_has_audio(by_id[uid]["output_path"]) for uid in take["unit_ids"]):
            continue
        if not _has_audio(take["output_path"]):
            todo.append({"id": take["take_id"], "sentences": len(take["unit_ids"]), "estimated_seconds": take["estimated_seconds"],
                         "spoken_text": take["spoken_text"], "request": take["request"], "output_path": take["output_path"]})
    return todo


def prepare_doubao(run_dir: Path, manifest_path: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Strip the tag from every delivered take and cut it back into sentences.

    Returns the requests still needed (also saved as ``todo``). A take that cannot be
    cut, or came back truncated, is replaced by two halves that are asked again.
    """

    by_id = {u["unit_id"]: u for u in manifest.get("units") or []}
    block = manifest.get("doubao") or {}
    rate = float(block.get("speech_rate") or C.DOUBAO_SPEECH_RATE_DEFAULT)
    takes: list[dict[str, Any]] = []
    queue = list(manifest.get("takes") or [])
    while queue:
        take = queue.pop(0)
        units = [by_id[uid] for uid in take["unit_ids"]]
        if take.get("replaced_by") or all(_has_audio(u["output_path"]) for u in units) or not _has_audio(take["output_path"]):
            takes.append(take)
            continue
        raw = decode_mono(Path(take["output_path"]))
        result = cut_take(raw, [str(u["spoken_text"]) for u in units], rate=rate)
        take["watermark"] = result["watermark"]
        take["raw_seconds"] = result["raw_seconds"]
        if result["status"] != "ok" and len(units) > 1:
            take["split"] = {"status": result["status"], "detail": result.get("detail")}
            halves = split_in_two(take["unit_ids"], {u["unit_id"]: u["scene_id"] for u in units},
                                  {u["unit_id"]: sentence_weight(str(u["spoken_text"])) for u in units})
            take["replaced_by"] = [f"{take['take_id']}-{i}" for i in range(1, len(halves) + 1)]
            takes.append(take)
            subs = [_make_take(block, run_dir, sub_id, [by_id[uid] for uid in ids]) for sub_id, ids in zip(take["replaced_by"], halves)]
            for sub in subs:
                Path(sub["output_path"]).unlink(missing_ok=True)
            takes.extend(subs)
            continue
        for unit, (start, end) in zip(units, result["ranges"]):
            write_wav(Path(unit["output_path"]), result["body"][start:end])
            unit["take_id"] = take["take_id"]
        take["split"] = {"status": result["status"], "method": result.get("method"), "detail": result.get("detail"),
                         "cuts_seconds": [round(r[1] / SAMPLE_RATE, 3) for r in result["ranges"][:-1]],
                         "speed_ratios": result.get("ratios")}
        take["voiced_seconds"] = result["voiced_seconds"]
        take["measured_rate"] = round(take["weight"] / result["voiced_seconds"], 3) if result["voiced_seconds"] > 0 else None
        takes.append(take)
    manifest["takes"] = takes
    manifest["todo"] = doubao_todo(manifest)
    _write_manifest(manifest_path, manifest)
    return manifest["todo"]


# ---------------------------------------------------------------------------
# the Doubao tag and cutting one take back into sentences
# ---------------------------------------------------------------------------
FRAME = SAMPLE_RATE // 100   # 10 ms analysis frames
SPLIT_MIN_PAUSE = (0.12, 0.06)   # strict pass, then a looser pass
SPLIT_SPEED_RANGE = (0.55, 1.8)  # each sentence's seconds-per-character vs the take average
SPLIT_GOOD_PAUSE = 0.45          # a pause this long counts as a full sentence break
SPLIT_PAUSE_WEIGHT = 0.3         # how much a long pause is preferred over a plausible length
TRUNCATED_MARGIN = 1.0           # an answer this close to the engine limit was probably cut off
TOO_SHORT_RATIO = 0.5            # voiced length under this share of the estimate: text was skipped


def _frame_levels(samples: array) -> list[float]:
    levels = []
    for start in range(0, len(samples), FRAME):
        block = samples[start:start + FRAME]
        peak = max(max(block), -min(block)) if block else 0
        levels.append(20 * math.log10(max(peak, 1) / 32768))
    return levels


def _silence_threshold(levels: Sequence[float]) -> float:
    ordered = sorted(levels)
    speech = ordered[int(len(ordered) * 0.9)]
    floor = ordered[int(len(ordered) * 0.1)]
    return max(speech - 35.0, min(floor + 12.0, speech - 20.0))


def watermark_scan(samples: array, sr: int = SAMPLE_RATE) -> dict[str, Any]:
    """Find the leading Doubao tag ("内容由豆包AI生成") and the pause after it.

    Layout of every Doubao answer: silence (0-0.33 s), the tag (0.33-2.25 s), a pause of at
    least 0.18 s, then the text. Same rule as measured on real Doubao audio: 10 ms frames, RMS
    >= DOUBAO_WATERMARK_RMS_THRESHOLD is voice; after the first voiced frame, the first silent
    run of at least DOUBAO_WATERMARK_MIN_GAP ends the tag, and the text starts where it ends.
    Safety check: that pause must start inside DOUBAO_WATERMARK_WINDOW, so a recording without
    the tag (whose first pause is a comma early on, or a sentence break later) is never cut.

    Returns ``status`` (``cut`` / ``not-found``), ``cut`` (sample index or None), the onset and
    pause times in seconds, and ``reason`` when nothing was cut.
    """

    window = int(sr * 0.01)
    lo, hi = C.DOUBAO_WATERMARK_WINDOW
    head = np.frombuffer(samples, dtype=np.int16)[: int((hi + 2.0) * sr)].astype(np.float64)
    frames = len(head) // window
    if frames == 0:
        return {"status": "not-found", "cut": None, "reason": "音频为空"}
    rms = np.sqrt((head[: frames * window].reshape(frames, window) ** 2).mean(axis=1))
    voiced = rms >= C.DOUBAO_WATERMARK_RMS_THRESHOLD
    onset = int(np.argmax(voiced)) if voiced.any() else None
    if onset is None:
        return {"status": "not-found", "cut": None, "reason": "开头没有有声段"}
    need = max(1, round(C.DOUBAO_WATERMARK_MIN_GAP * 100))
    run = 0
    for i in range(onset, frames):
        if not voiced[i]:
            run += 1
            continue
        if run >= need:
            start, end = (i - run) / 100, i / 100
            found = {"onset_seconds": onset / 100, "pause_start_seconds": start, "pause_end_seconds": end}
            if lo <= start <= hi:
                return {"status": "cut", "cut": i * window, **found}
            if start > hi:
                return {"status": "not-found", "cut": None, **found,
                        "reason": f"第一个 ≥{C.DOUBAO_WATERMARK_MIN_GAP}s 的停顿在 {start:.2f}s，晚于 {hi}s，不像水印"}
            # a pause before the window would lie inside the tag itself: keep looking
        run = 0
    return {"status": "not-found", "cut": None, "onset_seconds": onset / 100,
            "reason": f"开头 {hi + 2.0:.0f}s 内没有起点在 {lo}–{hi}s 之间、≥{C.DOUBAO_WATERMARK_MIN_GAP}s 的停顿"}


def watermark_cut(samples: array, sr: int = SAMPLE_RATE) -> int | None:
    """Sample index where the Doubao tag and the pause after it end (the text starts), or None."""

    return watermark_scan(samples, sr)["cut"]


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
    absolute positions keeps small estimation errors from piling up over a long recording.
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
    """Cut one recording into ``len(texts)`` sentences at its pauses.

    Returns contiguous sample ranges per sentence (cut in the middle of each chosen
    pause, so every piece keeps its half of the pause) or ``None`` when no cut passes
    the checks (every sentence must keep a plausible speaking speed).
    """

    n = len(samples)
    if len(texts) <= 1:
        return {"ranges": [(0, n)], "method": "single", "ratios": [1.0]}
    levels = _frame_levels(samples)
    threshold = _silence_threshold(levels)
    voiced = [i for i, level in enumerate(levels) if level >= threshold]
    if not voiced:
        return None
    first, last = voiced[0], voiced[-1]
    weights = [sentence_weight(t) for t in texts]
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


def cut_take(raw: array, texts: list[str], *, rate: float) -> dict[str, Any]:
    """Tag removal, outer trim, truncation check and sentence cut for one Doubao answer.

    ``status``: ``ok`` (ranges index into ``body``), ``truncated`` / ``too-short``
    (the answer does not hold the whole text) or ``uncut`` (no clean cut at the pauses).
    A single sentence is always ``ok`` (nothing to cut, the detail says what was odd).
    """

    raw_seconds = len(raw) / SAMPLE_RATE
    scan = watermark_scan(raw)
    wm = scan["cut"]
    body = raw[wm:] if wm else raw
    start, end = _voiced_bounds(body)
    body = body[start:end]
    voiced_seconds = len(body) / SAMPLE_RATE
    expected = sum(sentence_weight(t) for t in texts) / rate
    out: dict[str, Any] = {"raw_seconds": round(raw_seconds, 3), "voiced_seconds": round(voiced_seconds, 3), "body": body,
                           "watermark": {**{k: v for k, v in scan.items() if k != "cut"}, "seconds": round((wm or 0) / SAMPLE_RATE, 3)}}
    problem = None
    if raw_seconds >= C.DOUBAO_TAKE_MAX_SECONDS - TRUNCATED_MARGIN:
        problem = ("truncated", f"音频 {raw_seconds:.1f}s，接近单次上限 {C.DOUBAO_TAKE_MAX_SECONDS:.0f}s，可能没读完")
    elif voiced_seconds < TOO_SHORT_RATIO * expected:
        problem = ("too-short", f"有声部分 {voiced_seconds:.1f}s，远短于估计的 {expected:.1f}s，可能漏读")
    if problem and len(texts) > 1:
        return {**out, "status": problem[0], "detail": problem[1]}
    cut = split_batch(body, texts)
    if cut is None:
        return {**out, "status": "uncut", "detail": "停顿处切不开"}
    return {**out, "status": "ok", "detail": problem[1] if problem else None, **cut}


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
            except Exception:  # network / service errors
                out.unlink(missing_ok=True)
                if attempt == attempts:
                    failed.append(unit["unit_id"])
                else:
                    time.sleep(2 * attempt)
    return failed


# ---------------------------------------------------------------------------
# normalize + align
# ---------------------------------------------------------------------------
def decode_mono(path: Path) -> array:
    result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
                            capture_output=True, timeout=300, check=False)
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


def _voiced_bounds(samples: array, *, threshold_db: float = -42.0, window: int = 480, pad_seconds: float = 0.04) -> tuple[int, int]:
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
    pad = int(pad_seconds * SAMPLE_RATE)
    return max(0, first - pad), min(n, last + pad)


_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[.\-#/+%][A-Za-z0-9]+)*|[一-鿿]")


def _word_tokens(text: str) -> list[tuple[str, float]]:
    """Speakable tokens as (token, weight): one English word / number or one Han character each."""

    return [(m.group(0), sentence_weight(m.group(0)) if re.search(r"[A-Za-z0-9]", m.group(0)) else 1.0) for m in _WORD_RE.finditer(text)]


def word_alignment(text: str, duration: float, *, offset: float = 0.0) -> list[dict[str, Any]]:
    """Word timings inside one sentence, spread by spoken weight (Doubao gives no timestamps)."""

    tokens = _word_tokens(text)
    total = sum(weight for _, weight in tokens)
    if not tokens or total <= 0:
        return []
    words, cursor = [], 0.0
    for token, weight in tokens:
        start = offset + cursor / total * duration
        cursor += weight
        words.append({"text": token, "start": round(start, 4), "end": round(offset + cursor / total * duration, 4)})
    return words


def wav_seconds(path: Path) -> float:
    return len(decode_mono(path)) / SAMPLE_RATE


def finalize(run_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize and align every unit; write artifacts/voice_manifest.json.

    Edge sentences are trimmed to their voice. Pieces of a Doubao take are kept whole
    (they carry the take's natural pauses) and report ``lead`` / ``tail``: the silence
    at their start and end, so the timeline can lay the take back down as it was read.
    """

    missing = missing_units(manifest)
    if missing:
        raise VoiceError("missing narration audio for units: " + ", ".join(missing))
    entries = []
    loose: dict[str, str] = {}   # Doubao sentences saved one file each (not from a take): tag status
    for unit in manifest.get("units") or []:
        samples = decode_mono(Path(unit["output_path"]))
        take_id = unit.get("take_id")
        if not take_id and manifest.get("provider") == "doubao":
            # Safety net: the agent synthesized this sentence on its own instead of reading the take.
            # Every Doubao answer carries the tag, so strip it here too.
            scan = watermark_scan(samples)
            loose[unit["unit_id"]] = scan["status"]
            if scan["cut"]:
                samples = samples[scan["cut"]:]
        if take_id:
            body = samples
            start, end = _voiced_bounds(samples, pad_seconds=0.0)
            offset = 0.0
        else:
            start, end = _voiced_bounds(samples)
            body = samples[start:end]
            offset = start / SAMPLE_RATE
        if len(body) < int(0.2 * SAMPLE_RATE):
            raise VoiceError(f"unit {unit['unit_id']} audio is too short")
        out = unit_audio_path(run_dir, unit["unit_id"])
        write_wav(out, body)
        duration = len(body) / SAMPLE_RATE
        lead = start / SAMPLE_RATE if take_id else 0.0
        tail = (len(body) - end) / SAMPLE_RATE if take_id else 0.0
        raw_words_path = run_dir / "artifacts" / "alignments" / f"{unit['unit_id']}.raw.json"
        if not take_id and raw_words_path.is_file():
            raw = json.loads(raw_words_path.read_text(encoding="utf-8"))
            words = [{"text": w["text"], "start": round(max(0.0, w["start"] - offset), 4), "end": round(min(duration, max(0.0, w["end"] - offset)), 4)}
                     for w in raw.get("words") or []]
            alignment = "native-word-boundary"
        else:
            words = word_alignment(str(unit["display_text"]), max(0.05, duration - lead - tail), offset=lead)
            alignment = "weighted-words"
        align_path = run_dir / "artifacts" / "alignments" / f"{unit['unit_id']}.json"
        align_path.parent.mkdir(parents=True, exist_ok=True)
        align_path.write_text(json.dumps({"unit_id": unit["unit_id"], "alignment": alignment, "duration": duration, "lead": round(lead, 4),
                                          "tail": round(tail, 4), "words": words}, ensure_ascii=False), encoding="utf-8")
        entries.append({"unit_id": unit["unit_id"], "scene_id": unit["scene_id"], "audio_path": str(out.relative_to(run_dir)),
                        "duration": round(duration, 4), "lead": round(lead, 4), "tail": round(tail, 4), "take_id": take_id,
                        "alignment": alignment, "spoken_text": unit["spoken_text"], "display_text": unit["display_text"]})
    summary = _take_summary(manifest)
    if summary is not None and loose:
        summary["sentences_outside_takes"] = sorted(loose)
        summary["watermark_not_found"] += [uid for uid, status in sorted(loose.items()) if status != "cut"]
    if summary and summary["measurements"]:
        record_speech_rate(run_dir.parent / "_state", run_dir.name, summary["measurements"])
    result = {"version": C.V6_VERSION, "provider": manifest.get("provider"), "units": entries,
              "doubao_takes": summary,
              "total_speech_seconds": round(sum(e["duration"] - e["lead"] - e["tail"] for e in entries), 3),
              "total_audio_seconds": round(sum(e["duration"] for e in entries), 3)}
    (run_dir / "artifacts" / "voice_manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _take_summary(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    if manifest.get("provider") != "doubao":
        return None
    takes = manifest.get("takes") or []
    used = [t for t in takes if not t.get("replaced_by") and t.get("split")]
    return {"requests": sum(1 for t in takes if t.get("raw_seconds") is not None),
            "takes": [{"take_id": t["take_id"], "sentences": len(t["unit_ids"]), "estimated_seconds": t.get("estimated_seconds"),
                       "raw_seconds": t.get("raw_seconds"), "watermark": (t.get("watermark") or {}).get("status"),
                       "split": (t.get("split") or {}).get("status"), "replaced_by": t.get("replaced_by")} for t in takes],
            "asked_again": [t["take_id"] for t in takes if t.get("replaced_by")],
            "watermark_not_found": [t["take_id"] for t in takes if (t.get("watermark") or {}).get("status") == "not-found"],
            "speech_rate_used": (manifest.get("doubao") or {}).get("speech_rate"),
            "measurements": [{"take": t["take_id"], "rate": t["measured_rate"], "seconds": t.get("voiced_seconds")}
                             for t in used if t.get("measured_rate") and len(t["unit_ids"]) > 1]}
