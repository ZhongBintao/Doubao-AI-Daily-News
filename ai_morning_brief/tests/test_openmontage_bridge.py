import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_morning_brief.openmontage_bridge import reuse_synthesized_audio


class ReuseAudioSubtitleTests(unittest.TestCase):
    def _run_reuse(self, root: Path, *, provider: str, align: bool, alignment_provider: str):
        artifacts = root / "artifacts"
        audio_dir = root / "assets" / "audio"
        alignment_dir = artifacts / "alignments"
        artifacts.mkdir(parents=True)
        audio_dir.mkdir(parents=True)
        if align and provider != "gemini":
            alignment_dir.mkdir(parents=True)
            (alignment_dir / "story.json").write_text(json.dumps({"word_timestamps": [{
                "word": "甲", "text_offset": 0, "word_length": 1, "start": 0.2, "end": 0.4,
            }]}), encoding="utf-8")
        (audio_dir / "narration-story.wav").write_bytes(b"audio")
        (artifacts / ("google_audio_manifest.json" if provider == "gemini" else "azure_audio_manifest.json")).write_text(json.dumps({
            "provider": provider,
            "segments": [{
                "segment_id": "story",
                "alignment_provider": alignment_provider,
                "alignment_path": "artifacts/alignments/story.json",
                "spoken_duration_seconds": 1.0,
            }],
        }), encoding="utf-8")
        script = {"segments": [{"id": "story", "kind": "news", "category": "ai-products", "display_text": "甲。", "spoken_text": "甲。"}]}
        fake_audio = {"chime": root / "chime.wav", "whoosh": root / "whoosh.wav"}
        with mock.patch("ai_morning_brief.openmontage_bridge.media_duration", return_value=1.0), \
             mock.patch("ai_morning_brief.openmontage_bridge.concat_narration", return_value=root / "narration.wav"), \
             mock.patch("ai_morning_brief.openmontage_bridge.make_audio_assets", return_value=fake_audio), \
             mock.patch("ai_morning_brief.openmontage_bridge.mix_audio", return_value=root / "final-mix.wav"), \
             mock.patch("ai_morning_brief.openmontage_bridge.write_subtitles", return_value=(root / "subtitles.srt", [])) as write_subtitles:
            result = reuse_synthesized_audio(root, script, align=align, speech_provider=provider)
        return result, write_subtitles

    def test_azure_reuse_forwards_native_alignment(self):
        with tempfile.TemporaryDirectory() as directory:
            result, write_subtitles = self._run_reuse(
                Path(directory), provider="azure", align=True, alignment_provider="azure-word-boundary"
            )

            self.assertTrue(write_subtitles.call_args.kwargs["aligned"])
            self.assertEqual(result["subtitle_alignment"]["mode"], "azure-word-boundary")
            self.assertEqual(result["subtitle_alignment"]["proportional_fallback_segments"], [])

    def test_gemini_reuse_keeps_explicit_proportional_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            result, write_subtitles = self._run_reuse(
                Path(directory), provider="gemini", align=True, alignment_provider="gemini-proportional"
            )

            self.assertFalse(write_subtitles.call_args.kwargs["aligned"])
            self.assertEqual(result["subtitle_alignment"]["mode"], "gemini-proportional")
            self.assertTrue(result["subtitle_alignment"]["approximate"])

    def test_azure_no_align_keeps_explicit_proportional_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            result, write_subtitles = self._run_reuse(
                Path(directory), provider="azure", align=False, alignment_provider="azure-word-boundary"
            )

            self.assertFalse(write_subtitles.call_args.kwargs["aligned"])
            self.assertEqual(result["subtitle_alignment"]["mode"], "proportional-fallback")
            self.assertEqual(result["subtitle_alignment"]["proportional_fallback_segments"], ["story"])

    def test_azure_reuse_rejects_missing_alignment_before_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = root / "artifacts"
            audio_dir = root / "assets" / "audio"
            artifacts.mkdir(parents=True)
            audio_dir.mkdir(parents=True)
            (audio_dir / "narration-story.wav").write_bytes(b"audio")
            (artifacts / "azure_audio_manifest.json").write_text(json.dumps({
                "provider": "azure",
                "segments": [{"segment_id": "story", "alignment_provider": "azure-word-boundary", "spoken_duration_seconds": 1.0}],
            }), encoding="utf-8")
            script = {"segments": [{"id": "story", "kind": "news", "category": "ai-products", "display_text": "甲。", "spoken_text": "甲。"}]}

            with self.assertRaisesRegex(RuntimeError, "reusable subtitle alignment is missing"):
                reuse_synthesized_audio(root, script, align=True, speech_provider="azure")


if __name__ == "__main__":
    unittest.main()
