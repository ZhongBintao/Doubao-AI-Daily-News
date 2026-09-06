from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("cover_workflow.py")
SPEC = importlib.util.spec_from_file_location("cover_workflow", MODULE_PATH)
assert SPEC and SPEC.loader
workflow = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = workflow
SPEC.loader.exec_module(workflow)


def prepare_args(editorial_path: Path, output_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        editorial_input=str(editorial_path),
        item_id="headline",
        headline="Anthropic面临音乐版权诉讼",
        subheadline="Sony Music与Warner Music Group提起诉讼",
        visual_brief="以AI模型核心连接版权音乐符号、法律天平与上升的数据路径，表现训练数据与版权责任的冲突。",
        ratio=None,
        brand=None,
        extra_logo_manifest=None,
        output_dir=str(output_dir),
        force=False,
    )


class CoverWorkflowTests(unittest.TestCase):
    def editorial(self) -> dict:
        return {
            "date": "2026-09-01",
            "input_sha256": "frozen-input",
            "selection": {
                "item_ids": ["headline", "uber", "openai", "huggingface"]
            },
            "items": [
                {
                    "id": "headline",
                    "selected": True,
                    "title": "Sony Music与Warner Music Group起诉Anthropic",
                    "summary": "唱片公司指控Anthropic使用版权音乐训练Claude。",
                    "score": 90,
                    "links": {},
                },
                {
                    "id": "uber",
                    "selected": True,
                    "title": "Uber用Agent接管代码PR",
                    "summary": "Uber披露AI工程进展。",
                    "score": 80,
                    "links": {},
                },
                {
                    "id": "openai",
                    "selected": True,
                    "title": "OpenAI发布新研究",
                    "summary": "研究工具进入Hugging Face生态。",
                    "score": 70,
                    "links": {},
                },
                {
                    "id": "huggingface",
                    "selected": True,
                    "title": "Hugging Face扩展工具",
                    "summary": "工具面向模型开发者。",
                    "score": 60,
                    "links": {},
                },
            ],
        }

    def write_editorial(self, root: Path) -> Path:
        editorial_path = root / "artifacts" / "editorial_input.json"
        editorial_path.parent.mkdir(parents=True)
        editorial_path.write_text(
            json.dumps(self.editorial(), ensure_ascii=False), encoding="utf-8"
        )
        plan = {
            "input_sha256": "frozen-input",
            "stories": [
                {"source_item_ids": ["openai"]},
                {"source_item_ids": ["headline"]},
                {"source_item_ids": ["uber"]},
                {"source_item_ids": ["huggingface"]},
            ],
        }
        (editorial_path.parent / "editorial_plan_final.json").write_text(
            json.dumps(plan), encoding="utf-8"
        )
        return editorial_path

    def test_standard_and_custom_ratios(self) -> None:
        standard = workflow.parse_ratio("16:9")
        self.assertEqual((standard.width, standard.height), (1920, 1080))
        custom = workflow.parse_ratio("4:5=1200x1500")
        self.assertEqual((custom.width, custom.height), (1200, 1500))
        with self.assertRaises(workflow.CoverWorkflowError):
            workflow.parse_ratio("4:5")

    def test_active_style_reference_is_the_user_asset(self) -> None:
        reference = workflow.STYLE_REFERENCE_PATH
        metadata = workflow.read_json(reference.with_suffix(".json"))
        self.assertTrue(reference.is_file())
        self.assertEqual(metadata["role"], "active_editorial_visual_system_reference")
        self.assertEqual(workflow.sha256_file(reference), metadata["sha256"])
        self.assertEqual(metadata["dimensions"], [1920, 1080])

    def test_numeric_copy_must_be_source_grounded(self) -> None:
        item = {"title": "调用量增长10倍", "summary": "账单零增长"}
        workflow.validate_copy("调用量增长10倍", "账单零增长", item)
        with self.assertRaises(workflow.CoverWorkflowError):
            workflow.validate_copy("调用量增长12倍", "账单零增长", item)

    def test_prepare_rejects_visual_brief_numeric_claim_absent_from_headliner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            editorial_path = self.write_editorial(root)
            args = prepare_args(editorial_path, root / "covers")
            args.visual_brief = "用12个模型核心连接版权音乐与法律天平。"
            with self.assertRaises(workflow.CoverWorkflowError):
                workflow.prepare_request(args)

    def test_full_cover_prompt_contains_dynamic_contract_and_no_legacy_route(self) -> None:
        brands = [{"name": "Anthropic"}, {"name": "Sony Music"}]
        prompt = workflow.build_prompt(
            date_label="2026.09.01",
            headline="Anthropic面临音乐版权诉讼",
            subheadline="Sony Music提起诉讼",
            ratio=workflow.parse_ratio("16:9"),
            brands=brands,
            visual_brief="AI核心连接版权音乐与法律天平。",
        )
        for value in (
            '"AI每日早报"',
            '"2026.09.01"',
            '"Anthropic面临音乐版权诉讼"',
            '"Sony Music提起诉讼"',
            "AI核心连接版权音乐与法律天平。",
            "Anthropic",
            "Sony Music",
            "一次性生成可直接发布的最终封面",
        ):
            self.assertIn(value, prompt)
        lowered = prompt.casefold()
        self.assertNotIn("text-free", lowered)
        self.assertNotIn("local compositor", lowered)
        self.assertNotIn("background plate", lowered)

    def test_adaptation_prompt_requires_native_recomposition(self) -> None:
        prompt = workflow.build_prompt(
            date_label="2026.09.01",
            headline="测试标题",
            subheadline="测试副标题",
            ratio=workflow.parse_ratio("3:4"),
            brands=[],
            visual_brief="测试视觉摘要。",
            generation_role="adaptation",
            anchor_ratio="16:9",
        )
        self.assertIn("Same-edition 16:9 cover", prompt)
        self.assertIn("fully recompose it for 3:4", prompt)
        self.assertIn("do not crop, stretch", prompt)

    def test_brand_selection_is_headliner_first_source_only_and_capped_at_six(self) -> None:
        editorial = self.editorial()
        brands = workflow.select_edition_brands(
            editorial,
            "headline",
            ["openai", "uber", "huggingface"],
        )
        self.assertEqual(
            [value["name"] for value in brands],
            [
                "Anthropic",
                "Sony Music",
                "Warner Music Group",
                "OpenAI",
                "Hugging Face",
                "Uber",
            ],
        )
        with self.assertRaises(workflow.CoverWorkflowError):
            workflow.select_edition_brands(
                editorial,
                "headline",
                ["openai", "uber", "huggingface"],
                ["Invented Corp"],
            )

    def test_source_present_new_brand_is_inserted_in_story_order(self) -> None:
        editorial = self.editorial()
        editorial["selection"]["item_ids"].append("runway")
        editorial["items"].append({
            "id": "runway",
            "selected": True,
            "title": "Runway发布界面世界模型",
            "summary": "Runway展示新模型。",
            "score": 50,
            "links": {},
        })
        brands = workflow.select_edition_brands(
            editorial,
            "headline",
            ["runway", "uber"],
            ["Runway"],
            maximum=10,
        )
        names = [value["name"] for value in brands]
        self.assertLess(names.index("Runway"), names.index("Uber"))
        self.assertEqual(next(value for value in brands if value["name"] == "Runway")["fallback_status"], "exact_text_identity_only")

    def test_prepare_schema_five_defaults_to_three_one_shot_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            editorial_path = self.write_editorial(root)
            request = workflow.prepare_request(
                prepare_args(editorial_path, root / "release-kit" / "covers")
            )
            self.assertEqual(request["schema_version"], 5)
            self.assertEqual(request["generation_mode"], "full_cover_imagegen")
            self.assertFalse(request["post_processing"])
            self.assertEqual(request["anchor_ratio"], "16:9")
            self.assertEqual(
                [value["ratio"] for value in request["ratios"]],
                ["16:9", "3:4", "9:16"],
            )
            self.assertEqual(
                {value["generation_request_count"] for value in request["ratios"]},
                {1},
            )
            self.assertEqual(len(request["brands"]), 6)
            self.assertEqual(
                request["presentation_item_ids"],
                ["openai", "headline", "uber", "huggingface"],
            )
            portrait_refs = request["ratios"][1]["reference_inputs"]
            self.assertEqual(portrait_refs[0]["role"], "active_editorial_visual_system_reference")
            self.assertEqual(portrait_refs[1]["role"], "same_edition_anchor_reference")
            self.assertTrue(any(value["role"] == "official_brand_identity_reference" for value in portrait_refs))

    def test_record_copies_all_outputs_byte_for_byte_without_visual_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            editorial_path = self.write_editorial(root)
            output_dir = root / "release-kit" / "covers"
            request = workflow.prepare_request(prepare_args(editorial_path, output_dir))
            sources: dict[str, Path] = {}
            for index, ratio in enumerate(("16:9", "3:4", "9:16"), start=1):
                source = root / f"source-{ratio.replace(':', 'x')}.png"
                source.write_bytes((f"raw-gpt-image-{index}-" * 31).encode("utf-8"))
                sources[ratio] = source
            manifest = workflow.record_outputs(argparse.Namespace(
                request=request["request_path"],
                image=[f"{ratio}={path}" for ratio, path in sources.items()],
                force=False,
            ))
            self.assertEqual(manifest["status"], "complete_unreviewed")
            self.assertEqual(manifest["generation_mode"], "full_cover_imagegen")
            self.assertFalse(manifest["post_processing"])
            self.assertEqual(manifest["attempts"], 1)
            for ratio, source in sources.items():
                result = manifest["results"][ratio]
                generated = Path(result["generated_file"])
                self.assertEqual(source.read_bytes(), generated.read_bytes())
                self.assertEqual(workflow.sha256_file(source), workflow.sha256_file(generated))
                self.assertNotIn("normalized_file", result)
                self.assertNotIn("brand_render", result)
                self.assertNotIn("visual_review", result)

    def test_cli_exposes_record_but_not_legacy_compose_or_finalize(self) -> None:
        parser = workflow.build_parser()
        choices = parser._subparsers._group_actions[0].choices
        self.assertIn("record", choices)
        self.assertNotIn("compose", choices)
        self.assertNotIn("finalize", choices)


if __name__ == "__main__":
    unittest.main()
