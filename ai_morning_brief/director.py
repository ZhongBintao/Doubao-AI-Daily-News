"""v6 director plan: the only thing a writer model produces.

The writer model answers a fill-in task (``director_task.json``) instead of
designing a video. It chooses picks from a fixed pool and fills short fields
under hard budgets. ``validate_plan`` returns Chinese, field-addressed errors a
weak model can act on; ``fallback_plan`` builds a valid plan from AIHOT text
alone so an edition always renders.

Plan shape (version 6.0)::

    {
      "version": "6.0",
      "picks": {"headlines": [id], "flashes": [id, ...]},
      "hook": {"text": "只改 【0.016%】 的权重，数学成绩涨到 6 倍", "keyfact": "..."},
      "stories": [
        {"item_id": id, "role": "headline", "subject": "...", "title": "...",
         "cards": [{"label": "...", "headline": "...", "body": "..."}],
         "narration": [{"text": "...。", "card": 1}]},
        {"item_id": id, "role": "flash", "subject": "...", "title": "...",
         "point": "...", "narration": [{"text": "...。"}]}
      ]
    }
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import config as C
from .brands import find_brand
from .writing import caption_visible_units, normalize_with_ledger

EXAMPLES_DIR = C.REPO_ROOT / "skills" / "ai-brief-editorial-writer" / "references" / "examples"
_MARK = re.compile(r"【(.+?)】")
_TERMINAL = re.compile(r"[。！？!?]$")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_ASCII = re.compile(r"[A-Za-z][A-Za-z0-9\-]*(?:\.[A-Za-z0-9]+)*")
_ASCII_ALLOW = {"ai", "app", "ceo", "cto", "api", "ok", "vs"}
CARD_LABELS = ("核心信息", "关键细节", "补充说明", "后续进展", "背景")


def plain(text: Any) -> str:
    return _MARK.sub(r"\1", str(text or "")).replace("【", "").replace("】", "").strip()


def units(text: Any) -> int:
    return caption_visible_units(plain(text))


# ---------------------------------------------------------------------------
# task
# ---------------------------------------------------------------------------
def story_material(cluster: Mapping[str, Any]) -> dict[str, Any]:
    lead = cluster.get("lead") or {}
    text = "\n".join(str(lead.get(k) or "") for k in ("title", "hot_title", "summary", "digest"))
    brand = find_brand(" ".join(str(lead.get(k) or "") for k in ("hot_title", "title")))
    return {
        "item_id": lead.get("item_id"),
        "title": lead.get("title"),
        "hot_title": lead.get("hot_title") or "",
        "summary": lead.get("summary") or "",
        "digest": lead.get("digest") or "",
        "related_titles": [t for t in cluster.get("member_titles") or [] if t != lead.get("title")][:4],
        "category": lead.get("category") or "other",
        "category_label": lead.get("category_label") or C.V6_CATEGORY_LABELS.get(lead.get("category") or "other", "AI 动态"),
        "source": lead.get("source") or "",
        "heat": cluster.get("heat"),
        "signals": lead.get("signals") or {},
        "subject_hint": (brand or {}).get("name") or "",
        "has_logo": bool((brand or {}).get("logo_path")),
        "links": {"aihot": lead.get("aihot_url") or "", "original": lead.get("original_url") or ""},
        "_material": text,
    }


def load_examples() -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for path in sorted(EXAMPLES_DIR.glob("*.json")):
        try:
            examples.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return examples


def budgets() -> dict[str, int]:
    return {
        "hook": C.BUDGET_HOOK_MAX, "keyfact": C.BUDGET_KEYFACT_MAX, "title": C.BUDGET_TITLE_MAX,
        "card_label": C.BUDGET_CARD_LABEL_MAX, "card_headline": C.BUDGET_CARD_HEADLINE_MAX, "card_body": C.BUDGET_CARD_BODY_MAX,
        "sentence": C.BUDGET_SENTENCE_MAX, "headline_narration": C.BUDGET_HEADLINE_NARRATION_MAX,
        "flash_point": C.BUDGET_FLASH_POINT_MAX, "flash_narration": C.BUDGET_FLASH_NARRATION_MAX,
        "total_narration": C.BUDGET_TOTAL_NARRATION_MAX, "headline_cards_min": C.HEADLINE_CARDS_MIN, "headline_cards_max": C.HEADLINE_CARDS_MAX,
    }


TASK_STEPS = [
    "第 1 步 选新闻：从 pool 里选 1 条头条（两条都极重磅时可选 2 条）和 1–3 条快讯，总数不超过 4 条。不确定就直接照抄 recommended。",
    "第 2 步 写开头钩子 hook.text：一句话说出头条最抓人的点，可以用一对【】标出一个重点词或数字。参考 examples 里的 4 种写法。",
    "第 3 步 写 hook.keyfact：一句客观事实，说清楚“谁做了什么”。",
    "第 4 步 写每条新闻：subject（主体，如公司名）、title（标题）；头条写 2–5 张 cards 和逐句 narration；快讯写 point 和 1–2 句 narration。",
    "第 5 步 每句 narration 以句号/问号/感叹号结尾；头条的每句用 card 字段写它在讲第几张卡（从 1 开始，按顺序讲，每张卡至少讲一句）。",
    "第 6 步 运行 check_command 校验；按报错逐条修改，最多 2 次。仍失败也没关系，程序会自动用兜底稿出片。",
]
RULES = [
    "所有数字、英文名、产品名必须原样出现在该条新闻的素材（title/hot_title/summary/digest/related_titles）里，不能编造。",
    "不写来源没有的因果、预测、建议；素材说“尚未验证/尚无复现”时，要在卡片或旁白里保留这层意思。",
    "不在文案里写分数、链接、AIHOT。",
    "字数按可见字符计算，见 budgets；【】不计入字数，每个字段最多一对【】。",
]


def build_task(run_date: date, selection: Mapping[str, Any], *, plan_path: Path, check_command: str) -> dict[str, Any]:
    pool = [story_material(cluster) for cluster in selection.get("pool") or []]
    public_pool = [{k: v for k, v in item.items() if not k.startswith("_")} for item in pool]
    return {
        "version": C.V6_VERSION,
        "task": "director-plan",
        "run_date": run_date.isoformat(),
        "mode": selection.get("mode"),
        "output_path": str(plan_path),
        "check_command": check_command,
        "steps": TASK_STEPS,
        "rules": RULES,
        "budgets": budgets(),
        "recommended": selection.get("recommended") or {},
        "pool": public_pool,
        "output_template": {
            "version": C.V6_VERSION,
            "picks": {"headlines": ["<pool 里的 item_id>"], "flashes": ["<item_id>", "<item_id>"]},
            "hook": {"text": "<钩子，可含一对【】>", "keyfact": "<一句客观事实>"},
            "stories": [
                {"item_id": "<头条 id>", "role": "headline", "subject": "<主体>", "title": "<标题>",
                 "cards": [{"label": "<≤6字>", "headline": "<≤16字>", "body": "<≤48字，可含一对【】>"}],
                 "narration": [{"text": "<一句话。>", "card": 1}]},
                {"item_id": "<快讯 id>", "role": "flash", "subject": "<主体>", "title": "<标题>",
                 "point": "<一句要点>", "narration": [{"text": "<一句话。>"}]},
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


def validate_plan(plan: Any, selection: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(plan, Mapping):
        return ["整个文件必须是一个 JSON 对象。"]
    if str(plan.get("version")) != C.V6_VERSION:
        errors.append(f"version：必须是 \"{C.V6_VERSION}\"。")
    materials = material_index(selection)
    picks = plan.get("picks") or {}
    headlines = [str(v) for v in picks.get("headlines") or []]
    flashes = [str(v) for v in picks.get("flashes") or []]
    if not 1 <= len(headlines) <= C.RUNDOWN_MAX_HEADLINES:
        errors.append(f"picks.headlines：必须选 1–{C.RUNDOWN_MAX_HEADLINES} 条头条。")
    if len(flashes) > C.RUNDOWN_MAX_STORIES - len(headlines):
        errors.append(f"picks.flashes：头条加快讯总数不能超过 {C.RUNDOWN_MAX_STORIES} 条。")
    chosen = headlines + flashes
    if len(set(chosen)) != len(chosen):
        errors.append("picks：同一条新闻不能被选两次。")
    for item_id in chosen:
        if item_id not in materials:
            errors.append(f"picks：{item_id} 不在 pool 里，只能从 pool 中选择。")
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
        material = materials.get(str(story.get("item_id")), {}).get("_material", "")
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
            if not isinstance(cards, list) or not C.HEADLINE_CARDS_MIN <= len(cards) <= C.HEADLINE_CARDS_MAX:
                errors.append(f"{prefix}.cards：头条需要 {C.HEADLINE_CARDS_MIN}–{C.HEADLINE_CARDS_MAX} 张卡片。")
                cards = cards if isinstance(cards, list) else []
            for c, card in enumerate(cards):
                cp = f"{prefix}.cards[{c}]"
                if not isinstance(card, Mapping):
                    errors.append(f"{cp}：必须是对象。")
                    continue
                _check_text(errors, f"{cp}.label", card.get("label"), maximum=C.BUDGET_CARD_LABEL_MAX)
                _check_text(errors, f"{cp}.headline", card.get("headline"), maximum=C.BUDGET_CARD_HEADLINE_MAX)
                _check_text(errors, f"{cp}.body", card.get("body"), maximum=C.BUDGET_CARD_BODY_MAX, marks=True)
                texts += [(f"{cp}.headline", str(card.get("headline") or "")), (f"{cp}.body", str(card.get("body") or ""))]
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
            if story_units > C.BUDGET_HEADLINE_NARRATION_MAX:
                errors.append(f"{prefix}.narration：头条旁白合计超过 {C.BUDGET_HEADLINE_NARRATION_MAX} 字（当前 {story_units} 字）。")
        else:
            _check_text(errors, f"{prefix}.point", story.get("point"), maximum=C.BUDGET_FLASH_POINT_MAX)
            texts.append((f"{prefix}.point", str(story.get("point") or "")))
            if story_units > C.BUDGET_FLASH_NARRATION_MAX:
                errors.append(f"{prefix}.narration：快讯旁白合计超过 {C.BUDGET_FLASH_NARRATION_MAX} 字（当前 {story_units} 字）。")
        if material:
            for path, text in texts:
                bad = _unsupported_tokens(text, material)
                if bad:
                    errors.append(f"{path}：这些数字或英文在该新闻素材里找不到：{'、'.join(bad)}。请改成素材里的原样写法。")
    total += units(hook.get("text"))
    if total > C.BUDGET_TOTAL_NARRATION_MAX:
        errors.append(f"全片旁白合计超过 {C.BUDGET_TOTAL_NARRATION_MAX} 字（当前 {total} 字），请删减快讯或头条旁白。")
    return errors


# ---------------------------------------------------------------------------
# deterministic fallback
# ---------------------------------------------------------------------------
_SENT_SPLIT = re.compile(r"(?<=[。！？；;])")


def _sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(str(text or "")) if p.strip()]
    return [re.sub(r"[；;]$", "。", p) for p in parts]


def _clauses(text: str) -> list[str]:
    return [p for p in re.split(r"(?<=[，,：:、])", text) if p.strip()]


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


_ATTRIBUTION = re.compile(r"(称|表示|报道|指出|认为|介绍|透露|宣布|回应|据.{0,12})[，,：:]?$")


def _card_headline(sentence: str) -> str:
    clauses = [c.strip() for c in _clauses(sentence) if c.strip()]
    while len(clauses) > 1 and (_ATTRIBUTION.search(clauses[0].rstrip("，,：:")) or units(clauses[0]) < 4):
        clauses = clauses[1:]
    return _fit(clauses[0] if clauses else sentence, C.BUDGET_CARD_HEADLINE_MAX)


def fallback_plan(selection: Mapping[str, Any]) -> dict[str, Any]:
    materials = material_index(selection)
    rec = selection.get("recommended") or {}
    headlines = [i for i in rec.get("headlines") or [] if i in materials][: C.RUNDOWN_MAX_HEADLINES]
    if not headlines and materials:
        headlines = [next(iter(materials))]
    flashes = [i for i in rec.get("flashes") or [] if i in materials and i not in headlines][: C.RUNDOWN_MAX_STORIES - len(headlines)]
    lead = materials[headlines[0]]
    hook_source = lead["hot_title"] if lead["hot_title"] and units(lead["hot_title"]) <= C.BUDGET_HOOK_MAX else lead["title"]
    first_sentences = _sentences(lead["summary"] or lead["digest"]) or [lead["title"]]
    plan: dict[str, Any] = {
        "version": C.V6_VERSION,
        "writer": {"source": "deterministic-fallback"},
        "picks": {"headlines": headlines, "flashes": flashes},
        "hook": {"text": _mark_number(_fit(hook_source, C.BUDGET_HOOK_MAX)), "keyfact": _fit(first_sentences[0], C.BUDGET_KEYFACT_MAX)},
        "stories": [],
    }
    used_total = units(plan["hook"]["text"])
    for item_id in headlines:
        m = materials[item_id]
        sentences = _sentences(m["summary"]) + _sentences(m["digest"])
        sentences = [s for i, s in enumerate(sentences) if s not in sentences[:i]] or [m["title"] + "。"]
        cards: list[dict[str, Any]] = []
        narration: list[dict[str, Any]] = []
        story_units = 0
        for sentence in sentences:
            if len(cards) >= 3:
                break
            body = _fit(sentence, C.BUDGET_CARD_BODY_MAX)
            spoken = _fit(sentence, C.BUDGET_SENTENCE_MAX, terminal=True)
            if story_units + units(spoken) > C.BUDGET_HEADLINE_NARRATION_MAX - 10:
                break
            headline = _card_headline(sentence)
            cards.append({"label": CARD_LABELS[len(cards)], "headline": headline, "body": body})
            narration.append({"text": spoken, "card": len(cards)})
            story_units += units(spoken)
        if len(cards) < C.HEADLINE_CARDS_MIN:
            # Split one long sentence into two cards so the page never has a single orphan card.
            pieces = _clauses(sentences[0])
            half = max(1, len(pieces) // 2)
            first, second = "".join(pieces[:half]), "".join(pieces[half:]) or m["title"]
            cards = [
                {"label": CARD_LABELS[0], "headline": _card_headline(first), "body": _fit(first, C.BUDGET_CARD_BODY_MAX)},
                {"label": CARD_LABELS[1], "headline": _card_headline(second), "body": _fit(second, C.BUDGET_CARD_BODY_MAX)},
            ]
            narration = [{"text": _fit(first, C.BUDGET_SENTENCE_MAX, terminal=True), "card": 1}, {"text": _fit(second, C.BUDGET_SENTENCE_MAX, terminal=True), "card": 2}]
        plan["stories"].append({"item_id": item_id, "role": "headline", "subject": _subject(m), "title": _fit(m["title"], C.BUDGET_TITLE_MAX),
                                "cards": cards, "narration": narration})
        used_total += sum(units(n["text"]) for n in narration)
    for item_id in list(flashes):
        m = materials[item_id]
        point_source = (_sentences(m["summary"]) or _sentences(m["digest"]) or [m["title"]])[0]
        title = _fit(m["hot_title"] if m["hot_title"] and units(m["hot_title"]) <= C.BUDGET_TITLE_MAX else m["title"], C.BUDGET_TITLE_MAX)
        sentence = _fit(title + "。", C.BUDGET_SENTENCE_MAX, terminal=True)
        if used_total + units(sentence) > C.BUDGET_TOTAL_NARRATION_MAX:
            plan["picks"]["flashes"].remove(item_id)
            continue
        plan["stories"].append({"item_id": item_id, "role": "flash", "subject": _subject(m), "title": title,
                                "point": _fit(point_source, C.BUDGET_FLASH_POINT_MAX), "narration": [{"text": sentence}]})
        used_total += units(sentence)
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

    def add_unit(scene_id: str, text: str, *, card: int | None = None) -> None:
        display = plain(text)
        normalized = normalize_with_ledger(display)
        unit_id = f"u{len(narration_units) + 1:02d}"
        narration_units.append({"unit_id": unit_id, "scene_id": scene_id, "display_text": normalized.display_text,
                                "spoken_text": normalized.spoken_text, "card": card})
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
        brand = find_brand(str(first_headline.get("subject") or "")) or find_brand(m.get("title") or "")
        scenes.append({"scene_id": "hook", "kind": "hook", "category": m.get("category", "other"), "section": m.get("category_label", ""),
                       "subject": first_headline.get("subject"), "logo": (brand or {}).get("logo_path", ""),
                       "hook": hook.get("text"), "keyfact": hook.get("keyfact"), "progress": {"role": "头条", "index": 1, "total": total_stories}})
        add_unit("hook", str(hook.get("text") or "") + ("" if _TERMINAL.search(plain(hook.get("text"))) else "。"))
    for n, story in enumerate(stories, 1):
        m = materials.get(str(story.get("item_id")), {})
        brand = find_brand(str(story.get("subject") or "")) or find_brand(m.get("title") or "")
        role = "头条" if story.get("role") == "headline" else "快讯"
        scene_id = f"story-{n:02d}"
        scene = {"scene_id": scene_id, "kind": "story" if story.get("role") == "headline" else "flash", "item_id": story.get("item_id"),
                 "category": m.get("category", "other"), "section": m.get("category_label", ""), "subject": story.get("subject"),
                 "logo": (brand or {}).get("logo_path", ""), "title": story.get("title"),
                 "progress": {"role": role, "index": n, "total": total_stories},
                 "links": m.get("links", {}), "source": m.get("source", "")}
        if story.get("role") == "headline":
            scene["cards"] = [dict(card) for card in story.get("cards") or []]
        else:
            scene["point"] = story.get("point")
        scenes.append(scene)
        for sentence in story.get("narration") or []:
            add_unit(scene_id, str(sentence.get("text") or ""), card=sentence.get("card") if story.get("role") == "headline" else None)
    scenes.append({"scene_id": "outro", "kind": "outro", "text": C.OUTRO_TEXT, "progress": None})
    add_unit("outro", C.OUTRO_TEXT)
    return {
        "version": C.V6_VERSION,
        "plan_source": source,
        "counts": {"headlines": headline_count, "flashes": flash_count},
        "scenes": scenes,
        "units": narration_units,
        "pronunciation_ledger": ledger,
        "picks": plan.get("picks"),
    }
