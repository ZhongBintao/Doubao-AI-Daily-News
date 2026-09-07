import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest import mock

from ai_morning_brief.aihot import load_fixture
from ai_morning_brief.pipeline import PIPELINE_VERSION, _can_reuse_success, current_build_contract, run_pipeline


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "aihot_24h.json"
EMPTY_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "aihot_empty_24h.json"


class PipelineTests(unittest.TestCase):
    def test_success_reuse_requires_current_contract_and_video_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "outputs" / "2026-09-02"
            output = run_dir / "renders" / "ai-daily-news-2026-09-02.mp4"
            quality_path = run_dir / "artifacts" / "quality_report.json"
            output.parent.mkdir(parents=True)
            quality_path.parent.mkdir(parents=True)
            payload = b"verified video bytes"
            output.write_bytes(payload)
            contract = current_build_contract(pipeline_version=PIPELINE_VERSION)
            quality_path.write_text(json.dumps({
                "status": "pass",
                "build_contract": contract,
                "output_sha256": hashlib.sha256(payload).hexdigest(),
            }), encoding="utf-8")
            report = {"status": "success", "build_contract": contract, "details": {"render": {"output_path": str(output)}}}
            self.assertTrue(_can_reuse_success(run_dir, date(2026, 9, 2), report, contract))
            output.write_bytes(b"tampered")
            self.assertFalse(_can_reuse_success(run_dir, date(2026, 9, 2), report, contract))
    def test_prepare_only_is_offline_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "outputs"
            first = run_pipeline(run_date=date(2026, 8, 28), output_root=root, fixture=FIXTURE, prepare_only=True)
            self.assertEqual(first["status"], "prepared")
            run_dir = root / "2026-08-28"
            self.assertTrue((run_dir / "artifacts" / "source_snapshot.json").is_file())
            self.assertTrue((run_dir / "artifacts" / "editorial_input.json").is_file())
            self.assertFalse((run_dir / "artifacts" / "narration_plan.json").is_file())
            second = run_pipeline(run_date=date(2026, 8, 28), output_root=root, fixture=FIXTURE, prepare_only=True)
            self.assertEqual(second["status"], "prepared")
            report = json.loads((run_dir / "run_report.json").read_text(encoding="utf-8"))
            self.assertTrue(report["no_secrets_in_artifacts"])

    def test_prepare_full_workflow_records_source_visual_minimum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "outputs"
            report = run_pipeline(
                run_date=date(2026, 8, 28),
                output_root=root,
                fixture=FIXTURE,
                prepare_only=True,
                source_visual_mode="auto",
                source_visual_min_stories=1,
            )
            self.assertEqual(report["status"], "prepared")
            source_visuals = report["details"]["source_visuals"]
            self.assertEqual(source_visuals["acceptance"]["test_scope"], "full_workflow")
            self.assertEqual(source_visuals["acceptance"]["minimum_selected_stories"], 1)

    def test_sparse_live_day_uses_unseen_seven_day_fallback(self):
        primary = load_fixture(EMPTY_FIXTURE)
        fallback_base = load_fixture(FIXTURE)
        fallback_payload = dict(fallback_base.payload)
        fallback_payload["query"] = {
            **dict(fallback_payload.get("query") or {}),
            "window": "7d",
        }
        fallback = replace(fallback_base, payload=fallback_payload, window="7d")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "outputs"
            previous = root / "2026-08-27"
            (previous / "artifacts").mkdir(parents=True)
            (previous / "renders").mkdir(parents=True)
            (previous / "artifacts" / "quality_report.json").write_text(
                json.dumps({"status": "pass"}), encoding="utf-8"
            )
            (previous / "renders" / "ai-daily-news-2026-08-27.mp4").write_bytes(b"passing video")
            (previous / "artifacts" / "editorial_input.json").write_text(
                json.dumps({"selection": {"item_ids": ["fixture-model-01"]}}), encoding="utf-8"
            )

            with mock.patch(
                "ai_morning_brief.pipeline.AIHotClient.fetch_selected",
                side_effect=[primary, fallback],
            ) as fetch_selected:
                report = run_pipeline(
                    run_date=date(2026, 8, 28),
                    output_root=root,
                    prepare_only=True,
                    source_visual_mode="off",
                )

            self.assertEqual(report["status"], "prepared")
            fetch_selected.assert_any_call(window="24h", limit=50, force=False)
            fetch_selected.assert_any_call(window="7d", limit=50, force=False)
            run_dir = root / "2026-08-28"
            source = json.loads((run_dir / "artifacts" / "source_snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual(source["effective_window"], "7d")
            self.assertEqual([attempt["window"] for attempt in source["source_attempts"]], ["24h", "7d"])
            selection = json.loads((run_dir / "artifacts" / "selection_report.json").read_text(encoding="utf-8"))
            self.assertLessEqual(selection["selected_count"], 3)
            self.assertNotIn("fixture-model-01", [item["item_id"] for item in selection["items"]])

    def test_no_news_day_gets_deterministic_status_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "outputs"
            report = run_pipeline(
                run_date=date(2026, 8, 28),
                output_root=root,
                fixture=EMPTY_FIXTURE,
                prepare_only=True,
                source_visual_mode="off",
            )
            self.assertEqual(report["status"], "prepared")
            run_dir = root / "2026-08-28"
            selection = json.loads((run_dir / "artifacts" / "selection_report.json").read_text(encoding="utf-8"))
            self.assertEqual(selection["status"], "no-news")
            editorial_input = json.loads((run_dir / "artifacts" / "editorial_input.json").read_text(encoding="utf-8"))
            self.assertEqual(editorial_input["selection"]["mode"], "no-news")
            self.assertTrue(editorial_input["selection"]["provenance"]["no_news"])
            plan = json.loads((run_dir / "artifacts" / "editorial_plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["edition_mode"], "no-news")
            self.assertEqual(plan["stories"], [])

    def test_auto_visual_shortage_stops_before_editorial_script_and_tts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "outputs"
            openmontage = Path(directory) / "OpenMontage"
            openmontage.mkdir()
            with mock.patch("ai_morning_brief.pipeline._preflight", return_value={"test": True}), mock.patch("ai_morning_brief.config.load_allowed_env", return_value={}):
                report = run_pipeline(
                    run_date=date(2026, 8, 28),
                    output_root=root,
                    fixture=FIXTURE,
                    openmontage_root=openmontage,
                    source_visual_mode="auto",
                    source_visual_min_stories=1,
                )
            self.assertEqual(report["status"], "awaiting_screenshots")
            self.assertEqual(report["failed_stage"], None)
            self.assertEqual(report["details"]["stop_before"], "script_tts_render")
            run_dir = root / "2026-08-28"
            self.assertFalse((run_dir / "artifacts" / "narration_plan.json").is_file())
            self.assertEqual(
                report["details"]["error_code"],
                "browser_capture_unavailable",
            )

    def test_reuse_audio_skips_provider_credentials_and_tts_benchmark(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "outputs"
            run_dir = root / "2026-08-28"
            artifacts = run_dir / "artifacts"
            artifacts.mkdir(parents=True)
            (artifacts / "source_snapshot.json").write_text("{}", encoding="utf-8")
            with mock.patch("ai_morning_brief.pipeline.run_tts_benchmark") as benchmark:
                from ai_morning_brief.pipeline import _preflight
                result = _preflight(Path(directory), prepare_only=False, speech_provider="azure", reuse_audio=True)
            self.assertFalse(result["reuse_audio"] is False)
            benchmark.assert_not_called()
