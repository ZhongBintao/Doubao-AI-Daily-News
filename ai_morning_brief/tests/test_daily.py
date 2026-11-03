"""Offline tests for the v6 pipeline (newsroom, director, timeline, render markup, publish, orchestrator)."""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ai_morning_brief import config as C
from ai_morning_brief import daily, director, newsroom, publish, render, timeline
from ai_morning_brief.aihot_v2 import AIHotV2Client

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "aihot_v2"
RUN_DATE = date(2026, 10, 30)
LATER = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)


def fixture_selection():
    client = AIHotV2Client(fixture_dir=FIXTURES)
    snapshot = newsroom.gather(client, RUN_DATE, now=LATER)
    return snapshot, newsroom.select_edition(snapshot, [], RUN_DATE)


class NewsroomTests(unittest.TestCase):
    def test_edition_window_past_date_ends_at_daily_cutoff(self):
        window = newsroom.edition_window(RUN_DATE, LATER)
        self.assertEqual(window["end"], datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(window["end"] - window["start"], timedelta(hours=24))
        self.assertFalse(window["live"])
        self.assertTrue(window["daily_available"])

    def test_edition_window_early_morning_is_live_and_ends_now(self):
        now = datetime(2026, 10, 29, 22, 30, tzinfo=timezone.utc)  # 06:30 Beijing on 10-30
        window = newsroom.edition_window(RUN_DATE, now)
        self.assertEqual(window["end"], now)
        self.assertTrue(window["live"])
        self.assertFalse(window["daily_available"])

    def test_selection_uses_daily_and_scores_across_categories(self):
        snapshot, selection = fixture_selection()
        self.assertEqual(selection["sources_used"]["selected_items"], 21)
        self.assertTrue(selection["sources_used"]["daily"])
        self.assertGreaterEqual(len(selection["recommended"]["headlines"]), 1)
        self.assertLessEqual(len(selection["recommended"]["headlines"]) + len(selection["recommended"]["flashes"]), C.RUNDOWN_MAX_STORIES)
        lead = selection["pool"][0]["lead"]
        self.assertTrue(lead["signals"]["daily_lead"], "the daily lead model launch should rank first")
        heats = [c["heat"] for c in selection["pool"]]
        self.assertEqual(heats, sorted(heats, reverse=True))

    def test_reports_of_one_event_are_clustered(self):
        _, selection = fixture_selection()
        mistral = [c for c in selection["clusters"] if "Mistral" in c["lead"]["title"] or "Le Chonk" in c["lead"]["title"]]
        self.assertEqual(len(mistral), 1)
        self.assertGreaterEqual(len(mistral[0]["member_ids"]), 2)

    def test_aired_event_is_pushed_down(self):
        snapshot, selection = fixture_selection()
        top = selection["pool"][0]["lead"]
        aired = [{"date": "2026-10-29", "item_ids": [top["item_id"]], "title": top["title"]}]
        again = newsroom.select_edition(snapshot, aired, RUN_DATE)
        self.assertNotIn(top["item_id"], again["recommended"]["headlines"])
        self.assertTrue(any(c["aired"] for c in again["clusters"]))

    def test_same_event_requires_shared_subject(self):
        self.assertTrue(newsroom.same_event("Anthropic 递交 S-1 拟上市", "Anthropic S-1 文件披露现金储备"))
        self.assertFalse(newsroom.same_event("Google 发布 Gemini 4", "OpenAI 合并 Codex 额度"))

    def test_quiet_day_gets_one_light_headline(self):
        cluster = newsroom.EventCluster(key="a", lead=newsroom.Candidate(item_id="a", title="小更新", heat=70), members=[], heat=70)
        rundown = newsroom.plan_rundown([cluster])
        self.assertEqual(rundown["mode"], "quiet")
        self.assertEqual(len(rundown["headlines"]), 1)


class DirectorTests(unittest.TestCase):
    def setUp(self):
        _, self.selection = fixture_selection()

    def test_fallback_plan_is_always_valid(self):
        plan = director.fallback_plan(self.selection)
        self.assertEqual(director.validate_plan(plan, self.selection), [])

    def test_validator_reports_field_addressed_chinese_errors(self):
        plan = director.fallback_plan(self.selection)
        plan["picks"]["flashes"].append("not-in-pool")
        plan["hook"]["text"] = "这是一个特别特别特别特别长、完全超过二十八个字预算的开头钩子句子【一】【二】"
        plan["stories"][0]["narration"][0]["text"] = "数字 99999 是编造的"
        errors = director.validate_plan(plan, self.selection)
        joined = "\n".join(errors)
        self.assertIn("不在 pool 里", joined)
        self.assertIn("hook.text：超过", joined)
        self.assertIn("最多只能有一对【】", joined)
        self.assertIn("必须以句号", joined)
        self.assertIn("99999", joined)

    def test_related_titles_count_as_evidence(self):
        cluster = {"lead": {"item_id": "m", "title": "Mistral Large 4 登陆", "summary": "总参数约 1T"},
                   "member_titles": ["Mistral Large 4 登陆", "vLLM 发布 Mistral Large 4 Day 0 支持"]}
        material = director.story_material(cluster)
        self.assertEqual(material["related_titles"], ["vLLM 发布 Mistral Large 4 Day 0 支持"])
        self.assertEqual(director._unsupported_tokens("vLLM 首日支持 Day 0", material["_material"]), [])

    def test_card_references_must_cover_every_card_in_order(self):
        plan = director.fallback_plan(self.selection)
        story = plan["stories"][0]
        story["narration"] = [{"text": "只讲第一张卡。", "card": 1}]
        errors = director.validate_plan(plan, self.selection)
        self.assertTrue(any("没有被任何一句旁白讲到" in e for e in errors))

    def test_finalize_builds_scenes_and_sentence_units(self):
        plan = director.fallback_plan(self.selection)
        final = director.finalize(plan, self.selection, source="fallback")
        kinds = [s["kind"] for s in final["scenes"]]
        self.assertEqual(kinds[0], "hook")
        self.assertEqual(kinds[-1], "outro")
        self.assertEqual(final["units"][0]["scene_id"], "hook")
        self.assertTrue(all(u["spoken_text"] for u in final["units"]))
        self.assertNotIn("【", "".join(u["display_text"] for u in final["units"]))

    def test_examples_respect_budgets(self):
        examples = director.load_examples()
        self.assertGreaterEqual(len(examples), 4)
        for example in examples:
            selection = {"pool": [{"lead": {"item_id": "x", "title": example["material_excerpt"], "summary": json.dumps(example, ensure_ascii=False)}, "member_titles": []}]}
            plan = {"version": C.V6_VERSION, "picks": {"headlines": ["x"], "flashes": []}, "hook": example["hook"], "stories": [dict(example["story"], item_id="x")]}
            self.assertEqual(director.validate_plan(plan, selection), [], example["hook_style"])

    def test_task_contains_steps_pool_and_examples(self):
        task = director.build_task(RUN_DATE, self.selection, plan_path=Path("plan.json"), check_command="check")
        self.assertTrue(task["steps"])
        self.assertEqual(len(task["pool"]), len(self.selection["pool"]))
        self.assertNotIn("_material", task["pool"][0])
        self.assertTrue(task["examples"])


class TimelineTests(unittest.TestCase):
    def test_split_caption_never_cuts_ascii_words(self):
        chunks = timeline.split_caption("Hugging Face 发布 Mistral Large 4（Le Chonk）的上线博客，模型为多模态多语言 MoE 架构。", maximum=20)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), "Hugging Face 发布 Mistral Large 4（Le Chonk）的上线博客，模型为多模态多语言 MoE 架构。")
        for chunk in chunks:
            self.assertNotRegex(chunk, r"^[a-z]")

    def test_timeline_places_units_with_pauses(self):
        _, selection = fixture_selection()
        final = director.finalize(director.fallback_plan(selection), selection, source="fallback")
        voice = {"units": [{"unit_id": u["unit_id"], "duration": 2.0} for u in final["units"]]}
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "artifacts").mkdir()
            tl = timeline.build_timeline(run_dir, final, voice)
            self.assertEqual(tl["scenes"][0]["start"], 0.0)
            starts = [u["start"] for u in tl["units"]]
            self.assertEqual(starts, sorted(starts))
            self.assertAlmostEqual(tl["units"][1]["start"] - tl["units"][0]["end"], C.PAUSE_AFTER_HOOK, places=3)
            self.assertTrue((run_dir / "assets" / "subtitles" / "subtitles.srt").is_file())
            for cue in tl["captions"]:
                self.assertLess(cue["start"], cue["end"])


class RenderMarkupTests(unittest.TestCase):
    def test_marks_become_emphasis_and_text_is_escaped(self):
        scene = {"scene_id": "hook", "kind": "hook", "category": "paper", "subject": "<OpenAI>", "hook": "只改 【0.016%】 的权重", "keyfact": "a&b", "progress": {"role": "头条", "index": 1, "total": 3}}
        markup = render.scene_markup(scene, RUN_DATE, {})
        self.assertIn("<em>0.016%</em>", markup)
        self.assertIn("&lt;OpenAI&gt;", markup)
        self.assertIn("a&amp;b", markup)
        self.assertIn("头条 1/3", markup)

    def test_story_cards_carry_index_and_highlight(self):
        scene = {"scene_id": "story-01", "kind": "story", "category": "ai-models", "subject": "X", "title": "T", "section": "模型发布",
                 "cards": [{"label": "实测", "headline": "涨到 6 倍", "body": "从 7.9 升至 【48.1】"}], "progress": {"role": "头条", "index": 1, "total": 2}}
        markup = render.scene_markup(scene, RUN_DATE, {})
        self.assertIn('data-card="1"', markup)
        self.assertIn('<span class="hl">48.1</span>', markup)


class PublishTests(unittest.TestCase):
    def test_copy_has_short_xhs_title_and_hashtags(self):
        _, selection = fixture_selection()
        final = director.finalize(director.fallback_plan(selection), selection, source="fallback")
        copy = publish.build_copy(RUN_DATE, final, 70)
        self.assertLessEqual(len(copy["xiaohongshu"]["title"]), publish.XHS_TITLE_MAX)
        self.assertEqual(copy["xiaohongshu"]["title"].count("（"), copy["xiaohongshu"]["title"].count("）"))
        self.assertIn("#AI", copy["hashtags"])
        self.assertIn("AI每日早报", copy["tiktok"]["title"])


class OrchestratorTests(unittest.TestCase):
    def make_run(self, tmp, **kw):
        return daily.Run(RUN_DATE, Path(tmp), tts=kw.get("tts", "doubao"), no_agent=kw.get("no_agent", False), fixture_dir=FIXTURES)

    def test_director_hands_off_then_falls_back_without_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.make_run(tmp)
            daily.stage_fetch(run)
            with self.assertRaises(daily.Handoff):
                daily.stage_director(run)
            self.assertTrue((run.art / "director_task.json").is_file())
            auto = self.make_run(tmp, no_agent=True)
            result = daily.stage_director(auto)
            self.assertEqual(result["source"], "fallback-no-agent")
            self.assertTrue((run.art / "director_final.json").is_file())

    def test_invalid_plan_retries_once_then_uses_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.make_run(tmp)
            daily.stage_fetch(run)
            (run.art / "director_plan.json").write_text(json.dumps({"version": "6.0", "picks": {"headlines": ["nope"]}}), encoding="utf-8")
            with self.assertRaises(daily.Handoff):
                daily.stage_director(run)
            result = daily.stage_director(run)
            self.assertEqual(result["source"], "fallback-after-invalid")
            self.assertTrue((run.art / "director_plan_rejected.json").is_file())

    def test_valid_agent_plan_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.make_run(tmp)
            daily.stage_fetch(run)
            selection = json.loads((run.art / "selection.json").read_text(encoding="utf-8"))
            (run.art / "director_plan.json").write_text(json.dumps(director.fallback_plan(selection), ensure_ascii=False), encoding="utf-8")
            ok, errors = daily.check_plan(run)
            self.assertTrue(ok, errors)
            self.assertEqual(daily.stage_director(run)["source"], "agent")

    def test_doubao_voice_hands_off_with_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.make_run(tmp, no_agent=False)
            daily.stage_fetch(run)
            daily.stage_director(self.make_run(tmp, no_agent=True))
            with self.assertRaises(daily.Handoff):
                daily.stage_voice(run)
            manifest = json.loads((run.art / "tts_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["provider"], "doubao")
            self.assertIn("audio_to_audio_plus", json.dumps(manifest, ensure_ascii=False))

    def test_status_and_check_plan_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(daily.main(["--date", RUN_DATE.isoformat(), "--output-root", tmp, "--status"]), 0)
            self.assertEqual(daily.main(["--date", RUN_DATE.isoformat(), "--output-root", tmp, "--check-plan"]), 1)


if __name__ == "__main__":
    unittest.main()
