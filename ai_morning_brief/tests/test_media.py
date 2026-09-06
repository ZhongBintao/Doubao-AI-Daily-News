import json
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from ai_morning_brief.media import MediaError, make_audio_assets, mix_audio, write_subtitles


class SubtitleTests(unittest.TestCase):
    def test_authored_caption_units_are_not_split_at_commas(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alignment_dir = root / "artifacts" / "alignments"
            alignment_dir.mkdir(parents=True)
            spoken = "数据集包含 四千八百八十八位说话人。地区分布不均。"
            words = [
                {"word": spoken[:10], "text_offset": 0, "word_length": 10, "start": 0.0, "end": 1.0},
                {"word": spoken[10:], "text_offset": 10, "word_length": len(spoken) - 10, "start": 1.1, "end": 2.0},
            ]
            (alignment_dir / "story.json").write_text(json.dumps({"word_timestamps": words}, ensure_ascii=False), encoding="utf-8")
            script = {"segments": [{
                "id": "story",
                "display_text": "数据集包含 4888 位说话人。地区分布不均。",
                "spoken_text": spoken,
                "caption_units": [
                    {"display_text": "数据集包含 4888 位说话人。", "spoken_text": spoken[:18]},
                    {"display_text": "地区分布不均。", "spoken_text": spoken[18:]},
                ],
            }]}
            _, cues = write_subtitles(root, script, {"story": 2.5}, aligned=True)
            self.assertEqual([cue["text"] for cue in cues], ["数据集包含 4888 位说话人。", "地区分布不均。"])
            self.assertEqual(len(cues), 2)
            self.assertTrue(all(len(cue["text"]) <= 28 for cue in cues))

    def test_aligned_caption_units_follow_word_boundaries_not_text_proportions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alignment_dir = root / "artifacts" / "alignments"
            alignment_dir.mkdir(parents=True)
            text = "甲。乙。"
            words = [
                {"word": "甲", "text_offset": 0, "word_length": 1, "start": 0.80, "end": 1.00},
                {"word": "乙", "text_offset": 2, "word_length": 1, "start": 1.60, "end": 1.80},
            ]
            (alignment_dir / "story.json").write_text(json.dumps({"word_timestamps": words}, ensure_ascii=False), encoding="utf-8")
            script = {"segments": [{
                "id": "story",
                "display_text": text,
                "spoken_text": text,
                "caption_units": [
                    {"display_text": "甲。", "spoken_text": "甲。"},
                    {"display_text": "乙。", "spoken_text": "乙。"},
                ],
            }]}

            _, cues = write_subtitles(root, script, {"story": 3.0}, aligned=True, spoken_durations={"story": 2.0})

            self.assertEqual([(cue["start"], cue["end"]) for cue in cues], [(0.8, 1.0), (1.6, 1.8)])
            self.assertTrue(all(cue["end"] <= 2.0 for cue in cues))

    def test_aligned_subtitles_fail_when_alignment_file_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = {"segments": [{"id": "story", "display_text": "甲。", "spoken_text": "甲。"}]}

            with self.assertRaisesRegex(MediaError, "subtitle alignment is required"):
                write_subtitles(root, script, {"story": 1.0}, aligned=True)

    def test_canonical_text_wins_over_noisy_stt_and_respects_punctuation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alignment_dir = root / "artifacts" / "alignments"
            alignment_dir.mkdir(parents=True)
            words = [
                {"word": "这里是AI每日早报，", "start": 0.0, "end": 0.7},
                {"word": "今天带来两条消息。", "start": 0.8, "end": 1.55},
                {"word": "。Google推出新模型，", "start": 1.7, "end": 2.35},
                {"word": "性能更强。", "start": 2.45, "end": 3.2},
            ]
            (alignment_dir / "story.json").write_text(json.dumps({"word_timestamps": words}, ensure_ascii=False), encoding="utf-8")
            script = {"segments": [{"id": "story", "broadcast_text": "这里是AI每日早报，今天带来两条消息。Google推出新模型，性能更强。"}]}
            _, cues = write_subtitles(root, script, {"story": 3.5}, aligned=True)
            texts = [cue["text"] for cue in cues]
            self.assertEqual("".join(texts), script["segments"][0]["broadcast_text"])
            self.assertFalse(any(text[0] in "，。！？,!?" for text in texts if text))
            self.assertFalse(any(all(char in "，。！？,!?；;：:" for char in text) for text in texts))
            self.assertTrue(all(0.0 <= cue["start"] < cue["end"] <= 3.5 for cue in cues))
            self.assertTrue(all(cues[index]["end"] <= cues[index + 1]["start"] for index in range(len(cues) - 1)))

    def test_unaligned_text_is_split_into_readable_phrases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = {"segments": [{"id": "intro", "broadcast_text": "各位观众早上好，今天是8月28日，星期五，欢迎收看今天的AI早报。"}]}
            _, cues = write_subtitles(root, script, {"intro": 4.0}, aligned=False)
            self.assertGreaterEqual(len(cues), 2)
            self.assertTrue(all(len(cue["text"]) <= 28 for cue in cues))
            self.assertFalse(any(cue["text"].startswith(("，", "。", "！", "？")) for cue in cues))

    def test_collapsed_alignment_keeps_the_authored_tail_and_complete_phrases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alignment_dir = root / "artifacts" / "alignments"
            alignment_dir.mkdir(parents=True)
            words = [
                {"word": "首", "start": 0.0, "end": 0.4},
                {"word": "先", "start": 0.4, "end": 0.8},
                {"word": "来", "start": 0.8, "end": 1.2},
                {"word": "看", "start": 1.2, "end": 1.6},
                {"word": "今", "start": 1.6, "end": 2.0},
                {"word": "日", "start": 2.0, "end": 2.4},
                # The provider collapses the remainder onto one timestamp.
                {"word": "资", "start": 2.4, "end": 2.45},
                {"word": "讯概览，请看屏幕上的主要内容。", "start": 2.4, "end": 2.45},
            ]
            (alignment_dir / "overview.json").write_text(json.dumps({"word_timestamps": words}, ensure_ascii=False), encoding="utf-8")
            text = "首先来看今日资讯概览，请看屏幕上的主要内容。"
            script = {"segments": [{"id": "overview", "broadcast_text": text}]}
            _, cues = write_subtitles(root, script, {"overview": 4.0}, aligned=True)
            self.assertEqual("".join(cue["text"] for cue in cues), text)
            self.assertEqual(len(cues), 1)
            self.assertFalse(any(cue["text"].lstrip().startswith(("，", "。")) for cue in cues))

    def test_multi_segment_timeline_keeps_later_cues_and_authored_spacing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = {
                "segments": [
                    {"id": "intro", "broadcast_text": "欢迎收看。"},
                    {"id": "story", "broadcast_text": "OpenAI 发布 2.0 版本。"},
                ]
            }
            _, cues = write_subtitles(root, script, {"intro": 2.0, "story": 3.0}, aligned=False)
            self.assertTrue(any(cue["start"] >= 2.0 for cue in cues))
            self.assertEqual("".join(cue["text"] for cue in cues), "欢迎收看。OpenAI 发布 2.0 版本。")
            self.assertTrue(all(cue["end"] <= 5.0 for cue in cues))

    def test_publication_music_mix_writes_audited_three_section_stems(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def tone(path: Path, duration: float, frequency: float) -> None:
                frames = []
                for index in range(round(duration * 48000)):
                    sample = round(5000 * math.sin(2 * math.pi * frequency * index / 48000))
                    frames.append(struct.pack("<h", sample))
                with wave.open(str(path), "wb") as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(48000)
                    audio.writeframes(b"".join(frames))

            narration = root / "narration.wav"
            tone(narration, 4.0, 220.0)
            music_dir = root / "music"
            music_dir.mkdir()
            music = {}
            for name, frequency in (("opening", 330.0), ("middle", 440.0), ("ending", 550.0)):
                path = music_dir / f"{name}.wav"
                tone(path, 1.0, frequency)
                music[name] = path

            output = mix_audio(root, narration, music, section_boundaries={"intro_end": 1.0, "middle_start": 1.0, "ending_start": 2.5})
            self.assertTrue(output.is_file())
            report = json.loads((root / "artifacts" / "background-music.json").read_text(encoding="utf-8"))
            self.assertEqual(report["quality_gate"]["status"], "passed")
            self.assertEqual(report["rules"]["duck_window_ms"], 20.0)
            self.assertEqual(report["rules"]["duck_recovery_ms"], 300.0)
            self.assertEqual(report["placements"]["ending"]["start_seconds"], 2.5)
            self.assertEqual(report["peak_protection"]["newly_clipped_samples"], 0)
