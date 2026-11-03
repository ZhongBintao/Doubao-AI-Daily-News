"""v6 director plan: the only thing a writer model produces.

The writer model answers a fill-in task (``director_task.json``) instead of
designing a video. It chooses picks from a fixed pool and fills fixed fields
under budgets that scale with how much AIHOT material each event has.
``validate_plan`` returns Chinese, field-addressed errors a weak model can act
on; ``fallback_plan`` builds a valid plan from the AIHOT material pack alone so
an edition always renders.

Plan shape (version 6.1)::

    {
      "version": "6.1",
      "picks": {"headlines": [id, ...], "flashes": [id, ...]},
      "hook": {"text": "只改 【0.016%】 的权重，数学成绩涨到 6 倍", "keyfact": "..."},
      "stories": [
        {"item_id": id, "role": "headline", "subject": "...", "title": "...",
         "cards": [{"label": "发生了什么", "headline": "...", "body": "..."}],
         "narration": [{"text": "...。", "card": 1}]},
        {"item_id": id, "role": "flash", "subject": "...", "title": "...",
         "point": "...", "why": "...", "narration": [{"text": "...。", "card": 1}]}
      ]
    }

Card labels are a choice from ``config.CARD_LABELS``; an event with enough
material must have at least one "depth" card (各方说法 / 背景 / 为什么重要 /
对你的影响). Facts — numbers and names — must appear in the event's AIHOT
material pack (see ``material.py``); the AIHOT angle hint is not evidence.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import config as C
from . import material as M
from .brands import find_brand
from .writing import caption_visible_units, normalize_with_ledger

PLAN_VERSION = "6.1"
EXAMPLES_DIR = C.REPO_ROOT / "skills" / "ai-brief-editorial-writer" / "references" / "examples"
_MARK = re.compile(r"【(.+?)】")
_TERMINAL = re.compile(r"[。！？!?]$")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_ASCII = re.compile(r"[A-Za-z][A-Za-z0-9\-]*(?:\.[A-Za-z0-9]+)*")
_ASCII_ALLOW = {"ai", "app", "ceo", "cto", "api", "ok", "vs"}
_CAVEAT = re.compile(r"尚无|尚未|未经|待验证|未证实|无法核实|无法核查|没有第三方|仍待|有待|未获|并未确认|未直接确认|说法不一|存在争议")
_CAVEAT_KEPT = re.compile(r"尚|未|待|没有|还没|不确定|仍需|有待|争议|不一|无法")
_ATTRIBUTION_WORDS = re.compile(r"据|称|认为|表示|指出|报道|回应|透露|解读|分析|说")
CARD_LABELS = C.CARD_LABELS


def plain(text: Any) -> str:
    return _MARK.sub(r"\1", str(text or "")).replace("【", "").replace("】", "").strip()


def units(text: Any) -> int:
    return caption_visible_units(plain(text))


# ---------------------------------------------------------------------------
# task
# ---------------------------------------------------------------------------
def _fallback_pack(lead: Mapping[str, Any], related: list[str]) -> dict[str, Any]:
    """Pack for a cluster dict that predates material packs (tests, old selections)."""

    facts = [{"kind": k, "source": str(lead.get("source") or ""), "date": "", "text": str(lead.get(f) or "")}
             for k, f in (("摘要", "summary"), ("AIHOT 综述", "digest")) if lead.get(f)]
    facts += [{"kind": "同事件报道", "source": "", "date": "", "text": t} for t in related]
    chars = sum(len(f["text"]) for f in facts)
    depth = "rich" if chars >= C.PACK_DEPTH_RICH else "medium" if chars >= C.PACK_DEPTH_MEDIUM else "thin"
    return {"facts": facts, "background": [], "angle_hint": "", "fact_chars": chars, "background_chars": 0, "source_count": 0, "depth": depth}


def _fact_line(fact: Mapping[str, Any]) -> str:
    meta = " · ".join(p for p in (fact.get("kind"), fact.get("source"), str(fact.get("date") or "")[5:]) if p)
    return f"[{meta}] {fact.get('text')}"


def story_material(cluster: Mapping[str, Any]) -> dict[str, Any]:
    lead = cluster.get("lead") or {}
    related = [t for t in cluster.get("member_titles") or [] if t != lead.get("title")][:4]
    pack = cluster.get("pack") or _fallback_pack(lead, related)
    budget = cluster.get("budget") or M.story_budget(pack)
    # The writer is told that every fact and background line is valid evidence, so the validator accepts them too.
    text = "\n".join([*(str(lead.get(k) or "") for k in ("title", "hot_title", "summary", "digest")), *related, M.pack_evidence(pack)])
    brand = find_brand(" ".join(str(lead.get(k) or "") for k in ("hot_title", "title")))
    return {
        "item_id": lead.get("item_id"),
        "title": lead.get("title"),
        "hot_title": lead.get("hot_title") or "",
        "summary": lead.get("summary") or "",
        "digest": lead.get("digest") or "",
        "related_titles": related,
        "category": lead.get("category") or "other",
        "category_label": lead.get("category_label") or C.V6_CATEGORY_LABELS.get(lead.get("category") or "other", "AI 动态"),
        "source": lead.get("source") or "",
        "heat": cluster.get("heat"),
        "signals": lead.get("signals") or {},
        "subject_hint": (brand or {}).get("name") or "",
        "has_logo": bool((brand or {}).get("logo_path")),
        "depth": pack.get("depth"),
        "budget": budget,
        "facts": [_fact_line(f) for f in pack.get("facts") or []],
        "background": [_fact_line({**b, "text": f"{b.get('title') or ''}：{b.get('text')}"}) for b in pack.get("background") or []],
        "angle_hint": pack.get("angle_hint") or "",
        "links": {"aihot": lead.get("aihot_url") or "", "original": lead.get("original_url") or ""},
        "_material": text,
        "_pack": pack,
    }


def load_examples() -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for path in sorted(EXAMPLES_DIR.glob("*.json")):
        try:
            examples.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return examples


def budgets() -> dict[str, Any]:
    return {
        "hook": C.BUDGET_HOOK_MAX, "keyfact": C.BUDGET_KEYFACT_MAX, "title": C.BUDGET_TITLE_MAX,
        "card_label_choices": list(CARD_LABELS), "card_depth_labels": list(C.CARD_DEPTH_LABELS),
        "card_headline": C.BUDGET_CARD_HEADLINE_MAX, "card_body": C.BUDGET_CARD_BODY_MAX,
        "sentence": C.BUDGET_SENTENCE_MAX, "flash_point": C.BUDGET_FLASH_POINT_MAX, "flash_why": C.BUDGET_FLASH_WHY_MAX,
        "total_narration": C.BUDGET_TOTAL_NARRATION_MAX, "max_headlines": C.RUNDOWN_MAX_HEADLINES, "max_flashes": C.RUNDOWN_MAX_FLASHES,
        "per_story": "每条新闻自己的字数范围见 pool[].budget（素材越多，要求讲得越多）",
    }


TASK_STEPS = [
    "第 1 步 选新闻：从 pool 里选头条（最多 3 条，只能选 budget.can_headline=true 的）和快讯（最多 5 条）。条数由当天内容决定，不必凑满；拿不准就直接照抄 recommended。",
    "第 2 步 读素材：每条新闻的 facts 是 AIHOT 的摘要、综述和各来源报道，background 是 AIHOT 里的相关事件和此前报道，angle_hint 是 AIHOT 编辑写的“为什么值得看”（只用来决定讲哪个角度，不能当事实引用）。",
    "第 3 步 写开头钩子 hook.text：一句话说出第一条头条最抓人的点，可以用一对【】标出一个重点词或数字。hook.keyfact 用一句客观事实说清“谁做了什么”。",
    "第 4 步 写头条：subject、title；按 budget.cards 写卡片，每张卡的 label 从 card_label_choices 里选一个（第一张通常是“发生了什么”）。素材足够时（budget.needs_depth_card=true）至少要有一张“各方说法 / 背景 / 为什么重要 / 对你的影响”。",
    "第 5 步 写头条旁白：逐句写，每句用 card 字段标明在讲第几张卡（从 1 开始，按顺序，每张卡至少一句）。旁白要解释卡片，而不是把卡片原样念一遍；合计字数落在 budget.headline_narration 范围内。",
    "第 6 步 写快讯：point 写发生了什么；why 写“所以呢”——反差、影响或各方反应（素材足够时必填）；narration 写 budget.flash_sentences 句，第 1 句讲 point（card=1），后面讲 why（card=2）。",
    "第 7 步 运行 check_command 校验；按报错逐条修改，最多 2 次。仍失败也没关系，程序会自动用兜底稿出片。",
]
RULES = [
    "所有数字、英文名、产品名必须原样出现在该条新闻的 facts 或 background 里，不能编造；angle_hint 不算素材。",
    "不写素材里没有的因果、预测、建议；“为什么重要 / 对你的影响”只能复述素材里已经写出的意义或影响。",
    "素材里说“尚未验证 / 尚无复现 / 尚未确定 / 说法不一”时，必须在“还没确定”卡片或旁白里保留这层意思。",
    "“各方说法”要写清是谁说的（例如“据 TechCrunch”“马斯克称”）；背景信息要说明是此前发生的事。",
    "卡片小标题（headline）是一句结论，正文（body）补充细节，两者不能是同一句话换个说法。",
    "不在文案里写分数、链接、AIHOT。",
    "字数按可见字符计算，英文单词按字母数算，见 budgets 和 pool[].budget；【】不计入字数，每个字段最多一对【】。",
]


def build_task(run_date: date, selection: Mapping[str, Any], *, plan_path: Path, check_command: str) -> dict[str, Any]:
    pool = [story_material(cluster) for cluster in selection.get("pool") or []]
    public_pool = [{k: v for k, v in item.items() if not k.startswith("_")} for item in pool]
    return {
        "version": PLAN_VERSION,
        "task": "director-plan",
        "run_date": run_date.isoformat(),
        "mode": selection.get("mode"),
        "estimated_seconds": selection.get("estimated_seconds"),
        "max_seconds": C.RUNDOWN_MAX_SECONDS,
        "output_path": str(plan_path),
        "check_command": check_command,
        "steps": TASK_STEPS,
        "rules": RULES,
        "budgets": budgets(),
        "recommended": selection.get("recommended") or {},
        "pool": public_pool,
        "output_template": {
            "version": PLAN_VERSION,
            "picks": {"headlines": ["<pool 里的 item_id>"], "flashes": ["<item_id>", "<item_id>"]},
            "hook": {"text": "<钩子，可含一对【】>", "keyfact": "<一句客观事实>"},
            "stories": [
                {"item_id": "<头条 id>", "role": "headline", "subject": "<主体>", "title": "<标题>",
                 "cards": [{"label": "<从 card_label_choices 选>", "headline": f"<≤{C.BUDGET_CARD_HEADLINE_MAX}字结论>", "body": f"<≤{C.BUDGET_CARD_BODY_MAX}字细节，可含一对【】>"}],
                 "narration": [{"text": "<一句话。>", "card": 1}]},
                {"item_id": "<快讯 id>", "role": "flash", "subject": "<主体>", "title": "<标题>",
                 "point": "<发生了什么，可含一对【】>", "why": "<所以呢>",
                 "narration": [{"text": "<讲 point 的一句话。>", "card": 1}, {"text": "<讲 why 的一句话。>", "card": 2}]},
            ],
        },
        "examples": load_examples(),
    }


def material_index(selection: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["item_id"]): item for item in (story_material(c) for c in selection.get("pool") or []) if item.get("item_id")}


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def _norm_source(text: str) -> str:
    return re.sub(r"(?<=\d),(?=\d{3})", "", text)


def _unsupported_tokens(text: str, material: str) -> list[str]:
    source = _norm_source(material)
    source_lower = source.lower()
    numbers = set(_NUMBER.findall(_norm_source(source)))
    bad: list[str] = []
    for number in _NUMBER.findall(_norm_source(plain(text))):
        if number not in numbers:
            bad.append(number)
    for word in _ASCII.findall(plain(text)):
        if word.lower() in _ASCII_ALLOW:
            continue
        if word.lower() not in source_lower:
            bad.append(word)
    return sorted(set(bad))


def _check_text(errors: list[str], path: str, value: Any, *, maximum: int, required: bool = True, marks: bool = False, terminal: bool = False) -> None:
    text = str(value or "").strip()
    if not text:
        if required:
            errors.append(f"{path}：不能为空。")
        return
    if units(text) > maximum:
        errors.append(f"{path}：超过 {maximum} 字（当前 {units(text)} 字），请删减。")
    count = text.count("【")
    if count != text.count("】"):
        errors.append(f"{path}：【 和 】 必须成对出现。")
    if count and not marks:
        errors.append(f"{path}：这里不能使用【】标记，请去掉。")
    if count > 1:
        errors.append(f"{path}：最多只能有一对【】。")
    if terminal and not _TERMINAL.search(plain(text)):
        errors.append(f"{path}：句子必须以句号、问号或感叹号结尾。")


def _duplicates(headline: str, body: str) -> bool:
    """A card whose body only restates its headline carries half the information it could."""

    h, b = M.bigrams(plain(headline)), M.bigrams(plain(body))
    if not h:
        return False
    contained = len(h & b) / len(h)
    return contained >= 0.8 and units(body) - units(headline) < 12


def _range_error(errors: list[str], path: str, value: int, bounds: Iterable[int], what: str) -> None:
    lo, hi = list(bounds)
    if value < lo:
        errors.append(f"{path}：{what}只有 {value} 字，这条素材足够讲到 {lo}–{hi} 字，请补充素材里的细节、背景或各方说法。")
    elif value > hi:
        errors.append(f"{path}：{what}超过 {hi} 字（当前 {value} 字），请删减。")


def validate_plan(plan: Any, selection: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(plan, Mapping):
        return ["整个文件必须是一个 JSON 对象。"]
    if str(plan.get("version")) != PLAN_VERSION:
        errors.append(f"version：必须是 \"{PLAN_VERSION}\"。")
    materials = material_index(selection)
    picks = plan.get("picks") or {}
    headlines = [str(v) for v in picks.get("headlines") or []]
    flashes = [str(v) for v in picks.get("flashes") or []]
    if not 1 <= len(headlines) <= C.RUNDOWN_MAX_HEADLINES:
        errors.append(f"picks.headlines：必须选 1–{C.RUNDOWN_MAX_HEADLINES} 条头条。")
    if len(flashes) > C.RUNDOWN_MAX_FLASHES:
        errors.append(f"picks.flashes：快讯最多 {C.RUNDOWN_MAX_FLASHES} 条。")
    chosen = headlines + flashes
    if len(set(chosen)) != len(chosen):
        errors.append("picks：同一条新闻不能被选两次。")
    for item_id in chosen:
        if item_id not in materials:
            errors.append(f"picks：{item_id} 不在 pool 里，只能从 pool 中选择。")
    for index, item_id in enumerate(headlines):
        budget = (materials.get(item_id) or {}).get("budget") or {}
        if item_id in materials and not budget.get("can_headline", True) and len(headlines) > 1:
            errors.append(f"picks.headlines[{index}]：{item_id} 的素材太少，讲不深，请改为快讯（放到 picks.flashes）。")
    hook = plan.get("hook") or {}
    _check_text(errors, "hook.text", hook.get("text"), maximum=C.BUDGET_HOOK_MAX, marks=True)
    _check_text(errors, "hook.keyfact", hook.get("keyfact"), maximum=C.BUDGET_KEYFACT_MAX)
    if headlines and headlines[0] in materials:
        for field in ("text", "keyfact"):
            bad = _unsupported_tokens(str(hook.get(field) or ""), materials[headlines[0]]["_material"])
            if bad:
                errors.append(f"hook.{field}：这些数字或英文在头条素材里找不到：{'、'.join(bad)}。请改成素材里的原样写法。")
    stories = plan.get("stories")
    if not isinstance(stories, list) or not stories:
        errors.append("stories：必须是列表，按“头条在前、快讯在后”的顺序写出每条新闻。")
        return errors
    expected = [(i, "headline") for i in headlines] + [(i, "flash") for i in flashes]
    actual = [(str(s.get("item_id")), str(s.get("role"))) for s in stories if isinstance(s, Mapping)]
    if actual != expected:
        errors.append("stories：顺序和 picks 不一致。必须先按 picks.headlines 顺序写头条（role=headline），再按 picks.flashes 顺序写快讯（role=flash）。")
    total = 0
    for index, story in enumerate(stories):
        prefix = f"stories[{index}]"
        if not isinstance(story, Mapping):
            errors.append(f"{prefix}：必须是对象。")
            continue
        m = materials.get(str(story.get("item_id")), {})
        material = m.get("_material", "")
        budget = m.get("budget") or M.story_budget({"depth": "thin"})
        _check_text(errors, f"{prefix}.subject", story.get("subject"), maximum=20)
        _check_text(errors, f"{prefix}.title", story.get("title"), maximum=C.BUDGET_TITLE_MAX)
        texts: list[tuple[str, str]] = [(f"{prefix}.title", str(story.get("title") or ""))]
        narration = story.get("narration")
        if not isinstance(narration, list) or not narration:
            errors.append(f"{prefix}.narration：至少写一句旁白。")
            narration = []
        story_units = 0
        for n, sentence in enumerate(narration):
            path = f"{prefix}.narration[{n}].text"
            text = sentence.get("text") if isinstance(sentence, Mapping) else None
            _check_text(errors, path, text, maximum=C.BUDGET_SENTENCE_MAX, terminal=True)
            texts.append((path, str(text or "")))
            story_units += units(text)
        total += story_units
        if story.get("role") == "headline":
            cards = story.get("cards")
            lo, hi = budget.get("cards") or (C.STORY_BUDGETS["thin"]["cards"])
            if not isinstance(cards, list) or not lo <= len(cards) <= hi:
                errors.append(f"{prefix}.cards：这条头条需要 {lo}–{hi} 张卡片（当前 {len(cards) if isinstance(cards, list) else 0} 张）。")
                cards = cards if isinstance(cards, list) else []
            labels: list[str] = []
            for c, card in enumerate(cards):
                cp = f"{prefix}.cards[{c}]"
                if not isinstance(card, Mapping):
                    errors.append(f"{cp}：必须是对象。")
                    continue
                label = str(card.get("label") or "").strip()
                labels.append(label)
                if label not in CARD_LABELS:
                    errors.append(f"{cp}.label：只能从这些标签里选一个：{'、'.join(CARD_LABELS)}。")
                _check_text(errors, f"{cp}.headline", card.get("headline"), maximum=C.BUDGET_CARD_HEADLINE_MAX)
                _check_text(errors, f"{cp}.body", card.get("body"), maximum=C.BUDGET_CARD_BODY_MAX, marks=True)
                if card.get("headline") and card.get("body") and _duplicates(str(card["headline"]), str(card["body"])):
                    errors.append(f"{cp}：小标题和正文几乎是同一句话。小标题写结论，正文请补充素材里的具体细节。")
                if label == "各方说法" and not _ATTRIBUTION_WORDS.search(plain(card.get("body"))):
                    errors.append(f"{cp}.body：“各方说法”要写清是谁说的，例如“据 TechCrunch，……”或“马斯克称……”。")
                texts += [(f"{cp}.headline", str(card.get("headline") or "")), (f"{cp}.body", str(card.get("body") or ""))]
            if len(set(labels)) != len(labels):
                errors.append(f"{prefix}.cards：同一个 label 不能用两次，每张卡讲一个不同的角度。")
            if budget.get("needs_depth_card") and labels and not set(labels) & set(C.CARD_DEPTH_LABELS):
                errors.append(f"{prefix}.cards：这条素材足够深入，至少要有一张“{' / '.join(C.CARD_DEPTH_LABELS)}”卡片（从 facts 的各来源报道或 background 里找内容）。")
            refs: list[int] = []
            for n, sentence in enumerate(narration):
                ref = sentence.get("card") if isinstance(sentence, Mapping) else None
                if not isinstance(ref, int) or not 1 <= ref <= max(1, len(cards)):
                    errors.append(f"{prefix}.narration[{n}].card：必须写 1 到 {len(cards)} 之间的整数，表示这句在讲第几张卡。")
                    continue
                refs.append(ref)
            if refs and refs != sorted(refs):
                errors.append(f"{prefix}.narration：card 编号必须从小到大，按卡片顺序讲。")
            missing = [str(i) for i in range(1, len(cards) + 1) if i not in refs]
            if refs and missing:
                errors.append(f"{prefix}.narration：第 {'、'.join(missing)} 张卡没有被任何一句旁白讲到。")
            _range_error(errors, f"{prefix}.narration", story_units, budget["headline_narration"], "头条旁白合计")
            story_text = " ".join(t for _, t in texts)
            if _CAVEAT.search(material) and not _CAVEAT_KEPT.search(plain(story_text)):
                errors.append(f"{prefix}：素材里提到“尚未验证 / 尚未确定”之类的限定，请用“还没确定”卡片或一句旁白保留这层意思。")
        else:
            _check_text(errors, f"{prefix}.point", story.get("point"), maximum=C.BUDGET_FLASH_POINT_MAX, marks=True)
            texts.append((f"{prefix}.point", str(story.get("point") or "")))
            lo_s, hi_s = budget["flash_sentences"]
            why_required = lo_s >= 2
            _check_text(errors, f"{prefix}.why", story.get("why"), maximum=C.BUDGET_FLASH_WHY_MAX, required=why_required, marks=True)
            if story.get("why"):
                texts.append((f"{prefix}.why", str(story.get("why") or "")))
                if story.get("point") and _duplicates(str(story["point"]), str(story["why"])):
                    errors.append(f"{prefix}.why：和 point 几乎是同一句话。why 要写“所以呢”：反差、影响或各方反应。")
            if not lo_s <= len(narration) <= hi_s:
                errors.append(f"{prefix}.narration：这条快讯要写 {lo_s}–{hi_s} 句旁白（当前 {len(narration)} 句）。")
            cards_n = 2 if story.get("why") else 1
            refs = [s.get("card") for s in narration if isinstance(s, Mapping) and s.get("card") is not None]
            if refs and (any(not isinstance(r, int) or not 1 <= r <= cards_n for r in refs) or refs != sorted(refs)):
                errors.append(f"{prefix}.narration：快讯的 card 只能是 1（讲 point）或 2（讲 why），并且从小到大。")
            _range_error(errors, f"{prefix}.narration", story_units, budget["flash_narration"], "快讯旁白合计")
        if material:
            for path, text in texts:
                bad = _unsupported_tokens(text, material)
                if bad:
                    errors.append(f"{path}：这些数字或英文在该新闻素材里找不到：{'、'.join(bad)}。请改成素材里的原样写法。")
    total += units(hook.get("text"))
    if total > C.BUDGET_TOTAL_NARRATION_MAX:
        errors.append(f"全片旁白合计超过 {C.BUDGET_TOTAL_NARRATION_MAX} 字（约 {C.RUNDOWN_MAX_SECONDS / 60:.0f} 分钟，当前 {total} 字），请减少快讯条数或删减旁白。")
    return errors


# ---------------------------------------------------------------------------
# deterministic fallback
# ---------------------------------------------------------------------------
_SENT_SPLIT = re.compile(r"(?<=[。！？；;])")
_ATTRIBUTION = re.compile(r"(称|表示|报道|指出|认为|介绍|透露|宣布|回应|据.{0,12})[，,：:]?$")
_WHY = re.compile(r"若|意味|将使|有望|影响|担忧|压力|风险|首次|最大|最高|第一|超过|相当于|为.{0,6}的.{0,3}倍")
_HOW = re.compile(r"通过|采用|基于|架构|方法|方式|利用|由.{0,8}(层|模块)")


def _sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(re.sub(r"\s+", " ", str(text or ""))) if p.strip()]
    return [re.sub(r"[；;]$", "。", p) for p in parts]


def _clauses(text: str) -> list[str]:
    # 、 joins list items ("电力公司、监管机构"), so it is never a cut point.
    return [p for p in re.split(r"(?<=[，,：:])", text) if p.strip()]


def _fit(text: str, maximum: int, *, terminal: bool = False) -> str:
    """Shorten at clause boundaries; hard-cut only as a last resort."""

    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if units(text) <= maximum:
        out = text
    else:
        out = ""
        for clause in _clauses(text):
            if units(out + clause) > maximum:
                break
            out += clause
        out = out.rstrip("，,：:、 ")
        if units(out) < max(6, maximum // 3):
            out = _hard_cut(text, maximum - (1 if terminal else 0))
    out = out.rstrip("，,：:、；; ")
    if terminal and not _TERMINAL.search(out):
        out = out.rstrip("。.") + "。"
    elif not terminal:
        out = out.rstrip("。")
    return out


def _spoken(sentence: str, limit: int = 2) -> list[str]:
    """Split one source sentence into up to ``limit`` speakable sentences of <= BUDGET_SENTENCE_MAX."""

    out: list[str] = []
    rest = re.sub(r"\s+", " ", sentence).strip()
    lead = re.match(r"^(据.{1,30}?)[，,]", rest)
    if lead and units(rest) > C.BUDGET_SENTENCE_MAX:
        rest = rest[lead.end():]  # an attribution alone is not a sentence; the card keeps it
    while rest and len(out) < limit:
        piece = _fit(rest, C.BUDGET_SENTENCE_MAX, terminal=True)
        out.append(piece)
        consumed = plain(piece).rstrip("。")
        index = rest.find(consumed)
        if index < 0 or units(consumed) < 6:
            break
        rest = rest[index + len(consumed):].lstrip("，,：:、。；; ")
        if units(rest) < 8:
            break
    return out


def _subject(material: Mapping[str, Any]) -> str:
    if material.get("subject_hint"):
        return str(material["subject_hint"])
    title = str(material.get("hot_title") or material.get("title") or "")
    head = re.split(r"[：:，, ]", title, maxsplit=1)[0]
    ascii_words = _ASCII.findall(title)
    if ascii_words:
        return ascii_words[0][:20]
    return head[:8] or str(material.get("category_label") or "AI 动态")


def _hard_cut(text: str, maximum: int) -> str:
    """Cut to ``maximum`` units without splitting an ASCII word or number."""

    out = ""
    for match in re.finditer(r"[A-Za-z0-9][A-Za-z0-9.+#/\-]*|\s+|.", text):
        piece = match.group(0)
        if units(out + piece) > maximum:
            break
        out += piece
    return out.rstrip()


def _mark_number(text: str) -> str:
    """Highlight the first standalone number (never one inside a name like GPT-6)."""

    if "【" in text:
        return text
    match = re.search(r"(?<![A-Za-z0-9.\-])\d+(?:\.\d+)?(?:%|％|倍|亿美元|亿|万|个|家|项|款)?(?![A-Za-z0-9\-])", text)
    if not match:
        return text
    return text[: match.start()] + "【" + match.group(0) + "】" + text[match.end():]


def _card_headline(sentence: str, *, first: bool = False) -> str:
    """Pick the clause that reads best as a short conclusion (numbers win, attributions lose)."""

    clauses = [c.strip().rstrip("，,：:、。；;") for c in _clauses(sentence) if c.strip()]
    clauses = [c for c in clauses if c and not _ATTRIBUTION.search(c) and units(c) >= 4] or clauses or [sentence]

    def score(clause: str) -> tuple[int, int]:
        fits = units(clause) <= C.BUDGET_CARD_HEADLINE_MAX
        return ((2 if fits else 0) + (2 if _NUMBER.search(clause) else 0) + (1 if 6 <= units(clause) else 0), -units(clause))

    best = clauses[0] if first and units(clauses[0]) <= C.BUDGET_CARD_HEADLINE_MAX + 4 else max(clauses, key=score)
    return _fit(best, C.BUDGET_CARD_HEADLINE_MAX)


def _label_for(sentence: str, kind: str) -> str:
    if _CAVEAT.search(sentence):
        return "还没确定"
    if kind == "background":
        return "背景"
    if kind == "report" and _ATTRIBUTION_WORDS.search(sentence):
        return "各方说法"
    if _WHY.search(sentence):
        return "为什么重要"
    if _HOW.search(sentence):
        return "怎么做到的"
    if _NUMBER.search(sentence):
        return "关键数字"
    return "下一步" if re.search(r"计划|将于|下一步|随后|即将", sentence) else ""


def _candidate_sentences(m: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(sentence, kind) from the pack in reading order: summary, digest, reports/coverage, background."""

    pack = m.get("_pack") or {}
    out: list[tuple[str, str]] = []
    for fact in pack.get("facts") or []:
        kind = "report" if fact.get("kind") in {"来源报道", "同事件报道"} else "core"
        for s in _sentences(fact.get("text")):
            if kind == "report" and fact.get("source") and not _ATTRIBUTION_WORDS.search(s[:12]):
                s = f"据{fact['source']}，{s}" if units(fact["source"]) <= 12 else s
            out.append((s, kind))
    for bg in pack.get("background") or []:
        for s in _sentences(bg.get("text"))[:1]:
            out.append((f"此前，{s}" if not s.startswith("此前") else s, "background"))
    seen: list[str] = []
    unique: list[tuple[str, str]] = []
    for s, kind in out:
        if units(s) < 10 or M.near_duplicate(s, seen, 0.5):
            continue
        seen.append(s)
        unique.append((s, kind))
    return unique


def _fallback_headline(item_id: str, m: Mapping[str, Any]) -> dict[str, Any]:
    budget = m["budget"]
    lo_cards, hi_cards = budget["cards"]
    lo_chars, hi_chars = budget["headline_narration"]
    sentences = _candidate_sentences(m) or [(m["title"] + "。", "core")]
    cards: list[dict[str, Any]] = []
    narration: list[dict[str, Any]] = []
    used_labels: set[str] = set()
    story_units = 0

    def add(sentence: str, label: str) -> bool:
        nonlocal story_units
        headline = _card_headline(sentence, first=not cards)
        body = _fit(sentence, C.BUDGET_CARD_BODY_MAX)
        if _duplicates(headline, body) or label in used_labels:
            return False
        said = [n["text"] for n in narration]
        pieces = 2 if story_units < lo_chars * 0.6 else 1  # two sentences per card early on, one once the story has its core
        spoken = [t for t in _spoken(sentence, pieces) if not M.near_duplicate(t, said, 0.5)]
        if not spoken:
            return False
        cost = sum(units(s) for s in spoken)
        if story_units + cost > hi_chars:
            spoken = spoken[:1]
            cost = units(spoken[0])
            if story_units + cost > hi_chars:
                return False
        cards.append({"label": label, "headline": headline, "body": body})
        narration.extend({"text": s, "card": len(cards)} for s in spoken)
        used_labels.add(label)
        story_units += cost
        return True

    first = sentences[0][0]
    add(first, "发生了什么")
    rest = sentences[1:]
    if budget.get("needs_depth_card"):
        # Guarantee one depth card: an attributed report, a stated significance, or background.
        for s, kind in rest:
            label = _label_for(s, kind)
            if label in C.CARD_DEPTH_LABELS and add(s, label):
                break
    caveat = next(((s, k) for s, k in sentences if _CAVEAT.search(s)), None)
    if caveat and caveat[0] != first:
        add(caveat[0], "还没确定")  # the material's own caveat is never dropped
    for s, kind in rest:
        if len(cards) >= hi_cards:
            break
        if story_units >= lo_chars + 0.4 * (hi_chars - lo_chars) and len(cards) >= lo_cards:
            break  # leave room in the 3-minute budget for the day's flashes
        label = _label_for(s, kind)
        if label and label != "还没确定":
            add(s, label)
    for s, kind in rest:  # top up to the minimum card count with any remaining label
        if len(cards) >= lo_cards:
            break
        for label in ("关键数字", "怎么做到的", "各方说法" if kind == "report" else "下一步", "背景" if kind == "background" else "为什么重要"):
            if label not in used_labels and add(s, label):
                break
    if len(cards) < 2:
        pieces = _clauses(first)
        half = max(1, len(pieces) // 2)
        a, b = "".join(pieces[:half]), "".join(pieces[half:]) or m["title"]
        cards = [{"label": "发生了什么", "headline": _card_headline(a), "body": _fit(first, C.BUDGET_CARD_BODY_MAX)},
                 {"label": "关键数字", "headline": _card_headline(b), "body": _fit(b + "。", C.BUDGET_CARD_BODY_MAX)}]
        narration = [{"text": s, "card": 1} for s in _spoken(a + "。", 1)] + [{"text": s, "card": 2} for s in _spoken(b, 1)]
    order = {label: i for i, label in enumerate(CARD_LABELS)}
    first_card = cards[0]
    tail = sorted(cards[1:], key=lambda c: order.get(c["label"], 99) if c["label"] != "还没确定" else 98)
    mapping = {id(c): i + 1 for i, c in enumerate([first_card, *tail])}
    old_index = {i + 1: id(c) for i, c in enumerate(cards)}
    narration = sorted(({"text": n["text"], "card": mapping[old_index[n["card"]]]} for n in narration), key=lambda n: n["card"])
    return {"item_id": item_id, "role": "headline", "subject": _subject(m), "title": _fit(m["title"], C.BUDGET_TITLE_MAX),
            "cards": [first_card, *tail], "narration": narration}


def _fallback_flash(item_id: str, m: Mapping[str, Any]) -> dict[str, Any]:
    budget = m["budget"]
    lo_s, hi_s = budget["flash_sentences"]
    _, hi_chars = budget["flash_narration"]
    sentences = _candidate_sentences(m) or [(m["title"] + "。", "core")]
    point_source = sentences[0][0]
    fresh = [(s, k) for s, k in sentences[1:] if not M.near_duplicate(s, [point_source], 0.3)]
    why_source = next((s for s, k in fresh if _label_for(s, k) in {"为什么重要", "各方说法", "关键数字", "还没确定", "背景"}), "")
    if not why_source and fresh:
        why_source = fresh[0][0]
    title = _fit(m["hot_title"] if m["hot_title"] and units(m["hot_title"]) <= C.BUDGET_TITLE_MAX else m["title"], C.BUDGET_TITLE_MAX)
    story: dict[str, Any] = {"item_id": item_id, "role": "flash", "subject": _subject(m), "title": title,
                             "point": _mark_number(_fit(point_source, C.BUDGET_FLASH_POINT_MAX))}
    narration = [{"text": s, "card": 1} for s in _spoken(point_source, 2 if not why_source else 1)]
    why = _fit(why_source, C.BUDGET_FLASH_WHY_MAX) if why_source else ""
    if why and not _duplicates(story["point"], why):
        story["why"] = why
        narration += [{"text": s, "card": 2} for s in _spoken(why_source, hi_s - len(narration))]
    while sum(units(n["text"]) for n in narration) > hi_chars and len(narration) > lo_s:
        narration.pop()
    story["narration"] = narration[:hi_s]
    return story


def fallback_plan(selection: Mapping[str, Any]) -> dict[str, Any]:
    materials = material_index(selection)
    rec = selection.get("recommended") or {}
    headlines = [i for i in rec.get("headlines") or [] if i in materials][: C.RUNDOWN_MAX_HEADLINES]
    if not headlines and materials:
        headlines = [next(iter(materials))]
    flashes = [i for i in rec.get("flashes") or [] if i in materials and i not in headlines][: C.RUNDOWN_MAX_FLASHES]
    lead = materials[headlines[0]]
    hook_source = lead["hot_title"] if lead["hot_title"] and units(lead["hot_title"]) <= C.BUDGET_HOOK_MAX else lead["title"]
    first_sentences = _sentences(lead["summary"] or lead["digest"]) or [lead["title"]]
    plan: dict[str, Any] = {
        "version": PLAN_VERSION,
        "writer": {"source": "deterministic-fallback"},
        "picks": {"headlines": headlines, "flashes": []},
        "hook": {"text": _mark_number(_fit(hook_source, C.BUDGET_HOOK_MAX)), "keyfact": _fit(first_sentences[0], C.BUDGET_KEYFACT_MAX)},
        "stories": [],
    }
    used_total = units(plan["hook"]["text"])
    for item_id in headlines:
        story = _fallback_headline(item_id, materials[item_id])
        plan["stories"].append(story)
        used_total += sum(units(n["text"]) for n in story["narration"])
    for item_id in flashes:
        story = _fallback_flash(item_id, materials[item_id])
        cost = sum(units(n["text"]) for n in story["narration"])
        if used_total + cost > C.BUDGET_TOTAL_NARRATION_MAX:
            continue
        plan["picks"]["flashes"].append(item_id)
        plan["stories"].append(story)
        used_total += cost
    return plan


# ---------------------------------------------------------------------------
# finalize -> scenes and narration units
# ---------------------------------------------------------------------------
def finalize(plan: Mapping[str, Any], selection: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    """Normalize a valid plan into scenes + per-sentence narration units."""

    materials = material_index(selection)
    scenes: list[dict[str, Any]] = []
    narration_units: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []

    def add_unit(scene_id: str, text: str, *, card: int | None = None, caption: bool = True) -> None:
        display = plain(text)
        normalized = normalize_with_ledger(display)
        unit_id = f"u{len(narration_units) + 1:02d}"
        narration_units.append({"unit_id": unit_id, "scene_id": scene_id, "display_text": normalized.display_text,
                                "spoken_text": normalized.spoken_text, "card": card, "caption": caption})
        if normalized.rewrites:
            ledger.append({"unit_id": unit_id, "rewrites": list(normalized.rewrites)})

    stories = [s for s in plan.get("stories") or [] if isinstance(s, Mapping)]
    headline_count = sum(1 for s in stories if s.get("role") == "headline")
    flash_count = len(stories) - headline_count
    total_stories = len(stories)
    first_headline = next((s for s in stories if s.get("role") == "headline"), None)
    hook = plan.get("hook") or {}
    if first_headline:
        m = materials.get(str(first_headline.get("item_id")), {})
        brand = find_brand(str(first_headline.get("subject") or ""))
        scenes.append({"scene_id": "hook", "kind": "hook", "category": m.get("category", "other"), "section": m.get("category_label", ""),
                       "subject": first_headline.get("subject"), "logo": (brand or {}).get("logo_path", ""),
                       "hook": hook.get("text"), "keyfact": hook.get("keyfact"), "progress": {"role": "头条", "index": 1, "total": total_stories}})
        # The hook is already on screen in large type, so its caption stays off-screen (it remains in the SRT).
        add_unit("hook", str(hook.get("text") or "") + ("" if _TERMINAL.search(plain(hook.get("text"))) else "。"), caption=False)
    recap: list[dict[str, Any]] = []
    for n, story in enumerate(stories, 1):
        m = materials.get(str(story.get("item_id")), {})
        # The logo follows the subject the writer chose; a brand merely mentioned in the title
        # (e.g. "Karpathy 用 Claude ……") must not put another company's logo on the story.
        brand = find_brand(str(story.get("subject") or ""))
        is_headline = story.get("role") == "headline"
        role = "头条" if is_headline else "快讯"
        scene_id = f"story-{n:02d}"
        scene = {"scene_id": scene_id, "kind": "story" if is_headline else "flash", "item_id": story.get("item_id"),
                 "category": m.get("category", "other"), "section": m.get("category_label", ""), "subject": story.get("subject"),
                 "logo": (brand or {}).get("logo_path", ""), "title": story.get("title"),
                 "progress": {"role": role, "index": n, "total": total_stories},
                 "links": m.get("links", {}), "source": m.get("source", "")}
        if is_headline:
            scene["cards"] = [dict(card) for card in story.get("cards") or []]
        else:
            scene["point"] = story.get("point")
            if story.get("why"):
                scene["why"] = story.get("why")
        scenes.append(scene)
        recap.append({"role": role, "title": plain(story.get("title"))})
        flash_cards = 2 if story.get("why") else 1
        for k, sentence in enumerate(story.get("narration") or []):
            card = sentence.get("card")
            if not is_headline:
                card = card if isinstance(card, int) and 1 <= card <= flash_cards else (1 if k == 0 or flash_cards == 1 else 2)
            add_unit(scene_id, str(sentence.get("text") or ""), card=card)
    scenes.append({"scene_id": "outro", "kind": "outro", "text": C.OUTRO_TEXT, "recap": recap, "progress": None})
    add_unit("outro", C.OUTRO_TEXT)
    return {
        "version": C.V6_VERSION,
        "plan_version": PLAN_VERSION,
        "plan_source": source,
        "counts": {"headlines": headline_count, "flashes": flash_count},
        "scenes": scenes,
        "units": narration_units,
        "pronunciation_ledger": ledger,
        "picks": plan.get("picks"),
    }
