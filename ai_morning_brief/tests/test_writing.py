import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ai_morning_brief.aihot import load_fixture
from ai_morning_brief.config import load_allowed_env
from ai_morning_brief.editorial import build_editorial_input
from ai_morning_brief.selection import select_items
from ai_morning_brief.speech import benchmark_phrases, evaluate_tts_scores, run_tts_benchmark
from ai_morning_brief.writing import (
    build_editorial_draft,
    build_writing_request,
    finalize_editorial_plan,
    normalize_with_ledger,
    split_caption_sentences,
    split_caption_units,
)


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "aihot_24h.json"


class WritingTests(unittest.TestCase):
    def test_google_ai_studio_key_alias_is_loaded_without_printing_value(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("GOOGLE_AI_STUDIO_API_KEY=secret-value\n", encoding="utf-8")
            values = load_allowed_env(env_path)
            self.assertEqual(values["GOOGLE_AI_STUDIO_API_KEY"], "secret-value")
            self.assertEqual(values["GEMINI_API_KEY"], "secret-value")

    def setUp(self):
        response = load_fixture(FIXTURE)
        selection = select_items(response.items)
        self.editorial_input = build_editorial_input(response.url, selection, run_date=date(2026, 8, 28))

    def test_normalizer_rewrites_rate_storage_code_and_fullwidth_number(self):
        result = normalize_with_ledger("采用 Q4_K_M 量化（17GB），速度 14 tokens/s，窗口 262，144 token。")
        self.assertIn("每秒十四个 token", result.spoken_text)
        self.assertIn("十七吉字节", result.spoken_text)
        self.assertIn("二十六万二千一百四十四个 token", result.spoken_text)
        self.assertNotRegex(result.spoken_text, r"tokens?/s|Q4_K_M")
        self.assertGreaterEqual(len(result.rewrites), 3)

    def test_grouped_chinese_number_has_stable_display_and_spoken_forms(self):
        result = normalize_with_ledger("数据集包含 4，888 位说话人和 12 项属性。")
        self.assertEqual(result.display_text, "数据集包含 4888 位说话人和 12 项属性。")
        self.assertIn("四千八百八十八位", result.spoken_text)
        self.assertNotIn("4，888", result.spoken_text)
        self.assertIn("四千八百八十八", normalize_with_ledger("数量为 4,888。").spoken_text)
        self.assertEqual(split_caption_sentences("第一句。第二句。"), ["第一句。", "第二句。"])

    def test_writer_request_and_draft_are_source_bound(self):
        request = build_writing_request(self.editorial_input)
        draft = build_editorial_draft(self.editorial_input)
        self.assertEqual(request["input_sha256"], self.editorial_input["input_sha256"])
        self.assertEqual(len(draft["stories"]), self.editorial_input["selection"]["selected_count"])
        self.assertEqual(draft["writer"]["status"], "draft")

    def test_finalize_adds_dual_narration_text(self):
        draft = build_editorial_draft(self.editorial_input)
        finalized, ledger = finalize_editorial_plan(draft)
        self.assertEqual(finalized["speech"]["canonical_text"], "spoken_text")
        self.assertEqual(ledger["status"], "passed")
        for story in finalized["stories"]:
            beats = story["narration"]["beats"]
            self.assertTrue(all(beat.get("spoken_text") for beat in beats))
            self.assertIn("display_text", story["narration"])
            self.assertIn("spoken_text", story["narration"])

    def test_long_editorial_beat_is_split_after_writing_without_content_loss(self):
        text = "Anthropic公布了更完整的安全说明，其中包含评估范围、触发条件和后续修正；这些信息需要连续讲清楚，不能为了单行字幕而删减。"
        units = split_caption_units(text)
        self.assertGreater(len(units), 1)
        self.assertEqual("".join(units), text)
        self.assertTrue(all(len(unit) <= 28 for unit in units))

    def test_benchmark_is_blind_and_waits_without_gemini_key(self):
        script = {"date": "2026-08-28", "segments": []}
        with tempfile.TemporaryDirectory() as directory:
            report = run_tts_benchmark(Path(directory), script, live=True)
            self.assertEqual(report["status"], "awaiting_gemini_credentials")
            self.assertEqual(report["case_count"], 20)
            self.assertTrue((Path(directory) / "artifacts" / "tts-benchmark" / "benchmark.json").is_file())
            self.assertTrue((Path(directory) / "artifacts" / "tts-benchmark" / "provider_map.private.json").is_file())

    def test_benchmark_gate_requires_all_dimensions(self):
        self.assertEqual(evaluate_tts_scores([{"critical_numeric_errors": 0, "term_accuracy": 1, "naturalness_delta": 0}])["status"], "pass")
        self.assertEqual(evaluate_tts_scores([{"critical_numeric_errors": 1, "term_accuracy": 1, "naturalness_delta": 0}])["status"], "fail")
        self.assertEqual(len(benchmark_phrases({"segments": []})), 20)
