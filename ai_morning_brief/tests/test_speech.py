import unittest

from ai_morning_brief.config import DEFAULT_AZURE_TTS_TEMPERATURE
from ai_morning_brief.speech import _build_plain_ssml, proportional_word_timestamps, validate_word_boundaries


class SpeechBoundaryTests(unittest.TestCase):
    def test_dragonhd_ssml_uses_template_temperature(self):
        ssml, _, _ = _build_plain_ssml(
            "zh-CN",
            "zh-CN-Xiaochen:DragonHDLatestNeural",
            "测试文本",
        )
        self.assertEqual(DEFAULT_AZURE_TTS_TEMPERATURE, 0.7)
        self.assertIn('parameters="temperature=0.7"', ssml)

    def test_gemini_proportional_timing_is_complete_and_monotonic(self):
        result = proportional_word_timestamps("今天发布 14 tokens/s。", 2.0)
        self.assertEqual("".join(item["word"] for item in result), "今天发布14tokens/s。")
        self.assertEqual(result[0]["start"], 0.0)
        self.assertEqual(result[-1]["end"], 2.0)
        self.assertTrue(all(item["start"] < item["end"] for item in result))
        self.assertTrue(all(result[index]["end"] <= result[index + 1]["start"] + 0.04 for index in range(len(result) - 1)))

    def test_word_boundary_validation_rejects_non_monotonic_offsets(self):
        with self.assertRaisesRegex(RuntimeError, "non-monotonic"):
            validate_word_boundaries(
                "你好世界",
                [
                    {"boundary_type": "word", "audio_offset_ticks": 20, "duration_ticks": 5, "text_offset": 0, "word_length": 2},
                    {"boundary_type": "word", "audio_offset_ticks": 10, "duration_ticks": 5, "text_offset": 2, "word_length": 2},
                ],
            )

    def test_word_boundary_validation_maps_canonical_text(self):
        result = validate_word_boundaries(
            "你好世界",
            [
                {"boundary_type": "word", "audio_offset_ticks": 0, "duration_ticks": 5, "text_offset": 0, "word_length": 2},
                {"boundary_type": "word", "audio_offset_ticks": 6, "duration_ticks": 5, "text_offset": 2, "word_length": 2},
            ],
        )
        self.assertEqual([item["text"] for item in result], ["你好", "世界"])
        self.assertEqual(result[1]["start_seconds"], 0.0000006)
