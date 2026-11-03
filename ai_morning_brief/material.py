"""v6 material packs: everything AIHOT itself says about one event.

A writer model can only be as specific as its material. AIHOT's one-line
summary of an item is too thin for a headline, so for the strongest events
the newsroom gathers more **AIHOT-only** text:

* the hot-topic story: AI digest, latest development and per-source reports
  (each report is AIHOT's own summary of one covering source);
* other AIHOT items about the same event, found by keyword search over all
  public AIHOT items of the last 7 days (selected or not);
* AIHOT "related stories" and earlier AIHOT items, as clearly labelled
  background;
* the AIHOT editorial ``reason`` of a selected item, as an *angle hint* only.

Original article links (``links.original``) are never fetched or opened.

``build_pack`` is deterministic over the frozen snapshot so selection and
validation are reproducible. Fact text (everything except the angle hint)
is the evidence the director validator checks numbers and names against.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from . import config as C
from .brands import find_brand

_STOP = {"ai", "api", "the", "and", "for", "with", "new", "app", "llm", "gpt", "pro", "plus", "mode", "model", "agent", "agents",
         "ceo", "cto", "cfo", "inc", "ltd", "rss", "x", "of", "to", "in", "on", "is", "by", "vs"}
_ASCII_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9.\-]{1,}")
_SENT_SPLIT = re.compile(r"(?<=[。！？；;])")


# ---------------------------------------------------------------------------
# similarity helpers (shared with newsroom clustering)
# ---------------------------------------------------------------------------
def ascii_tokens(text: str) -> set[str]:
    return {t.lower().rstrip(".-") for t in _ASCII_TOKEN.findall(text or "") if t.lower().rstrip(".-") not in _STOP and len(t) >= 3}


def bigrams(text: str) -> set[str]:
    han = re.sub(r"[^一-鿿]", "", text or "")
    return {han[i:i + 2] for i in range(len(han) - 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / max(1, len(a | b))


def near_duplicate(text: str, others: Iterable[str], threshold: float = 0.55) -> bool:
    grams = bigrams(text)
    if not grams:
        return any(text.strip() == o.strip() for o in others)
    return any(jaccard(grams, bigrams(o)) >= threshold for o in others)


# ---------------------------------------------------------------------------
# search queries for more AIHOT coverage of one event
# ---------------------------------------------------------------------------
def _brand_alias(title: str) -> str:
    """The brand name as it is written in the title (so the search matches AIHOT text)."""

    brand = find_brand(title)
    if not brand:
        return ""
    name = str(brand.get("name") or "")
    if name and name.lower() in title.lower():
        return name
    for token in re.findall(r"[A-Za-z][A-Za-z0-9.\-]+|[一-鿿]{2,4}", title):
        if find_brand(token):
            return token
    return name


def search_queries(title: str, hot_title: str = "") -> list[str]:
    """Up to two AIHOT keyword queries for an event: subject + key term, then subject alone."""

    text = hot_title or title
    subject = _brand_alias(text) or _brand_alias(title)
    tokens = [t for t in _ASCII_TOKEN.findall(text) if t.lower() not in _STOP and len(t) >= 3]
    if not subject and tokens:
        subject = tokens[0]
    if not subject:
        return []
    second = next((t for t in tokens if t.lower() != subject.lower() and not t[0].isdigit()), "")
    queries = []
    if second:
        queries.append(f"{subject} {second}")
    queries.append(subject)
    return [q for q in queries if len(q) >= 2]


def trim_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or ""), "title": str(item.get("title") or ""), "summary": str(item.get("summary") or ""),
        "source": str((item.get("source") or {}).get("name") or ""), "selected": bool(item.get("selected")),
        "score": item.get("score"), "category": item.get("category"),
        "publishedAt": item.get("publishedAt"), "discoveredAt": item.get("discoveredAt"),
    }


def trim_story(story: Mapping[str, Any], *, reports: int = 6) -> dict[str, Any]:
    return {
        "publicId": story.get("publicId"), "title": story.get("title"), "latest": story.get("latest"), "digest": story.get("digest"),
        "sourceCount": story.get("sourceCount"), "firstReportAt": story.get("firstReportAt"), "latestAt": story.get("latestAt"),
        "reports": [{"title": r.get("title"), "summary": r.get("summary"), "source": {"name": (r.get("source") or {}).get("name")},
                     "publishedAt": r.get("publishedAt")} for r in (story.get("reports") or [])[:reports] if isinstance(r, Mapping)],
    }


def relation(lead_title: str, lead_text: str, item: Mapping[str, Any], *, subject: str = "") -> str:
    """Classify a search hit: 'same' event, 'related' background, or '' (noise).

    Uses containment (how much of the hit's text also appears in the event's
    own text) rather than symmetric similarity, so a short on-topic report of
    a long, digest-rich event still matches, while posts that merely share a
    product name ("Grok Bot 新功能一览") do not.
    """

    from .newsroom import same_event  # local import: newsroom imports this module

    title = str(item.get("title") or "")
    summary = str(item.get("summary") or "")
    if len(summary) < 40:
        return ""
    text = f"{title} {summary}"
    grams = bigrams(text)
    contained = len(grams & bigrams(lead_text)) / max(1, len(grams))
    shared = (ascii_tokens(text) & ascii_tokens(lead_text)) - {subject.lower()}
    if contained >= 0.35 or (same_event(title, lead_title) and (contained >= 0.15 or len(shared) >= 3)):
        return "same"
    if contained >= 0.18 or (len(shared) >= 3 and contained >= 0.12):
        return "related"
    return ""


# ---------------------------------------------------------------------------
# pack
# ---------------------------------------------------------------------------
def _date(value: Any) -> str:
    return str(value or "")[:10]


def _short_source(name: str) -> str:
    """'X：Rohan Paul (@rohanpaul_ai)' -> 'Rohan Paul'; 'TechCrunch：AI（RSS）' -> 'TechCrunch'."""

    name = str(name or "").strip()
    if name.startswith("X：") or name.startswith("X:"):
        name = name[2:]
    name = re.split(r"[：:（(]", name, maxsplit=1)[0].strip()
    return name[:24]


def _first_paragraph(text: str, maximum: int = 320) -> str:
    para = str(text or "").strip().split("\n\n")[0].strip()
    if len(para) <= maximum:
        return para
    out = ""
    for sentence in _SENT_SPLIT.split(para):
        if len(out + sentence) > maximum:
            break
        out += sentence
    return out or para[:maximum]


def build_pack(cluster: Any, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Collect every AIHOT paragraph about one event cluster (see module docstring)."""

    lead = cluster.lead
    members = list(cluster.members)
    stories = snapshot.get("stories") or {}
    searches = snapshot.get("searches") or {}
    search_for = snapshot.get("search_for") or {}
    related_stories = snapshot.get("related_stories") or {}
    window_start = str((snapshot.get("window") or {}).get("start") or "")
    reasons = {str(i.get("id")): str(i.get("reason") or "") for i in snapshot.get("selected_items") or []}
    member_ids = {m.item_id for m in members}

    facts: list[dict[str, Any]] = []
    background: list[dict[str, Any]] = []
    chars = 0

    def add_fact(text: Any, *, kind: str, source: str = "", date: str = "", limit: int = 420) -> None:
        nonlocal chars
        text = re.sub(r"[ \t]*\n[ \t]*", "\n", str(text or "")).strip()
        text = text if kind == "AIHOT 综述" else re.sub(r"\s+", " ", text)
        if len(text) < 24 or len(facts) >= C.PACK_MAX_FACTS or chars >= C.PACK_MAX_FACT_CHARS:
            return
        if near_duplicate(text, [f["text"] for f in facts]):
            return
        text = text[:limit]
        facts.append({"kind": kind, "source": _short_source(source), "date": date, "text": text})
        chars += len(text)

    add_fact(lead.summary, kind="摘要", source=lead.source, date=_date(lead.published_at))
    story_ids = [m.story_id for m in members if m.story_id]
    story = next((stories[s] for s in story_ids if s in stories), None)
    if story:
        add_fact(story.get("digest"), kind="AIHOT 综述", source="AIHOT", date=_date(story.get("digestUpdatedAt") or story.get("latestAt")), limit=1200)
        add_fact(story.get("latest"), kind="最新进展", source="AIHOT", date=_date(story.get("latestAt")))
    for m in members[1:]:
        add_fact(m.summary, kind="同事件报道", source=m.source, date=_date(m.published_at))
    if story:
        for report in story.get("reports") or []:
            add_fact(report.get("summary"), kind="来源报道", source=(report.get("source") or {}).get("name", ""), date=_date(report.get("publishedAt")))
    lead_text = " ".join([lead.title, lead.hot_title, lead.summary, str((story or {}).get("digest") or "")])
    subject = _brand_alias(lead.hot_title or lead.title)
    seen_ids = set(member_ids)
    for m in members:
        for query in search_for.get(m.item_id) or []:
            for item in searches.get(query) or []:
                item_id = str(item.get("id") or "")
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                kind = relation(lead.hot_title or lead.title, lead_text, item, subject=subject)
                found = str(item.get("discoveredAt") or item.get("publishedAt") or "")
                if kind == "same" and not (window_start and found and found < window_start and not item.get("selected")):
                    add_fact(item.get("summary") or item.get("title"), kind="同事件报道", source=item.get("source", ""), date=_date(found))
                elif kind in {"same", "related"} and len(background) < C.PACK_MAX_BACKGROUND:
                    text = str(item.get("summary") or item.get("title") or "").strip()
                    if text and not near_duplicate(text, [b["text"] for b in background] + [f["text"] for f in facts]):
                        background.append({"kind": "此前报道" if found < window_start else "相关报道", "source": _short_source(item.get("source", "")),
                                           "date": _date(found), "title": item.get("title"), "text": text[:300]})
    if story:
        for ref in story.get("related") or []:
            rel = related_stories.get(str(ref.get("publicId") or ""))
            if not rel or len(background) >= C.PACK_MAX_BACKGROUND:
                continue
            text = _first_paragraph(rel.get("digest") or rel.get("latest") or "")
            if text and not near_duplicate(text, [b["text"] for b in background] + [f["text"] for f in facts]):
                background.append({"kind": "相关事件", "source": "AIHOT", "date": _date(rel.get("latestAt")), "title": rel.get("title"), "text": text})
    hint = next((reasons[i] for i in [lead.item_id, *member_ids] if reasons.get(i)), "")
    fact_chars = sum(len(f["text"]) for f in facts)
    sources = {f["source"] for f in facts if f["source"] and f["source"] != "AIHOT"}
    if fact_chars >= C.PACK_DEPTH_RICH or (story and len(sources) >= 3 and fact_chars >= C.PACK_DEPTH_MEDIUM + 150):
        depth = "rich"
    elif fact_chars >= C.PACK_DEPTH_MEDIUM:
        depth = "medium"
    else:
        depth = "thin"
    return {
        "facts": facts,
        "background": background,
        "angle_hint": hint,
        "fact_chars": fact_chars,
        "background_chars": sum(len(b["text"]) for b in background),
        "source_count": max(len(sources), int(getattr(lead, "source_count", 0) or 0)),
        "depth": depth,
    }


def pack_evidence(pack: Mapping[str, Any]) -> str:
    """Text the validator accepts numbers and names from (never the angle hint)."""

    parts = [f"{f.get('source', '')} {f.get('text', '')}" for f in pack.get("facts") or []]
    parts += [f"{b.get('source', '')} {b.get('title', '')} {b.get('text', '')}" for b in pack.get("background") or []]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# budgets and duration estimates
# ---------------------------------------------------------------------------
def story_budget(pack: Mapping[str, Any]) -> dict[str, Any]:
    depth = str(pack.get("depth") or "thin")
    spec = C.STORY_BUDGETS.get(depth, C.STORY_BUDGETS["thin"])
    available = int((int(pack.get("fact_chars") or 0) + int(pack.get("background_chars") or 0) // 2) * C.BUDGET_MIN_SHARE_OF_MATERIAL)
    h_min, h_max = spec["headline"]
    f_min, f_max = spec["flash"]
    # Never demand more cards than the material has distinct sentences to fill.
    sentences = sum(len([x for x in _SENT_SPLIT.split(str(f.get("text") or "")) if len(x.strip()) >= 10])
                    for f in pack.get("facts") or []) + len(pack.get("background") or [])
    c_min, c_max = spec["cards"]
    c_min = max(2, min(c_min, sentences))
    return {
        "depth": depth,
        "can_headline": depth != "thin",
        "headline_narration": [max(40, min(h_min, available)), h_max],
        "cards": [c_min, max(c_min, c_max)],
        "flash_narration": [max(20, min(f_min, available)), f_max],
        "flash_sentences": list(spec["flash_sentences"]),
        "needs_depth_card": depth != "thin",
    }


def estimate_seconds(budget: Mapping[str, Any], role: str) -> float:
    """Planning estimate: the middle of the narration budget at the speaking rate, plus pauses and transitions."""

    lo, hi = budget["headline_narration" if role == "headline" else "flash_narration"]
    chars = (lo + hi) / 2
    sentences = max(1.0, chars / 30)
    return chars / C.SPEECH_CHARS_PER_SECOND + sentences * C.PAUSE_BETWEEN_SENTENCES + C.PAUSE_BETWEEN_STORIES + 0.4
