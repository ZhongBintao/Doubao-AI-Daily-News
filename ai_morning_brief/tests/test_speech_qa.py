import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from ai_morning_brief.asr_match import (
    compare_texts,
    extract_key_tokens,
    find_repetition,
    normalize_for_match,
)
from ai_morning_brief import speech_qa
from ai_morning_brief.speech_qa import (
    MAX_ATTEMPTS,
    SpeechQABlocked,
    ensure_speech_qa,
    run_speech_qa,
)


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

    def test_key_tokens_cover_numbers_and_words(self):
        tokens = extract_key_tokens("RSA-260 模型，参数 552B，2020 年发布。")
        self.assertIn("RSA-260", tokens)
        self.assertIn("552B", tokens)
        self.assertIn("2020", tokens)

    def test_find_repetition_detects_stutter(self):
        start, length = find_repetition(normalize_for_match("今天天气很好。今天天气很好。"))
        self.assertEqual(length, 6)
        self.assertEqual(start, 0)
        self.assertIsNone(find_repetition(normalize_for_match("这是一段完全正常的中文旁白文本。")))

    def test_compare_passes_variants_of_the_same_reading(self):
        verdict = compare_texts(
            "DeepSeek 发布 RSA-260 模型，GPU 集群规模翻倍。",
            "DeepSeek 发布 R S A 二百六十 模型，G P U 集群规模翻倍。",
        )
        self.assertEqual(verdict["verdict"], "pass", verdict)

    def test_compare_flags_missing_facts(self):
        verdict = compare_texts("模型参数达到 552B。", "模型参数达到亮眼水平。")
        self.assertEqual(verdict["verdict"], "missing_tokens")
        self.assertIn("552B", verdict["missing_tokens"])

    def test_compare_flags_repetition(self):
        verdict = compare_texts("今天天气很好。", "今天天气很好。今天天气很好。")
        self.assertEqual(verdict["verdict"], "repetition")

    def test_compare_flags_extra_content(self):
        verdict = compare_texts(
            "参数翻倍。",
            "参数翻倍。需要补充说明的是这是编辑临时增加的一段完全不相关的内容用来凑时长。",
        )
        self.assertEqual(verdict["verdict"], "extra_content")

    def test_compare_flags_empty_transcript(self):
        verdict = compare_texts("有实际内容的一段话。", "嗯。 啊。")
        self.assertNotEqual(verdict["verdict"], "pass")

    def test_override_attempts_still_check_display_facts(self):
        verdict = compare_texts(
            "这一模型的命名很有意思。",  # colloquial override wording
            "这一模型的命名很有意思。",
            extra_expected_tokens=extract_key_tokens("DeepSeek 发布 RSA-260 模型。"),
        )
        self.assertEqual(verdict["verdict"], "missing_tokens")


class _QAFixture(unittest.TestCase):
    """Build a fake run directory with narration plan, audio, and manifest."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name) / "2026-09-11"
        (self.run_dir / "artifacts").mkdir(parents=True)
        (self.run_dir / "assets" / "audio").mkdir(parents=True)
        (self.run_dir / "artifacts" / "narration_plan.json").write_text(
            json.dumps({"version": "3.0", "segments": [dict(s) for s in SEGMENTS]}, ensure_ascii=False),
            encoding="utf-8",
        )
        manifest_segments = [
            {
                "segment_id": segment["id"],
                "spoken_text": segment["spoken_text"],
                "output_path": str(self.run_dir / "assets" / "audio" / f"narration-{segment['id']}.wav"),
                "char_count": len(segment["spoken_text"]),
            }
            for segment in SEGMENTS
        ]
        (self.run_dir / "artifacts" / "tts_manifest.json").write_text(
            json.dumps({"version": "2.0", "segments": manifest_segments}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.audio_counter = 0
        for segment in SEGMENTS:
            self._write_audio(segment["id"])
        # The gate never decodes audio in unit tests: durations come from ffprobe.
        self._duration_patch = mock.patch.object(speech_qa, "media_duration", return_value=6.0)
        self._duration_patch.start()

    def tearDown(self):
        self._duration_patch.stop()
        self._tmp.cleanup()

    def _write_audio(self, segment_id: str) -> Path:
        self.audio_counter += 1
        path = self.run_dir / "assets" / "audio" / f"narration-{segment_id}.wav"
        # Bytes change per "resynthesis" so the gate sees a fresh hash.
        path.write_bytes(f"fake wav audio {segment_id} take {self.audio_counter}".encode("utf-8"))
        return path

    def _install_transcriber(self, responses: Mapping[str, str]):
        """Patch speech_qa.transcribe with a canned per-segment transcript."""

        calls: list[str] = []

        def fake_transcribe(audio_path: Path, *, engine: str | None = None, model: str | None = None) -> dict[str, Any]:
            segment_id = audio_path.stem.replace("narration-", "")
            calls.append(segment_id)
            text = responses.get(segment_id, "")
            return {
                "text": text,
                "words": [{"word": word, "start": index * 0.3, "end": index * 0.3 + 0.28} for index, word in enumerate(text.split())],
                "engine": "mock",
                "model": "mock-small",
            }

        patcher = mock.patch.object(speech_qa, "transcribe", side_effect=fake_transcribe)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls


class SpeechQARunTests(_QAFixture):
    def test_passing_readings_write_report_and_alignment(self):
        good = "DeepSeek 发布 R S A 二百六十 模型，G P U 集群规模翻倍。"
        self._install_transcriber({
            "intro": "各位观众早上好，今天是9月11日。欢迎收看AI早报。",
            "story-01": good,
            "outro": "今天的AI资讯播送完毕。我们明天见。",
        })
        report = run_speech_qa(self.run_dir)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["resynthesis_queue"], [])
        alignment = json.loads((self.run_dir / "artifacts" / "alignments" / "story-01.json").read_text(encoding="utf-8"))
        self.assertEqual(alignment["alignment_provider"], "asr-local")
        self.assertIn("asr_transcript", alignment)
        self.assertTrue(alignment["word_timestamps"])
        manifest = json.loads((self.run_dir / "artifacts" / "tts_manifest.json").read_text(encoding="utf-8"))
        qa_by_id = {s["segment_id"]: s.get("qa") for s in manifest["segments"]}
        self.assertEqual(qa_by_id["story-01"]["status"], "passed")

        # Unchanged audio: ensure_speech_qa reuses the passed report.
        reused = ensure_speech_qa(self.run_dir)
        self.assertEqual(reused["status"], "passed")

    def test_failed_block_enters_resynthesis_queue_and_manifest(self):
        responses = {
            "intro": "各位观众早上好，今天是9月11日。欢迎收看AI早报。",
            "story-01": "嗯，这个模型好像挺不错的样子吧大概是这样。",  # facts skipped
            "outro": "今天的AI资讯播送完毕。我们明天见。",
        }
        self._install_transcriber(responses)
        report = run_speech_qa(self.run_dir)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["resynthesis_queue"], ["story-01"])
        manifest = json.loads((self.run_dir / "artifacts" / "tts_manifest.json").read_text(encoding="utf-8"))
        qa_by_id = {s["segment_id"]: s.get("qa") for s in manifest["segments"]}
        self.assertEqual(qa_by_id["story-01"]["status"], "needs_resynthesis")
        self.assertEqual(qa_by_id["story-01"]["attempt"], 1)

        # Re-running without resynthesis must not burn another attempt.
        report = run_speech_qa(self.run_dir)
        story = next(s for s in report["segments"] if s["segment_id"] == "story-01")
        self.assertEqual(story["attempt"], 1)
        self.assertEqual(story["status"], "needs_resynthesis")

    def test_retry_ladder_reaches_override_then_blocks(self):
        bad = "嗯，这个模型好像挺不错的样子吧大概是这样。"
        responses = {
            "intro": "各位观众早上好，今天是9月11日。欢迎收看AI早报。",
            "story-01": bad,
            "outro": "今天的AI资讯播送完毕。我们明天见。",
        }
        self._install_transcriber(responses)
        run_speech_qa(self.run_dir)  # attempt 1 fails

        # Attempt 2: agent resynthesizes (new audio bytes), still bad.
        self._write_audio("story-01")
        report = run_speech_qa(self.run_dir)
        story = next(s for s in report["segments"] if s["segment_id"] == "story-01")
        self.assertEqual(story["attempt"], 2)
        self.assertEqual(story["status"], "needs_resynthesis")

        # Attempt 3: agent provides a colloquial override; screen text unchanged.
        overrides_path = self.run_dir / "artifacts" / "speech_qa" / "colloquial_overrides.json"
        overrides_path.parent.mkdir(parents=True, exist_ok=True)
        overrides_path.write_text(json.dumps({"story-01": "这个命名方式我们直接说它的编号。"}, ensure_ascii=False), encoding="utf-8")
        self._write_audio("story-01")
        report = run_speech_qa(self.run_dir)
        story = next(s for s in report["segments"] if s["segment_id"] == "story-01")
        self.assertEqual(story["attempt"], MAX_ATTEMPTS)
        self.assertEqual(story["spoken_override"], "这个命名方式我们直接说它的编号。")
        self.assertEqual(story["status"], "blocked")
        self.assertEqual(report["status"], "blocked")

        # Render gate must refuse.
        with self.assertRaises(SpeechQABlocked):
            ensure_speech_qa(self.run_dir)

    def test_resynthesized_audio_passing_on_second_attempt(self):
        responses = {
            "intro": "各位观众早上好，今天是9月11日。欢迎收看AI早报。",
            "story-01": "DeepSeek 发布模型，集群规模翻倍。",  # attempt 1: RSA-260/GPU skipped
            "outro": "今天的AI资讯播送完毕。我们明天见。",
        }
        self._install_transcriber(responses)
        run_speech_qa(self.run_dir)
        self._install_transcriber({
            "story-01": "DeepSeek 发布 R S A 二百六十 模型，G P U 集群规模翻倍。",
        })
        self._write_audio("story-01")
        report = run_speech_qa(self.run_dir)
        self.assertEqual(report["status"], "passed")
        story = next(s for s in report["segments"] if s["segment_id"] == "story-01")
        self.assertEqual(story["attempt"], 2)
        self.assertEqual(story["status"], "passed")

    def test_no_engine_marks_unavailable_and_does_not_block(self):
        with mock.patch.object(speech_qa, "detect_engine", return_value=None):
            report = run_speech_qa(self.run_dir)
            self.assertEqual(report["status"], "unavailable")
            # Render gate tolerates an unavailable gate (proportional fallback).
            reused = ensure_speech_qa(self.run_dir)
        self.assertEqual(reused["status"], "unavailable")


class TtsManifestDisplayTextTests(_QAFixture):
    def test_manifest_records_display_and_spoken(self):
        from ai_morning_brief.doubao_tts_adapter import generate_tts_manifest

        manifest = generate_tts_manifest(self.run_dir, tts_mode="voice-clone")
        self.assertTrue(manifest["speech_contract"]["spoken_equals_display"])
        for segment in manifest["segments"]:
            self.assertIn("display_text", segment)
            self.assertEqual(segment["display_text"], segment["spoken_text"])


if __name__ == "__main__":
    unittest.main()
