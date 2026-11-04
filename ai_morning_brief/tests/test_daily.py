"""Offline tests for the v6 pipeline (newsroom, director, timeline, render markup, publish, orchestrator)."""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ai_morning_brief import config as C
from ai_morning_brief import daily, director, material, newsroom, publish, render, timeline
from ai_morning_brief.aihot_v2 import AIHotV2Client

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "aihot_v2"
LIVE_SNAPSHOT = FIXTURES / "snapshot_2026-11-03_live.json"   # a real enriched live edition (stories, searches, related)
LIVE_PLAN = Path(__file__).resolve().parents[2] / "docs" / "handoff" / "2026-11-03-director_plan.json"
RUN_DATE = date(2026, 10, 30)
LATER = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)


def fixture_selection():
    client = AIHotV2Client(fixture_dir=FIXTURES)
    snapshot = newsroom.enrich(client, newsroom.gather(client, RUN_DATE, now=LATER))
    return snapshot, newsroom.select_edition(snapshot, [], RUN_DATE)


def live_selection():
    snapshot = json.loads(LIVE_SNAPSHOT.read_text(encoding="utf-8"))
    return snapshot, newsroom.select_edition(snapshot, [], date(2026, 11, 3))


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
        self.assertLessEqual(len(selection["recommended"]["headlines"]), C.RUNDOWN_MAX_HEADLINES)
        self.assertLessEqual(len(selection["recommended"]["flashes"]), C.RUNDOWN_MAX_FLASHES)
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

    def test_examples_respect_budgets_and_cite_only_their_excerpt(self):
        examples = director.load_examples()
        self.assertGreaterEqual(len([e for e in examples if e["kind"] == "headline"]), 4)
        self.assertTrue(any(e["kind"] == "flash" for e in examples))

        def item(item_id, example):
            facts = [{"kind": "摘要", "source": "", "date": "", "text": t} for t in example["material_excerpt"]]
            pack = {"facts": facts, "background": [], "angle_hint": "", "fact_chars": example["material_chars"], "background_chars": 0,
                    "source_count": 0, "depth": example["material_depth"]}
            return {"lead": {"item_id": item_id, "title": example["story"]["title"], "summary": ""}, "member_titles": [], "pack": pack}

        head = next(e for e in examples if e["kind"] == "headline")
        for example in examples:
            if example["kind"] == "headline":
                selection = {"pool": [item("x", example)]}
                plan = {"version": director.PLAN_VERSION, "picks": {"headlines": ["x"], "flashes": []}, "hook": example["hook"],
                        "stories": [dict(example["story"], item_id="x")]}
            else:
                selection = {"pool": [item("h", head), item("x", example)]}
                plan = {"version": director.PLAN_VERSION, "picks": {"headlines": ["h"], "flashes": ["x"]}, "hook": head["hook"],
                        "stories": [dict(head["story"], item_id="h"), dict(example["story"], item_id="x")]}
            self.assertEqual(director.validate_plan(plan, selection), [], example["hook_style"])
            self.assertEqual(len(example["story"]["title_candidates"]), C.TITLE_CANDIDATES)

    def test_task_contains_steps_pool_and_examples(self):
        task = director.build_task(RUN_DATE, self.selection, plan_path=Path("plan.json"), check_command="check")
        self.assertTrue(task["steps"])
        self.assertEqual(len(task["pool"]), len(self.selection["pool"]))
        self.assertNotIn("_material", task["pool"][0])
        self.assertTrue(task["examples"])


class FallbackTextTests(unittest.TestCase):
    """The program's own fallback writer must always produce a plan that passes the checks."""

    def test_full_stop_never_pushes_a_sentence_over_the_limit(self):
        text = "Anthropic 开发 FlashProve 开源系统，用密码学证明 AI 输出来自哪个模型"   # exactly 42 units without "。"
        self.assertEqual(director.units(text), C.BUDGET_SENTENCE_MAX)
        out = director._fit(text, C.BUDGET_SENTENCE_MAX, terminal=True)
        self.assertLessEqual(director.units(out), C.BUDGET_SENTENCE_MAX)
        self.assertTrue(out.endswith("。"))

    def test_numbers_with_ascii_commas_are_never_split(self):
        self.assertEqual(director._clauses("每 1,024 token 请求，证明约 19 秒：比例 16:9，完成"),
                         ["每 1,024 token 请求，", "证明约 19 秒：", "比例 16:9，", "完成"])

    def test_links_are_removed_and_brackets_stay_closed(self):
        text = ("技术报告讲述其研发过程：https://huggingface.co/x/y.pdf，Mistral AI 官方转发并称可在 alphaxiv 上阅读"
                "（https://www.alphaxiv.org/abs/2611.x）。")
        self.assertEqual(director._sentences(text), ["技术报告讲述其研发过程，Mistral AI 官方转发并称可在 alphaxiv 上阅读。"])
        self.assertEqual(director._fit("宣布 ML4（Mistral Large 4）正式开放权重，称其在代码智能体与网络安全方向具备前沿能力", 18), "宣布 ML4")


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


class DoubaoTakeTests(unittest.TestCase):
    """The Doubao tag, the speaking-rate memory and laying a take back down on the timeline."""

    @staticmethod
    def _audio(*parts):
        import math
        from array import array
        from ai_morning_brief import voice
        out = array("h")
        for kind, seconds in parts:
            n = int(seconds * voice.SAMPLE_RATE)
            out.extend(int(7000 * math.sin(2 * math.pi * 220 * i / voice.SAMPLE_RATE)) for i in range(n)) if kind == "v" else out.extend([0] * n)
        return out

    def test_tag_and_the_pause_after_it_are_cut(self):
        from ai_morning_brief import voice
        samples = self._audio(("s", 0.32), ("v", 1.93), ("s", 0.40), ("v", 3.0), ("s", 0.3))
        cut = voice.watermark_cut(samples) / voice.SAMPLE_RATE
        self.assertTrue(2.55 <= cut <= 2.66, cut)

    def test_a_short_pause_inside_the_tag_is_not_the_end(self):
        from ai_morning_brief import voice
        samples = self._audio(("s", 0.32), ("v", 0.6), ("s", 0.20), ("v", 1.13), ("s", 0.30), ("v", 3.0))
        cut = voice.watermark_cut(samples) / voice.SAMPLE_RATE
        self.assertTrue(2.5 <= cut <= 2.6, cut)

    def test_colleague_rule_rms_300_and_first_pause_of_018s(self):
        import math
        from array import array
        from ai_morning_brief import voice
        rate = voice.SAMPLE_RATE

        def tone(seconds, amp):
            return [int(amp * math.sin(2 * math.pi * 220 * i / rate)) for i in range(int(seconds * rate))]

        # no leading silence, tag 0.0-2.25 s, a pause that is quiet but not digital silence (RMS ~140 < 300), text
        samples = array("h", tone(2.25, 7000) + tone(0.20, 200) + tone(3.0, 7000))
        scan = voice.watermark_scan(samples)
        self.assertEqual(scan["status"], "cut")
        self.assertAlmostEqual(scan["onset_seconds"], 0.0, places=2)
        self.assertAlmostEqual(scan["pause_start_seconds"], 2.25, delta=0.011)
        self.assertAlmostEqual(scan["cut"] / rate, 2.45, delta=0.011, msg="the text starts where the pause ends")
        # a 0.17 s pause is too short to end the tag
        short = array("h", tone(0.33, 0) + tone(1.92, 7000) + tone(0.17, 0) + tone(3.0, 7000))
        self.assertIsNone(voice.watermark_cut(short))

    def test_audio_without_the_tag_is_never_trimmed(self):
        from ai_morning_brief import voice
        # "OpenAI，……": a comma pause 0.85 s in, then speech through the whole tag window
        self.assertIsNone(voice.watermark_cut(self._audio(("s", 0.05), ("v", 0.8), ("s", 0.2), ("v", 3.0))))
        self.assertIsNone(voice.watermark_cut(self._audio(("v", 4.0))))
        # a pause well after the window is a sentence break, not the tag
        self.assertIsNone(voice.watermark_cut(self._audio(("s", 0.3), ("v", 3.5), ("s", 0.4), ("v", 2.0))))

    def test_edge_audio_is_not_touched_by_the_tag_rule(self):
        from ai_morning_brief import voice
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            final = {"units": [{"unit_id": "u01", "scene_id": "hook", "spoken_text": "OpenAI，今天发布了新模型。", "display_text": "OpenAI，今天发布了新模型。"}]}
            manifest = voice.build_manifest(run_dir, final, "edge")
            voice.write_wav(Path(manifest["units"][0]["output_path"]), self._audio(("s", 0.1), ("v", 1.9), ("s", 0.35), ("v", 2.0), ("s", 0.1)))
            result = voice.finalize(run_dir, manifest)
            self.assertAlmostEqual(result["units"][0]["duration"], 1.9 + 0.35 + 2.0 + 0.08, delta=0.03)

    def test_speech_rate_is_remembered_as_a_median(self):
        from ai_morning_brief import voice
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.assertEqual(voice.load_speech_rate(state), C.DOUBAO_SPEECH_RATE_DEFAULT)
            voice.record_speech_rate(state, "2026-11-04", [{"take": "t01", "rate": 5.4}, {"take": "t02", "rate": 5.8}, {"take": "t03", "rate": 42}])
            voice.record_speech_rate(state, "2026-11-05", [{"take": "t01", "rate": 6.0}])
            voice.record_speech_rate(state, "2026-11-05", [{"take": "t01", "rate": 6.0}])   # a re-run does not count twice
            self.assertEqual(voice.load_speech_rate(state), 5.8)

    def test_timeline_lays_a_take_back_down_and_tops_up_story_breaks(self):
        final = {"scenes": [{"scene_id": "hook", "kind": "hook"}, {"scene_id": "story-01", "kind": "story"},
                            {"scene_id": "story-02", "kind": "story"}, {"scene_id": "outro", "kind": "outro"}],
                 "units": [{"unit_id": "u01", "scene_id": "hook", "display_text": "钩子。", "caption": False},
                           {"unit_id": "u02", "scene_id": "story-01", "display_text": "第一句。", "card": 1},
                           {"unit_id": "u03", "scene_id": "story-01", "display_text": "第二句。", "card": 2},
                           {"unit_id": "u04", "scene_id": "story-02", "display_text": "第三句。", "card": 1},
                           {"unit_id": "u05", "scene_id": "outro", "display_text": "关注我。"}]}
        # one take u01-u04 (pieces carry half pauses); u05 from a second take
        units = [("u01", 2.0, 0.02, 0.10, "t01"), ("u02", 3.0, 0.10, 0.15, "t01"), ("u03", 3.0, 0.15, 0.10, "t01"),
                 ("u04", 3.0, 0.10, 0.05, "t01"), ("u05", 2.0, 0.03, 0.03, "t02")]
        voice = {"units": [{"unit_id": u, "duration": d, "lead": a, "tail": b, "take_id": t} for u, d, a, b, t in units]}
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "artifacts").mkdir()
            tl = timeline.build_timeline(run_dir, final, voice)
            u = {x["unit_id"]: x for x in tl["units"]}
            self.assertAlmostEqual(u["u01"]["speech_start"], timeline.HOOK_SPEECH_START, places=3)
            self.assertAlmostEqual(u["u03"]["start"], u["u02"]["end"], places=3, msg="same take, same story: exactly as read")
            # hook -> story: natural 0.10 + 0.10 = 0.20 is topped up to PAUSE_AFTER_HOOK
            self.assertAlmostEqual(u["u02"]["speech_start"] - u["u01"]["speech_end"], C.PAUSE_AFTER_HOOK, places=3)
            # story -> story inside the take: 0.10 + 0.10 topped up to PAUSE_BETWEEN_STORIES
            self.assertAlmostEqual(u["u04"]["speech_start"] - u["u03"]["speech_end"], C.PAUSE_BETWEEN_STORIES, places=3)
            self.assertAlmostEqual(u["u05"]["speech_start"] - u["u04"]["speech_end"], C.PAUSE_BETWEEN_STORIES, places=3)
            # a natural pause longer than the minimum is kept, never shortened
            voice["units"][2]["tail"] = 0.5
            tl = timeline.build_timeline(run_dir, final, voice)
            u = {x["unit_id"]: x for x in tl["units"]}
            self.assertAlmostEqual(u["u04"]["speech_start"] - u["u03"]["speech_end"], 0.6, places=3)
            # cards and captions follow the voice, not the silence around it
            self.assertEqual(tl["card_events"]["story-01"][1]["start"], u["u03"]["speech_start"])
            first_caption = next(c for c in tl["captions"] if c["onscreen"])
            self.assertAlmostEqual(first_caption["start"], u["u02"]["speech_start"], places=3)


class RenderMarkupTests(unittest.TestCase):
    def test_marks_become_emphasis_and_text_is_escaped(self):
        scene = {"scene_id": "hook", "kind": "hook", "category": "paper", "subject": "<OpenAI>", "hook": "只改 【0.016%】 的权重", "keyfact": "a&b", "progress": {"role": "头条", "index": 1, "total": 3}}
        markup = render.scene_markup(scene, RUN_DATE, {})
        self.assertIn("<em>0.016%</em>", markup)
        self.assertIn("&lt;OpenAI&gt;", markup)
        self.assertIn("a&amp;b", markup)
        header = render.header_markup([scene], RUN_DATE)
        self.assertIn("头条 1/3", header)
        self.assertIn('data-for="hook"', header)
        self.assertNotIn("header", markup, "the header lives in one persistent layer, not inside each scene")

    def test_story_cards_carry_index_and_highlight(self):
        scene = {"scene_id": "story-01", "kind": "story", "category": "ai-models", "subject": "X", "title": "T", "section": "模型发布",
                 "cards": [{"label": "实测", "headline": "涨到 6 倍", "body": "从 7.9 升至 【48.1】"}], "progress": {"role": "头条", "index": 1, "total": 2}}
        markup = render.scene_markup(scene, RUN_DATE, {})
        self.assertIn('data-card="1"', markup)
        self.assertIn('<span class="hl">48.1</span>', markup)
        self.assertIn('class="more"', markup)

    def test_card_icons_follow_labels_and_flash_has_two_cards(self):
        icons = {render.icon_for(label) for label in C.CARD_LABELS}
        self.assertEqual(len(icons), len(C.CARD_LABELS), "every card label has its own icon")
        scene = {"scene_id": "story-02", "kind": "flash", "category": "industry", "subject": "OpenAI", "title": "T", "section": "行业动态",
                 "point": "发生了 【1】 件事", "why": "所以呢", "progress": {"role": "快讯", "index": 2, "total": 2}}
        markup = render.scene_markup(scene, RUN_DATE, {})
        self.assertIn('data-card="2"', markup)
        self.assertIn("为什么值得关注", markup)

    def test_palette_follows_section_and_outro_has_no_source_line(self):
        scenes = [{"scene_id": "story-01", "kind": "story", "progress": {"role": "头条", "index": 1, "total": 2}},
                  {"scene_id": "story-02", "kind": "flash", "progress": {"role": "快讯", "index": 2, "total": 2}}]
        header = render.header_markup(scenes, RUN_DATE)
        self.assertIn('<i class="head on"></i><i class="flash on"></i>', header)
        outro = render.scene_markup({"scene_id": "outro", "kind": "outro", "recap": [{"role": "快讯", "title": "T"}]}, RUN_DATE, {})
        self.assertNotIn("AIHOT", outro)
        self.assertIn('class="tag flash"', outro)
        template = render.TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn(".cat-industry", template, "news category no longer drives colour")

    def test_logo_chip_adds_name_only_for_symbol_logos(self):
        scene = {"scene_id": "s", "subject": "Grok"}
        self.assertIn('class="name"', render._subject_chip(scene, {"s": {"src": "a.png", "wide": False}}))
        self.assertNotIn('class="name"', render._subject_chip(scene, {"s": {"src": "a.png", "wide": True}}))


class PublishTests(unittest.TestCase):
    def test_copy_has_short_xhs_title_and_hashtags(self):
        _, selection = fixture_selection()
        final = director.finalize(director.fallback_plan(selection), selection, source="fallback")
        copy = publish.build_copy(RUN_DATE, final, 70)
        self.assertLessEqual(len(copy["xiaohongshu"]["title"]), publish.XHS_TITLE_MAX)
        self.assertEqual(copy["xiaohongshu"]["title"].count("（"), copy["xiaohongshu"]["title"].count("）"))
        self.assertIn("#AI", copy["hashtags"])
        self.assertIn("AI每日早报", copy["tiktok"]["title"])
        self.assertIn(publish.SOURCE_NOTE, copy["xiaohongshu"]["body"])
        self.assertEqual(publish._short("不要 AI？那 Google 搜索也别用了", publish.XHS_TITLE_MAX), "不要AI？那Google搜索也别用了")
        self.assertIn(publish.SOURCE_NOTE, copy["tiktok"]["caption"])
        self.assertIn(C.OUTRO_TEXT, copy["tiktok"]["caption"])


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
            self.assertEqual(manifest["doubao"]["tool"], "text_to_audio_plus")
            self.assertEqual(manifest["doubao"]["voice"], C.DOUBAO_TTS_VOICE_PROFILE)
            self.assertEqual(manifest["doubao"]["speech_rate"], C.DOUBAO_SPEECH_RATE_DEFAULT)
            final = json.loads((run.art / "director_final.json").read_text(encoding="utf-8"))
            self.assertEqual([u["spoken_text"] for u in manifest["units"]], [u["display_text"] for u in final["units"]],
                             "Doubao reads the raw sentence: no 'G P T' spelling, no Chinese numerals")
            # Long takes: a few requests, every take under the engine limit with margin.
            self.assertTrue(manifest["todo"])
            self.assertLess(len(manifest["todo"]), len(manifest["units"]))
            self.assertEqual([u for t in manifest["takes"] for u in t["unit_ids"]], [u["unit_id"] for u in manifest["units"]])
            for take in manifest["takes"]:
                self.assertIn(C.DOUBAO_TTS_VOICE_PROFILE, take["request"])
                self.assertIn(take["spoken_text"], take["request"])
                self.assertLessEqual(take["estimated_seconds"] * C.DOUBAO_TAKE_SAFETY, C.DOUBAO_TAKE_MAX_SECONDS + C.DOUBAO_WATERMARK_SECONDS)
                self.assertNotIn("语速", take["request"].replace(C.DOUBAO_TTS_VOICE_PROFILE, ""))

    def test_doubao_reads_raw_text_and_edge_reads_normalised_text(self):
        from ai_morning_brief import voice
        with tempfile.TemporaryDirectory() as tmp:
            final = {"units": [{"unit_id": "u01", "scene_id": "hook", "spoken_text": "G P T 六。", "display_text": "GPT-6。"}]}
            manifest = voice.build_manifest(Path(tmp), final, "doubao")
            self.assertEqual(manifest["doubao"]["tool"], "text_to_audio_plus")
            self.assertNotIn("reference_audio", manifest["doubao"])
            self.assertEqual(manifest["units"][0]["spoken_text"], "GPT-6。")
            edge = voice.build_manifest(Path(tmp), final, "edge")
            self.assertEqual(edge["units"][0]["spoken_text"], "G P T 六。")

    def test_doubao_keeps_natural_speed(self):
        from ai_morning_brief import voice
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            final = {"units": [{"unit_id": "u01", "scene_id": "hook", "spoken_text": "一句比较长的话，用来测试语速。", "display_text": "一句比较长的话，用来测试语速。"}]}
            manifest = voice.build_manifest(run_dir, final, "doubao")
            samples = self._tagged(self._speech_like(["一句比较长的话，用来测试语速。"])[0])
            voice.write_wav(Path(manifest["takes"][0]["output_path"]), samples)
            self.assertEqual(voice.prepare_doubao(run_dir, run_dir / "artifacts" / "tts_manifest.json", manifest), [])
            result = voice.finalize(run_dir, manifest)
            spoken = self._speech_like(["一句比较长的话，用来测试语速。"])[0]
            self.assertAlmostEqual(result["units"][0]["duration"], voice._voiced_bounds(spoken)[1] / voice.SAMPLE_RATE
                                   - voice._voiced_bounds(spoken)[0] / voice.SAMPLE_RATE, delta=0.06)
            self.assertNotIn("tempo", result)

    def test_min_stories_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = daily.main(["--date", RUN_DATE.isoformat(), "--output-root", tmp, "--fixture-dir", str(FIXTURES), "--min-stories", "99"])
            self.assertEqual(code, daily.EXIT_STOPPED)
            state = json.loads((Path(tmp) / RUN_DATE.isoformat() / "run_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["stages"]["fetch"]["status"], "stopped")
            self.assertIn("少于要求的 99 条", state["stages"]["fetch"]["error"])

    def test_rebuilt_manifest_drops_audio_of_changed_sentences(self):
        from ai_morning_brief import voice
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            final = {"units": [{"unit_id": "u01", "scene_id": "hook", "spoken_text": "旧句子。", "display_text": "旧句子。"},
                               {"unit_id": "u02", "scene_id": "outro", "spoken_text": "不变。", "display_text": "不变。"}]}
            manifest = voice.build_manifest(run_dir, final, "doubao")
            for unit in manifest["units"]:
                Path(unit["output_path"]).parent.mkdir(parents=True, exist_ok=True)
                Path(unit["output_path"]).write_bytes(b"audio")
            final["units"][0]["spoken_text"] = final["units"][0]["display_text"] = "新句子。"
            manifest = voice.build_manifest(run_dir, final, "doubao")
            self.assertEqual(voice.missing_units(manifest), ["u01"])

    # -- Doubao batches: few requests, cut back into sentences ---------------------------
    @staticmethod
    def _speech_like(sentences, *, word_gap=0.07, comma_gap=0.30, sentence_gap=0.45, per_unit=0.16, flat=False):
        """Tone bursts shaped like speech: words, comma pauses and sentence pauses. Returns samples and true gap spans."""
        import math
        from array import array
        from ai_morning_brief import voice
        rate = voice.SAMPLE_RATE
        out, gaps = array("h"), []

        def tone(seconds, freq=210):
            out.extend(int(7000 * math.sin(2 * math.pi * freq * i / rate)) for i in range(int(seconds * rate)))

        def silence(seconds):
            out.extend([0] * int(seconds * rate))

        silence(0.2)
        for index, text in enumerate(sentences):
            for c, clause in enumerate(text.rstrip("。").split("，")):
                if c:
                    (tone if flat else silence)(comma_gap)
                for w in range(0, len(clause), 2):
                    if w:
                        (tone if flat else silence)(word_gap)
                    tone(per_unit * len(clause[w:w + 2]), 180 + 40 * (w % 3))
            if index < len(sentences) - 1:
                start = len(out) / rate
                (tone if flat else silence)(sentence_gap)
                gaps.append((start, len(out) / rate))
        silence(0.3)
        return out, gaps

    @staticmethod
    def _tagged(samples, *, lead=0.32, tag=1.93, after=0.35):
        """Prefix a Doubao-style tag: silence, ~1.9 s of speech ending at 2.25 s, a pause, then the text."""
        import math
        from array import array
        from ai_morning_brief import voice
        rate = voice.SAMPLE_RATE
        out = array("h", [0] * int(lead * rate))
        out.extend(int(7000 * math.sin(2 * math.pi * 230 * i / rate)) for i in range(int(tag * rate)))
        out.extend([0] * int(after * rate))
        out.extend(samples[int(0.2 * rate):])   # _speech_like starts with 0.2 s of silence
        return out

    def test_takes_split_between_stories_balanced_and_under_the_limit(self):
        from ai_morning_brief import voice
        units = ([{"unit_id": "u01", "scene_id": "hook", "spoken_text": "钩" * 20}]
                 + [{"unit_id": f"u{i:02d}", "scene_id": "story-01", "spoken_text": "头" * 40} for i in range(2, 9)]
                 + [{"unit_id": f"u{i:02d}", "scene_id": "story-02", "spoken_text": "二" * 40} for i in range(9, 14)]
                 + [{"unit_id": f"u{i:02d}", "scene_id": f"flash-0{i - 13}", "spoken_text": "快" * 50} for i in range(14, 18)]
                 + [{"unit_id": "u18", "scene_id": "outro", "spoken_text": "关" * 15}])
        # 20 + 280 + 200 + 200 + 15 = 715 units at 5/s = 143 s -> two takes of ~70 s, not 100 + 43
        takes = voice.plan_takes(units, rate=5.0)
        self.assertEqual(len(takes), 2)
        self.assertEqual(takes[0][0], "u01", "the hook is read with the first story")
        self.assertEqual(takes[-1][-1], "u18", "the outro is read with the last story")
        scene = {u["unit_id"]: u["scene_id"] for u in units}
        self.assertNotEqual(scene[takes[0][-1]], scene[takes[1][0]], "a take boundary falls between two stories")
        self.assertEqual(takes[0][-1], "u08")
        self.assertEqual([u for t in takes for u in t], [u["unit_id"] for u in units])
        # A short edition is one request.
        self.assertEqual(len(voice.plan_takes(units[:8], rate=5.0)), 1)
        # A single story longer than the limit is split between sentences into balanced parts.
        big = [{"unit_id": f"u{i:02d}", "scene_id": "story-01", "spoken_text": "长" * 40} for i in range(1, 21)]
        parts = voice.plan_takes(big, rate=5.0)
        self.assertEqual([len(p) for p in parts], [10, 10])
        limit = C.DOUBAO_TAKE_MAX_SECONDS / C.DOUBAO_TAKE_SAFETY - C.DOUBAO_WATERMARK_SECONDS
        for part in voice.plan_takes(units * 1, rate=3.0):
            self.assertLessEqual(sum(voice.sentence_weight(u["spoken_text"]) for u in units if u["unit_id"] in part) / 3.0, limit)

    def test_takes_for_a_real_edition(self):
        from ai_morning_brief import voice
        _, selection = live_selection()
        plan = json.loads(LIVE_PLAN.read_text(encoding="utf-8"))
        final = director.finalize(plan, selection, source="agent")
        units = [{**u, "spoken_text": u["display_text"]} for u in final["units"]]
        kind = {s["scene_id"]: s["kind"] for s in final["scenes"]}
        takes = voice.plan_takes(units, rate=C.DOUBAO_SPEECH_RATE_DEFAULT, kind_of=kind)
        self.assertEqual(len(takes), 2, "~165 s of narration -> two takes")
        scene = {u["unit_id"]: u["scene_id"] for u in units}
        for a, b in zip(takes, takes[1:]):
            self.assertNotEqual(scene[a[-1]], scene[b[0]])
        for take in takes:
            seconds = sum(voice.sentence_weight(u["spoken_text"]) for u in units if u["unit_id"] in take) / C.DOUBAO_SPEECH_RATE_DEFAULT
            self.assertLessEqual((seconds + C.DOUBAO_WATERMARK_SECONDS) * C.DOUBAO_TAKE_SAFETY, C.DOUBAO_TAKE_MAX_SECONDS)
            self.assertGreater(seconds, 40, "balanced: no tiny last take")

    def test_doubao_batch_is_cut_back_into_sentences(self):
        from ai_morning_brief import voice
        sentences = ["OpenAI 说，ChatGPT 的搜索量已相当于 Google 的 20%。", "作为参照，Bing 的份额约 3%。",
                     "周活从十亿涨到了十二亿，用了两个月。", "整个过程不需要接触模型权重。", "关注我，每天看懂 AI 大事。"]
        samples, gaps = self._speech_like(sentences)
        cut = voice.split_batch(samples, sentences)
        self.assertIsNotNone(cut)
        for (start, end), (gap_start, gap_end) in zip(cut["ranges"][:-1], gaps):
            self.assertTrue(gap_start <= end / voice.SAMPLE_RATE <= gap_end, (end / voice.SAMPLE_RATE, gap_start, gap_end))

    def test_doubao_take_tag_removed_cut_and_finalized(self):
        from ai_morning_brief import voice
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            texts = ["第一条头条讲了一件大事。", "它的关键数字是十二亿，很惊人。", "关注我，每天看懂大事。"]
            final = {"units": [{"unit_id": f"u0{i + 1}", "scene_id": "story-01", "spoken_text": t, "display_text": t} for i, t in enumerate(texts)]}
            manifest = voice.build_manifest(run_dir, final, "doubao")
            self.assertEqual([t["id"] for t in manifest["todo"]], ["t01"])
            self.assertIn("第一条头条讲了一件大事。它的关键数字", manifest["todo"][0]["request"])
            samples, gaps = self._speech_like(texts)
            voice.write_wav(Path(manifest["takes"][0]["output_path"]), self._tagged(samples))
            path = run_dir / "artifacts" / "tts_manifest.json"
            self.assertEqual(voice.prepare_doubao(run_dir, path, manifest), [])
            self.assertEqual(voice.missing_units(manifest), [])
            saved = json.loads(path.read_text(encoding="utf-8"))["takes"][0]
            self.assertEqual(saved["split"]["status"], "ok")
            self.assertEqual(saved["watermark"]["status"], "cut")
            result = voice.finalize(run_dir, manifest)
            self.assertEqual(len(result["units"]), 3)
            self.assertEqual(result["doubao_takes"]["requests"], 1)
            self.assertEqual(result["doubao_takes"]["watermark_not_found"], [])
            first = result["units"][0]
            self.assertEqual(first["take_id"], "t01")
            self.assertLess(first["lead"], 0.1, "the tag and the pause after it are gone")
            # Pieces keep their halves of the natural pause: tail + next lead ~ the real sentence gap.
            gap = gaps[0][1] - gaps[0][0]
            self.assertAlmostEqual(result["units"][0]["tail"] + result["units"][1]["lead"], gap, delta=0.08)
            # The measured rate is kept for the next edition.
            state = json.loads(voice.rate_state_path(run_dir.parent / "_state").read_text(encoding="utf-8"))
            self.assertEqual([m["take"] for m in state["measurements"] if m["date"] == run_dir.name], ["t01"])

    def test_doubao_take_without_pauses_is_asked_again_in_halves(self):
        from ai_morning_brief import voice
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            texts = ["第一句话比较长一些。", "第二句。", "第三句话也不短。"]
            final = {"units": [{"unit_id": f"u0{i + 1}", "scene_id": "story-01", "spoken_text": t, "display_text": t} for i, t in enumerate(texts)]}
            manifest = voice.build_manifest(run_dir, final, "doubao")
            path = run_dir / "artifacts" / "tts_manifest.json"
            voice.write_wav(Path(manifest["takes"][0]["output_path"]), self._tagged(self._speech_like(texts, flat=True)[0]))
            todo = voice.prepare_doubao(run_dir, path, manifest)
            self.assertEqual([t["id"] for t in todo], ["t01-1", "t01-2"])
            takes = {t["take_id"]: t for t in manifest["takes"]}
            self.assertEqual(takes["t01"]["replaced_by"], ["t01-1", "t01-2"])
            self.assertEqual(takes["t01-1"]["unit_ids"], ["u01"])
            self.assertEqual(takes["t01-2"]["unit_ids"], ["u02", "u03"])
            self.assertIn("第二句。第三句话也不短。", todo[1]["request"])
            # Still no pauses in the half: it is halved again, down to single sentences.
            voice.write_wav(Path(takes["t01-1"]["output_path"]), self._tagged(self._speech_like(texts[:1])[0]))
            voice.write_wav(Path(takes["t01-2"]["output_path"]), self._tagged(self._speech_like(texts[1:], flat=True)[0]))
            todo = voice.prepare_doubao(run_dir, path, manifest)
            self.assertEqual([t["id"] for t in todo], ["t01-2-1", "t01-2-2"])
            self.assertEqual(voice.missing_units(manifest), ["u02", "u03"])

    def test_doubao_resume_keeps_finished_sentences(self):
        from ai_morning_brief import voice
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            final = {"units": [{"unit_id": "u01", "scene_id": "story-01", "spoken_text": "甲" * 200, "display_text": "甲" * 200},
                               {"unit_id": "u02", "scene_id": "story-02", "spoken_text": "乙" * 200, "display_text": "乙" * 200}]}
            manifest = voice.build_manifest(run_dir, final, "doubao")
            self.assertEqual([t["id"] for t in manifest["todo"]], ["t01"], "~80 s of text is one take")
            self.assertEqual(manifest["takes"][0]["unit_ids"], ["u01", "u02"])
            self.assertIn("\n", manifest["takes"][0]["spoken_text"], "a line break between two stories")
            # Sentences already synthesised (an older, interrupted run) are kept, not asked again.
            Path(manifest["units"][0]["output_path"]).parent.mkdir(parents=True, exist_ok=True)
            Path(manifest["units"][0]["output_path"]).write_bytes(b"audio")
            again = voice.build_manifest(run_dir, final, "doubao")
            self.assertEqual([(t["id"], again["takes"][0]["unit_ids"]) for t in again["todo"]], [("t01", ["u02"])])

    def test_doubao_take_that_cannot_be_cut_is_halved_between_stories(self):
        from ai_morning_brief import voice
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            final = {"units": [{"unit_id": f"u{i:02d}", "scene_id": f"story-0{(i - 1) // 2 + 1}", "spoken_text": "句" * 60 + "。", "display_text": "句" * 60 + "。"}
                               for i in range(1, 7)]}
            manifest = voice.build_manifest(run_dir, final, "doubao")
            self.assertEqual([t["id"] for t in manifest["todo"]], ["t01"])
            samples, _ = self._speech_like([u["spoken_text"] for u in final["units"]], flat=True)
            voice.write_wav(Path(manifest["takes"][0]["output_path"]), self._tagged(samples))
            todo = voice.prepare_doubao(run_dir, run_dir / "artifacts" / "tts_manifest.json", manifest)
            takes = {t["take_id"]: t for t in manifest["takes"]}
            self.assertEqual([t["id"] for t in todo], ["t01-1", "t01-2"])
            self.assertEqual(takes["t01-1"]["unit_ids"], ["u01", "u02"], "halved at a story boundary")
            self.assertEqual(takes["t01-2"]["unit_ids"], ["u03", "u04", "u05", "u06"])

    def test_doubao_truncated_take_is_asked_again(self):
        from array import array
        from ai_morning_brief import voice
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            final = {"units": [{"unit_id": "u01", "scene_id": "story-01", "spoken_text": "甲" * 200 + "。", "display_text": "甲" * 200 + "。"},
                               {"unit_id": "u02", "scene_id": "story-02", "spoken_text": "乙" * 200 + "。", "display_text": "乙" * 200 + "。"}]}
            manifest = voice.build_manifest(run_dir, final, "doubao")
            tone, _ = self._speech_like(["一二三四五六七八九十，一二三四五六七八九十。"])
            long = array("h", tone * int(C.DOUBAO_TAKE_MAX_SECONDS / (len(tone) / voice.SAMPLE_RATE) + 1))
            voice.write_wav(Path(manifest["takes"][0]["output_path"]), self._tagged(long))
            todo = voice.prepare_doubao(run_dir, run_dir / "artifacts" / "tts_manifest.json", manifest)
            self.assertEqual([t["id"] for t in todo], ["t01-1", "t01-2"])
            self.assertEqual(manifest["takes"][0]["split"]["status"], "truncated")

    def test_status_and_check_plan_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(daily.main(["--date", RUN_DATE.isoformat(), "--output-root", tmp, "--status"]), 0)
            self.assertEqual(daily.main(["--date", RUN_DATE.isoformat(), "--output-root", tmp, "--check-plan"]), 1)


class MusicPlacementTests(unittest.TestCase):
    def test_ending_is_tail_aligned_and_loops_crossfade(self):
        import numpy as np
        from ai_morning_brief import audio_mix
        track = np.arange(10, dtype=np.float32).reshape(-1, 1).repeat(2, axis=1)
        self.assertEqual(audio_mix._tail(track, 4)[:, 0].tolist(), [6, 7, 8, 9])
        self.assertEqual(audio_mix._tail(track, 12)[:2, 0].tolist(), [0, 0])
        long = audio_mix._fit_length(np.ones((audio_mix.SR * 4, 2), dtype=np.float32), audio_mix.SR * 10)
        self.assertEqual(len(long), audio_mix.SR * 10)
        self.assertLess(float(np.max(np.abs(np.diff(long[:, 0])))), 0.01, "loop seams are crossfaded, never a hard cut")


class MaterialPackTests(unittest.TestCase):
    """Material packs use AIHOT data only and decide how deep each story can go."""

    @classmethod
    def setUpClass(cls):
        cls.snapshot, cls.selection = live_selection()

    def test_live_packs_gather_multi_source_aihot_text(self):
        top = self.selection["pool"][0]
        kinds = {f["kind"] for f in top["pack"]["facts"]}
        self.assertIn("AIHOT 综述", kinds)
        self.assertIn("来源报道", kinds)
        self.assertGreaterEqual(top["pack"]["fact_chars"], C.PACK_DEPTH_RICH)
        self.assertEqual(top["budget"]["depth"], "rich")
        for cluster in self.selection["pool"]:
            self.assertNotIn("original", json.dumps(cluster["pack"], ensure_ascii=False), "original article links are never part of a pack")

    def test_one_storyline_is_merged_into_one_event(self):
        anthropic = [c for c in self.selection["pool"] if "Anthropic" in c["lead"]["title"] and ("S-1" in " ".join(c["member_titles"]) or "$895" in c["lead"]["title"])]
        self.assertEqual(len(anthropic), 1)
        self.assertGreaterEqual(len(anthropic[0]["member_ids"]), 3)

    def test_relation_uses_containment_not_shared_product_names(self):
        lead = "马斯克承认Grok Bot成本高于收入并下调限额"
        text = lead + " 马斯克在 X 平台回应称，周末部分用户 Grok Bot 每周额度被用满并非故障，原因是服务成本相对于收入而言高到难以为继，并已下调用量限制。"
        same = {"title": "Elon Musk 以成本过高为由下调 Grok Bot 用量限制", "summary": "马斯克在 X 上称 Grok Bot 服务成本相对于收入高到难以为继，已下调用量限制，部分用户每周额度被用满。"}
        noise = {"title": "Grok Bot 新功能一览", "summary": "购物卡片、商品页面、订单跟踪、支付、自定义模式、团队 Bot 登录与设置、分享与查找团队 Bot 等功能演示。"}
        self.assertEqual(material.relation(lead, text, same), "same")
        self.assertEqual(material.relation(lead, text, noise), "")

    def test_search_queries_use_subject_and_key_term(self):
        self.assertEqual(material.search_queries("OpenAI 推出 Grid Protectors 计划")[0], "OpenAI Grid")
        self.assertEqual(material.search_queries("没有任何主体的标题"), [])

    def test_rundown_count_follows_content(self):
        rec = self.selection["recommended"]
        self.assertGreaterEqual(len(rec["headlines"]), 2)
        self.assertLessEqual(len(rec["headlines"]), C.RUNDOWN_MAX_HEADLINES)
        self.assertLessEqual(len(rec["flashes"]), C.RUNDOWN_MAX_FLASHES)
        pool = {c["lead"]["item_id"]: c for c in self.selection["pool"]}
        self.assertTrue(all(pool[i]["budget"]["can_headline"] for i in rec["headlines"]))

    def test_heavy_but_thin_event_becomes_a_flash(self):
        def cluster(key, heat, depth):
            k = newsroom.EventCluster(key=key, lead=newsroom.Candidate(item_id=key, title=f"标题 {key}", heat=heat), members=[], heat=heat)
            k.pack = {"depth": depth, "fact_chars": 900 if depth == "rich" else 100}
            return k
        rundown = newsroom.plan_rundown([cluster("a", 95, "rich"), cluster("b", 92, "thin"), cluster("c", 90, "rich")])
        self.assertEqual([k.key for k in rundown["headlines"]], ["a", "c"])
        self.assertEqual([k.key for k in rundown["flashes"]], ["b"])

    def test_close_heat_prefers_more_material(self):
        def cluster(key, heat, depth):
            k = newsroom.EventCluster(key=key, lead=newsroom.Candidate(item_id=key, title=f"标题 {key}", heat=heat), members=[], heat=heat)
            k.pack = {"depth": depth, "fact_chars": {"rich": 1200, "medium": 400, "thin": 90}[depth]}
            return k
        head = cluster("h", 95, "rich")
        thins = [cluster(f"t{i}", 84, "thin") for i in range(5)]
        rich = cluster("r", 80, "rich")      # 4 points below, but far more material
        far = cluster("f", 77, "rich")       # 7 points below: heat still wins
        rundown = newsroom.plan_rundown([head, *thins, rich, far])
        keys = [k.key for k in rundown["flashes"]]
        self.assertIn("r", keys)
        self.assertNotIn("f", keys)
        self.assertEqual(len(keys), C.RUNDOWN_MAX_FLASHES)

    def test_no_time_cap_keeps_every_slot(self):
        clusters = []
        for i in range(10):
            k = newsroom.EventCluster(key=f"k{i}", lead=newsroom.Candidate(item_id=f"k{i}", title=f"T{i}", heat=100 - i), members=[], heat=100 - i)
            k.pack = {"depth": "rich", "fact_chars": 2000}
            clusters.append(k)
        rundown = newsroom.plan_rundown(clusters)
        self.assertEqual(len(rundown["headlines"]), C.RUNDOWN_MAX_HEADLINES)
        self.assertEqual(len(rundown["flashes"]), C.RUNDOWN_MAX_FLASHES, "no 3-minute cap: the slot limits alone bound the edition")
        self.assertEqual(rundown["dropped"], [])
        self.assertGreater(rundown["estimated_seconds"], 180)


class DepthContractTests(unittest.TestCase):
    """The v6.1 contract asks for depth where the material allows it."""

    @classmethod
    def setUpClass(cls):
        cls.snapshot, cls.selection = live_selection()
        cls.plan = json.loads(LIVE_PLAN.read_text(encoding="utf-8"))

    def fresh(self):
        return json.loads(json.dumps(self.plan, ensure_ascii=False))

    def test_reference_agent_plan_and_fallback_are_valid(self):
        self.assertEqual(director.validate_plan(self.plan, self.selection), [])
        fallback = director.fallback_plan(self.selection)
        self.assertEqual(director.validate_plan(fallback, self.selection), [])
        self.assertGreaterEqual(len(fallback["stories"]), 4, "a rich day gets several stories even without an agent")

    def test_card_angle_is_a_hidden_fixed_choice(self):
        plan = self.fresh()
        plan["stories"][0]["cards"][1]["angle"] = "规模"
        self.assertTrue(any("只能从这些角度里选" in e for e in director.validate_plan(plan, self.selection)))
        old = self.fresh()
        card = old["stories"][0]["cards"][0]
        card["label"] = card.pop("angle")
        self.assertTrue(any("不再有 label" in e for e in director.validate_plan(old, self.selection)))

    def test_title_needs_three_formulas_and_must_be_one_of_them(self):
        plan = self.fresh()
        story = plan["stories"][0]
        story["title_candidates"] = story["title_candidates"][:1]
        self.assertTrue(any("个不同套路的标题候选" in e for e in director.validate_plan(plan, self.selection)))
        plan = self.fresh()
        plan["stories"][0]["title"] = "一个不在候选里的标题"
        self.assertTrue(any("原样照抄 title_candidates" in e for e in director.validate_plan(plan, self.selection)))
        plan = self.fresh()
        cands = plan["stories"][0]["title_candidates"]
        cands[1]["formula"] = cands[0]["formula"]
        self.assertTrue(any("不同的套路" in e for e in director.validate_plan(plan, self.selection)))

    def test_suspense_title_must_name_the_card_that_answers_it(self):
        plan = self.fresh()
        mit = plan["stories"][1]
        self.assertIn(next(c["formula"] for c in mit["title_candidates"] if c["text"] == mit["title"]), C.SUSPENSE_FORMULAS)
        mit.pop("answer_card")
        self.assertTrue(any("answer_card" in e for e in director.validate_plan(plan, self.selection)))

    def test_hook_titles_stay_curious_not_sensational(self):
        plan = self.fresh()
        story = plan["stories"][0]
        story["title_candidates"][0]["text"] = story["title"] = "震惊！营收涨了十二倍，Anthropic 却转亏了！"
        errors = director.validate_plan(plan, self.selection)
        self.assertTrue(any("夸张词" in e for e in errors), errors)
        self.assertTrue(any("最多一个" in e for e in errors), errors)

    def test_card_headline_is_not_a_column_name_and_questions_are_capped(self):
        plan = self.fresh()
        cards = plan["stories"][0]["cards"]
        cards[0]["headline"] = "发生了什么"
        errors = director.validate_plan(plan, self.selection)
        self.assertTrue(any("是栏目名" in e for e in errors), errors)
        plan = self.fresh()
        for card, q in zip(plan["stories"][0]["cards"], ("上市了吗？", "为什么还亏？", "账单多大？")):
            card["headline"] = q
        self.assertTrue(any("个问句" in e for e in director.validate_plan(plan, self.selection)))

    def test_title_must_not_repeat_the_hook(self):
        plan = self.fresh()
        story = plan["stories"][0]
        story["title_candidates"][0]["text"] = story["title"] = director.plain(plan["hook"]["text"])
        self.assertTrue(any("和开头钩子几乎一样" in e for e in director.validate_plan(plan, self.selection)))

    def test_rich_story_needs_a_depth_card(self):
        plan = self.fresh()
        for card in plan["stories"][0]["cards"]:
            if card["angle"] in C.CARD_DEPTH_ANGLES:
                card["angle"] = "下一步" if card["angle"] != "下一步" else "怎么做到的"
        errors = director.validate_plan(plan, self.selection)
        self.assertTrue(any("至少要有一张" in e for e in errors), errors)

    def test_card_body_must_not_restate_headline(self):
        plan = self.fresh()
        plan["stories"][0]["cards"][0] = {"angle": "发生了什么", "headline": "拟以 ANT 登陆纳斯达克", "body": "拟以 ANT 登陆纳斯达克。"}
        self.assertTrue(any("几乎是同一句话" in e for e in director.validate_plan(plan, self.selection)))

    def test_thin_narration_on_rich_material_is_rejected(self):
        plan = self.fresh()
        story = plan["stories"][0]
        story["narration"] = [{"text": "Anthropic 递交了 S-1。", "card": i + 1} for i in range(len(story["cards"]))]
        self.assertTrue(any("这条素材足够讲到" in e for e in director.validate_plan(plan, self.selection)))

    def test_flash_needs_a_so_what(self):
        plan = self.fresh()
        flash = next(s for s in plan["stories"] if s["role"] == "flash")
        flash.pop("why")
        flash["narration"] = flash["narration"][:1]
        errors = director.validate_plan(plan, self.selection)
        self.assertTrue(any(".why：不能为空" in e for e in errors), errors)
        self.assertTrue(any("句旁白" in e for e in errors), errors)

    def test_caveat_in_material_must_be_kept(self):
        plan = self.fresh()
        mit = plan["stories"][1]
        mit["cards"] = [c for c in mit["cards"] if c["angle"] != "还没确定"]
        mit["answer_card"] = min(mit.get("answer_card", 1), len(mit["cards"]))
        mit["narration"] = [n for n in mit["narration"] if n["card"] <= len(mit["cards"])]
        errors = director.validate_plan(plan, self.selection)
        self.assertTrue(any("尚未验证" in e for e in errors), errors)

    def test_finalize_hides_hook_caption_and_builds_recap(self):
        final = director.finalize(self.plan, self.selection, source="agent")
        self.assertIs(final["units"][0]["caption"], False)
        outro = final["scenes"][-1]
        self.assertEqual(len(outro["recap"]), len(self.plan["stories"]))
        story = next(s for s in final["scenes"] if s["kind"] == "story")
        self.assertNotEqual(story["title"], story["aihot_title"], "screen shows the hook title, the ledger keeps the AIHOT title")
        flash_units = [u for u in final["units"] if u["scene_id"] == "story-04"]
        self.assertEqual([u["card"] for u in flash_units], [1, 2])

    def test_caption_text_drops_trailing_punctuation(self):
        self.assertEqual(timeline.caption_text("据《纽约时报》报道，"), "据《纽约时报》报道")
        self.assertEqual(timeline.caption_text("周末项目 HackerTube（"), "周末项目 HackerTube")


if __name__ == "__main__":
    unittest.main()
