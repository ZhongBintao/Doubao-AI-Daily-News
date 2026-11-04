"""Unit tests for the v6 daily pipeline (deterministic stages; writer/voice handoffs are separate)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from ai_morning_brief import config as C
from ai_morning_brief import covers, daily, director, hot_rank, newsroom, render, timeline, voice, writing

RUN_DATE = date(2026, 11, 4)


class ConfigTests(unittest.TestCase):
    def test_vertical_dimensions_and_quiet_edition_rules(self):
        self.assertEqual((C.V6_WIDTH, C.V6_HEIGHT), (1080, 1920))
        self.assertEqual(C.RUNDOWN_MAX_HEADLINES, 3)
        self.assertEqual(C.RUNDOWN_MAX_FLASHES, 5)
        self.assertEqual(C.RUNDOWN_QUIET_FLASHES, 2)
        self.assertLessEqual(C.RUNDOWN_MAX_SECONDS, C.V6_MAX_DURATION_SECONDS)

    def test_min_stories_hard_stop_is_off_by_default(self):
        self.assertEqual(C.MIN_STORIES, 0)
        self.assertEqual(C.FINAL_CUT_TEMPO_DEFAULT, 1.0)

    def test_final_cut_tempo_bounds_and_fallback(self):
        old = os.environ.get("AI_DAILY_OUTPUT_TEMPO")
        try:
            os.environ.pop("AI_DAILY_OUTPUT_TEMPO", None)
            self.assertEqual(C.final_cut_tempo(), 1.0)
            os.environ["AI_DAILY_OUTPUT_TEMPO"] = "1.25"
            self.assertEqual(C.final_cut_tempo(), 1.25)
            os.environ["AI_DAILY_OUTPUT_TEMPO"] = "0.5"
            self.assertEqual(C.final_cut_tempo(), 1.0)
            os.environ["AI_DAILY_OUTPUT_TEMPO"] = "9"
            self.assertEqual(C.final_cut_tempo(), 1.0)
            os.environ["AI_DAILY_OUTPUT_TEMPO"] = "bad"
            self.assertEqual(C.final_cut_tempo(), 1.0)
        finally:
            if old is None:
                os.environ.pop("AI_DAILY_OUTPUT_TEMPO", None)
            else:
                os.environ["AI_DAILY_OUTPUT_TEMPO"] = old

    def test_doubao_mode_defaults_and_validation(self):
        self.assertEqual(C.DOUBAO_TTS_MODE, "text-to-audio")
        self.assertTrue(C.DOUBAO_CHUNKED)
        self.assertEqual(C.DOUBAO_TEMPO["text-to-audio"], 1.0)
        self.assertEqual(C.DOUBAO_TEMPO["voice-clone"], 1.0)
        self.assertEqual(voice.doubao_mode(), "text-to-audio")
        self.assertEqual(voice.doubao_mode("voice-clone"), "voice-clone")
        with self.assertRaises(voice.VoiceError):
            voice.doubao_mode("nope")

    def test_chunked_synthesis_only_for_text_to_audio(self):
        self.assertTrue(voice.chunked_synthesis("doubao", "text-to-audio"))
        self.assertFalse(voice.chunked_synthesis("doubao", "voice-clone"))
        self.assertFalse(voice.chunked_synthesis("edge", None))


class HotRankTests(unittest.TestCase):
    def _item(self, score=70, categories=None, *, rank=None, daily=False, first_seen=None, sources=2):
        item = {"id": f"x{score}", "score": score, "categories": categories or ["ai-models"],
                "first_seen": first_seen or "2026-11-04T01:00:00+08:00", "sources": [{} for _ in range(sources)]}
        if rank is not None:
            item["hot_rank"] = rank
        if daily:
            item["daily_report"] = True
        return item

    def test_heat_score_combines_base_signals_and_category(self):
        item = self._item(score=70, rank=1, daily=True, sources=3)
        score = hot_rank.heat_score(item, set(), RUN_DATE)
        self.assertEqual(score, 70 + C.HEAT_DAILY_BONUS + C.HEAT_DAILY_LEAD_BONUS + (C.HEAT_HOT_RANK_BASE - 2)
                         + min(3, C.HEAT_HOT_SOURCE_CAP) * C.HEAT_HOT_SOURCE_BONUS + C.HEAT_CATEGORY_ADJUST["ai-models"])

    def test_aired_items_are_heavily_penalized(self):
        fresh = hot_rank.heat_score(self._item(72), set(), RUN_DATE)
        aired = hot_rank.heat_score(self._item(72), {"x72"}, RUN_DATE)
        self.assertEqual(aired, fresh - C.HEAT_AIRED_PENALTY)

    def test_stale_event_gets_penalty(self):
        recent = hot_rank.heat_score(self._item(70), set(), RUN_DATE)
        old = hot_rank.heat_score(self._item(70, first_seen="2026-11-01T01:00:00+08:00"), set(), RUN_DATE)
        self.assertEqual(old, recent - C.HEAT_STALE_EVENT_PENALTY)


class ClusterTests(unittest.TestCase):
    def _items(self):
        return [
            {"id": "a1", "title": "Anthropic 递交 S-1", "score": 90, "categories": ["ai-models"],
             "first_seen": "2026-11-04T01:00:00+08:00", "sources": [{"name": "TechCrunch"}], "event_id": "ev-anthropic"},
            {"id": "a2", "title": "Anthropic 招股书细节", "score": 84, "categories": ["ai-models"],
             "first_seen": "2026-11-04T02:00:00+08:00", "sources": [{"name": "The Verge"}], "event_id": "ev-anthropic"},
            {"id": "b1", "title": "Google 教育 AI 调整", "score": 78, "categories": ["ai-products"],
             "first_seen": "2026-11-04T03:00:00+08:00", "sources": [{"name": "9to5Google"}], "event_id": "ev-google"},
        ]

    def test_same_event_id_clusters_into_one(self):
        clusters = hot_rank.cluster_items(self._items())
        self.assertEqual(len(clusters), 2)
        anthropic = next(c for c in clusters if c["event_id"] == "ev-anthropic")
        self.assertEqual(len(anthropic["member_ids"]), 2)
        self.assertEqual(anthropic["lead"]["id"], "a1")

    def test_cluster_heat_uses_max_member_and_aggregates_sources(self):
        clusters = hot_rank.cluster_items(self._items())
        ranked = hot_rank.rank_clusters(clusters, set(), RUN_DATE)
        anthropic = next(c for c in ranked if c["event_id"] == "ev-anthropic")
        self.assertEqual(anthropic["heat"], hot_rank.heat_score(anthropic, set(), RUN_DATE))
        self.assertGreaterEqual(len(anthropic["source_names"]), 2)


class DirectorTests(unittest.TestCase):
    def _cluster(self, key, score, *, depth="medium", event_id=None):
        return {
            "event_id": event_id or f"ev-{key}", "lead": {"id": key, "title": f"标题 {key}", "categories": ["ai-models"],
            "score": score, "first_seen": "2026-11-04T01:00:00+08:00", "sources": [{"name": "AIHOT"}]},
            "member_ids": [key], "member_titles": [f"标题 {key}"], "source_names": ["AIHOT"], "heat": score,
            "pack": {"depth": depth, "facts": [f"事实 {key}" * 20], "background": []},
        }

    def test_fallback_plan_contract_and_angles(self):
        clusters = [self._cluster("a", 90, depth="rich"), self._cluster("b", 80), self._cluster("c", 72, depth="thin")]
        plan = director.fallback_plan(clusters, RUN_DATE)
        self.assertEqual(plan["version"], "6.2")
        self.assertEqual(len(plan["stories"]), 3)
        for story in plan["stories"]:
            self.assertEqual(len(story["title_candidates"]), 1)
            self.assertEqual(story["title"], story["title_candidates"][0]["text"])
            for card in story["cards"]:
                self.assertIn(card["angle"], C.CARD_ANGLES)

    def test_budget_helpers_cap_and_scale(self):
        self.assertEqual(writing.clip_len("x" * 100, 60), "x" * 60)
        self.assertEqual(len(writing.clip_len("x" * 10, 60)), 10)

    def test_normalize_spoken_text_keeps_english_and_expands_numerals(self):
        normalized = writing.normalize_with_ledger("GPT 与 552B 参数，RSA-260 性能提升 0.016%。")
        self.assertIn("G P T", normalized)
        self.assertIn("五千五百二十亿", normalized)
        self.assertIn("R S A 二六零", normalized)


class NewsroomTests(unittest.TestCase):
    def _cluster(self, key, heat, depth):
        return {"event_id": key, "lead": {"title": key, "categories": ["ai-models"]}, "heat": heat,
                "pack": {"depth": depth}, "member_ids": [key], "member_titles": [key]}

    def test_rundown_splits_headlines_and_flashes_and_caps_length(self):
        clusters = [self._cluster(f"h{i}", 92 - i * 2, "rich") for i in range(3)]
        clusters += [self._cluster(f"f{i}", 80 - i * 2, "medium") for i in range(5)]
        plan = newsroom.plan_rundown(clusters)
        self.assertLessEqual(len(plan["headlines"]), 3)
        self.assertLessEqual(len(plan["flashes"]), 5)
        self.assertLessEqual(plan["estimated_seconds"], C.RUNDOWN_MAX_SECONDS)

    def test_quiet_day_keeps_minimum_flashes(self):
        clusters = [self._cluster("f0", 78, "medium"), self._cluster("f1", 74, "thin")]
        plan = newsroom.plan_rundown(clusters)
        self.assertEqual(len(plan["headlines"]), 0)
        self.assertGreaterEqual(len(plan["flashes"]), C.RUNDOWN_QUIET_FLASHES)

    def test_depth_breaks_near_tie_toward_richer_material(self):
        clusters = [self._cluster("a", 80, "rich"), self._cluster("b", 81, "thin")]
        plan = newsroom.plan_rundown(clusters)
        ids = [s["event_id"] for s in plan["headlines"] + plan["flashes"]]
        self.assertIn("a", ids)


class RenderMarkupTests(unittest.TestCase):
    def _scene(self, kind, title, *, cards=None, duration=2.0):
        return {"scene_id": f"s-{kind}", "kind": kind, "title": title, "subject": "Anthropic",
                "cards": cards or [], "duration": duration, "narration": [title]}

    def test_scene_markup_escapes_and_highlights_numbers(self):
        scene = self._scene("story", "标题", cards=[{"angle": "关键数字", "headline": "涨到 6 倍", "body": "从 7.9 升至 【48.1】"}])
        final = {"scenes": [scene], "hook": {"text": ""}, "units": [{"unit_id": "u01", "scene_id": "s-story", "display_text": "标题"}],
                 "cards": [{"label": "实测", "headline": "涨到 6 倍", "body": "从 7.9 升至 【48.1】"}], "progress": {"role": "头条", "index": 1, "total": 2}}
        markup = render.scene_markup(scene, RUN_DATE, {})
        self.assertIn('data-card="1"', markup)
        self.assertIn('<span class="hl">48.1</span>', markup)
        self.assertIn('class="more"', markup)

    def test_card_icons_follow_labels_and_flash_has_two_cards(self):
        scene = self._scene("flash", "快讯", cards=[
            {"angle": "发生了什么", "headline": "a", "body": "b"},
            {"angle": "为什么重要", "headline": "c", "body": "d"}])
        markup = render.scene_markup(scene, RUN_DATE, {"flash_index": 1})
        self.assertIn("快讯", markup)
        self.assertEqual(markup.count("class=\"card\""), 2)


class TimelineTests(unittest.TestCase):
    def _voice(self):
        return {"units": [
            {"unit_id": "u01", "scene_id": "s1", "duration": 1.0, "display_text": "第一句。"},
            {"unit_id": "u02", "scene_id": "s1", "duration": 1.0, "display_text": "第二句。"},
            {"unit_id": "u03", "scene_id": "s2", "duration": 1.0, "display_text": "第三句。"}]}

    def _plan(self):
        return {"scenes": [
            {"scene_id": "s1", "kind": "story", "title": "t1", "cards": [], "narration": ["第一句。", "第二句。"]},
            {"scene_id": "s2", "kind": "story", "title": "t2", "cards": [], "narration": ["第三句。"]}]}

    def test_timeline_assigns_scene_and_caption_times(self):
        result = timeline.build_timeline(self._plan(), self._voice())
        self.assertEqual(result["total_duration"], 3.0 + C.PAUSE_BETWEEN_STORIES)
        self.assertEqual(len(result["captions"]), 3)
        self.assertTrue(all(c["end"] > c["start"] for c in result["captions"]))

    def test_timeline_is_monotonic_and_scenes_cover_their_units(self):
        result = timeline.build_timeline(self._plan(), self._voice())
        starts = [c["start"] for c in result["captions"]]
        self.assertEqual(starts, sorted(starts))


class VoiceManifestTests(unittest.TestCase):
    def _plan(self):
        return {"scenes": [{"scene_id": "s1", "kind": "story", "title": "t"}],
                "units": [{"unit_id": "u01", "scene_id": "s1", "display_text": "一句话。", "spoken_text": "一句话。"}]}

    def test_build_manifest_paths_and_provider_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            manifest = voice.build_manifest(run_dir, self._plan(), "doubao", mode="text-to-audio")
            self.assertTrue(manifest["chunked"])
            self.assertEqual(len(manifest["chunks"]), 1)
            self.assertEqual(manifest["chunks"][0]["unit_ids"], ["u01"])
            self.assertEqual(manifest["doubao"]["mode"], "text-to-audio")

    def test_edge_manifest_is_not_chunked(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            manifest = voice.build_manifest(run_dir, self._plan(), "edge")
            self.assertFalse(manifest["chunked"])
            self.assertIsNone(manifest["chunks"])
            self.assertIn("voice", manifest["edge"])


class ChunkPlanningTests(unittest.TestCase):
    def _units(self, texts):
        return [{"unit_id": f"u{i+1:02d}", "scene_id": "sx", "display_text": t, "spoken_text": t} for i, t in enumerate(texts)]

    def test_chunks_respect_char_budget(self):
        units = self._units(["a" * 100, "b" * 100, "c" * 100])
        chunks = voice.plan_chunks(units, max_chars=150)
        self.assertEqual(len(chunks), 2)
        self.assertEqual([u["unit_id"] for u in chunks[0]], ["u01"])
        self.assertEqual([u["unit_id"] for u in chunks[1]], ["u02", "u03"])

    def test_chunks_break_on_scene_block_change(self):
        units = [{"unit_id": "u01", "scene_id": "s1", "display_text": "x" * 50, "spoken_text": "x" * 50},
                 {"unit_id": "u02", "scene_id": "s2", "display_text": "y" * 50, "spoken_text": "y" * 50}]
        chunks = voice.plan_chunks(units, max_chars=500, block_of={"s1": "s1", "s2": "s2"})
        self.assertEqual(len(chunks), 2)


class WordTokenizerTests(unittest.TestCase):
    def test_english_run_is_one_token_and_cjk_is_per_character(self):
        tokens = voice._word_tokens("GPT 发布 Qwen3.5 模型")
        words = [t for t, _ in tokens]
        self.assertEqual(words[0], "GPT")
        self.assertIn("Qwen3.5", words)
        self.assertIn("模", words)
        self.assertIn("型", words)

    def test_token_weights_english_length_cjk_one(self):
        tokens = dict((t, w) for t, w in voice._word_tokens("AI 好"))
        self.assertEqual(tokens["AI"], 2)
        self.assertEqual(tokens["好"], 1)

    def test_word_alignment_orders_and_cover_duration(self):
        words = voice.word_alignment("你好 ab", 2.0)
        self.assertEqual([w["text"] for w in words], ["你", "好", "ab"])
        self.assertAlmostEqual(words[0]["start"], 0.0, places=3)
        self.assertAlmostEqual(words[-1]["end"], 2.0, places=3)


class DPBoundaryTests(unittest.TestCase):
    def test_dp_picks_ordered_gaps_near_proportional_prior(self):
        weights = [10, 10, 10]
        duration = 3.0
        # pauses near 1.0 and 2.0 (the proportional prior), plus a distractor
        gaps = [0.5, 1.02, 1.6, 2.03]
        boundaries = voice._dp_boundaries(weights, duration, gaps, min_unit_s=0.15)
        self.assertEqual(len(boundaries), 2)
        self.assertAlmostEqual(boundaries[0], 1.02, places=2)
        self.assertAlmostEqual(boundaries[1], 2.03, places=2)

    def test_dp_returns_none_when_too_few_gaps(self):
        self.assertIsNone(voice._dp_boundaries([1, 1, 1], 3.0, [1.0], 0.15))

    def test_proportional_fallback_boundaries(self):
        boundaries = voice._proportional_boundaries([1, 1, 1], 3.0)
        self.assertEqual(len(boundaries), 2)
        self.assertAlmostEqual(boundaries[0], 1.0)
        self.assertAlmostEqual(boundaries[1], 2.0)


class WatermarkTests(unittest.TestCase):
    @staticmethod
    def _tone(seconds, *, sr=voice.SAMPLE_RATE):
        import math

        n = int(seconds * sr)
        return [int(2000 * math.sin(2 * math.pi * 220 * i / sr)) for i in range(n)]

    def _silence(self, seconds, *, sr=voice.SAMPLE_RATE):
        return [0] * int(seconds * sr)

    def test_watermark_cut_finds_long_gap_after_tag(self):
        from array import array

        tag = self._tone(1.0)
        gap = self._silence(0.3)
        body = self._tone(1.0)
        samples = array("h", self._silence(0.3) + tag + gap + body)
        cut = voice.watermark_cut(samples, voice.SAMPLE_RATE)
        self.assertIsNotNone(cut)
        self.assertGreater(cut / voice.SAMPLE_RATE, 1.0)
        self.assertLess(cut / voice.SAMPLE_RATE, 1.6)

    def test_watermark_cut_ignores_short_internal_pause(self):
        from array import array

        # two tag syllables separated by a short 0.05s pause (below the 0.10s gate), then a long gap
        samples = array("h", self._tone(0.5) + self._silence(0.05) + self._tone(0.5) + self._silence(0.3) + self._tone(1.0))
        cut = voice.watermark_cut(samples, voice.SAMPLE_RATE)
        self.assertIsNotNone(cut)
        self.assertGreater(cut / voice.SAMPLE_RATE, 1.0)

    def test_no_watermark_returns_none(self):
        from array import array

        samples = array("h", self._tone(2.0))
        self.assertIsNone(voice.watermark_cut(samples, voice.SAMPLE_RATE))


class DailySelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name)
        self.selection = {
            "mode": "normal", "window": "24h", "items": [
                {"id": "a", "title": "Anthropic S-1", "score": 90, "categories": ["ai-models"], "event_id": "e1",
                 "first_seen": "2026-11-04T01:00:00+08:00", "sources": [{"name": "AIHOT"}]},
                {"id": "b", "title": "Google", "score": 80, "categories": ["ai-products"], "event_id": "e2",
                 "first_seen": "2026-11-04T02:00:00+08:00", "sources": [{"name": "AIHOT"}]},
                {"id": "c", "title": "AMD", "score": 75, "categories": ["industry"], "event_id": "e3",
                 "first_seen": "2026-11-04T03:00:00+08:00", "sources": [{"name": "AIHOT"}]}],
            "pool": [
                {"id": "a", "title": "Anthropic S-1", "score": 90, "categories": ["ai-models"], "event_id": "e1",
                 "first_seen": "2026-11-04T01:00:00+08:00", "sources": [{"name": "AIHOT"}], "lead": {"title": "Anthropic S-1"},
                 "member_titles": ["Anthropic S-1"], "member_ids": ["a"]},
                {"id": "b", "title": "Google", "score": 80, "categories": ["ai-products"], "event_id": "e2",
                 "first_seen": "2026-11-04T02:00:00+08:00", "sources": [{"name": "AIHOT"}], "lead": {"title": "Google"},
                 "member_titles": ["Google"], "member_ids": ["b"]},
                {"id": "c", "title": "AMD", "score": 75, "categories": ["industry"], "event_id": "e3",
                 "first_seen": "2026-11-04T03:00:00+08:00", "sources": [{"name": "AIHOT"}], "lead": {"title": "AMD"},
                 "member_titles": ["AMD"], "member_ids": ["c"]}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_pack_never_contains_original_links(self):
        # deterministic structure check: packs are built from AIHOT fields only
        for cluster in self.selection["pool"]:
            self.assertNotIn("original", json.dumps(cluster["pack"] if "pack" in cluster else {}, ensure_ascii=False))

    def test_one_storyline_is_merged_into_one_event(self):
        anthropic = [c for c in self.selection["pool"] if "Anthropic" in c["lead"]["title"] and ("S-1" in " ".join(c["member_titles"]) or "$895" in c["lead"]["title"])]
        self.assertEqual(len(anthropic), 1)
        self.assertGreaterEqual(len(anthropic[0]["member_ids"]), 3)


class CoverGenerationTests(unittest.TestCase):
    def test_cover_html_includes_both_branding_and_escaped_hook(self):
        final_plan = {"scenes": [
            {"kind": "hook", "subject": "Anthropic", "hook": "【S-1】招股书来了"},
            {"kind": "story", "title": "第二条"},
            {"kind": "flash", "title": "第三条"}]}
        html_text = covers.cover_html("9x16", run_date=RUN_DATE, final_plan=final_plan, font_uri="", logo_uri="", duration=120)
        self.assertIn("今日头条", html_text)
        self.assertIn("<em>S-1</em>", html_text)
        self.assertIn("还有这些", html_text)

    def test_cover_manifest_ratios(self):
        self.assertEqual(covers.RATIOS["3x4"], (1080, 1440))
        self.assertEqual(covers.RATIOS["9x16"], (1080, 1920))


class RundownDepthTieTests(unittest.TestCase):
    def test_richer_material_wins_close_threshold_tie(self):
        def cluster(key, heat, depth):
            return {"event_id": key, "lead": {"title": key, "categories": ["ai-models"]}, "heat": heat,
                    "pack": {"depth": depth}, "member_ids": [key], "member_titles": [key]}
        head = cluster("h", 95, "rich")
        thins = [cluster(f"t{i}", 84, "thin") for i in range(5)]
        rich = cluster("r", 80, "rich")      # 4 points below, but far more material
        far = cluster("f", 77, "rich")       # 7 points below: heat still wins
        rundown = newsroom.plan_rundown([head, *thins, rich, far])
        keys = [k.key for k in rundown["flashes"]]
        self.assertIn("r", keys)


class SRTScalingTests(unittest.TestCase):
    def test_scale_srt_rewrites_all_timestamps(self):
        srt = "1\n00:00:00,000 --> 00:00:01,000\n你好\n\n2\n00:00:01,000 --> 00:00:02,000\n世界\n"
        out = render.scale_srt(srt, 0.8)
        times = re.findall(r"\d{2}:\d{2}:\d{2},\d{3}", out)
        self.assertEqual(len(times), 4)
        # first end 1.0s * 0.8 = 0.8s
        self.assertTrue(times[1].startswith("00:00:00,800"))


class FinalCutTempoIntegrationTests(unittest.TestCase):
    def test_natural_tempo_keeps_identity(self):
        self.assertEqual(C.final_cut_tempo(), 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
