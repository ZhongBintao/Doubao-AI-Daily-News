"""v6 mix: per-sentence voice placed on the timeline over a quiet music bed.

* voice units are placed at their exact timeline start
* music (only the three tracks in ``assets/music``), placed to fit the v6 template:
    - opening: from frame 0 under the hook, its tail fading out over
      ``OPENING_TAIL`` seconds once the first headline is on screen
    - middle-loop: starts exactly when the first headline appears, so its first
      accent lands on that cut; it covers the whole programme (220 s > the
      3-minute cap) and only loops, with a crossfade, if a programme is longer
    - ending: tail-aligned, so its final note lands on the last frame; its quiet
      first seconds sit under the last story
  Each track gets one gain from its whole-file level (not from the slice used),
  so the bed sits ``MIX_MUSIC_UNDER_VOICE_DB`` below the voice and a voice-keyed
  envelope (30 ms attack / 350 ms release) ducks it ``DUCK_EXTRA_DB`` more while
  someone speaks (about 18 dB under the voice in total)
* soft whoosh at every story change
* final two-pass loudnorm to -16 LUFS / -1.5 dBTP
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from . import config as C

SR = 48000
MUSIC_DIR = Path(__file__).resolve().parent / "assets" / "music"
MUSIC = {"opening": MUSIC_DIR / "opening.mp3", "middle": MUSIC_DIR / "middle-loop.mp3", "ending": MUSIC_DIR / "ending.mp3"}
DUCK_EXTRA_DB = 4.0
FADE = 0.8            # crossfade between sections
OPENING_TAIL = 2.0    # opening keeps ringing this long into the first headline, fading out
MIDDLE_FADE_IN = 0.3  # short, so the middle track's first accent still reads as the cut
END_FADE = 0.15       # click guard only: the ending track ends by itself
LOOP_CROSSFADE = 1.5


class MixError(RuntimeError):
    pass


def _decode(path: Path, channels: int = 2) -> np.ndarray:
    result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", str(channels), "-ar", str(SR), "-"],
                            capture_output=True, timeout=180, check=False)
    if result.returncode != 0:
        raise MixError(f"cannot decode {path.name}")
    data = np.frombuffer(result.stdout, dtype=np.float32)
    return data.reshape(-1, channels).copy()


def _rms_db(x: np.ndarray) -> float:
    if x.size == 0:
        return -120.0
    value = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return 20 * np.log10(max(value, 1e-9))


def _active_rms_db(mono: np.ndarray, window: int = 4800) -> float:
    """RMS over voiced 100 ms blocks only (ignores pauses)."""

    n = len(mono) // window
    if n == 0:
        return _rms_db(mono)
    blocks = mono[: n * window].reshape(n, window)
    levels = np.sqrt(np.mean(np.square(blocks, dtype=np.float64), axis=1))
    voiced = blocks[levels > 10 ** (-40 / 20)]
    return _rms_db(voiced) if voiced.size else _rms_db(mono)


def _fade(x: np.ndarray, fade_in: float, fade_out: float) -> np.ndarray:
    y = x.copy()
    fi, fo = int(fade_in * SR), int(fade_out * SR)
    if fi:
        n = min(fi, len(y))
        y[:n] *= np.linspace(0, 1, n)[:, None]
    if fo:
        n = min(fo, len(y))
        y[-n:] *= np.linspace(1, 0, n)[:, None]
    return y


def _fit_length(x: np.ndarray, length: int) -> np.ndarray:
    """First ``length`` samples; loops with a crossfade only when the track is too short."""

    if length <= 0:
        return np.zeros((0, x.shape[1]), dtype=np.float32)
    if len(x) >= length:
        return x[:length]
    xf = min(int(LOOP_CROSSFADE * SR), len(x) // 4)
    out = x.copy()
    ramp = np.linspace(0, 1, xf, dtype=np.float32)[:, None]
    while len(out) < length:
        out[-xf:] = out[-xf:] * (1 - ramp) + x[:xf] * ramp
        out = np.concatenate([out, x[xf:]])
    return out[:length]


def _tail(x: np.ndarray, length: int) -> np.ndarray:
    """Last ``length`` samples (front-padded with silence if the track is shorter)."""

    if length <= len(x):
        return x[len(x) - length:]
    return np.concatenate([np.zeros((length - len(x), x.shape[1]), dtype=np.float32), x])


def _music_level_db(x: np.ndarray, window: int = 4800) -> float:
    """Level of a music file over its non-silent 100 ms blocks (whole file, so a slice never changes the gain)."""

    mono = x.mean(axis=1) if x.ndim == 2 else x
    n = len(mono) // window
    if n == 0:
        return _rms_db(mono)
    blocks = mono[: n * window].reshape(n, window)
    levels = np.sqrt(np.mean(np.square(blocks, dtype=np.float64), axis=1))
    sounding = blocks[levels > 10 ** (-70 / 20)]
    return _rms_db(sounding) if sounding.size else _rms_db(mono)


def _whoosh(length: float = 0.55) -> np.ndarray:
    n = int(length * SR)
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(n).astype(np.float32)
    # crude band-pass via moving averages, swelling envelope
    lowpassed = np.convolve(noise, np.ones(6) / 6, mode="same")
    band = lowpassed - np.convolve(lowpassed, np.ones(60) / 60, mode="same")
    env = np.sin(np.linspace(0, np.pi, n)) ** 2
    mono = (band * env).astype(np.float32)
    mono /= max(1e-6, float(np.max(np.abs(mono))))
    return np.stack([mono, mono], axis=1) * 0.12


def _envelope(voice_mono: np.ndarray, attack: float = 0.03, release: float = 0.35) -> np.ndarray:
    """Voice activity envelope 0..1 with attack/release smoothing (10 ms resolution)."""

    hop = SR // 100
    n = int(np.ceil(len(voice_mono) / hop))
    padded = np.zeros(n * hop, dtype=np.float32)
    padded[: len(voice_mono)] = voice_mono
    level = np.sqrt(np.mean(np.square(padded.reshape(n, hop)), axis=1))
    target = (level > 10 ** (-45 / 20)).astype(np.float32)
    env = np.zeros(n, dtype=np.float32)
    a, r = 1 - np.exp(-0.01 / attack), 1 - np.exp(-0.01 / release)
    value = 0.0
    for i, t in enumerate(target):
        value += (t - value) * (a if t > value else r)
        env[i] = value
    return np.repeat(env, hop)[: len(voice_mono)]


def _loudnorm(src: Path, dst: Path) -> dict[str, Any]:
    first = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(src), "-af",
                            f"loudnorm=I={C.MIX_TARGET_LUFS}:TP={C.MIX_TRUE_PEAK}:LRA=11:print_format=json", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=300, check=False)
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", first.stderr, re.S)
    if not match:
        raise MixError("loudnorm measurement failed")
    m = json.loads(match.group(0))
    second = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-af",
                             f"loudnorm=I={C.MIX_TARGET_LUFS}:TP={C.MIX_TRUE_PEAK}:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
                             f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true",
                             "-ar", str(SR), "-ac", "2", "-c:a", "pcm_s16le", str(dst)], capture_output=True, text=True, timeout=300, check=False)
    if second.returncode != 0:
        raise MixError(f"loudnorm apply failed: {second.stderr[-300:]}")
    return m


def measure(path: Path) -> dict[str, float]:
    result = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"],
                            capture_output=True, text=True, timeout=300, check=False)
    integrated = re.findall(r"I:\s+(-?[\d.]+) LUFS", result.stderr)
    peak = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", result.stderr)
    return {"lufs": float(integrated[-1]) if integrated else float("nan"), "true_peak": float(peak[-1]) if peak else float("nan")}


def mix(run_dir: Path, timeline: Mapping[str, Any], voice: Mapping[str, Any]) -> dict[str, Any]:
    total = float(timeline["total_duration"])
    length = int(np.ceil(total * SR))
    voice_track = np.zeros(length, dtype=np.float32)
    paths = {u["unit_id"]: run_dir / u["audio_path"] for u in voice.get("units") or []}
    for unit in timeline.get("units") or []:
        audio = _decode(paths[unit["unit_id"]], channels=1)[:, 0]
        start = int(round(float(unit["start"]) * SR))
        end = min(length, start + len(audio))
        voice_track[start:end] += audio[: end - start]
    voice_db = _active_rms_db(voice_track)
    scenes = list(timeline.get("scenes") or [])
    hook_end = next((s["end"] for s in scenes if s["kind"] == "hook"), min(4.0, total))
    outro_start = next((s["start"] for s in scenes if s["kind"] == "outro"), max(0.0, total - 3.0))
    target_db = voice_db - C.MIX_MUSIC_UNDER_VOICE_DB
    bed = np.zeros((length, 2), dtype=np.float32)
    tracks = {name: _decode(path) for name, path in MUSIC.items()}
    ending_len = len(tracks["ending"]) / SR
    # The ending is tail-aligned: it starts (total - its length) so its last note is the last frame.
    ending_start = max(hook_end + OPENING_TAIL, total - ending_len)
    sections = {
        "opening": (0.0, min(total, hook_end + OPENING_TAIL)),
        "middle": (min(total, hook_end), min(total, ending_start + FADE)),
        "ending": (ending_start, total),
    }
    fades = {"opening": (0.0, OPENING_TAIL), "middle": (MIDDLE_FADE_IN, FADE), "ending": (FADE, END_FADE)}
    gains: dict[str, float] = {}
    for name, (start, end) in sections.items():
        n = int((end - start) * SR)
        if n <= int(0.05 * SR):
            continue
        source = tracks[name]
        segment = _tail(source, n) if name == "ending" else _fit_length(source, n)
        gain_db = target_db - _music_level_db(source)
        gains[name] = round(gain_db, 2)
        fade_in, fade_out = fades[name]
        segment = _fade(segment * (10 ** (gain_db / 20)), fade_in, fade_out)
        s = int(start * SR)
        bed[s:s + len(segment)] += segment[: length - s]
    env = _envelope(voice_track)
    bed *= (10 ** (-DUCK_EXTRA_DB * env / 20))[:, None]
    whoosh = _whoosh()
    for scene in scenes[1:]:
        s = max(0, int((float(scene["start"]) - 0.15) * SR))
        e = min(length, s + len(whoosh))
        bed[s:e] += whoosh[: e - s]
    mixed = bed + voice_track[:, None]
    peak = float(np.max(np.abs(mixed))) or 1.0
    if peak > 0.98:
        mixed *= 0.98 / peak
    audio_dir = run_dir / "assets" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    def write(path: Path, data: np.ndarray) -> None:
        pcm = np.clip(data, -1, 1)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", str(pcm.shape[1] if pcm.ndim == 2 else 1), "-i", "-",
                        "-c:a", "pcm_s16le", str(path)], input=pcm.astype(np.float32).tobytes(), check=True, timeout=300)

    write(audio_dir / "narration-track.wav", voice_track[:, None])
    write(audio_dir / "music-bed.wav", bed)
    write(audio_dir / "premix.wav", mixed)
    final = audio_dir / "final-mix.wav"
    _loudnorm(audio_dir / "premix.wav", final)
    final_measure = measure(final)
    bed_active = bed.mean(axis=1)[env > 0.9]
    gap = voice_db - _rms_db(bed_active) if bed_active.size else float("nan")
    report = {
        "version": C.V6_VERSION,
        "voice_active_rms_db": round(voice_db, 2),
        "music_target_under_voice_db": C.MIX_MUSIC_UNDER_VOICE_DB,
        "music_under_voice_while_speaking_db": round(gap, 2),
        "section_gains_db": gains,
        "sections": {k: [round(a, 3), round(b, 3)] for k, (a, b) in sections.items()},
        "alignment": {"middle_starts_with_first_story": round(hook_end, 3), "ending_track_seconds": round(ending_len, 3),
                      "ending_tail_aligned": abs((total - ending_start) - ending_len) < 0.05},
        "final": final_measure,
        "status": "pass" if abs(final_measure["lufs"] - C.MIX_TARGET_LUFS) <= 1.0 and final_measure["true_peak"] <= C.MIX_TRUE_PEAK + 0.5 and C.MIX_MUSIC_UNDER_VOICE_DB - 2 <= gap <= C.MIX_MUSIC_UNDER_VOICE_DB + DUCK_EXTRA_DB + 6 else "fail",
    }
    (run_dir / "artifacts" / "mix_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
