"""Unit tests for the cloud-computer native daily orchestrator.

All tests are offline — no AIHOT, TTS, or rendering calls are made.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from ai_morning_brief import daily
from ai_morning_brief.daily import (
    AGENT_STAGES,
    EXIT_AGENT_HANDOFF,
    EXIT_FAILURE,
    STAGES,
    STAGE_LABELS,
    _extract_prompt,
    _first_incomplete,
    _read_json,
    _set_stage,
    _stage_status,
    load_state,
    save_state,
)


class TestStageDefinitions(unittest.TestCase):
    def test_stages_order_is_stable(self):
        self.assertEqual(
            STAGES,
            ("bootstrap", "fetch", "editorial", "script_tts", "voice_clone",
             "render", "cover", "release", "report"),
        )

    def test_agent_stages_subset(self):
        self.assertTrue(AGENT_STAGES.issubset(set(STAGES)))
        self.assertEqual(AGENT_STAGES, {"editorial", "voice_clone", "cover"})

    def test_every_stage_has_label(self):
        for name in STAGES:
            self.assertIn(name, STAGE_LABELS, f"missing label for stage {name}")
            self.assertTrue(STAGE_LABELS[name])

    def test_runners_cover_all_non_agent_stages(self):
        for name in STAGES:
            if name in AGENT_STAGES:
                self.assertIn(name, daily.AGENT_HANDOFF_RUNNERS)
            else:
                self.assertIn(name, daily.STAGE_RUNNERS)


class TestStateManagement(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name) / "2026-09-06"
        self.run_dir.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_state_creates_default(self):
        state = load_state(self.run_dir)
        self.assertEqual(state["version"], daily.DAILY_VERSION)
        self.assertEqual(state["date"], "2026-09-06")
        for name in STAGES:
            self.assertEqual(_stage_status(state, name), "pending")

    def test_save_and_reload(self):
        state = load_state(self.run_dir)
        _set_stage(state, "bootstrap", status="done", finished_at="2026-09-06T07:30:00+08:00")
        save_state(self.run_dir, state)
        reloaded = load_state(self.run_dir)
        self.assertEqual(_stage_status(reloaded, "bootstrap"), "done")
        self.assertEqual(_stage_status(reloaded, "fetch"), "pending")

    def test_set_stage_preserves_other_fields(self):
        state = load_state(self.run_dir)
        _set_stage(state, "fetch", status="running", started_at="x", artifacts={"a": 1})
        _set_stage(state, "fetch", status="done", finished_at="y")
        stage = state["stages"]["fetch"]
        self.assertEqual(stage["status"], "done")
        self.assertEqual(stage["started_at"], "x")
        self.assertEqual(stage["finished_at"], "y")
        self.assertEqual(stage["artifacts"], {"a": 1})

    def test_state_file_backfills_new_stages(self):
        # Simulate an old state file with fewer stages.
        old_state = {"version": "0.9", "date": "2026-09-06", "stages": {"bootstrap": {"status": "done"}}}
        (self.run_dir / "run_state.json").write_text(json.dumps(old_state), encoding="utf-8")
        state = load_state(self.run_dir)
        self.assertEqual(_stage_status(state, "bootstrap"), "done")
        for name in STAGES:
            self.assertIn(name, state["stages"])


class TestFirstIncomplete(unittest.TestCase):
    def test_all_pending_returns_first(self):
        state = load_state.__wrapped__(Path("/tmp/nonexistent")) if hasattr(load_state, "__wrapped__") else None
        # Build a fresh state manually.
        state = {"stages": {name: {"status": "pending"} for name in STAGES}}
        self.assertEqual(_first_incomplete(state, frozenset()), "bootstrap")

    def test_skips_done_stages(self):
        state = {"stages": {name: {"status": "done"} for name in STAGES}}
        state["stages"]["fetch"] = {"status": "pending"}
        self.assertEqual(_first_incomplete(state, frozenset()), "fetch")

    def test_all_done_returns_none(self):
        state = {"stages": {name: {"status": "done"} for name in STAGES}}
        self.assertIsNone(_first_incomplete(state, frozenset()))

    def test_skip_excludes_stages(self):
        state = {"stages": {name: {"status": "pending"} for name in STAGES}}
        self.assertEqual(_first_incomplete(state, frozenset({"bootstrap", "fetch"})), "editorial")


class TestHelpers(unittest.TestCase):
    def test_read_json_missing(self):
        self.assertIsNone(_read_json(Path("/tmp/nonexistent-file-12345.json")))

    def test_read_json_invalid(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write("not valid json {{{")
            path = Path(f.name)
        try:
            self.assertIsNone(_read_json(path))
        finally:
            path.unlink()

    def test_read_json_valid(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False, encoding="utf-8") as f:
            json.dump({"key": "value"}, f)
            path = Path(f.name)
        try:
            data = _read_json(path)
            self.assertEqual(data, {"key": "value"})
        finally:
            path.unlink()

    def test_extract_prompt_with_seedream(self):
        ratios = {"16:9": {"seedream_prompt": "hello world", "prompt": "old"}}
        self.assertEqual(_extract_prompt(ratios, "16:9"), "hello world")

    def test_extract_prompt_fallback(self):
        ratios = {"16:9": {"prompt": "fallback"}}
        self.assertEqual(_extract_prompt(ratios, "16:9"), "fallback")

    def test_extract_prompt_missing(self):
        self.assertEqual(_extract_prompt({}, "16:9"), "")
        self.assertEqual(_extract_prompt(None, "16:9"), "")


class TestCLIParsing(unittest.TestCase):
    def test_build_parser(self):
        parser = daily.build_parser()
        args = parser.parse_args(["--date", "2026-09-06"])
        self.assertEqual(args.run_date, "2026-09-06")
        self.assertFalse(args.force)
        self.assertFalse(args.status)

    def test_force_flag(self):
        parser = daily.build_parser()
        args = parser.parse_args(["--date", "2026-09-06", "--force"])
        self.assertTrue(args.force)

    def test_status_flag(self):
        parser = daily.build_parser()
        args = parser.parse_args(["--date", "2026-09-06", "--status"])
        self.assertTrue(args.status)

    def test_skip_parsing(self):
        parser = daily.build_parser()
        args = parser.parse_args(["--date", "2026-09-06", "--skip", "cover,release"])
        self.assertEqual(args.skip, "cover,release")

    def test_from_to_choices(self):
        parser = daily.build_parser()
        args = parser.parse_args(["--date", "2026-09-06", "--from", "render", "--to", "report"])
        self.assertEqual(args.from_stage, "render")
        self.assertEqual(args.to_stage, "report")

    def test_invalid_date_returns_failure(self):
        # main() should return EXIT_FAILURE for invalid date (not raise).
        with mock.patch("sys.argv", ["daily", "--date", "not-a-date"]):
            result = daily.main()
        self.assertEqual(result, EXIT_FAILURE)


class TestRunDailyOffline(unittest.TestCase):
    """Test the orchestration loop with all runners mocked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.output_root = Path(self._tmp.name) / "outputs"
        self.run_date = date(2026, 9, 6)

    def tearDown(self):
        self._tmp.cleanup()

    def _mock_all_runners(self):
        """Replace every runner with a no-op that marks the stage done."""
        patches = []
        for name, fn in list(daily.STAGE_RUNNERS.items()):
            def _make(stage_name):
                def _runner(run_dir, state, *args, **kwargs):
                    _set_stage(state, stage_name, status="done", finished_at="2026-09-06T08:00:00+08:00")
                return _runner
            p = mock.patch.dict(daily.STAGE_RUNNERS, {name: _make(name)})
            patches.append(p)
        for name in list(daily.AGENT_HANDOFF_RUNNERS.keys()):
            def _make_handoff(stage_name):
                def _handoff(run_dir, state):
                    _set_stage(state, stage_name, status="done", finished_at="2026-09-06T08:00:00+08:00")
                    return False  # no handoff needed
                return _handoff
            p = mock.patch.dict(daily.AGENT_HANDOFF_RUNNERS, {name: _make_handoff(name)})
            patches.append(p)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_full_run_succeeds(self):
        self._mock_all_runners()
        result = daily.run_daily(run_date=self.run_date, output_root=self.output_root)
        self.assertEqual(result, 0)
        state = load_state(self.output_root / "2026-09-06")
        for name in STAGES:
            self.assertEqual(_stage_status(state, name), "done", f"stage {name} not done")

    def test_agent_handoff_returns_special_code(self):
        self._mock_all_runners()
        # Override editorial to require handoff.
        def _editorial_handoff(run_dir, state):
            return True
        with mock.patch.dict(daily.AGENT_HANDOFF_RUNNERS, {"editorial": _editorial_handoff}):
            result = daily.run_daily(run_date=self.run_date, output_root=self.output_root)
        self.assertEqual(result, EXIT_AGENT_HANDOFF)
        state = load_state(self.output_root / "2026-09-06")
        # bootstrap and fetch done, editorial still pending (handoff printed).
        self.assertEqual(_stage_status(state, "bootstrap"), "done")
        self.assertEqual(_stage_status(state, "fetch"), "done")
        self.assertEqual(_stage_status(state, "editorial"), "running")

    def test_runner_failure_returns_failure_code(self):
        self._mock_all_runners()
        def _failing_fetch(run_dir, state, run_date):
            raise RuntimeError("simulated fetch failure")
        with mock.patch.dict(daily.STAGE_RUNNERS, {"fetch": _failing_fetch}):
            result = daily.run_daily(run_date=self.run_date, output_root=self.output_root)
        self.assertEqual(result, EXIT_FAILURE)
        state = load_state(self.output_root / "2026-09-06")
        self.assertEqual(_stage_status(state, "fetch"), "failed")
        self.assertIn("simulated fetch failure", state["stages"]["fetch"].get("error", ""))

    def test_force_clears_state(self):
        self._mock_all_runners()
        # First run.
        daily.run_daily(run_date=self.run_date, output_root=self.output_root)
        # Second run with --force should reset and re-run.
        result = daily.run_daily(run_date=self.run_date, output_root=self.output_root, force=True)
        self.assertEqual(result, 0)

    def test_skip_stages(self):
        self._mock_all_runners()
        result = daily.run_daily(
            run_date=self.run_date, output_root=self.output_root,
            skip=["cover", "release"],
        )
        self.assertEqual(result, 0)
        state = load_state(self.output_root / "2026-09-06")
        self.assertEqual(_stage_status(state, "cover"), "skipped")
        self.assertEqual(_stage_status(state, "release"), "skipped")
        self.assertEqual(_stage_status(state, "report"), "done")

    def test_status_only_does_not_execute(self):
        self._mock_all_runners()
        result = daily.run_daily(
            run_date=self.run_date, output_root=self.output_root, status_only=True,
        )
        self.assertEqual(result, 0)
        state = load_state(self.output_root / "2026-09-06")
        # Nothing should be done.
        for name in STAGES:
            self.assertEqual(_stage_status(state, name), "pending")

    def test_fetch_accepts_no_news_and_uses_custom_output_root(self):
        run_date = date(2026, 9, 6)
        run_dir = self.output_root / run_date.isoformat()
        run_dir.mkdir(parents=True)
        state = load_state(run_dir)
        report = {
            "status": "prepared",
            "details": {
                "selection": {
                    "status": "no-news",
                    "selected_count": 0,
                    "eligible_count": 0,
                }
            },
        }
        with mock.patch("ai_morning_brief.pipeline.run_pipeline", return_value=report) as run_pipeline:
            daily.run_fetch(run_dir, state, run_date)
        self.assertEqual(_stage_status(state, "fetch"), "done")
        self.assertEqual(run_pipeline.call_args.kwargs["output_root"], self.output_root)

    def test_from_stage_resets_range(self):
        self._mock_all_runners()
        # First full run.
        daily.run_daily(run_date=self.run_date, output_root=self.output_root)
        # Re-run from render stage.
        result = daily.run_daily(
            run_date=self.run_date, output_root=self.output_root,
            from_stage="render",
        )
        self.assertEqual(result, 0)
        state = load_state(self.output_root / "2026-09-06")
        # Stages before render should still be done (not reset).
        self.assertEqual(_stage_status(state, "bootstrap"), "done")
        self.assertEqual(_stage_status(state, "voice_clone"), "done")
        # Render and after should be done (re-ran).
        self.assertEqual(_stage_status(state, "render"), "done")


if __name__ == "__main__":
    unittest.main()
