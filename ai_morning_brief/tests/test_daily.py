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
        self.assertLessEqual(selection["estimated_seconds"], C.RUNDOWN_MAX_SECONDS)
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
            self.assertIn("audio_to_audio_plus", json.dumps(manifest, ensure_ascii=False))

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
            final["units"][0]["spoken_text"] = "新句子。"
            manifest = voice.build_manifest(run_dir, final, "doubao")
            self.assertEqual(voice.missing_units(manifest), ["u01"])

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

    def test_rundown_count_follows_content_and_fits_three_minutes(self):
        rec = self.selection["recommended"]
        self.assertGreaterEqual(len(rec["headlines"]), 2)
        self.assertLessEqual(len(rec["headlines"]), C.RUNDOWN_MAX_HEADLINES)
        self.assertLessEqual(len(rec["flashes"]), C.RUNDOWN_MAX_FLASHES)
        self.assertLessEqual(self.selection["estimated_seconds"], C.RUNDOWN_MAX_SECONDS)
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

    def test_three_minute_cap_drops_weakest_flashes_first(self):
        clusters = []
        for i in range(10):
            k = newsroom.EventCluster(key=f"k{i}", lead=newsroom.Candidate(item_id=f"k{i}", title=f"T{i}", heat=100 - i), members=[], heat=100 - i)
            k.pack = {"depth": "rich", "fact_chars": 2000}
            clusters.append(k)
        rundown = newsroom.plan_rundown(clusters)
        self.assertEqual(len(rundown["headlines"]), C.RUNDOWN_MAX_HEADLINES)
        self.assertLessEqual(rundown["estimated_seconds"], C.RUNDOWN_MAX_SECONDS)
        self.assertTrue(rundown["dropped"])
        self.assertGreater(min(k.heat for k in rundown["flashes"]), max(d["heat"] for d in rundown["dropped"]) - 0.01)


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
