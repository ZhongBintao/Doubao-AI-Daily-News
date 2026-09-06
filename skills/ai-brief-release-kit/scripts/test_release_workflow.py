from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image


MODULE_PATH = Path(__file__).with_name("release_workflow.py")
SPEC = importlib.util.spec_from_file_location("release_workflow", MODULE_PATH)
assert SPEC and SPEC.loader
workflow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workflow)


def args_for(**values):
    defaults = {
        "editorial_input": "",
        "description": "",
        "primary_item_id": None,
        "secondary_item_id": None,
        "output_dir": None,
        "force": False,
    }
    defaults.update(values)
    return argparse.Namespace(**defaults)


class ReleaseWorkflowTests(unittest.TestCase):
    def editorial(self) -> dict:
        return {
            "date": "2026-08-31",
            "input_sha256": "frozen-input",
            "selection": {"item_ids": ["anthropic", "uber"]},
            "items": [
                {
                    "id": "anthropic",
                    "title": "索尼与华纳起诉Anthropic，指控其大规模盗用版权音乐训练Claude",
                    "summary": "索尼音乐、华纳音乐等唱片公司起诉Anthropic，训练Claude模型。",
                    "score": 76,
                },
                {
                    "id": "uber",
                    "title": "Uber 用 Agent 接管 70% 代码 PR，AI 账单零增长",
                    "summary": "全公司 70% 的代码 PR 已由 AI Agent 接管，单次会话成本降低 52%。",
                    "score": 76,
                },
            ],
        }

    def test_copy_selection_and_unified_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            editorial_path = root / "2026-08-31" / "artifacts" / "editorial_input.json"
            editorial_path.parent.mkdir(parents=True)
            editorial_path.write_text(json.dumps(self.editorial(), ensure_ascii=False), encoding="utf-8")
            plan = workflow.build_release_plan(
                args_for(
                    editorial_input=str(editorial_path),
                    description="索尼、华纳起诉Anthropic；Uber用AI Agent接管70%代码PR",
                )
            )
            self.assertEqual(plan["cover_story_item_id"], "anthropic")
            self.assertEqual(plan["schema_version"], 3)
            self.assertEqual(plan["description_item_ids"], ["anthropic", "uber"])
            self.assertEqual(
                plan["publish_copy"],
                {
                    "title": "AI每日早报2026-08-31",
                    "description": "索尼、华纳起诉Anthropic；Uber用AI Agent接管70%代码PR",
                },
            )
            self.assertNotIn("max_characters", plan["publish_copy"])
            self.assertEqual(plan["validation"]["status"], "pass")

    def test_numeric_leading_model_token_is_source_grounded(self) -> None:
        editorial = self.editorial()
        editorial["selection"]["item_ids"] = ["anthropic", "qwen"]
        editorial["items"] = [
            editorial["items"][0],
            {
                "id": "qwen",
                "title": "Qwen3.8 27B 本地运行",
                "summary": "模型文件大小是 17GB。",
                "score": 75,
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            editorial_path = Path(temporary) / "2026-08-31" / "artifacts" / "editorial_input.json"
            editorial_path.parent.mkdir(parents=True)
            editorial_path.write_text(json.dumps(editorial, ensure_ascii=False), encoding="utf-8")
            plan = workflow.build_release_plan(
                args_for(
                    editorial_input=str(editorial_path),
                    description="索尼、华纳起诉Anthropic；Qwen3.8 27B 本地运行 17GB",
                )
            )
            self.assertEqual(plan["validation"]["status"], "pass")

    def test_description_with_no_matching_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            editorial_path = root / "2026-08-31" / "artifacts" / "editorial_input.json"
            editorial_path.parent.mkdir(parents=True)
            editorial_path.write_text(json.dumps(self.editorial(), ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(workflow.ReleaseKitError):
                workflow.build_release_plan(
                    args_for(
                        editorial_input=str(editorial_path),
                        description="索尼、华纳起诉Anthropic；不存在的公司发布7个新模型",
                    )
                )

    def test_long_description_is_preserved_without_length_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            editorial_path = root / "2026-08-31" / "artifacts" / "editorial_input.json"
            editorial_path.parent.mkdir(parents=True)
            editorial_path.write_text(json.dumps(self.editorial(), ensure_ascii=False), encoding="utf-8")
            plan = workflow.build_release_plan(
                args_for(
                    editorial_input=str(editorial_path),
                    description="索尼、华纳起诉Anthropic；Uber用AI Agent接管70%代码PR并且这是一段不必要的超长补充文字内容",
                )
            )
            self.assertEqual(
                plan["publish_copy"]["description"],
                "索尼、华纳起诉Anthropic；Uber用AI Agent接管70%代码PR并且这是一段不必要的超长补充文字内容",
            )
            self.assertNotIn("second_story_dropped_for_length", plan["validation"])
            self.assertEqual(plan["description_item_ids"], ["anthropic", "uber"])

    def test_finalize_builds_verified_user_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "2026-08-31"
            editorial_path = run_dir / "artifacts" / "editorial_input.json"
            editorial_path.parent.mkdir(parents=True)
            editorial_path.write_text(json.dumps(self.editorial(), ensure_ascii=False), encoding="utf-8")
            plan = workflow.build_release_plan(
                args_for(
                    editorial_input=str(editorial_path),
                    description="索尼、华纳起诉Anthropic；Uber用AI Agent接管70%代码PR",
                )
            )
            plan_path = run_dir / "release-kit" / "release_plan.json"
            (run_dir / "run_report.json").write_text(json.dumps({"status": "success", "details": {"script": {"editorial": {"input_sha256": "frozen-input"}}}}), encoding="utf-8")
            (run_dir / "artifacts" / "quality_report.json").write_text(json.dumps({"status": "pass", "date": "2026-08-31", "input_sha256": "frozen-input"}), encoding="utf-8")
            video = run_dir / "renders" / "ai-daily-news-2026-08-31.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"valid-video")
            cover_dir = run_dir / "release-kit" / "covers"
            cover_dir.mkdir(parents=True)
            cover = cover_dir / "16x9.png"
            Image.new("RGB", (1920, 1080), (20, 40, 80)).save(cover)
            cover_manifest = {
                "schema_version": 3,
                "status": "pass",
                "input_sha256": "frozen-input",
                "selected_item": {"id": "anthropic"},
                "family_id": "2026-08-31-family-01",
                "formal_cover_count": 1,
                "anchor": {"ratio": "16:9"},
                "family_review": {
                    "status": "pass",
                    "palette_and_color_system": True,
                    "typography_mood_and_hierarchy": True,
                    "overall_editorial_style": True,
                    "brand_treatment": True,
                },
                "results": {
                    "16:9": {
                        "approval_status": "approved",
                        "normalized_file": str(cover),
                        "normalized_size": [1920, 1080],
                    }
                },
            }
            manifest_path = cover_dir / "cover_manifest.json"
            manifest_path.write_text(json.dumps(cover_manifest), encoding="utf-8")
            result = workflow.finalize_package(argparse.Namespace(
                release_plan=str(plan_path),
                run_dir=None,
                video=None,
                cover_manifest=None,
                output_dir=None,
                force=False,
            ))
            package_dir = Path(result["package_dir"])
            self.assertTrue((package_dir / "publish-copy.md").is_file())
            publish_copy_text = (package_dir / "publish-copy.md").read_text(encoding="utf-8")
            self.assertIn("标题：AI每日早报2026-08-31", publish_copy_text)
            self.assertIn("文章简介：索尼、华纳起诉Anthropic；Uber用AI Agent接管70%代码PR", publish_copy_text)
            self.assertNotIn("哔哩哔哩", publish_copy_text)
            self.assertNotIn("小红书", publish_copy_text)
            self.assertTrue((package_dir / "covers" / "16x9.png").is_file())
            self.assertTrue((package_dir / "videos" / video.name).is_file())
            package = json.loads((package_dir / "package.json").read_text(encoding="utf-8"))
            self.assertEqual(package["status"], "pass")
            self.assertEqual(len(package["files"]), 3)
            self.assertEqual(package["source_reports"]["cover_family_id"], "2026-08-31-family-01")
            reused = workflow.finalize_package(argparse.Namespace(
                release_plan=str(plan_path), run_dir=None, video=None,
                cover_manifest=None, output_dir=None, force=False,
            ))
            self.assertTrue(reused["reused"])

    def test_schema_five_packages_three_generated_files_without_visual_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "2026-08-31"
            editorial_path = run_dir / "artifacts" / "editorial_input.json"
            editorial_path.parent.mkdir(parents=True)
            editorial_path.write_text(
                json.dumps(self.editorial(), ensure_ascii=False), encoding="utf-8"
            )
            plan = workflow.build_release_plan(
                args_for(
                    editorial_input=str(editorial_path),
                    description="索尼、华纳起诉Anthropic；Uber用AI Agent接管70%代码PR",
                )
            )
            plan_path = run_dir / "release-kit" / "release_plan.json"
            (run_dir / "run_report.json").write_text(
                json.dumps({"status": "success", "details": {"script": {"editorial": {"input_sha256": "frozen-input"}}}}),
                encoding="utf-8",
            )
            (run_dir / "artifacts" / "quality_report.json").write_text(
                json.dumps({"status": "pass", "date": "2026-08-31", "input_sha256": "frozen-input"}),
                encoding="utf-8",
            )
            video = run_dir / "renders" / "ai-daily-news-2026-08-31.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"valid-video")
            cover_dir = run_dir / "release-kit" / "covers"
            cover_dir.mkdir(parents=True)
            sources = {}
            results = {}
            for index, ratio in enumerate(("16:9", "3:4", "9:16"), start=1):
                cover = cover_dir / f"{ratio.replace(':', 'x')}.png"
                cover.write_bytes((f"first-imagegen-result-{index}" * 17).encode("utf-8"))
                sources[ratio] = cover
                results[ratio] = {
                    "generation_status": "succeeded",
                    "attempts": 1,
                    "generated_file": str(cover),
                    "post_processing": False,
                }
            cover_manifest = {
                "schema_version": 5,
                "status": "complete_unreviewed",
                "generation_mode": "full_cover_imagegen",
                "post_processing": False,
                "attempts": 1,
                "input_sha256": "frozen-input",
                "selected_item": {"id": "anthropic"},
                "family_id": "2026-08-31-family-02",
                "formal_cover_count": 3,
                "results": results,
            }
            manifest_path = cover_dir / "cover_manifest.json"
            manifest_path.write_text(json.dumps(cover_manifest), encoding="utf-8")
            result = workflow.finalize_package(argparse.Namespace(
                release_plan=str(plan_path),
                run_dir=None,
                video=None,
                cover_manifest=None,
                output_dir=None,
                force=False,
            ))
            package_dir = Path(result["package_dir"])
            for ratio, source in sources.items():
                packaged = package_dir / "covers" / f"{ratio.replace(':', 'x')}.png"
                self.assertEqual(packaged.read_bytes(), source.read_bytes())
            package = json.loads((package_dir / "package.json").read_text(encoding="utf-8"))
            image_records = [value for value in package["files"] if value["media_type"] == "image/png"]
            self.assertEqual(len(image_records), 3)
            self.assertTrue(all("dimensions" not in value for value in image_records))
            self.assertEqual(package["source_reports"]["cover_schema_version"], 5)
            self.assertEqual(package["source_reports"]["cover_generation_mode"], "full_cover_imagegen")

    def test_schema_four_historical_logo_receipt_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "2026-08-31"
            editorial_path = run_dir / "artifacts" / "editorial_input.json"
            editorial_path.parent.mkdir(parents=True)
            editorial_path.write_text(
                json.dumps(self.editorial(), ensure_ascii=False), encoding="utf-8"
            )
            plan = workflow.build_release_plan(args_for(
                editorial_input=str(editorial_path),
                description="索尼、华纳起诉Anthropic",
            ))
            plan_path = run_dir / "release-kit" / "release_plan.json"
            (run_dir / "run_report.json").write_text(
                json.dumps({"status": "success", "details": {"script": {"editorial": {"input_sha256": "frozen-input"}}}}),
                encoding="utf-8",
            )
            (run_dir / "artifacts" / "quality_report.json").write_text(
                json.dumps({"status": "pass", "date": "2026-08-31", "input_sha256": "frozen-input"}),
                encoding="utf-8",
            )
            video = run_dir / "renders" / "ai-daily-news-2026-08-31.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"valid-video")
            cover_dir = run_dir / "release-kit" / "covers"
            cover_dir.mkdir(parents=True)
            cover = cover_dir / "16x9.png"
            Image.new("RGB", (1920, 1080), (20, 40, 80)).save(cover)
            manifest = {
                "schema_version": 4,
                "status": "pass",
                "input_sha256": "frozen-input",
                "selected_item": {"id": "anthropic"},
                "family_id": "2026-08-31-family-legacy-v4",
                "formal_cover_count": 1,
                "anchor": {"ratio": "16:9"},
                "family_review": {
                    "status": "pass",
                    "palette_and_color_system": True,
                    "typography_mood_and_hierarchy": True,
                    "overall_editorial_style": True,
                    "brand_treatment": True,
                },
                "brand_policy": {"required_brand": {"name": "Anthropic", "asset": "official.png"}},
                "results": {
                    "16:9": {
                        "approval_status": "approved",
                        "normalized_file": str(cover),
                        "brand_render": {"status": "pass", "visible": True},
                    }
                },
            }
            manifest_path = cover_dir / "cover_manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = workflow.finalize_package(argparse.Namespace(
                release_plan=str(plan_path), run_dir=None, video=None,
                cover_manifest=None, output_dir=None, force=False,
            ))
            package = json.loads((Path(result["package_dir"]) / "package.json").read_text(encoding="utf-8"))
            self.assertEqual(package["source_reports"]["cover_schema_version"], 4)
            image_record = next(value for value in package["files"] if value["media_type"] == "image/png")
            self.assertEqual(image_record["dimensions"], [1920, 1080])

    def test_update_video_only_preserves_frozen_cover_and_publish_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "2026-08-31"
            package_dir = run_dir / "release-kit" / "video-publish-package"
            (run_dir / "artifacts").mkdir(parents=True)
            (run_dir / "renders").mkdir()
            (package_dir / "covers").mkdir(parents=True)
            (package_dir / "videos").mkdir()
            (run_dir / "run_report.json").write_text(
                json.dumps({"status": "success"}), encoding="utf-8"
            )
            (run_dir / "artifacts" / "quality_report.json").write_text(
                json.dumps({"status": "pass", "date": "2026-08-31"}), encoding="utf-8"
            )
            publish_copy = package_dir / "publish-copy.md"
            publish_copy.write_text("冻结发布文案\n", encoding="utf-8")
            cover = package_dir / "covers" / "16x9.png"
            Image.new("RGB", (1920, 1080), (20, 40, 80)).save(cover)
            packaged_video = package_dir / "videos" / "ai-daily-news-2026-08-31.mp4"
            packaged_video.write_bytes(b"old-video")
            replacement = run_dir / "renders" / packaged_video.name
            replacement.write_bytes(b"new-rendered-video")
            package = {
                "schema_version": 2,
                "status": "pass",
                "date": "2026-08-31",
                "input_sha256": "old-frozen-input",
                "cover_story_item_id": "anthropic",
                "description_item_ids": ["anthropic"],
                "publish_copy": {"title": "AI每日早报2026-08-31", "description": "冻结"},
                "files": [
                    workflow.file_record(publish_copy, package_dir, "text/markdown"),
                    workflow.file_record(cover, package_dir, "image/png", [1920, 1080]),
                    workflow.file_record(packaged_video, package_dir, "video/mp4"),
                ],
                "source_reports": {
                    "cover_manifest_sha256": "old-cover-manifest",
                    "cover_schema_version": 4,
                    "video_sha256": workflow.sha256_file(packaged_video),
                },
                "publishing_performed": False,
            }
            (package_dir / "package.json").write_text(
                json.dumps(package, ensure_ascii=False), encoding="utf-8"
            )
            preserved_hashes = {
                "publish-copy.md": workflow.sha256_file(publish_copy),
                "covers/16x9.png": workflow.sha256_file(cover),
            }
            result = workflow.update_video_package(argparse.Namespace(
                package_dir=str(package_dir),
                run_dir=str(run_dir),
                video=str(replacement),
            ))
            self.assertEqual(result["updated"], "video-only")
            self.assertEqual(packaged_video.read_bytes(), replacement.read_bytes())
            self.assertEqual(workflow.sha256_file(publish_copy), preserved_hashes["publish-copy.md"])
            self.assertEqual(workflow.sha256_file(cover), preserved_hashes["covers/16x9.png"])
            updated = json.loads((package_dir / "package.json").read_text(encoding="utf-8"))
            self.assertEqual(updated["input_sha256"], "old-frozen-input")
            self.assertEqual(updated["source_reports"]["cover_manifest_sha256"], "old-cover-manifest")
            self.assertEqual(updated["source_reports"]["video_sha256"], workflow.sha256_file(replacement))
            video_record = next(record for record in updated["files"] if record["media_type"] == "video/mp4")
            self.assertEqual(video_record["sha256"], workflow.sha256_file(replacement))

    def test_finalize_requires_quality_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path = root / "release-kit" / "release_plan.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(json.dumps({"status": "pass", "validation": {"status": "pass"}}), encoding="utf-8")
            run_dir = root
            (run_dir / "run_report.json").write_text(json.dumps({"status": "success"}), encoding="utf-8")
            (run_dir / "artifacts").mkdir()
            (run_dir / "artifacts" / "quality_report.json").write_text(json.dumps({"status": "fail"}), encoding="utf-8")
            with self.assertRaises(workflow.ReleaseKitError):
                workflow.finalize_package(argparse.Namespace(
                    release_plan=str(plan_path), run_dir=str(run_dir), video=None,
                    cover_manifest=None, output_dir=None, force=False,
                ))


if __name__ == "__main__":
    unittest.main()
