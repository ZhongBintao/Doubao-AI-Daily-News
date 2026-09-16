import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_morning_brief import pipeline as pipeline_module
from ai_morning_brief.config import DEFAULT_OUTPUT_TEMPO, output_tempo
from ai_morning_brief.media import MediaError
from ai_morning_brief.pipeline import (
    _apply_final_cut_tempo,
    _scale_srt_timestamps,
    _speed_up_video,
)


class OutputTempoConfigTests(unittest.TestCase):
    def test_default_is_hardcoded_1_25(self):
        self.assertEqual(DEFAULT_OUTPUT_TEMPO, 1.25)
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("AI_BRIEF_OUTPUT_TEMPO", None)
            self.assertEqual(output_tempo(), 1.25)

    def test_env_override_and_bounds(self):
        with mock.patch.dict("os.environ", {"AI_BRIEF_OUTPUT_TEMPO": "1.0"}):
            self.assertEqual(output_tempo(), 1.0)
        with mock.patch.dict("os.environ", {"AI_BRIEF_OUTPUT_TEMPO": "1.5"}):
            self.assertEqual(output_tempo(), 1.5)
        # Out-of-range and garbage fall back to the default, never to 1.0.
        for raw in ("0.5", "2.5", "abc", ""):
            with mock.patch.dict("os.environ", {"AI_BRIEF_OUTPUT_TEMPO": raw}):
                self.assertEqual(output_tempo(), DEFAULT_OUTPUT_TEMPO, raw)


class SpeedUpCommandTests(unittest.TestCase):
    def test_command_uses_setpts_fps_and_pitch_preserving_atempo(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.mp4"
            source.write_bytes(b"x")
            target = Path(tmp) / "out.mp4"
            captured: dict = {}

            def fake_run(args, **kwargs):
                captured["args"] = args
                target.write_bytes(b"y")
                return mock.Mock(returncode=0, stderr="")

            with mock.patch.object(pipeline_module, "run_command", side_effect=fake_run):
                _speed_up_video(source, target, 1.25)

        args = captured["args"]
        self.assertIn("setpts=PTS/1.25,fps=30", args)
        self.assertIn("atempo=1.25", args)
        self.assertIn("+faststart", args)

    def test_rejects_neutral_or_out_of_range_tempo(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.mp4"
            source.write_bytes(b"x")
            for tempo in (1.0, 0.5, 3.0):
                with self.assertRaises(ValueError):
                    _speed_up_video(source, Path(tmp) / "out.mp4", tempo)

    def test_ffmpeg_failure_raises_media_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.mp4"
            source.write_bytes(b"x")
            with mock.patch.object(
                pipeline_module, "run_command",
                return_value=mock.Mock(returncode=1, stderr="boom"),
            ):
                with self.assertRaises(MediaError):
                    _speed_up_video(source, Path(tmp) / "out.mp4", 1.25)


class SrtRescaleTests(unittest.TestCase):
    def test_timestamps_are_divided_by_the_tempo(self):
        with tempfile.TemporaryDirectory() as tmp:
            srt = Path(tmp) / "subtitles.srt"
            srt.write_text(
                "1\n00:00:00,000 --> 00:00:02,500\n第一句\n\n"
                "2\n00:00:02,500 --> 00:00:05,000\n第二句\n\n",
                encoding="utf-8",
            )
            _scale_srt_timestamps(srt, 1.25)
            text = srt.read_text(encoding="utf-8")
            self.assertIn("00:00:00,000 --> 00:00:02,000", text)
            self.assertIn("00:00:02,000 --> 00:00:04,000", text)
            # Cue text is untouched.
            self.assertIn("第一句", text)
            self.assertIn("第二句", text)

    def test_missing_file_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            _scale_srt_timestamps(Path(tmp) / "absent.srt", 1.25)  # must not raise


class FinalCutTempoGateTests(unittest.TestCase):
    def _staged(self, tmp: str) -> Path:
        staged = Path(tmp) / "staged.mp4"
        staged.write_bytes(b"video")
        return staged

    def test_disabled_when_tempo_is_1_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"AI_BRIEF_OUTPUT_TEMPO": "1.0"}):
                report = _apply_final_cut_tempo(Path(tmp), self._staged(tmp))
        self.assertEqual(report["status"], "disabled")
        self.assertFalse(report["applied"])

    def test_applied_path_records_durations_and_rescales_srt(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "assets" / "subtitles").mkdir(parents=True)
            srt = run_dir / "assets" / "subtitles" / "subtitles.srt"
            srt.write_text("1\n00:00:00,000 --> 00:00:02,500\n句子\n\n", encoding="utf-8")
            staged = self._staged(tmp)

            durations = iter([100.0, 80.0])

            def fake_duration(_path):
                return next(durations)

            def fake_speed_up(_source, target, _tempo):
                target.write_bytes(b"sped")

            with mock.patch.object(pipeline_module, "media_duration", side_effect=fake_duration), \
                 mock.patch.object(pipeline_module, "_speed_up_video", side_effect=fake_speed_up):
                report = _apply_final_cut_tempo(run_dir, staged)

            self.assertEqual(report["status"], "applied")
            self.assertTrue(report["applied"])
            self.assertEqual(report["source_duration_seconds"], 100.0)
            self.assertEqual(report["output_duration_seconds"], 80.0)
            self.assertTrue(report["srt_rescaled"])
            self.assertIn("00:00:02,000", srt.read_text(encoding="utf-8"))

    def test_failed_speed_up_degrades_to_1_0x_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = self._staged(tmp)
            with mock.patch.object(pipeline_module, "media_duration", return_value=100.0), \
                 mock.patch.object(pipeline_module, "_speed_up_video", side_effect=MediaError("ffmpeg died")), \
                 mock.patch("sys.stderr") as stderr:
                report = _apply_final_cut_tempo(Path(tmp), staged)
            # The staged cut survives untouched so the 1.0x version still ships.
            self.assertEqual(staged.read_bytes(), b"video")
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["applied"])
        self.assertIn("ffmpeg died", report["error"])
        self.assertTrue(stderr.write.called or stderr.flush.called)


if __name__ == "__main__":
    unittest.main()
