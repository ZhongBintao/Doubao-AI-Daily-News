import unittest
from dataclasses import replace
from pathlib import Path

from ai_morning_brief.aihot import load_fixture
from ai_morning_brief.config import EDITORIAL_DIMENSIONS
from ai_morning_brief.selection import SelectionPolicy, select_items, select_items_by_dimension


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "aihot_24h.json"


class SelectionTests(unittest.TestCase):
    def test_balances_categories_and_preserves_api_order(self):
        response = load_fixture(FIXTURE)
        result = select_items(response.items)
        self.assertEqual(result.mode, "normal")
        self.assertEqual(len(result.items), 8)
        self.assertEqual(result.items[0].item_id, "fixture-model-01")
        self.assertLessEqual(max(result.category_counts.values()), 2)
        self.assertNotIn("fixture-unselected-01", [item.item_id for item in result.items])

    def test_low_volume_is_short(self):
        response = load_fixture(FIXTURE)
        result = select_items(response.items[:5])
        self.assertEqual(result.mode, "short")
        self.assertEqual(len(result.items), 5)

    def test_under_three_is_failure(self):
        response = load_fixture(FIXTURE)
        result = select_items(response.items[:2])
        self.assertEqual(result.mode, "failure")
        self.assertIn("at least 3", result.reason or "")

    def test_new_selector_ranks_inside_dimensions_and_records_metadata(self):
        response = load_fixture(FIXTURE)
        by_dimension = {
            dimension: [item for item in response.items if item.category == dimension]
            for dimension in EDITORIAL_DIMENSIONS
        }
        result = select_items_by_dimension(by_dimension)
        self.assertEqual(result.mode, "normal")
        self.assertEqual(len(result.items), 7)
        self.assertEqual(result.selection_metadata["fixture-model-01"]["rank"], 1)
        self.assertEqual(result.selection_metadata["fixture-model-01"]["dimension"], "ai-models")
        self.assertEqual(result.selection_metadata["fixture-model-01"]["links"]["aihot"], "https://aihot.virxact.com/items/fixture-model-01")
        self.assertNotIn("fixture-tip-01", result.selection_metadata)

    def test_null_scores_are_eligible_and_ties_keep_api_order(self):
        response = load_fixture(FIXTURE)
        model_items = [item for item in response.items if item.category == "ai-models"]
        model_items = [replace(model_items[0], score=None), replace(model_items[1], score=None)]
        result = select_items_by_dimension({"ai-models": model_items}, policy=SelectionPolicy(soft_min=3, max_items=8))
        self.assertEqual([item.item_id for item in result.items], [item.item_id for item in model_items])
        self.assertEqual(result.selection_metadata[model_items[0].item_id]["rank_percentile"], 1.0)

    def test_empty_dimension_does_not_create_placeholder(self):
        result = select_items_by_dimension({dimension: [] for dimension in EDITORIAL_DIMENSIONS})
        self.assertEqual(result.items, tuple())
        self.assertEqual(result.mode, "failure")
