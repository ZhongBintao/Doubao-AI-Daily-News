import copy
import re
import unittest
from datetime import date
from pathlib import Path

from ai_morning_brief.aihot import load_fixture
from ai_morning_brief.editorial import build_editorial_input, validate_editorial_plan
from ai_morning_brief.script import build_script_from_editorial_plan
from ai_morning_brief.selection import select_items


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "aihot_24h.json"


def _summary_fragments(summary):
    return [part.strip() for part in (summary or "").replace("\n", " ").split("。") if part.strip()]


def make_plan(editorial_input, selection):
    stories = []
    for index, item in enumerate(selection.items, 1):
        summary = _summary_fragments(item.summary)[0] if item.summary else item.title
        claims = [
            {"id": f"claim-{index}-title", "source_item_id": item.item_id, "source_field": "title", "source_text": item.title},
            {"id": f"claim-{index}-summary", "source_item_id": item.item_id, "source_field": "summary", "source_text": summary},
        ]
        stories.append(
            {
                "event_key": f"fixture-event-{index}",
                "source_item_ids": [item.item_id],
                "category": item.category,
                "title": item.title,
                "title_claim_ids": [f"claim-{index}-title"],
                "narration": {
                    "beats": [
                        {"type": "hook", "text": item.title, "claim_ids": [f"claim-{index}-title"]},
                        {"type": "fact", "text": summary, "claim_ids": [f"claim-{index}-summary"]},
                        {"type": "impact", "text": f"对开发者和使用者来说，这条变化需要结合已披露的细节来理解：{summary}", "claim_ids": [f"claim-{index}-summary"]},
                    ]
                },
                "claims": claims,
                "layout": {"type": "timeline" if index % 2 else "impact-path", "density": "regular"},
                "cards": [
                    {"role": "lead", "label": "核心变化", "headline": item.title[:16], "body": item.title, "span": 3, "claim_ids": [f"claim-{index}-title"]},
                    {"role": "impact", "label": "关键细节", "headline": "来源信息", "body": summary, "span": 3, "claim_ids": [f"claim-{index}-summary"]},
                ],
            }
        )
    return {"version": "2.0", "prompt_version": editorial_input["prompt_version"], "input_sha256": editorial_input["input_sha256"], "stories": stories}


def make_v5_plan(editorial_input, selection):
    stories = []
    for index, item in enumerate(selection.items, 1):
        summary = _summary_fragments(item.summary)[0] if item.summary else item.title
        subject_match = re.search(r"[A-Z][A-Za-z0-9.+#/-]{1,30}", item.title)
        subject = subject_match.group(0) if subject_match else item.title[:4]
        title_claim = f"v5-{index}-title"
        summary_claim = f"v5-{index}-summary"
        card_count = 6 if index == 1 else 2
        card_names = ("一", "二", "三", "四", "五", "六")
        stories.append({
            "event_key": f"v5-event-{index}",
            "presentation_order": index,
            "source_item_ids": [item.item_id],
            "category": item.category,
            "subject": subject,
            "navigation_title": f"{subject}具体变化",
            "overview_text": f"{subject}本期披露的具体信息是：{summary}",
            "overview_claim_ids": [summary_claim],
            "title": f"{subject}资讯要点",
            "title_claim_ids": [title_claim],
            "narration": {"beats": [
                {"beat_id": f"story-{index:02d}-beat-01", "type": "hook", "text": f"{subject}出现一项具体变化，来源已经披露相关消息。", "claim_ids": [title_claim]},
                {"beat_id": f"story-{index:02d}-beat-02", "type": "evidence", "text": f"{subject}的已披露事实包括：{summary}。", "claim_ids": [summary_claim]},
            ]},
            "claims": [
                {"id": title_claim, "source_item_id": item.item_id, "source_field": "title", "source_text": item.title},
                {"id": summary_claim, "source_item_id": item.item_id, "source_field": "summary", "source_text": summary},
            ],
            "layout": {"type": "stack", "density": "regular"},
            "cards": [
                {"id": f"story-{index:02d}-card-{card_index + 1:02d}", "subject": subject, "role": "evidence", "label": f"事实要点{card_names[card_index]}", "headline": f"{subject}信息{card_names[card_index]}", "body": f"{subject}已披露的第{card_names[card_index]}项信息是：{summary}", "span": 2, "claim_ids": [summary_claim]}
                for card_index in range(card_count)
            ],
        })
    return {
        "version": "5.0",
        "prompt_version": "codex-editorial-v5",
        "input_sha256": editorial_input["input_sha256"],
        "writer": {"skill": "ai-brief-editorial-writer", "version": "4.0", "status": "approved"},
        "stories": stories,
    }


class EditorialPlanTests(unittest.TestCase):
    def setUp(self):
        response = load_fixture(FIXTURE)
        self.selection = select_items(response.items)
        self.source_items = {item.item_id: item for item in self.selection.items}
        self.editorial_input = build_editorial_input(response.url, self.selection, run_date=date(2026, 8, 28))
        self.plan = make_plan(self.editorial_input, self.selection)

    def test_valid_plan_builds_grounded_script(self):
        self.assertEqual(validate_editorial_plan(self.plan, self.editorial_input, self.source_items), [])
        script = build_script_from_editorial_plan(
            self.selection,
            run_date=date(2026, 8, 28),
            editorial_input=self.editorial_input,
            editorial_plan=self.plan,
        )
        self.assertEqual(len([segment for segment in script["segments"] if segment["kind"] == "news"]), 8)
        self.assertTrue(all(segment["cards"] for segment in script["segments"] if segment["kind"] == "news"))
        news = [segment for segment in script["segments"] if segment["kind"] == "news"]
        self.assertEqual([segment["category"] for segment in news], sorted((segment["category"] for segment in news), key=lambda value: {"ai-models": 0, "tip": 1, "ai-products": 2, "paper": 3, "industry": 4}.get(value, 99)))
        self.assertEqual({segment["id"] for segment in news}, {f"story-{index:02d}" for index in range(1, 9)})

    def test_unsupported_numeric_token_is_rejected(self):
        tampered = copy.deepcopy(self.plan)
        tampered["stories"][0]["cards"][0]["body"] = "新增 999K 上下文"
        errors = validate_editorial_plan(tampered, self.editorial_input, self.source_items)
        self.assertTrue(any("unsupported tokens" in error for error in errors))

    def test_duplicate_event_key_is_rejected(self):
        tampered = copy.deepcopy(self.plan)
        tampered["stories"][1]["event_key"] = tampered["stories"][0]["event_key"]
        errors = validate_editorial_plan(tampered, self.editorial_input, self.source_items)
        self.assertTrue(any("event_key" in error for error in errors))

    def test_missing_evidence_is_rejected(self):
        tampered = copy.deepcopy(self.plan)
        tampered["stories"][0]["cards"][0]["claim_ids"] = []
        errors = validate_editorial_plan(tampered, self.editorial_input, self.source_items)
        self.assertTrue(any("no claim_ids" in error for error in errors))

    def test_dense_layout_requires_three_cards_and_richer_bodies(self):
        tampered = copy.deepcopy(self.plan)
        tampered["stories"][0]["layout"]["density"] = "dense"
        errors = validate_editorial_plan(tampered, self.editorial_input, self.source_items)
        self.assertTrue(any("dense layout" in error for error in errors))

    def test_metric_must_be_inline_once_and_not_repeat_in_headline(self):
        tampered = copy.deepcopy(self.plan)
        card = tampered["stories"][0]["cards"][0]
        card["metric"] = "OpenAI"
        card["body"] = "OpenAI 发布新一代推理模型"
        card["headline"] = "OpenAI 发布"
        errors = validate_editorial_plan(tampered, self.editorial_input, self.source_items)
        self.assertTrue(any("metric must not repeat" in error for error in errors))

    def test_v3_rejects_source_copy_and_requires_writer_metadata(self):
        tampered = copy.deepcopy(self.plan)
        tampered["version"] = "3.0"
        tampered["writer"] = {"skill": "ai-brief-editorial-writer", "version": "2.0", "status": "approved"}
        errors = validate_editorial_plan(tampered, self.editorial_input, self.source_items)
        self.assertTrue(any("copies AIHOT source prose" in error for error in errors))

    def test_v3_rejects_a_long_multi_clause_caption_beat(self):
        tampered = copy.deepcopy(self.plan)
        tampered["version"] = "3.0"
        tampered["writer"] = {"skill": "ai-brief-editorial-writer", "version": "2.0", "status": "approved"}
        beat = tampered["stories"][0]["narration"]["beats"][1]
        beat["text"] = "这是一句超过单行字幕容量的事实说明，包含多个分句并且还会继续延伸到下一张字幕。"
        errors = validate_editorial_plan(tampered, self.editorial_input, self.source_items)
        self.assertTrue(any("single-line caption capacity" in error for error in errors))

    def test_v5_keeps_long_explanations_rich_overview_and_all_cards(self):
        plan = make_v5_plan(self.editorial_input, self.selection)
        self.assertEqual(validate_editorial_plan(plan, self.editorial_input, self.source_items), [])
        from ai_morning_brief.writing import finalize_editorial_plan
        finalized, _ = finalize_editorial_plan(plan)
        script = build_script_from_editorial_plan(
            self.selection,
            run_date=date(2026, 8, 28),
            editorial_input=self.editorial_input,
            editorial_plan=finalized,
        )
        overview = next(segment for segment in script["segments"] if segment["kind"] == "overview")
        overview_items = [item for page in overview["screen_pages"] for group in page["groups"] for item in group["items"]]
        self.assertEqual(len(overview_items), len(self.selection.items))
        self.assertTrue(all(item["text"] != item["title"] for item in overview_items))
        self.assertTrue(all(page["duration_seconds"] == 5.0 for page in overview["screen_pages"]))
        self.assertTrue(all(page["layout_basis"] == "content_height" for page in overview["screen_pages"]))
        self.assertTrue(all(page["layout_height_px"] <= page["layout_available_height_px"] for page in overview["screen_pages"]))
        self.assertLess(len(overview["screen_pages"]), len(overview_items))
        self.assertEqual(overview["minimum_duration_seconds"], sum(page["duration_seconds"] for page in overview["screen_pages"]))
        first_news = next(segment for segment in script["segments"] if segment["kind"] == "news")
        self.assertEqual(len(first_news["cards"]), 6)
        self.assertTrue(all(len(unit["display_text"]) <= 28 for unit in first_news["caption_units"]))

    def test_v5_allows_two_web_visual_beats_but_not_three(self):
        plan = make_v5_plan(self.editorial_input, self.selection)
        beats = plan["stories"][0]["narration"]["beats"]
        beats[1]["visual_asset_id"] = "source-item-visual-01"
        beats.append({
            "beat_id": "story-01-beat-03",
            "type": "evidence",
            "text": "来源还披露了同一条具体事实的补充证据。",
            "claim_ids": ["v5-1-summary"],
            "visual_asset_id": "source-item-visual-02",
        })
        self.assertEqual(validate_editorial_plan(plan, self.editorial_input, self.source_items), [])
        beats.append({
            "beat_id": "story-01-beat-04",
            "type": "evidence",
            "text": "这条补充证据仍然对应同一来源事实。",
            "claim_ids": ["v5-1-summary"],
            "visual_asset_id": "source-item-visual-03",
        })
        errors = validate_editorial_plan(plan, self.editorial_input, self.source_items)
        self.assertTrue(any("at most 2 source visual" in error for error in errors))
