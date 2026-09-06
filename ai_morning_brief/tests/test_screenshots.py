import json
import hashlib
import struct
import tempfile
import unittest
import zlib
from datetime import date
from pathlib import Path

from ai_morning_brief.aihot import load_fixture
from ai_morning_brief.materialize import materialize
from ai_morning_brief.screenshots import (
    CAPTURE_CONTRACT_ID,
    CAPTURE_METHOD,
    CAPTURE_STRATEGY,
    EXPANDED_VIEWPORT_HEIGHT,
    EXPANDED_VIEWPORT_WIDTH,
    ScreenshotError,
    ScreenshotPending,
    attach_screenshots_to_script,
    collect_screenshots,
    prepare_screenshot_requests,
    record_capture_preflight,
    source_visual_acceptance,
)
from ai_morning_brief.script import build_script
from ai_morning_brief.selection import select_items


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "aihot_24h.json"


def _fake_png(width: int = 640, height: int = 480) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class ScreenshotTests(unittest.TestCase):
    def setUp(self):
        response = load_fixture(FIXTURE)
        self.items = list(select_items(response.items).items)
        # The fixture has no X URL, so replace one source URL only in the
        # request-level test data; the production selector remains untouched.
        item = self.items[0]
        self.items[0] = type(item)(**{**item.__dict__, "original_url": "https://x.com/example/status/123"})

    def test_full_workflow_acceptance_rejects_off_and_zero_selected_stories(self):
        with self.assertRaises(ScreenshotError):
            source_visual_acceptance(mode="off", minimum_selected_stories=1)
        pending = source_visual_acceptance(mode="auto", minimum_selected_stories=1)
        self.assertEqual(pending["test_scope"], "full_workflow")
        self.assertEqual(pending["status"], "pending")
        failed = source_visual_acceptance(
            mode="auto", minimum_selected_stories=1, selected_stories=0
        )
        self.assertFalse(failed["requirement_met"])
        passed = source_visual_acceptance(
            mode="auto", minimum_selected_stories=1, selected_stories=1
        )
        self.assertTrue(passed["requirement_met"])

    def test_prepare_persists_full_workflow_acceptance_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            requests = prepare_screenshot_requests(
                run_dir,
                self.items[:1],
                mode="auto",
                minimum_selected_stories=1,
            )
            self.assertEqual(
                requests["acceptance"]["minimum_selected_stories"], 1
            )
            self.assertEqual(requests["acceptance"]["test_scope"], "full_workflow")

    def test_prepare_upgrades_matching_schema_two_request_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            first = prepare_screenshot_requests(
                run_dir,
                self.items[:1],
                mode="auto",
                minimum_selected_stories=1,
            )
            legacy = {**first, "version": "2.0"}
            legacy["acceptance"] = {
                **legacy["acceptance"],
                "target_max_selected_stories": 2,
            }
            request_path = run_dir / "artifacts" / "screenshot_requests.json"
            request_path.write_text(json.dumps(legacy), encoding="utf-8")

            upgraded = prepare_screenshot_requests(
                run_dir,
                self.items[:1],
                mode="auto",
                minimum_selected_stories=1,
            )

            self.assertEqual(upgraded["version"], "5.0")
            self.assertIsNone(upgraded["acceptance"]["target_max_selected_stories"])
            self.assertIsNone(upgraded["policy"]["max_video_visual_stories"])

    def test_matching_manifest_refreshes_expanded_viewport_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            first = prepare_screenshot_requests(run_dir, self.items[:1], mode="auto")
            legacy = json.loads(json.dumps(first))
            legacy["requests"][0]["capture_scope"] = "first_viewport_with_optional_image"
            legacy["requests"][0]["capture_contract"]["tight_element_bounds"] = True
            legacy["policy"]["capture_strategy"] = "tight_element_screenshot"
            (run_dir / "artifacts" / "screenshot_requests.json").write_text(json.dumps(legacy), encoding="utf-8")

            refreshed = prepare_screenshot_requests(run_dir, self.items[:1], mode="auto")

            self.assertEqual(refreshed["requests"][0]["capture_scope"], "original_post_only")
            self.assertFalse(refreshed["requests"][0]["capture_contract"]["tight_element_bounds"])
            self.assertEqual(refreshed["policy"]["capture_strategy"], CAPTURE_STRATEGY)
            self.assertEqual(
                refreshed["policy"]["viewport_override"],
                {"width": EXPANDED_VIEWPORT_WIDTH, "height": EXPANDED_VIEWPORT_HEIGHT},
            )

    def test_prepare_writes_expanded_viewport_capture_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            requests = prepare_screenshot_requests(run_dir, self.items[:1], mode="auto")
            request = requests["requests"][0]
            self.assertEqual(requests["policy"]["capture_strategy"], CAPTURE_STRATEGY)
            self.assertEqual(
                request["capture_contract"]["viewport_override"],
                {"width": EXPANDED_VIEWPORT_WIDTH, "height": EXPANDED_VIEWPORT_HEIGHT},
            )
            self.assertTrue(request["capture_contract"]["restore_viewport_after_capture"])
            self.assertEqual(CAPTURE_METHOD, "iab-expanded-viewport-screenshot")

    def test_manual_capture_is_gated_by_ready_marker_and_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            requests = prepare_screenshot_requests(run_dir, self.items[:1], mode="manual")
            self.assertEqual(requests["requests"][0]["naming_rule"], "source-fixture-model-01-visual-{index:02d}.{original_extension}")
            inbox = run_dir / requests["requests"][0]["inbox"]
            (inbox / "capture-02.png").write_bytes(_fake_png(2048, 1000))
            (inbox / "capture-01.png").write_bytes(_fake_png(2049, 1000))
            with self.assertRaises(ScreenshotPending):
                collect_screenshots(run_dir, mode="manual")
            (run_dir / "screenshots" / "READY").touch()
            manifest = collect_screenshots(run_dir, mode="manual")
            self.assertEqual(manifest["status"], "validated")
            self.assertEqual(manifest["total_pages"], 2)
            self.assertEqual([page["page"] for page in manifest["items"][0]["pages"]], [1, 2])
            self.assertTrue((run_dir / "source-visuals" / "presentation" / "source-fixture-model-01-visual-01.png").is_file())
            page = manifest["items"][0]["pages"][0]
            self.assertEqual(page["sha256"], page["presentation_sha256"])

    def test_auto_capture_requires_complete_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            requests = prepare_screenshot_requests(run_dir, self.items[:1], mode="auto")
            inbox = run_dir / requests["requests"][0]["inbox"]
            (inbox / "capture-01.png").write_bytes(_fake_png(2048, 1000))
            (inbox / "capture.json").write_text(json.dumps({
                "complete": True,
                "asset_count": 1,
                "translated": True,
                "source_url": "https://x.com/example/status/123",
                "capture_method": CAPTURE_METHOD,
                "capture_contract_id": CAPTURE_CONTRACT_ID,
                "capture_scope": "original_post_only",
                "capture_type": "viewport_screenshot",
                "asset_role": "x_original_post",
                "viewport": {"width": 1440, "height": 900},
                "viewport_override": {"width": 1440, "height": 900},
                "device_scale_factor": 2,
                "crop_box": {"x": 0, "y": 0, "width": 1440, "height": 900},
                "original_dimensions": {"width": 2048, "height": 1000},
                "evidence_text": "OpenAI 发布新一代推理模型",
            }), encoding="utf-8")
            manifest = collect_screenshots(run_dir, mode="auto")
            self.assertEqual(manifest["status"], "validated")
            self.assertTrue(manifest["items"][0]["translated"])
            self.assertEqual(manifest["items"][0]["attempts"], 1)
            self.assertEqual(manifest["items"][0]["terminal_state"], "validated")

    def test_auto_missing_capture_is_unavailable_once(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            prepare_screenshot_requests(run_dir, self.items[:1], mode="auto")
            first = collect_screenshots(run_dir, mode="auto")
            second = collect_screenshots(run_dir, mode="auto")
            for manifest in (first, second):
                item = manifest["items"][0]
                self.assertEqual(item["status"], "unavailable")
                self.assertEqual(item["terminal_state"], "unavailable")
                self.assertEqual(item["attempts"], 1)
                self.assertEqual(item["capture_executor"], "none")
                self.assertEqual(item["error_code"], "browser_capture_unavailable")
            request = json.loads((run_dir / "artifacts" / "source_visual_requests.json").read_text(encoding="utf-8"))["requests"][0]
            self.assertEqual(request["terminal_state"], "unavailable")
            self.assertEqual(request["attempts"], 1)
            self.assertEqual(request["error_code"], "browser_capture_unavailable")

    def test_failed_preflight_never_validates_or_retries_inbox_files(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            requests = prepare_screenshot_requests(run_dir, self.items[:1], mode="auto")
            inbox = run_dir / requests["requests"][0]["inbox"]
            (inbox / "capture-01.png").write_bytes(_fake_png(2048, 1000))
            record_capture_preflight(
                run_dir,
                browser_control=False,
                direct_project_file_write=False,
                expanded_viewport_screenshot=False,
                error_code="browser_capture_unavailable",
            )
            manifest = collect_screenshots(run_dir, mode="auto")
            self.assertEqual(manifest["items"][0]["status"], "unavailable")
            self.assertEqual(manifest["items"][0]["error_code"], "browser_capture_unavailable")
            self.assertEqual(manifest["items"][0]["attempts"], 1)

    def test_prepare_writes_source_specific_capture_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            web = type(self.items[1])(**{**self.items[1].__dict__, "original_url": "https://example.com/article/123"})
            requests = prepare_screenshot_requests(run_dir, [self.items[0], web], mode="auto")
            by_kind = {request["source_kind"]: request for request in requests["requests"]}
            self.assertEqual(by_kind["x"]["capture_scope"], "original_post_only")
            self.assertEqual(by_kind["x"]["capture_contract"]["allowed_asset_roles"], ["x_original_post"])
            self.assertEqual(by_kind["web"]["capture_scope"], "main_content_with_optional_image")
            self.assertEqual(by_kind["web"]["capture_contract"]["allowed_asset_roles"], ["article_main_content", "article_image"])
            self.assertFalse(by_kind["web"]["capture_contract"]["allow_scrolling"])
            self.assertFalse(requests["policy"]["post_processing"])

    def test_auto_x_rejects_reply_or_repost_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            requests = prepare_screenshot_requests(run_dir, self.items[:1], mode="auto")
            inbox = run_dir / requests["requests"][0]["inbox"]
            (inbox / "capture-01.png").write_bytes(_fake_png(2048, 1000))
            (inbox / "capture-02.png").write_bytes(_fake_png(2049, 1000))
            (inbox / "capture.json").write_text(json.dumps({
                "complete": True,
                "asset_count": 2,
                "source_url": "https://x.com/example/status/123",
                "capture_method": CAPTURE_METHOD,
                "capture_contract_id": CAPTURE_CONTRACT_ID,
                "capture_scope": "original_post_only",
                "capture_type": "element_screenshot",
                "asset_role": "x_reply",
                "viewport": {"width": 1440, "height": 1000},
                "viewport_override": {"width": 1440, "height": 900},
                "device_scale_factor": 2,
                "crop_box": {"x": 0, "y": 0, "width": 1440, "height": 1000},
                "original_dimensions": {"width": 2048, "height": 1000},
                "evidence_text": "OpenAI 发布新一代推理模型",
            }), encoding="utf-8")
            manifest = collect_screenshots(run_dir, mode="auto")
            self.assertEqual(manifest["items"][0]["status"], "unavailable")
            self.assertEqual(manifest["items"][0]["terminal_state"], "unavailable")
            self.assertEqual(manifest["items"][0]["error_code"], "capture_contract_invalid")
            self.assertIn("exactly one asset", manifest["items"][0]["error"])

    def test_auto_web_accepts_title_viewport_and_one_article_image(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            web = type(self.items[0])(**{**self.items[0].__dict__, "original_url": "https://example.com/article/123"})
            requests = prepare_screenshot_requests(run_dir, [web], mode="auto")
            inbox = run_dir / requests["requests"][0]["inbox"]
            (inbox / "capture-01.png").write_bytes(_fake_png(2048, 1000))
            (inbox / "capture-02.png").write_bytes(_fake_png(1600, 900))
            (inbox / "capture.json").write_text(json.dumps({
                "complete": True,
                "asset_count": 2,
                "source_url": "https://example.com/article/123",
                "capture_method": CAPTURE_METHOD,
                "capture_contract_id": CAPTURE_CONTRACT_ID,
                "capture_scope": "main_content_with_optional_image",
                "viewport": {"width": 1440, "height": 900},
                "viewport_override": {"width": 1440, "height": 900},
                "device_scale_factor": 2,
                "crop_box": {"x": 0, "y": 0, "width": 1440, "height": 900},
                "original_dimensions": {"width": 2048, "height": 1000},
                "files": [
                    {"file": "capture-01.png", "asset_role": "article_main_content", "capture_type": "viewport_screenshot", "title_visible": True, "content_bounds": {"x": 0, "y": 0, "width": 1024, "height": 500}, "evidence_text": "OpenAI 发布新一代推理模型"},
                    {"file": "capture-02.png", "asset_role": "article_image", "capture_type": "image_asset", "text_bearing": False, "evidence_text": "OpenAI 发布新一代推理模型"},
                ],
            }), encoding="utf-8")
            manifest = collect_screenshots(run_dir, mode="auto")
            self.assertEqual(manifest["items"][0]["status"], "validated")
            self.assertEqual([page["asset_role"] for page in manifest["items"][0]["pages"]], ["article_main_content", "article_image"])
            self.assertTrue(all(page["sha256"] == page["presentation_sha256"] for page in manifest["items"][0]["pages"]))

    def test_auto_web_rejects_scrolling_capture_or_missing_title(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            web = type(self.items[0])(**{**self.items[0].__dict__, "original_url": "https://example.com/article/123"})
            requests = prepare_screenshot_requests(run_dir, [web], mode="auto")
            inbox = run_dir / requests["requests"][0]["inbox"]
            (inbox / "capture-01.png").write_bytes(_fake_png(2048, 1000))
            (inbox / "capture.json").write_text(json.dumps({
                "complete": True,
                "asset_count": 1,
                "source_url": "https://example.com/article/123",
                "capture_method": CAPTURE_METHOD,
                "capture_contract_id": CAPTURE_CONTRACT_ID,
                "capture_scope": "main_content_with_optional_image",
                "capture_type": "viewport_screenshot",
                "asset_role": "article_main_content",
                "content_bounds": {"x": 0, "y": 0, "width": 1024, "height": 500},
                "title_visible": True,
                "viewport": {"width": 1440, "height": 900},
                "viewport_override": {"width": 1440, "height": 900},
                "device_scale_factor": 2,
                "crop_box": {"x": 0, "y": 0, "width": 1024, "height": 500},
                "original_dimensions": {"width": 2048, "height": 1000},
                "evidence_text": "OpenAI 发布新一代推理模型",
            }), encoding="utf-8")
            manifest = collect_screenshots(run_dir, mode="auto")
            self.assertEqual(manifest["items"][0]["status"], "unavailable")
            self.assertEqual(manifest["items"][0]["terminal_state"], "unavailable")
            self.assertEqual(manifest["items"][0]["error_code"], "capture_contract_invalid")
            self.assertIn("direct viewport screenshot", manifest["items"][0]["error"])

    def test_schema_five_web_assets_attach_in_sequence(self):
        item_id = "web-item"
        script = {
            "editorial": {"plan_version": "5.0"},
            "source_item_ids": [item_id],
            "segments": [{
                "id": "story-01",
                "kind": "news",
                "source_item_ids": [item_id],
                "source_fragments": [{"claim_id": "claim-1", "source_item_id": item_id, "source_text": "具体事实"}],
                "narration_beats": [
                    {"beat_id": "story-01-beat-01", "type": "fact", "claim_ids": ["claim-1"]},
                    {"beat_id": "story-01-beat-02", "type": "evidence", "claim_ids": ["claim-1"], "visual_asset_id": "source-web-item-visual-01"},
                    {"beat_id": "story-01-beat-03", "type": "evidence", "claim_ids": ["claim-1"], "visual_asset_id": "source-web-item-visual-02"},
                ],
                "visual_plan": {},
            }],
        }
        manifest = {
            "schema_version": 5,
            "mode": "auto",
            "status": "validated",
            "items": [{
                "item_id": item_id,
                "source_kind": "web",
                "status": "validated",
                "pages": [
                    {"asset_id": "source-web-item-visual-01", "item_id": item_id, "asset_role": "article_main_content", "evidence_text": "具体事实", "path": "source-visuals/presentation/first.png"},
                    {"asset_id": "source-web-item-visual-02", "item_id": item_id, "asset_role": "article_image", "evidence_text": "具体事实", "path": "source-visuals/presentation/image.png"},
                ],
            }],
        }
        attached = attach_screenshots_to_script(script, manifest)
        selected = attached["segments"][0]["visual_plan"]["screenshots"]
        self.assertEqual([page["asset_id"] for page in selected], ["source-web-item-visual-01", "source-web-item-visual-02"])
        self.assertEqual([page["bound_beat_id"] for page in selected], ["story-01-beat-02", "story-01-beat-03"])
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            for page, filename in zip(selected, ("first.png", "image.png")):
                payload = _fake_png(2048, 1000)
                source = run_dir / "source-visuals" / "presentation" / filename
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(payload)
                page["path"] = str(source.relative_to(run_dir))
                page["presentation_sha256"] = hashlib.sha256(payload).hexdigest()
            cues = [
                {"start": 1.0, "end": 2.0, "visual_asset_id": "source-web-item-visual-01"},
                {"start": 2.0, "end": 3.0, "visual_asset_id": "source-web-item-visual-02"},
            ]
            from ai_morning_brief.materialize import materialize
            materialize(run_dir, attached, {"story-01": 10.0}, cues)
            html = (run_dir / "hyperframes" / "index.html").read_text(encoding="utf-8")
            self.assertEqual(html.count('class="source-visual-item"'), 2)
            self.assertIn('data-source-visual-count="2"', html)

    def test_all_original_links_are_requested_and_auto_failures_are_soft(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            non_x = list(self.items)
            non_x[0] = type(non_x[0])(**{**non_x[0].__dict__, "original_url": "https://example.com/post/123"})
            requests = prepare_screenshot_requests(run_dir, non_x, mode="auto")
            self.assertEqual(len(requests["requests"]), len(non_x))
            self.assertEqual(requests["requests"][0]["source_kind"], "web")
            manifest = collect_screenshots(run_dir, mode="auto")
            self.assertEqual(manifest["status"], "validated_with_skips")
            self.assertEqual(manifest["total_pages"], 0)
            self.assertTrue((run_dir / "artifacts" / "source_visual_manifest.json").is_file())

    def test_attached_pages_extend_story_floor_and_materialize(self):
        response = load_fixture(FIXTURE)
        script = build_script(select_items(response.items), run_date=date(2026, 8, 28))
        first_news = next(segment for segment in script["segments"] if segment["kind"] == "news")
        manifest = {
            "mode": "manual",
            "status": "validated",
            "total_pages": 1,
            "items": [{
                "item_id": first_news["source_item_id"],
                "pages": [
                    {"asset_id": "source-fixture-model-01-visual-01", "item_id": first_news["source_item_id"], "page": 1, "path": "source-visuals/presentation/page-1.png", "duration_seconds": 4.5, "width": 2048, "height": 1000, "effective_scale": 0.62, "sha256": "a"},
                ],
            }],
        }
        attached = attach_screenshots_to_script(script, manifest)
        attached_news = next(segment for segment in attached["segments"] if segment["kind"] == "news")
        self.assertEqual(len(attached_news["visual_plan"]["screenshots"]), 1)
        self.assertGreater(attached_news["minimum_duration_seconds"], 12.0)
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            for filename in ("page-1.png",):
                path = run_dir / "source-visuals" / "presentation" / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(_fake_png(2048, 1000))
            result = materialize(run_dir, attached, {segment["id"]: 10.0 for segment in attached["segments"]}, [])
            html = (run_dir / "hyperframes" / "index.html").read_text(encoding="utf-8")
            self.assertIn("source-visual-layer", html)
            self.assertEqual(html.count("class=\"source-visual-item\""), 1)
            self.assertNotIn("原帖截图", html)
            self.assertNotIn("X 原帖", html)
            self.assertIn("story-card-stage", html)
            self.assertIn("timelineNumeric", html)
            self.assertTrue((run_dir / "hyperframes" / "assets" / "source-visuals" / "source-fixture-model-01-visual-01.png").is_file())
            self.assertEqual(result["scene_count"], len(attached["segments"]))

    def test_every_eligible_story_can_receive_one_visual(self):
        response = load_fixture(FIXTURE)
        script = build_script(select_items(response.items), run_date=date(2026, 8, 28))
        news = [segment for segment in script["segments"] if segment["kind"] == "news"]
        manifest = {
            "mode": "auto",
            "status": "validated",
            "items": [
                {
                    "item_id": segment["source_item_id"],
                    "status": "validated",
                    "pages": [{"item_id": segment["source_item_id"], "page": 1, "path": f"source-visuals/presentation/{index}.png", "width": 640, "height": 480, "sha256": str(index)}],
                }
                for index, segment in enumerate(news[:3], 1)
            ],
        }
        attached = attach_screenshots_to_script(script, manifest)
        selected = [segment for segment in attached["segments"] if segment["kind"] == "news" and segment["visual_plan"]["screenshots"]]
        self.assertEqual(len(selected), 3)

    def test_tight_text_capture_can_be_upscaled_without_recapture(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            requests = prepare_screenshot_requests(run_dir, self.items[:1], mode="auto")
            inbox = run_dir / requests["requests"][0]["inbox"]
            (inbox / "capture-01.png").write_bytes(_fake_png(752, 367))
            receipt = {
                "complete": True,
                "asset_count": 1,
                "translated": False,
                "source_url": "https://x.com/example/status/123",
                "capture_method": CAPTURE_METHOD,
                "capture_contract_id": CAPTURE_CONTRACT_ID,
                "capture_scope": "original_post_only",
                "capture_type": "viewport_screenshot",
                "asset_role": "x_original_post",
                "viewport": {"width": 1440, "height": 900},
                "viewport_override": {"width": 1440, "height": 900},
                "device_scale_factor": 2,
                "crop_box": {"x": 0, "y": 0, "width": 1440, "height": 900},
                "original_dimensions": {"width": 752, "height": 367},
                "evidence_text": "OpenAI 发布新一代推理模型",
                "capture_attempt": 1,
            }
            (inbox / "capture.json").write_text(json.dumps(receipt), encoding="utf-8")
            manifest = collect_screenshots(run_dir, mode="auto")
            self.assertEqual(manifest["items"][0]["status"], "validated")
            self.assertTrue(manifest["items"][0]["pages"][0]["presentation_upscaled"])

    def test_presentation_copy_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            requests = prepare_screenshot_requests(run_dir, self.items[:1], mode="manual")
            inbox = run_dir / requests["requests"][0]["inbox"]
            source = inbox / "capture-01.png"
            source.write_bytes(_fake_png(2048, 1000))
            (run_dir / "screenshots" / "READY").touch()
            manifest = collect_screenshots(run_dir, mode="manual")
            destination = run_dir / manifest["items"][0]["pages"][0]["path"]
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), hashlib.sha256(destination.read_bytes()).hexdigest())
