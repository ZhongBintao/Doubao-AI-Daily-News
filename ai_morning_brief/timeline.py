"""v6 timeline: exact placement of every sentence, scene, card and caption."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from . import config as C
from .writing import caption_visible_units, normalize_with_ledger

CAPTION_MAX_UNITS = 17          # one caption line at 46px inside the safe width
CAPTION_MAX_LINES = 2
HOOK_SPEECH_START = 0.20
SCENE_LEAD = 0.25               # visual transition starts slightly before speech
TAIL = 0.8


def _speakable_len(text: str) -> int:
    return len(re.sub(r"[\s，。！？、；：,.!?;:“”\"'（）()《》【】]", "", text))


def split_caption(text: str, maximum: int = CAPTION_MAX_UNITS * CAPTION_MAX_LINES) -> list[str]:
    """Split one sentence into caption chunks at punctuation, never inside an ASCII word."""

    text = text.strip()
    if caption_visible_units(text) <= maximum:
        return [text]
    pieces = [p for p in re.split(r"(?<=[，、：；,;:）)])|(?=[（(])", text) if p]
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and caption_visible_units(current + piece) > maximum:
            chunks.append(current)
            current = piece
        else:
            current += piece
    if current:
        chunks.append(current)
    out: list[str] = []
    for chunk in chunks:
        while caption_visible_units(chunk) > maximum:
            cut = ""
            for match in re.finditer(r"[A-Za-z0-9][A-Za-z0-9.+#/\-%]*|\s+|.", chunk):
                if caption_visible_units(cut + match.group(0)) > maximum:
                    break
                cut += match.group(0)
            if not cut:
                break
            out.append(cut)
            chunk = chunk[len(cut):]
        if chunk.strip():
            out.append(chunk)
    return out


def _chunk_times(chunks: list[str], words: list[Mapping[str, Any]], duration: float) -> list[tuple[float, float]]:
    """Map caption chunks to time using word boundaries over the spoken text."""

    spoken_lengths = [_speakable_len(normalize_with_ledger(chunk).spoken_text) for chunk in chunks]
    total = max(1, sum(spoken_lengths))
    if not words:
        times, cursor = [], 0.0
        for length in spoken_lengths:
            end = cursor + duration * length / total
            times.append((cursor, end))
            cursor = end
        return times
    cumulative: list[tuple[int, float, float]] = []
    count = 0
    for word in words:
        count += _speakable_len(str(word.get("text") or ""))
        cumulative.append((count, float(word["start"]), float(word["end"])))
    spoken_total = max(1, cumulative[-1][0])

    def time_at(position: int) -> float:
        target = position * spoken_total / total
        for cum, start, end in cumulative:
            if cum >= target:
                return end
        return duration

    times = []
    cursor_chars = 0
    start = 0.0
    for i, length in enumerate(spoken_lengths):
        cursor_chars += length
        end = duration if i == len(spoken_lengths) - 1 else min(duration, time_at(cursor_chars))
        times.append((start, max(start + 0.3, end)))
        start = end
    return times


def build_timeline(run_dir: Path, final_plan: Mapping[str, Any], voice: Mapping[str, Any]) -> dict[str, Any]:
    durations = {u["unit_id"]: float(u["duration"]) for u in voice.get("units") or []}
    plan_units = list(final_plan.get("units") or [])
    scenes = list(final_plan.get("scenes") or [])
    placed_units: list[dict[str, Any]] = []
    captions: list[dict[str, Any]] = []
    scene_spans: dict[str, list[float]] = {}
    cursor = HOOK_SPEECH_START
    previous_scene = None
    for unit in plan_units:
        scene_id = unit["scene_id"]
        if previous_scene is not None and scene_id != previous_scene:
            cursor += C.PAUSE_AFTER_HOOK if previous_scene == "hook" else C.PAUSE_BETWEEN_STORIES
        elif previous_scene is not None:
            cursor += C.PAUSE_BETWEEN_SENTENCES
        duration = durations[unit["unit_id"]]
        start, end = cursor, cursor + duration
        placed_units.append({**unit, "start": round(start, 3), "end": round(end, 3), "duration": round(duration, 3)})
        span = scene_spans.setdefault(scene_id, [start, end])
        span[1] = end
        align_path = run_dir / "artifacts" / "alignments" / f"{unit['unit_id']}.json"
        words = json.loads(align_path.read_text(encoding="utf-8")).get("words", []) if align_path.is_file() else []
        chunks = split_caption(unit["display_text"])
        for chunk, (cs, ce) in zip(chunks, _chunk_times(chunks, words, duration)):
            captions.append({"start": round(start + cs, 3), "end": round(start + ce, 3), "text": chunk.rstrip("。")})
        cursor = end
        previous_scene = scene_id
    total = cursor + TAIL
    outro_span = scene_spans.get("outro")
    if outro_span and total - (outro_span[0] - SCENE_LEAD) < C.OUTRO_MIN_SECONDS:
        total = outro_span[0] - SCENE_LEAD + C.OUTRO_MIN_SECONDS
    scene_out: list[dict[str, Any]] = []
    for i, scene in enumerate(scenes):
        span = scene_spans.get(scene["scene_id"])
        if not span:
            continue
        start = 0.0 if i == 0 else max(0.0, span[0] - SCENE_LEAD)
        scene_out.append({"scene_id": scene["scene_id"], "kind": scene["kind"], "start": round(start, 3)})
    for i, scene in enumerate(scene_out):
        scene["end"] = scene_out[i + 1]["start"] if i + 1 < len(scene_out) else round(total, 3)
    card_events: dict[str, list[dict[str, Any]]] = {}
    for unit in placed_units:
        if unit.get("card"):
            card_events.setdefault(unit["scene_id"], []).append({"card": unit["card"], "start": unit["start"], "end": unit["end"]})
    # Captions end when the next one starts, so the bar never flickers between sentences of a scene.
    for i in range(len(captions) - 1):
        gap = captions[i + 1]["start"] - captions[i]["end"]
        if 0 < gap < 0.5:
            captions[i]["end"] = captions[i + 1]["start"]
    timeline = {
        "version": C.V6_VERSION,
        "total_duration": round(total, 3),
        "scenes": scene_out,
        "units": placed_units,
        "card_events": card_events,
        "captions": captions,
    }
    (run_dir / "artifacts" / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    srt = []
    for n, cue in enumerate(captions, 1):
        srt += [str(n), f"{_srt(cue['start'])} --> {_srt(cue['end'])}", cue["text"], ""]
    sub_path = run_dir / "assets" / "subtitles" / "subtitles.srt"
    sub_path.parent.mkdir(parents=True, exist_ok=True)
    sub_path.write_text("\n".join(srt), encoding="utf-8")
    return timeline


def _srt(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
