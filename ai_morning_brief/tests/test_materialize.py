import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ai_morning_brief.aihot import load_fixture
from ai_morning_brief.materialize import _nav_motion_specs, _points_markup, materialize
from ai_morning_brief.script import build_script
from ai_morning_brief.selection import select_items


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "aihot_24h.json"


class MaterializeTests(unittest.TestCase):
    def test_brief_group_uses_four_cards_per_page_without_metadata(self):
        cards = [
            {"id": f"group-card-{index}", "subject": "资讯", "role": "evidence", "body": f"第{index}条独立事实"}
            for index in range(1, 6)
        ]
        markup = _points_markup([], category="ai-products", plan={"story_kind": "brief_group", "cards": cards})
        self.assertEqual(markup.count('class="story-card-page"'), 2)
        self.assertIn('data-card-page-index="0"', markup)
        self.assertIn('data-card-page-index="1"', markup)
        self.assertNotIn("selection_meta", markup)

    def test_v5_overview_timing_is_exactly_five_seconds_per_page(self):
        script = {
            "date": "2026-09-02",
            "editorial": {"plan_version": "5.0"},
            "source_item_ids": ["item-1"],
            "segments": [{
                "id": "overview",
                "kind": "overview",
                "category": "overview",
                "title": "资讯概览",
                "screen_pages": [{
                    "duration_seconds": 5.0,
                    "groups": [{"category": "ai-models", "label": "模型发布", "items": [{
                        "item_id": "item-1",
                        "title": "测试新闻",
                        "text": "来源披露的具体测试事实",
                        "claim_ids": ["claim-1"],
                    }]}],
                }, {
                    "duration_seconds": 5.0,
                    "groups": [],
                }],
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            result = materialize(Path(directory), script, {"overview": 10.0}, [])
            timing = result["overview_timing"]
            self.assertEqual(timing["page_durations"], [5.0, 5.0])
            self.assertEqual(timing["expected_total_duration_seconds"], 10.0)
            self.assertEqual(timing["actual_scene_duration_seconds"], 10.0)
            self.assertEqual(timing["status"], "passed")

    def test_authored_card_fields_drive_dynamic_markup(self):
        markup = _points_markup(
            ["fallback should not replace authored body"],
            category="ai-models",
            plan={
                "variant": "timeline",
                "density": "dense",
                "cards": [
                    {
                        "role": "timeline",
                        "label": "时间节点",
                        "headline": "11 月 12 日关停",
                        "body": "官方给出 2026 年 11 月 12 日这一明确的关停日期",
                        "metric": "2026 年 11 月 12 日",
                        "span": 4,
                    },
                    {
                        "role": "action",
                        "label": "开发者路径",
                        "headline": "自有 API 密钥仍可用",
                        "body": "开发者仍可通过自有 API 密钥继续使用模型",
                        "span": 2,
                    },
                ],
            },
        )
        self.assertIn("variant-timeline", markup)
        self.assertIn("density-dense", markup)
        self.assertIn("时间节点", markup)
        self.assertIn("11 月 12 日关停", markup)
        self.assertIn('class="token-highlight token-feature"', markup)
        self.assertNotIn("point-metric", markup)
        self.assertIn("span-4", markup)
        self.assertNotIn("fallback should not replace authored body", markup)

    def test_metric_badge_is_not_rendered_again_when_body_contains_metric(self):
        markup = _points_markup(
            ["本地运行速度约 14 tokens/s"],
            category="ai-models",
            plan={
                "variant": "hero-metric",
                "cards": [{
                    "role": "metric",
                    "label": "生成速度",
                    "body": "本地运行速度约 14 tokens/s",
                    "metric": "14 tokens/s",
                    "span": 6,
                }],
            },
        )
        self.assertNotIn("point-metric", markup)
        self.assertEqual(markup.count('class="token-highlight token-feature"'), 1)
        self.assertIn(">14 tokens/s</mark>", markup)
        self.assertNotIn("<div class=\"point-metric\"", markup)

    def test_generic_highlight_keeps_number_and_unit_together(self):
        markup = _points_markup(["本地运行速度约 14 tokens/s"], category="ai-models")
        self.assertEqual(markup.count('class="token-highlight"'), 1)
        self.assertIn(">14 tokens/s</mark>", markup)

    def test_writes_authored_hyperframes_workspace(self):
        response = load_fixture(FIXTURE)
        script = build_script(select_items(response.items), run_date=date(2026, 8, 28))
        durations = {segment["id"]: 2.0 for segment in script["segments"]}
        cues = [{"start": 0.0, "end": 1.0, "text": "各位观众早上好"}]
        with tempfile.TemporaryDirectory() as directory:
            result = materialize(Path(directory), script, durations, cues)
            html = (Path(directory) / "hyperframes" / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("__SCENES__", html)
            self.assertIn("AI每日早报", html)
            self.assertNotIn("AI Daily News</p>", html)
            intro = html.split('id="scene-intro"', 1)[1].split('</section>', 1)[0]
            self.assertNotIn("AI DAILY NEWS", intro)
            self.assertNotIn("屏幕上是今天的主要内容", html)
            self.assertIn("OpenAI 发布新一代推理模型", html)
            self.assertNotIn("08:00 / DAILY", html)
            self.assertNotIn("LIVE SIGNAL", html)
            self.assertNotIn("source-pill", html)
            self.assertNotIn("story-index", html)
            self.assertNotIn('class="bottom-progress"', html)
            self.assertIn('class="nav-marker"', html)
            self.assertNotIn('id="progress-global-fill"', html)
            self.assertNotIn('class="point-metric"', html)
            self.assertNotIn('progress-entry-story-01', html)
            self.assertNotIn('progress-entry-outro', html)
            self.assertNotIn('data-category="outro"', html.split('class="top-nav"', 1)[1].split('</nav>', 1)[0])
            self.assertIn('id="nav-chip-ai-models"', html)
            self.assertNotIn('nav-chip-active', html)
            self.assertIn('class="top-nav"', html)
            self.assertIn("资讯概览", html)
            self.assertIn("模型发布", html)
            self.assertIn('class="point-grid count-', html)
            self.assertIn('variant-', html)
            self.assertNotIn("story-lede", html)
            self.assertEqual(result["scene_count"], 11)
            project = json.loads((Path(directory) / "project.json").read_text(encoding="utf-8"))
            self.assertEqual(project["render_runtime"], "hyperframes")

    def test_detail_subject_label_is_not_injected(self):
        response = load_fixture(FIXTURE)
        script = build_script(select_items(response.items), run_date=date(2026, 8, 28))
        script["segments"][2]["subject"] = "OpenAI"
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            materialize(run_dir, script, {segment["id"]: 2.0 for segment in script["segments"]}, [])
            html = (run_dir / "hyperframes" / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("主体：", html)
            self.assertNotIn("主题：", html)
            self.assertIn("OpenAI 发布新一代推理模型", html)

    def test_navigation_motion_is_non_decreasing_in_presentation_order(self):
        segments = [
            {"id": "intro", "kind": "intro", "category": "开场"},
            {"id": "overview", "kind": "overview", "category": "overview"},
            {"id": "story-01", "kind": "news", "category": "ai-models"},
            {"id": "story-02", "kind": "news", "category": "tip"},
            {"id": "story-03", "kind": "news", "category": "tip"},
            {"id": "story-04", "kind": "news", "category": "paper"},
            {"id": "outro", "kind": "outro", "category": "结尾"},
        ]
        timings = {segment["id"]: {"start": index * 10.0} for index, segment in enumerate(segments)}
        specs = _nav_motion_specs(segments, timings)
        order = {"intro": 0, "overview": 1, "ai-models": 2, "tip": 3, "paper": 4}
        positions = [order[spec["nav_key"]] for spec in specs if spec["visible"] and spec["nav_key"]]
        self.assertEqual(positions, sorted(positions))
        self.assertFalse(specs[0]["visible"])
        self.assertFalse(specs[-1]["visible"])

    def test_more_than_five_cards_are_paginated_without_loss(self):
        cards = [
            {"id": f"card-{index}", "role": "evidence", "label": f"要点{index}", "body": f"第{index}条完整事实", "span": 2}
            for index in range(1, 8)
        ]
        markup = _points_markup([], category="ai-models", plan={"variant": "stack", "cards": cards})
        self.assertEqual(markup.count('class="story-card-page"'), 2)
        self.assertEqual(markup.count('class="point-card '), 7)
        self.assertIn('data-card-id="card-7"', markup)
        self.assertEqual(markup.count("条完整事实"), 7)
