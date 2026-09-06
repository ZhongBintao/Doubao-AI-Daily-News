from __future__ import annotations

import json
import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
import sys
from array import array
from pathlib import Path
from typing import Any, Iterable, Mapping


class MediaError(RuntimeError):
    """Raised when a local audio/video contract cannot be produced."""


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run_command(args: list[str], *, cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError(f"command failed to start or timed out: {args[0]}") from exc


def media_duration(path: Path) -> float:
    result = run_command([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ], timeout=30)
    if result.returncode != 0:
        raise MediaError(f"ffprobe could not read {path}: {(result.stderr or '')[-300:]}")
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise MediaError(f"ffprobe returned no duration for {path}") from exc


def _audio_files(project_dir: Path, segment_ids: Iterable[str]) -> list[Path]:
    paths = [project_dir / "assets" / "audio" / f"narration-{segment_id}.wav" for segment_id in segment_ids]
    missing = [str(path) for path in paths if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise MediaError(f"missing narration audio: {', '.join(missing)}")
    return paths


def concat_narration(project_dir: Path, segment_ids: Iterable[str]) -> Path:
    audio_paths = _audio_files(project_dir, segment_ids)
    output = project_dir / "assets" / "audio" / "narration-track.wav"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent, suffix=".txt", delete=False) as handle:
        list_path = Path(handle.name)
        for path in audio_paths:
            handle.write(f"file '{path.as_posix().replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n")
    try:
        result = run_command([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "1", str(output),
        ], timeout=300)
    finally:
        list_path.unlink(missing_ok=True)
    if result.returncode != 0 or not output.is_file():
        raise MediaError(f"could not concatenate narration: {(result.stderr or '')[-500:]}")
    return output


MUSIC_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "music"
MUSIC_SOURCES = {
    "opening": MUSIC_ASSET_DIR / "opening.mp3",
    "middle": MUSIC_ASSET_DIR / "middle-loop.mp3",
    "ending": MUSIC_ASSET_DIR / "ending.mp3",
}
# The previous template intentionally buried the music ~18 dB below speech.
# The revised brief uses an equal-loudness baseline and a very light sidechain
# so the bed remains audible without masking consonants.
MUSIC_TARGET_GAP_DB = 0.0
MUSIC_TARGET_GAP_LU = 0.0
MUSIC_DUCK_WINDOW_SECONDS = 0.02
MUSIC_DUCK_RECOVERY_SECONDS = 0.3
MUSIC_SIDECHAIN_ATTACK_SECONDS = 0.03
MUSIC_SIDECHAIN_RELEASE_SECONDS = 0.35
MUSIC_SIDECHAIN_MAX_DB = 4.0
MUSIC_MIN_PLAYED_DBFS = -45.0
# Keep a small audible floor for very quiet/padded narration sections while
# preserving the plugin's requested voice-to-music gap whenever it is louder.
MUSIC_MIN_HEADROOM_DB = 1.0
MUSIC_FADE_SECONDS = 0.5


def resolve_music_assets(project_dir: Path) -> dict[str, Path]:
    """Copy the pinned Publication Podcast Studio assets into the run."""

    output_dir = project_dir / "assets" / "music"
    output_dir.mkdir(parents=True, exist_ok=True)
    resolved: dict[str, Path] = {}
    for name, source in MUSIC_SOURCES.items():
        if not source.is_file() or source.stat().st_size == 0:
            raise MediaError(f"missing pinned music asset: {source}")
        output = output_dir / source.name
        shutil.copy2(source, output)
        resolved[name] = output
    return resolved


def _make_sfx(project_dir: Path, *, kind: str) -> Path:
    audio_dir = project_dir / "assets" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    if kind == "whoosh":
        output = audio_dir / "transition-whoosh.wav"
        result = run_command([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anoisesrc=color=violet:amplitude=0.22:duration=0.58:sample_rate=48000",
            "-af", "highpass=f=650,lowpass=f=5200,afade=t=in:st=0:d=0.08,afade=t=out:st=0.24:d=0.34,volume=0.55",
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "1", str(output),
        ], timeout=60)
    else:
        output = audio_dir / "category-chime.wav"
        expression = "0.18*sin(2*PI*783.99*t)*exp(-4*t)+0.12*sin(2*PI*1174.66*t)*exp(-5*t)+0.08*sin(2*PI*1567.98*t)*exp(-6*t)"
        result = run_command([
            "ffmpeg", "-y", "-f", "lavfi", "-i", f"aevalsrc={expression}:s=48000:d=0.75",
            "-af", "lowpass=f=3500,afade=t=out:st=0.16:d=0.59,volume=0.45",
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "1", str(output),
        ], timeout=60)
    if result.returncode != 0 or not output.is_file():
        raise MediaError(f"could not create {kind} sound effect: {(result.stderr or '')[-500:]}")
    return output


def make_audio_assets(project_dir: Path, duration: float) -> dict[str, Path]:
    return {
        **resolve_music_assets(project_dir),
        "whoosh": _make_sfx(project_dir, kind="whoosh"),
        "chime": _make_sfx(project_dir, kind="chime"),
    }


def _decode_pcm(path: Path, *, sample_rate: int = 48000, channels: int = 2) -> array:
    result = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ar", str(sample_rate), "-ac", str(channels), "pipe:1",
    ], capture_output=True, timeout=300, check=False)
    if result.returncode != 0:
        raise MediaError(f"could not decode audio {path.name}: {(result.stderr or b'')[-500:]}")
    samples = array("h")
    samples.frombytes(result.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _read_wav_pcm(path: Path, *, sample_rate: int = 48000, channels: int = 2) -> array:
    result = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ar", str(sample_rate), "-ac", str(channels), "pipe:1",
    ], capture_output=True, timeout=300, check=False)
    if result.returncode != 0:
        raise MediaError(f"could not decode audio {path.name}: {(result.stderr or b'')[-500:]}")
    samples = array("h")
    samples.frombytes(result.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _pcm_rms_dbfs(samples: array) -> float:
    if not samples:
        return float("-inf")
    try:
        import numpy as np
        values = np.frombuffer(samples.tobytes(), dtype=np.int16).astype(np.float64)
        rms = float(np.sqrt(np.mean(values * values))) if values.size else 0.0
    except Exception:
        rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    return float("-inf") if rms <= 0 else 20 * math.log10(rms / 32767.0)


def _measure_loudness(path: Path) -> dict[str, float]:
    """Measure integrated LUFS and true peak through ffmpeg's EBU R128 filter."""

    result = run_command([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
        "-f", "null", "-",
    ], timeout=300)
    if result.returncode != 0:
        raise MediaError(f"could not measure loudness for {path.name}: {(result.stderr or '')[-300:]}")
    match = re.search(r"\{\s*\"input_i\".*?\n\}", result.stderr or "", flags=re.DOTALL)
    if not match:
        raise MediaError(f"ffmpeg did not return EBU R128 metrics for {path.name}")
    try:
        value = json.loads(match.group(0))
        return {
            "lufs": float(value.get("input_i")),
            "true_peak_dbfs": float(value.get("input_tp")),
            "lra": float(value.get("input_lra")),
        }
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaError(f"invalid EBU R128 metrics for {path.name}") from exc


def _measure_pcm_loudness(samples: array, *, sample_rate: int = 48000) -> dict[str, float]:
    if not samples:
        return {"lufs": float("-inf"), "true_peak_dbfs": float("-inf"), "lra": 0.0}
    with tempfile.NamedTemporaryFile(suffix=".wav", dir=MUSIC_ASSET_DIR.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        _write_pcm_stereo(temporary, samples, sample_rate=sample_rate)
        return _measure_loudness(temporary)
    finally:
        temporary.unlink(missing_ok=True)


def _scale_samples(samples: array, gain_db: float) -> array:
    gain = 10 ** (float(gain_db) / 20.0)
    if not math.isfinite(gain) or gain < 0:
        raise MediaError("music gain must be a finite non-negative level")
    return array("h", (max(-32768, min(32767, round(sample * gain))) for sample in samples))


def _repeat_samples(samples: array, sample_count: int) -> array:
    if not samples or sample_count <= 0:
        return array("h")
    repeats = math.ceil(sample_count / len(samples))
    return array("h", (samples * repeats)[:sample_count])


def _add_section(target: array, source: array, *, start_frame: int, end_frame: int, gain_db: float, fade_in: float = 0.0, fade_out: float = 0.0, sample_rate: int = 48000, channels: int = 2) -> None:
    frame_count = max(0, end_frame - start_frame)
    if frame_count <= 0:
        return
    fade_in_frames = min(frame_count, round(fade_in * sample_rate))
    fade_out_frames = min(frame_count, round(fade_out * sample_rate))
    try:
        import numpy as np
        source_values = np.frombuffer(source.tobytes(), dtype=np.int16)
        if source_values.size:
            source_values = np.resize(source_values, frame_count * channels)
        else:
            source_values = np.zeros(frame_count * channels, dtype=np.int16)
        envelope = np.ones(frame_count, dtype=np.float64)
        if fade_in_frames:
            envelope = np.minimum(envelope, np.arange(frame_count, dtype=np.float64) / fade_in_frames)
        if fade_out_frames:
            envelope = np.minimum(envelope, (frame_count - np.arange(frame_count, dtype=np.float64) - 1) / fade_out_frames)
        gain = 10 ** (float(gain_db) / 20.0)
        delta = np.rint(source_values.reshape(frame_count, channels) * envelope[:, None] * gain)
        destination = np.frombuffer(target, dtype=np.int16).reshape(-1, channels)
        destination[start_frame:end_frame, :] = np.clip(destination[start_frame:end_frame, :].astype(np.int32) + delta, -32768, 32767).astype(np.int16)
        return
    except Exception:
        scaled = _scale_samples(_repeat_samples(source, frame_count * channels), gain_db)
    for frame in range(frame_count):
        envelope = 1.0
        if fade_in_frames:
            envelope = min(envelope, frame / fade_in_frames)
        if fade_out_frames:
            envelope = min(envelope, (frame_count - frame - 1) / fade_out_frames)
        destination = (start_frame + frame) * channels
        source_index = frame * channels
        for channel in range(channels):
            value = target[destination + channel] + round(scaled[source_index + channel] * max(0.0, min(1.0, envelope)))
            target[destination + channel] = max(-32768, min(32767, value))


def _peak_protected_mix(voice: array, background: array, *, sample_rate: int = 48000, channels: int = 2) -> tuple[array, dict[str, Any]]:
    if len(voice) != len(background):
        raise MediaError("voice and background tracks must have identical lengths")
    window_frames = max(1, round(sample_rate * MUSIC_DUCK_WINDOW_SECONDS))
    recovery_frames = max(1, round(sample_rate * MUSIC_DUCK_RECOVERY_SECONDS))
    scales = [1.0] * (len(voice) // channels)
    scale = 1.0
    minimum_scale = 1.0
    ducked_windows = 0
    for start in range(0, len(scales), window_frames):
        end = min(len(scales), start + window_frames)
        limit = 1.0
        for frame in range(start, end):
            for channel in range(channels):
                voice_sample = int(voice[frame * channels + channel])
                background_sample = int(background[frame * channels + channel])
                if background_sample > 0:
                    candidate = (32766 - voice_sample) / background_sample
                elif background_sample < 0:
                    candidate = (-32767 - voice_sample) / background_sample
                else:
                    candidate = 1.0
                limit = min(limit, max(0.0, min(1.0, candidate)))
        scale = min(limit, scale + ((end - start) / recovery_frames)) if limit >= scale else limit
        scales[start:end] = [scale] * (end - start)
        minimum_scale = min(minimum_scale, scale)
        if scale < 0.999999:
            ducked_windows += 1
    mixed = array("h")
    newly_clipped = 0
    peak = 0
    for index, (voice_sample, background_sample) in enumerate(zip(voice, background)):
        value = round(voice_sample + background_sample * scales[index // channels])
        if value in {-32768, 32767} and value != voice_sample:
            newly_clipped += 1
        value = max(-32768, min(32767, value))
        peak = max(peak, abs(value))
        mixed.append(value)
    attenuation = 20 * math.log10(minimum_scale) if minimum_scale > 0 else None
    return mixed, {
        "window_ms": MUSIC_DUCK_WINDOW_SECONDS * 1000,
        "recovery_ms": MUSIC_DUCK_RECOVERY_SECONDS * 1000,
        "ducked_windows": ducked_windows,
        "background_scale": minimum_scale,
        "background_attenuation_db": attenuation,
        "mixed_peak_sample": peak,
        "newly_clipped_samples": newly_clipped,
    }


def _sidechain_background(voice: array, background: array, *, sample_rate: int = 48000, channels: int = 2) -> tuple[array, dict[str, Any]]:
    """Apply a speech-triggered sidechain with bounded, smoothed attenuation."""

    if len(voice) != len(background):
        raise MediaError("voice and background tracks must have identical lengths")
    block_frames = max(1, round(sample_rate * 0.01))
    attack_frames = max(1, round(sample_rate * MUSIC_SIDECHAIN_ATTACK_SECONDS))
    release_frames = max(1, round(sample_rate * MUSIC_SIDECHAIN_RELEASE_SECONDS))
    max_attenuation = -abs(MUSIC_SIDECHAIN_MAX_DB)
    scales = [1.0] * (len(voice) // channels)
    current_db = 0.0
    active_blocks = 0
    minimum_db = 0.0
    for start in range(0, len(scales), block_frames):
        end = min(len(scales), start + block_frames)
        values: list[int] = []
        for frame in range(start, end):
            values.extend(int(voice[frame * channels + channel]) for channel in range(channels))
        voice_level = _pcm_rms_dbfs(array("h", values))
        target_db = max_attenuation if math.isfinite(voice_level) and voice_level > -42.0 else 0.0
        if target_db < current_db:
            current_db = max(target_db, current_db - (abs(max_attenuation) * (end - start) / attack_frames))
            active_blocks += 1
        else:
            current_db = min(target_db, current_db + (abs(max_attenuation) * (end - start) / release_frames))
        minimum_db = min(minimum_db, current_db)
        scale = 10 ** (current_db / 20.0)
        scales[start:end] = [scale] * (end - start)
    try:
        import numpy as np
        background_values = np.frombuffer(background.tobytes(), dtype=np.int16).astype(np.float64)
        scale_values = np.repeat(np.asarray(scales, dtype=np.float64), channels)
        ducked_values = np.clip(np.rint(background_values * scale_values), -32768, 32767).astype(np.int16)
        ducked = array("h", ducked_values.tolist())
    except Exception:
        ducked = array("h")
        for index, sample in enumerate(background):
            ducked.append(max(-32768, min(32767, round(sample * scales[index // channels]))))
    return ducked, {
        "attack_ms": MUSIC_SIDECHAIN_ATTACK_SECONDS * 1000,
        "release_ms": MUSIC_SIDECHAIN_RELEASE_SECONDS * 1000,
        "max_duck_db": round(abs(minimum_db), 4),
        "minimum_background_scale": round(min(scales) if scales else 1.0, 6),
        "active_blocks": active_blocks,
    }


def _mix_with_headroom(voice: array, background: array, *, channels: int = 2) -> tuple[array, dict[str, Any]]:
    """Sum two stems and scale the sum (never the voice stem) if needed."""

    if len(voice) != len(background):
        raise MediaError("voice and background tracks must have identical lengths")
    try:
        import numpy as np
        voice_values = np.frombuffer(voice.tobytes(), dtype=np.int16).astype(np.int32)
        background_values = np.frombuffer(background.tobytes(), dtype=np.int16).astype(np.int32)
        raw_values = voice_values + background_values
        raw_peak = int(np.max(np.abs(raw_values))) if raw_values.size else 0
    except Exception:
        raw_values = None
        raw = [int(voice_sample) + int(background_sample) for voice_sample, background_sample in zip(voice, background)]
        raw_peak = max((abs(value) for value in raw), default=0)
    scale = min(1.0, 32760.0 / raw_peak) if raw_peak else 1.0
    if raw_values is not None:
        mixed_values = np.clip(np.rint(raw_values * scale), -32768, 32767).astype(np.int16)
        mixed = array("h", mixed_values.tolist())
    else:
        mixed = array("h", (max(-32768, min(32767, round(value * scale))) for value in raw))
    return mixed, {
        "raw_peak_sample": raw_peak,
        "headroom_scale": scale,
        "headroom_gain_db": round(20 * math.log10(scale), 4) if scale > 0 else None,
        "newly_clipped_samples": 0,
        "mixed_peak_sample": max((abs(value) for value in mixed), default=0),
    }


def _normalize_final_mix(path: Path) -> None:
    """Normalize the rendered mix to a podcast-safe -16 LUFS / -1.5 dBTP."""

    temporary = path.with_name(f".{path.stem}-loudnorm.wav")
    result = run_command([
        "ffmpeg", "-y", "-v", "error", "-i", str(path),
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(temporary),
    ], timeout=300)
    if result.returncode != 0 or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        raise MediaError(f"could not normalize final mix: {(result.stderr or '')[-300:]}")
    os.replace(temporary, path)


def _write_pcm_stereo(path: Path, samples: array, *, sample_rate: int = 48000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    result = subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", str(sample_rate), "-ac", "2", "-i", "pipe:0", "-c:a", "pcm_s16le", "-f", "wav", str(temporary),
    ], input=samples.tobytes(), capture_output=True, timeout=300, check=False)
    if result.returncode != 0 or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        raise MediaError(f"could not write PCM audio: {(result.stderr or b'')[-500:]}")
    os.replace(temporary, path)


def mix_audio(project_dir: Path, narration: Path, music_assets: Mapping[str, Path], events: Iterable[Mapping[str, Any]] = (), section_boundaries: Mapping[str, float] | None = None) -> Path:
    """Mix three music sections at equal loudness with bounded sidechain ducking."""

    output = project_dir / "assets" / "audio" / "final-mix.wav"
    output.parent.mkdir(parents=True, exist_ok=True)
    duration = media_duration(narration)
    sample_rate = 48000
    channels = 2
    frame_count = max(1, round(duration * sample_rate))
    voice = _read_wav_pcm(narration, sample_rate=sample_rate, channels=channels)
    voice = array("h", voice[: frame_count * channels])
    if len(voice) < frame_count * channels:
        voice.extend([0] * (frame_count * channels - len(voice)))
    opening = _decode_pcm(Path(music_assets["opening"]), sample_rate=sample_rate, channels=channels)
    middle = _decode_pcm(Path(music_assets["middle"]), sample_rate=sample_rate, channels=channels)
    ending = _decode_pcm(Path(music_assets["ending"]), sample_rate=sample_rate, channels=channels)
    boundaries = dict(section_boundaries or {})
    intro_end = max(0.0, min(duration, float(boundaries.get("intro_end", duration * 0.12))))
    middle_start = max(intro_end, min(duration, float(boundaries.get("middle_start", intro_end))))
    ending_start = max(middle_start, min(duration, float(boundaries.get("ending_start", max(middle_start, duration - 3.0)))))
    frame_boundaries = {
        "opening": (0, round(intro_end * sample_rate)),
        "middle": (round(middle_start * sample_rate), round(ending_start * sample_rate)),
        "ending": (round(ending_start * sample_rate), frame_count),
    }
    raw_rms = {"opening": _pcm_rms_dbfs(opening), "middle": _pcm_rms_dbfs(middle), "ending": _pcm_rms_dbfs(ending)}
    # Section balancing is performed on already-decoded PCM. RMS in a fixed
    # channel/sample format is a stable LU proxy for the gain decision and
    # avoids launching a full-duration ffmpeg meter for every small slice.
    # The final rendered mix is still measured with ffmpeg loudnorm/EBU R128.
    raw_lufs = {name: _pcm_rms_dbfs(source) for name, source in (("opening", opening), ("middle", middle), ("ending", ending))}
    played_source_rms: dict[str, float] = {}
    section_voice_lufs: dict[str, float | None] = {}
    section_music_lufs_pre: dict[str, float | None] = {}
    section_gains: dict[str, float] = {}
    background = array("h", [0]) * (frame_count * channels)
    for name, source in (("opening", opening), ("middle", middle), ("ending", ending)):
        start_frame, end_frame = frame_boundaries[name]
        voice_slice = voice[start_frame * channels:end_frame * channels]
        voice_lufs = _pcm_rms_dbfs(voice_slice) if voice_slice else float("-inf")
        if not math.isfinite(voice_lufs):
            voice_lufs = -18.0
        section_voice_lufs[name] = voice_lufs
        section_frames = max(0, end_frame - start_frame)
        source_window = array("h", [0]) * (section_frames * channels)
        _add_section(
            source_window, source, start_frame=0, end_frame=section_frames, gain_db=0.0,
            fade_in=MUSIC_FADE_SECONDS, fade_out=MUSIC_FADE_SECONDS,
            sample_rate=sample_rate, channels=channels,
        )
        source_rms = _pcm_rms_dbfs(source_window)
        played_source_rms[name] = source_rms
        source_lufs = _pcm_rms_dbfs(source_window) if source_window else float("-inf")
        source_lufs = source_lufs if math.isfinite(source_lufs) else -60.0
        target_music_lufs = voice_lufs + MUSIC_TARGET_GAP_LU
        # Some bundled beds (notably the middle loop) are mastered much
        # quieter than the opening/ending stems. Allow enough positive gain to
        # reach the declared 0 LU pre-duck target; the headroom/true-peak gate
        # below still protects the final mix from clipping.
        gain = max(-30.0, min(30.0, target_music_lufs - source_lufs))
        section_gains[name] = round(gain, 4)
        _add_section(
            background, source, start_frame=start_frame, end_frame=end_frame, gain_db=gain,
            fade_in=MUSIC_FADE_SECONDS if start_frame else MUSIC_FADE_SECONDS,
            fade_out=MUSIC_FADE_SECONDS,
            sample_rate=sample_rate, channels=channels,
        )
        played_lufs = _pcm_rms_dbfs(background[start_frame * channels:end_frame * channels]) if end_frame > start_frame else float("-inf")
        section_music_lufs_pre[name] = played_lufs if math.isfinite(played_lufs) else None
    event_list = [event for event in events if Path(str(event.get("path") or "")).is_file()]
    for event in event_list:
        source = _decode_pcm(Path(str(event["path"])), sample_rate=sample_rate, channels=channels)
        start_frame = max(0, min(frame_count, round(float(event.get("start", 0.0)) * sample_rate)))
        volume = max(0.0, min(1.0, float(event.get("volume", 0.2))))
        gain_db = -120.0 if volume <= 0 else 20 * math.log10(volume)
        _add_section(background, source, start_frame=start_frame, end_frame=min(frame_count, start_frame + round(len(source) / channels)), gain_db=gain_db, sample_rate=sample_rate, channels=channels)
    background_pre_duck = array("h", background)
    background, sidechain_report = _sidechain_background(voice, background, sample_rate=sample_rate, channels=channels)
    mixed, peak_report = _mix_with_headroom(voice, background, channels=channels)
    section_rms: dict[str, float | None] = {}
    for name, (start_frame, end_frame) in frame_boundaries.items():
        level = _pcm_rms_dbfs(background[start_frame * channels:end_frame * channels])
        section_rms[name] = round(level, 4) if math.isfinite(level) else None
        if not math.isfinite(level) or level <= MUSIC_MIN_PLAYED_DBFS:
            raise MediaError(f"played {name} music is at or below {MUSIC_MIN_PLAYED_DBFS:.0f} dBFS")
    if mixed == voice:
        raise MediaError("background music mix equals the voice track")
    if peak_report["newly_clipped_samples"]:
        raise MediaError("background music mix introduced PCM clipping")
    voice_path = output.parent / "narration-track.wav"
    background_path = output.parent / "background-music.wav"
    _write_pcm_stereo(voice_path, voice)
    _write_pcm_stereo(background_path, background)
    _write_pcm_stereo(output, mixed)
    _normalize_final_mix(output)
    final_measurement = _measure_loudness(output)
    section_music_lufs_post: dict[str, float | None] = {}
    for name, (start_frame, end_frame) in frame_boundaries.items():
        post_lufs = _pcm_rms_dbfs(background[start_frame * channels:end_frame * channels]) if end_frame > start_frame else float("-inf")
        section_music_lufs_post[name] = round(post_lufs, 4) if math.isfinite(post_lufs) else None
    voice_lufs_values = [value for value in section_voice_lufs.values() if value is not None and math.isfinite(value)]
    music_pre_values = [value for value in section_music_lufs_pre.values() if value is not None and math.isfinite(value)]
    pre_gap = (sum(voice_lufs_values) / len(voice_lufs_values) - sum(music_pre_values) / len(music_pre_values)) if voice_lufs_values and music_pre_values else None
    equal_loudness_pass = bool(section_voice_lufs) and all(
        section_voice_lufs[name] is not None and section_music_lufs_pre[name] is not None and abs(float(section_voice_lufs[name]) - float(section_music_lufs_pre[name])) <= 0.5
        for name in frame_boundaries
    )
    mix_lufs = float(final_measurement["lufs"])
    true_peak = float(final_measurement["true_peak_dbfs"])
    quality_pass = bool(equal_loudness_pass and math.isfinite(mix_lufs) and abs(mix_lufs - (-16.0)) <= 0.5 and math.isfinite(true_peak) and true_peak <= -1.5 + 0.15 and sidechain_report["max_duck_db"] <= MUSIC_SIDECHAIN_MAX_DB + 0.05 and peak_report["newly_clipped_samples"] == 0)
    timeline = {
        "schema_version": "2.0",
        "provider": "publication_podcast_studio_local_pcm",
        "clock": {"sample_rate": sample_rate, "channels": channels, "total_frames": frame_count, "total_seconds": round(frame_count / sample_rate, 7)},
        "metering": {"section_method": "fixed-format PCM RMS LU proxy", "final_method": "ffmpeg loudnorm EBU R128", "section_gain_decision_is_same-format": True},
        "rules": {"target_background_gap_db": MUSIC_TARGET_GAP_DB, "target_background_gap_lu": MUSIC_TARGET_GAP_LU, "duck_window_ms": MUSIC_DUCK_WINDOW_SECONDS * 1000, "duck_recovery_ms": MUSIC_DUCK_RECOVERY_SECONDS * 1000, "sidechain_attack_ms": MUSIC_SIDECHAIN_ATTACK_SECONDS * 1000, "sidechain_release_ms": MUSIC_SIDECHAIN_RELEASE_SECONDS * 1000, "sidechain_max_duck_db": MUSIC_SIDECHAIN_MAX_DB, "transition_fade_seconds": MUSIC_FADE_SECONDS, "voice_lead_in_seconds": 0.0, "ending_lead_seconds": round(duration - ending_start, 7)},
        "placements": {name: {"start_seconds": round(start / sample_rate, 7), "end_seconds": round(end / sample_rate, 7), "start_frame": start, "end_frame": end} for name, (start, end) in frame_boundaries.items()},
        "gains_db": section_gains,
        "source_rms_dbfs": {name: round(level, 4) if math.isfinite(level) else None for name, level in raw_rms.items()},
        "source_lufs": {name: round(level, 4) if math.isfinite(level) else None for name, level in raw_lufs.items()},
        "played_source_rms_dbfs": {name: round(level, 4) if math.isfinite(level) else None for name, level in played_source_rms.items()},
        "played_rms_dbfs": section_rms,
        "loudness": {"voice_lufs": {name: round(value, 4) if value is not None else None for name, value in section_voice_lufs.items()}, "music_lufs_pre_duck": {name: round(value, 4) if value is not None else None for name, value in section_music_lufs_pre.items()}, "music_lufs_post_duck": section_music_lufs_post, "pre_duck_gap_lu": round(pre_gap, 4) if pre_gap is not None else None, "mix_lufs": round(mix_lufs, 4), "true_peak_dbfs": round(true_peak, 4)},
        "sidechain": sidechain_report,
        "peak_protection": peak_report,
        "quality_gate": {"status": "passed" if quality_pass else "failed", "minimum_played_music_dbfs": MUSIC_MIN_PLAYED_DBFS, "equal_loudness_tolerance_lu": 0.5, "equal_loudness_pass": equal_loudness_pass, "mix_lufs_target": -16.0, "mix_lufs_tolerance": 0.5, "true_peak_limit_dbfs": -1.5, "mixed_differs_from_voice": True, "newly_clipped_samples": 0},
        "inputs": {name: {"path": str(Path(music_assets[name]).name), "sha256": hashlib.sha256(Path(music_assets[name]).read_bytes()).hexdigest()} for name in ("opening", "middle", "ending")},
    }
    write_json(project_dir / "artifacts" / "background-music.json", timeline)
    return output


def _srt_timestamp(seconds: float) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, ms = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


_SUBTITLE_ASCII_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+#/-]*")
_SUBTITLE_HARD_BREAKS = set("。！？!?")
_SUBTITLE_SOFT_BREAKS = set("，,、；;：:")
_SUBTITLE_PUNCTUATION = _SUBTITLE_HARD_BREAKS | _SUBTITLE_SOFT_BREAKS | set("\"'（）()[]【】")


def _subtitle_units(text: str) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    index = 0
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        match = _SUBTITLE_ASCII_RE.match(text, index)
        if match:
            # Keep a single separator before an ASCII token so the visible
            # caption remains the authored broadcast text (e.g. ``128K 上下文``)
            # while timing still weights only spoken characters.
            prefix = " " if index > 0 and text[index - 1].isspace() else ""
            value = prefix + match.group(0)
            units.append({"text": value, "weight": max(1, len(match.group(0))), "punct": False})
            index = match.end()
            continue
        prefix = " " if index > 0 and text[index - 1].isspace() else ""
        value = prefix + text[index]
        units.append({"text": value, "weight": 1, "punct": value.strip() in _SUBTITLE_PUNCTUATION})
        index += 1
    return units


def _word_anchors(words: list[Mapping[str, Any]], duration: float) -> list[tuple[float, float]]:
    anchors: list[tuple[float, float]] = []
    for word in words:
        raw = str(word.get("word") or word.get("text") or "").strip()
        if not raw:
            continue
        chars = [char for char in raw if not char.isspace()]
        if not chars:
            continue
        start = max(0.0, min(duration, float(word.get("start", 0.0))))
        end = max(start + 0.04, min(duration, float(word.get("end", start + 0.2))))
        step = (end - start) / len(chars)
        anchors.extend((start + step * index, start + step * (index + 1)) for index in range(len(chars)))
    return anchors


def _timed_units(text: str, words: list[Mapping[str, Any]], duration: float) -> list[dict[str, Any]]:
    units = _subtitle_units(text)
    if not units:
        return []
    anchors = _word_anchors(words, duration)
    # Azure occasionally collapses the tail of a short utterance onto one
    # timestamp (notably when a segment is padded with silence).  Using those
    # anchors verbatim would give the final clause zero duration and silently
    # drop it from the SRT.  Fall back to deterministic proportional timing
    # whenever the alignment has too few distinct character anchors.
    distinct_anchors = {(round(start, 3), round(end, 3)) for start, end in anchors}
    if not anchors or len(distinct_anchors) < max(3, int(len(units) * 0.6)):
        anchors = [(duration * index / max(1, sum(unit["weight"] for unit in units)), duration * (index + 1) / max(1, sum(unit["weight"] for unit in units))) for index in range(max(1, sum(unit["weight"] for unit in units)))]
    total_weight = max(1, sum(int(unit["weight"]) for unit in units))
    total_anchors = len(anchors)
    cursor = 0
    timed: list[dict[str, Any]] = []
    for unit in units:
        weight = int(unit["weight"])
        start_index = min(total_anchors - 1, int(round(cursor / total_weight * total_anchors)))
        end_index = min(total_anchors, max(start_index + 1, int(round((cursor + weight) / total_weight * total_anchors))))
        timed.append({**unit, "start": anchors[start_index][0], "end": anchors[end_index - 1][1]})
        cursor += weight
    return timed


def _visible_weight(units: Iterable[Mapping[str, Any]]) -> int:
    return sum(max(0, int(unit.get("weight", 0))) for unit in units)


def _split_long_clause(clause: list[dict[str, Any]], maximum: int) -> list[list[dict[str, Any]]]:
    """Split at soft punctuation when possible, keeping phrases readable."""

    if _visible_weight(clause) <= maximum:
        return [clause]
    pieces: list[list[dict[str, Any]]] = []
    remaining = list(clause)
    while remaining:
        if _visible_weight(remaining) <= maximum:
            pieces.append(remaining)
            break
        weight = 0
        last_fit = 0
        last_soft_break = 0
        for index, unit in enumerate(remaining):
            unit_weight = int(unit.get("weight", 1))
            if weight + unit_weight > maximum:
                break
            weight += unit_weight
            last_fit = index + 1
            if str(unit.get("text") or "").strip() in _SUBTITLE_SOFT_BREAKS:
                last_soft_break = index + 1
        split_at = last_soft_break or last_fit or 1
        pieces.append(remaining[:split_at])
        remaining = remaining[split_at:]
    return pieces


def _merge_orphan_phrases(phrases: list[list[dict[str, Any]]], maximum: int) -> list[list[dict[str, Any]]]:
    """Fold short tails back into their neighbour without crossing sentences."""

    if len(phrases) < 2:
        return phrases
    merged = [list(phrase) for phrase in phrases]
    changed = True
    while changed and len(merged) > 1:
        changed = False
        for index in range(1, len(merged)):
            previous, current = merged[index - 1], merged[index]
            tail = _visible_weight(current)
            previous_punctuation = str(previous[-1].get("text", "")).strip()
            if tail <= 3 and previous_punctuation not in _SUBTITLE_HARD_BREAKS and _visible_weight(previous) + tail <= maximum:
                merged[index - 1] = previous + current
                del merged[index]
                changed = True
                break
    return merged


def _phrase_text(phrase: list[dict[str, Any]], previous: dict[str, Any] | None, *, final: bool) -> str:
    raw = "".join(str(unit.get("text") or "") for unit in phrase)
    if final:
        raw = raw.rstrip()
    return raw


def _phrase_cues(text: str, words: list[Mapping[str, Any]], offset: float, duration: float) -> list[dict[str, Any]]:
    timed = _timed_units(text.strip(), words, duration)
    if not timed:
        return []
    maximum = 28
    clauses: list[list[dict[str, Any]]] = []
    current_clause: list[dict[str, Any]] = []
    for unit in timed:
        current_clause.append(unit)
        punctuation = str(unit.get("text") or "").strip()
        if punctuation in _SUBTITLE_HARD_BREAKS:
            clauses.append(current_clause)
            current_clause = []
    if current_clause:
        clauses.append(current_clause)
    clauses = [piece for clause in clauses for piece in _split_long_clause(clause, maximum)]
    phrases: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for clause_index, clause in enumerate(clauses):
        if not current:
            current = list(clause)
            continue
        combined_weight = _visible_weight(current) + _visible_weight(clause)
        previous_punctuation = str(current[-1].get("text", "")).strip()
        # A short clause such as “星期五，” is kept with its next clause;
        # otherwise each comma becomes a distracting one- or two-word card.
        if previous_punctuation in _SUBTITLE_SOFT_BREAKS and _visible_weight(current) < 5 and combined_weight <= maximum:
            current.extend(clause)
        else:
            phrases.append(current)
            current = list(clause)
    if current:
        phrases.append(current)
    phrases = _merge_orphan_phrases(phrases, maximum)
    cues: list[dict[str, Any]] = []
    for index, phrase in enumerate(phrases):
        previous = cues[-1] if cues else None
        phrase_text = _phrase_text(phrase, previous, final=index == len(phrases) - 1)
        stripped_text = phrase_text.strip()
        if stripped_text and stripped_text[0] in _SUBTITLE_PUNCTUATION and previous is not None:
            previous["text"] = str(previous.get("text") or "") + stripped_text[0]
            phrase_text = phrase_text.replace(stripped_text[0], "", 1)
            stripped_text = phrase_text.strip()
        if stripped_text and all(char in _SUBTITLE_PUNCTUATION for char in stripped_text):
            if previous is not None:
                previous["text"] = str(previous.get("text") or "") + phrase_text
                previous["end"] = max(float(previous["end"]), offset + float(phrase[-1]["end"]))
            continue
        if not stripped_text:
            continue
        start = max(0.0, float(phrase[0]["start"]))
        end = min(duration, float(phrase[-1]["end"]))
        cues.append({"start": offset + start, "end": offset + end, "text": phrase_text})
    return cues


def _caption_unit_cues(
    units: list[Mapping[str, Any]],
    words: list[Mapping[str, Any]],
    offset: float,
    duration: float,
) -> list[dict[str, Any]]:
    """Time authored one-sentence caption units without re-splitting them.

    Azure offsets refer to ``spoken_text`` while the cue text is the reviewed
    ``display_text``.  Map each unit by its spoken character span; if a
    provider omits usable offsets, use deterministic proportional timing.
    """

    clean_units = [unit for unit in units if str(unit.get("display_text") or "").strip()]
    if not clean_units:
        return []
    spoken_cursor = 0
    spans: list[tuple[int, int]] = []
    for unit in clean_units:
        spoken = str(unit.get("spoken_text") or unit.get("display_text") or "")
        start = spoken_cursor
        spoken_cursor += len(spoken)
        spans.append((start, spoken_cursor))
    valid_words: list[tuple[int, int, float, float]] = []
    for word in words:
        try:
            start_offset = int(word.get("text_offset"))
            word_length = int(word.get("word_length"))
            start = float(word.get("start", word.get("start_seconds", 0.0)))
            end = float(word.get("end", word.get("end_seconds", start)))
        except (TypeError, ValueError):
            continue
        if start_offset < 0 or word_length <= 0 or end <= start:
            continue
        valid_words.append((start_offset, start_offset + word_length, max(0.0, start), min(duration, end)))
    cues: list[dict[str, Any]] = []
    total_weight = max(1, sum(len(str(unit.get("spoken_text") or unit.get("display_text") or "")) for unit in clean_units))
    previous_end = 0.0
    for index, (unit, (start_char, end_char)) in enumerate(zip(clean_units, spans)):
        matched = [word for word in valid_words if word[1] > start_char and word[0] < end_char]
        if matched:
            start = min(word[2] for word in matched)
            end = max(word[3] for word in matched)
        else:
            prior_weight = sum(len(str(value.get("spoken_text") or value.get("display_text") or "")) for value in clean_units[:index])
            unit_weight = len(str(unit.get("spoken_text") or unit.get("display_text") or ""))
            start = duration * prior_weight / total_weight
            end = duration * (prior_weight + max(1, unit_weight)) / total_weight
        start = max(previous_end, max(0.0, min(duration, start)))
        end = min(duration, max(start + 0.04, end))
        if end <= start:
            end = min(duration, start + 0.04)
        if end <= start and start > 0:
            start = max(0.0, start - 0.04)
            end = min(duration, start + 0.04)
        cues.append({
            "start": offset + start,
            "end": offset + end,
            "text": str(unit.get("display_text") or "").strip(),
            "card_ids": [str(value) for value in unit.get("card_ids") or []],
            "beat_id": str(unit.get("beat_id") or ""),
            "claim_ids": [str(value) for value in unit.get("claim_ids") or []],
            "visual_asset_id": str(unit.get("visual_asset_id") or ""),
        })
        previous_end = end
    return cues


def write_subtitles(project_dir: Path, script: Mapping[str, Any], durations: Mapping[str, float], *, aligned: bool = True, spoken_durations: Mapping[str, float] | None = None) -> tuple[Path, list[dict[str, Any]]]:
    cues: list[dict[str, Any]] = []
    cursor = 0.0
    alignment_dir = project_dir / "artifacts" / "alignments"
    for segment in script.get("segments", []):
        segment_id = str(segment["id"])
        duration = float(durations.get(segment_id, 0.0))
        alignment_path = alignment_dir / f"{segment_id}.json"
        # Captions show the authored display copy. Provider-specific
        # pronunciation rewrites are used only for TTS and alignment.
        text = str(segment.get("display_text") or segment.get("broadcast_text") or "").strip()
        if not text or duration <= 0:
            cursor += duration
            continue
        speech_duration = float((spoken_durations or {}).get(segment_id, duration))
        speech_duration = max(0.05, min(duration, speech_duration))
        words: list[Mapping[str, Any]] = []
        if aligned:
            if not alignment_path.is_file():
                raise MediaError(f"subtitle alignment is required for {segment_id}: {alignment_path}")
            data = json.loads(alignment_path.read_text(encoding="utf-8"))
            words = list(data.get("word_timestamps") or [])
            if not words:
                raise MediaError(f"subtitle alignment has no word timestamps for {segment_id}: {alignment_path}")
        caption_units = segment.get("caption_units") or []
        if isinstance(caption_units, list) and caption_units:
            cues.extend(_caption_unit_cues(caption_units, words, cursor, speech_duration))
        elif aligned:
            cues.extend(_phrase_cues(text, words, cursor, speech_duration))
        else:
            cues.extend(_phrase_cues(text, [], cursor, speech_duration))
        cursor += duration
    total_duration = cursor
    normalized: list[dict[str, Any]] = []
    previous_end = 0.0
    for cue in cues:
        text = str(cue.get("text") or "")
        visible_text = text.strip()
        if not visible_text or all(char in _SUBTITLE_PUNCTUATION for char in visible_text):
            continue
        start = max(previous_end, max(0.0, float(cue["start"])))
        end = min(max(0.0, float(cue["end"])), max(0.0, max(start, total_duration)))
        if end <= start:
            continue
        normalized.append({
            "index": len(normalized) + 1,
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text.rstrip(),
            "card_ids": [str(value) for value in cue.get("card_ids") or []],
            "beat_id": str(cue.get("beat_id") or ""),
            "claim_ids": [str(value) for value in cue.get("claim_ids") or []],
            "visual_asset_id": str(cue.get("visual_asset_id") or ""),
        })
        previous_end = end
    srt_path = project_dir / "assets" / "subtitles" / "subtitles.srt"
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for index, cue in enumerate(normalized, 1):
        lines.extend([str(index), f"{_srt_timestamp(cue['start'])} --> {_srt_timestamp(cue['end'])}", cue["text"], ""])
    srt_path.write_text("\n".join(lines), encoding="utf-8")
    return srt_path, normalized
