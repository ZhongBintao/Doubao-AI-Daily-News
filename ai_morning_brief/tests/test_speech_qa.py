import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from ai_morning_brief.asr_match import (
    locate_words_in_normalized,
    match_units_in_transcript,
    normalize_for_match,
)
from ai_morning_brief import speech_qa
from ai_morning_brief.speech_qa import transcribe_narration
from ai_morning_brief.media import _asr_caption_unit_cues


SEGMENTS = [
    {"id": "intro", "kind": "intro", "display_text": "各位观众早上好，今天是9月11日。欢迎收看AI早报。", "spoken_text": "各位观众早上好，今天是9月11日。欢迎收看AI早报。"},
    {"id": "story-01", "kind": "news", "display_text": "DeepSeek 发布 RSA-260 模型，GPU 集群规模翻倍。", "spoken_text": "DeepSeek 发布 RSA-260 模型，GPU 集群规模翻倍。"},
    {"id": "outro", "kind": "outro", "display_text": "今天的AI资讯播送完毕。我们明天见。", "spoken_text": "今天的AI资讯播送完毕。我们明天见。"},
]


class AsrMatchTests(unittest.TestCase):
    def test_chinese_numerals_collapse_to_arabic(self):
        self.assertEqual(normalize_for_match("二百六十"), "260")
        self.assertEqual(normalize_for_match("二千零二十"), "2020")
        self.assertEqual(normalize_for_match("五千五百二十亿"), "552000000000")
        self.assertEqual(normalize_for_match("十五"), "15")
        self.assertEqual(normalize_for_match("三点五"), normalize_for_match("3.5"))
        # Digit-list readings (how whisper transcribes years).
        self.assertEqual(normalize_for_match("二零二五"), "2025")

    def test_spaced_acronyms_match_compact_form(self):
        self.assertEqual(normalize_for_match("G P U"), normalize_for_match("GPU"))
        self.assertEqual(normalize_for_match("R S A 二百六十"), normalize_for_match("RSA-260"))

    def test_locate_words_maps_monotonically(self):
        transcript = normalize_for_match("DeepSeek 发布 R S A 二百六十 模型。")
        words = [
            {"word": "DeepSeek"}, {"word": " 发布"}, {"word": " R"}, {"word": " S"},
            {"word": " A"}, {"word": " 二"}, {"word": " 百"}, {"word": " 六"}, {"word": " 十"},
            {"word": " 模型"}, {"word": "。"},
        ]
        spans = locate_words_in_normalized(words, transcript)
        self.assertEqual(spans[0], (0, 8))
        self.assertEqual(spans[-1], (-1, -1))  # punctuation carries no characters
        self.assertEqual(spans[9], (transcript.index("模型"), transcript.index("模型") + 2))

    def test_match_units_finds_display_spans_in_transcript(self):
        transcript = normalize_for_match("DeepSeek 发布 R S A 二百六十 模型，G P U 集群规模翻倍。")
        spans = match_units_in_transcript(
            ["DeepSeek 发布 RSA-260 模型。", "GPU 集群规模翻倍。"],
            transcript,
        )
        self.assertIsNotNone(spans[0])
        self.assertIsNotNone(spans[1])
        start0, end0 = spans[0]
        self.assertEqual(transcript[start0:end0], normalize_for_match("DeepSeek 发布 RSA-260 模型"))
        # Units are matched in order, non-overlapping.
        self.assertLessEqual(spans[0][1], spans[1][0])


class _TranscribeFixture(unittest.TestCase):
    """Build a fake run directory with narration plan and audio."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name) / "2026-09-11"
        (self.run_dir / "artifacts").mkdir(parents=True)
        (self.run_dir / "assets" / "audio").mkdir(parents=True)
        (self.run_dir / "artifacts" / "narration_plan.json").write_text(
            json.dumps({"version": "3.0", "segments": [dict(s) for s in SEGMENTS]}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.audio_counter = 0
        for segment in SEGMENTS:
            self._write_audio(segment["id"])
        # The transcriber never decodes audio in unit tests: durations come
        # from ffprobe.
        self._duration_patch = mock.patch.object(speech_qa, "media_duration", return_value=6.0)
        self._duration_patch.start()

    def tearDown(self):
        self._duration_patch.stop()
        self._tmp.cleanup()

    def _write_audio(self, segment_id: str) -> Path:
        self.audio_counter += 1
        path = self.run_dir / "assets" / "audio" / f"narration-{segment_id}.wav"
        path.write_bytes(f"fake wav audio {segment_id} take {self.audio_counter}".encode("utf-8"))
        return path

    def _install_transcriber(self, responses: Mapping[str, Any]):
        """Patch speech_qa.transcribe with canned per-segment responses.

        A response value that is an Exception instance is raised instead,
        simulating an engine failure for that block only.
        """

        def fake_transcribe(audio_path: Path, *, engine: str | None = None, model: str | None = None) -> dict[str, Any]:
            segment_id = audio_path.stem.replace("narration-", "")
            response: Any = responses.get(segment_id, "")
            if isinstance(response, Exception):
                raise response
            text = str(response)
            return {
                "text": text,
                "words": [{"word": word, "start": index * 0.3, "end": index * 0.3 + 0.28} for index, word in enumerate(text.split())],
                "engine": "mock",
            }

        patcher = mock.patch.object(speech_qa, "transcribe", side_effect=fake_transcribe)
        patcher.start()
        self.addCleanup(patcher.stop)


class TranscribeNarrationTests(_TranscribeFixture):
    def test_transcribes_every_block_and_writes_alignments(self):
        self._install_transcriber({
            "intro": "各位观众早上好 今天是9月11日 欢迎收看AI早报",
            "story-01": "DeepSeek 发布 R S A 二百六十 模型 G P U 集群规模翻倍",
            "outro": "今天的AI资讯播送完毕 我们明天见",
        })
        summary = transcribe_narration(self.run_dir)
        self.assertEqual(len(summary["aligned_segments"]), 3)
        self.assertEqual(summary["failed_segments"], [])
        alignment = json.loads((self.run_dir / "artifacts" / "alignments" / "story-01.json").read_text(encoding="utf-8"))
        self.assertEqual(alignment["alignment_provider"], "asr-local")
        self.assertIn("asr_transcript", alignment)
        self.assertTrue(alignment["word_timestamps"])
        self.assertTrue(alignment.get("audio_sha256"))

        # Cached alignment for unchanged audio is reused (no engine call).
        with mock.patch.object(speech_qa, "transcribe", side_effect=AssertionError("must not re-transcribe")):
            summary = transcribe_narration(self.run_dir)
        self.assertEqual(sorted(summary["skipped_segments"]), ["intro", "outro", "story-01"])

    def test_engine_unavailable_degrades_without_raising(self):
        with mock.patch.object(speech_qa, "detect_engine", return_value=None):
            summary = transcribe_narration(self.run_dir)
        self.assertEqual(summary["aligned_segments"], [])
        self.assertEqual(len(summary["failed_segments"]), 3)
        self.assertFalse((self.run_dir / "artifacts" / "alignments").exists())

    def test_single_engine_failure_only_degrades_that_block(self):
        self._install_transcriber({
            "intro": "各位观众早上好 今天是9月11日 欢迎收看AI早报",
            "story-01": RuntimeError("engine exploded"),
            "outro": "今天的AI资讯播送完毕 我们明天见",
        })
        summary = transcribe_narration(self.run_dir)
        self.assertIn("story-01", summary["failed_segments"])
        self.assertEqual(sorted(summary["aligned_segments"]), ["intro", "outro"])


class CaptionTimingTests(unittest.TestCase):
    def test_asr_alignment_times_display_caption_units(self):
        alignment = {
            "asr_transcript": "DeepSeek 发布 R S A 二百六十 模型，G P U 集群参数亮眼。",
            "word_timestamps": [
                {"word": "DeepSeek", "start": 0.0, "end": 0.6},
                {"word": " 发布", "start": 0.6, "end": 1.0},
                {"word": " R", "start": 1.0, "end": 1.2},
                {"word": " S", "start": 1.2, "end": 1.4},
                {"word": " A", "start": 1.4, "end": 1.6},
                {"word": " 二", "start": 1.6, "end": 1.8},
                {"word": " 百", "start": 1.8, "end": 2.0},
                {"word": " 六", "start": 2.0, "end": 2.2},
                {"word": " 十", "start": 2.2, "end": 2.4},
                {"word": " 模型", "start": 2.4, "end": 2.9},
                {"word": "，", "start": 2.9, "end": 3.0},
                {"word": " G", "start": 3.0, "end": 3.2},
                {"word": " P", "start": 3.2, "end": 3.4},
                {"word": " U", "start": 3.4, "end": 3.6},
                {"word": " 集群", "start": 3.6, "end": 4.0},
                {"word": " 参数", "start": 4.0, "end": 4.4},
                {"word": " 亮眼", "start": 4.4, "end": 4.8},
                {"word": "。", "start": 4.8, "end": 4.9},
            ],
        }
        units = [
            {"display_text": "DeepSeek 发布 RSA-260 模型。", "beat_id": "b1"},
            {"display_text": "GPU 集群参数亮眼。", "beat_id": "b1"},
        ]
        cues = _asr_caption_unit_cues(units, alignment, offset=10.0, duration=5.5)
        self.assertIsNotNone(cues)
        self.assertEqual(cues[0]["text"], "DeepSeek 发布 RSA-260 模型。")
        self.assertAlmostEqual(cues[0]["start"], 10.0, places=2)
        self.assertAlmostEqual(cues[1]["start"], 13.0, places=2)


if __name__ == "__main__":
    unittest.main()
